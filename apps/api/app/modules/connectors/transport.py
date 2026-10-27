"""Fixed registered relay HTTP/WSS transport. Credentials stay in native memory."""
import asyncio
import base64
from datetime import datetime,timedelta,timezone
import threading
import time
from urllib.parse import urlsplit,urlunsplit
from uuid import uuid4
import httpx
import sqlalchemy as sa
from websockets.asyncio.client import connect
from app.errors import DomainError
from app.modules.auth.evidence import verify_signed_envelope,parse_time
from app.infrastructure.db import schema as s
from . import models as m
from .protocol import registered_target,strict_json,cursor


class RegisteredConnect(connect):
    """websockets 17.3 redirect hook: the registered endpoint never redirects."""
    def process_redirect(self,exc):return exc


class RelayControlPort:
    def __init__(self,registry,credential_provider,*,test_mode=False,client_factory=None,now_ns=time.monotonic_ns):
        self.registry=registry;self.credentials=credential_provider;self.test_mode=test_mode
        self.client_factory=client_factory or (lambda:httpx.Client(timeout=5,trust_env=False,follow_redirects=False))
        self.now_ns=now_ns

    def target(self,scope):return registered_target(scope['connector_id'],self.registry,test_mode=self.test_mode)

    def token(self,scope):
        refresh=getattr(self.credentials,'refresh_if_due',None)
        if refresh:refresh(self,scope)
        token=self.credentials(scope)
        if not isinstance(token,str) or not token or len(token.encode())>16384:raise DomainError('AUTH_REQUIRED','Native device credential unavailable',401)
        return token

    def request(self,scope,path,body,command_id=None):
        parsed=urlsplit(self.target(scope)['url']);base=urlunsplit(('https' if parsed.scheme=='wss' else 'http',parsed.netloc,path,'',''))
        headers={'Authorization':'Bearer '+self.token(scope),'Content-Type':'application/json','X-BE-Extension-Version':'LW-BE/1.1.0'}
        if command_id:headers['Idempotency-Key']=command_id
        try:
            with self.client_factory() as client:response=client.post(base,json=body,headers=headers)
            data=response.json()
            if response.status_code!=200:
                code=data.get('error',{}).get('code','RELAY_UNAVAILABLE') if isinstance(data,dict) else 'RELAY_UNAVAILABLE'
                raise DomainError(code,code,response.status_code,retryable=response.status_code in (429,502,503,504))
            if not isinstance(data,dict):raise ValueError()
            if data.get('be_extension_version')!='LW-BE/1.1.0':raise DomainError('BE_EXTENSION_VERSION_UNSUPPORTED','Relay did not negotiate required backend extension',409)
            return data
        except DomainError:raise
        except Exception:raise DomainError('RELAY_UNAVAILABLE','Registered relay request failed',503,True) from None

    def execute(self,scope,operation,args):
        lease=args.get('lease',{});body={'lease_id':lease.get('lease_id')};key=args.get('command_id')
        if operation=='prepare':
            lease=self.request(scope,'/relay/v1/leases',{},key)
            if args.get('fresh_start') is True:return self.request(scope,'/relay/v1/streams/bootstrap',{'lease_id':lease['lease_id'],'expected_stream_id':lease['stream_id'],'reason':'EXPLICIT_FRESH_SESSION'},key)
            return lease
        if operation=='renew':return self.request(scope,'/relay/v1/leases/renew',body,key)
        if operation=='recover':
            observed=self.request(scope,'/relay/v1/state',body,key)
            window=observed.get('window')
            if window and window['state']!='CLOSED':
                self.request(scope,'/relay/v1/windows/close',body|{'window_epoch':window['epoch'],'expected_version':window['version'],'reason':'INTERRUPTED'},key)
            stopped=self.request(scope,'/relay/v1/tasks/stop',body|{'retry_failed':True},key)
            if stopped.get('status')=='failed':raise DomainError(stopped.get('reason_code') or 'PLATFORM_TASK_FAILED','Previous platform task did not stop',503)
            if stopped.get('status')!='succeeded':raise DomainError('PLATFORM_TASK_PENDING','Previous platform task stop is pending',503,True)
            if args.get('strategy')=='end_session':return stopped|{'strategy':'end_session'}
            return {'lease':self.request(scope,'/relay/v1/leases',{},key),'strategy':'new_round','status':'succeeded'}
        if operation=='end':return self.request(scope,'/relay/v1/tasks/stop',body|{'reason':args.get('reason','OPERATOR_STOP'),'retry_failed':True},key)
        if operation in ('open','pause','resume','close'):
            if operation=='open':
                start=self.request(scope,'/relay/v1/tasks/start',body,key)
                if start.get('status')=='failed':raise DomainError(start.get('reason_code') or 'PLATFORM_TASK_FAILED','Platform task failed',503)
                if start.get('status')!='succeeded':raise DomainError('PLATFORM_TASK_PENDING','Platform task is still starting',503,True)
                if key:
                    prior=self.request(scope,'/relay/v1/state',body|{'operation':'open','command_id':key})
                    if prior.get('command_result') is not None:return prior['command_result']|{'be_extension_version':'LW-BE/1.1.0'}
            body|={k:args[k] for k in ('round_id','window_epoch','expected_version','reason','winner_seq') if k in args and args[k] is not None}
            if operation=='open':
                if not lease.get('server_time') or not lease.get('mono_sent_ns'):raise DomainError('CLOCK_UNCERTAIN','Trusted relay time anchor missing',409)
                deadline=parse_time(lease['server_time'])+timedelta(milliseconds=(self.now_ns()-lease['mono_sent_ns'])/1000000+args['duration_ms'])
                body['deadline_at']=deadline.isoformat()
            return self.request(scope,'/relay/v1/windows/'+operation,body,key)
        raise DomainError('INPUT_INVALID','Unsupported relay operation',422)

    def freshness(self,scope,challenge):return self.request(scope,'/relay/v1/freshness',challenge)['proof']

    def revocations(self,scope):return self.request(scope,'/relay/v1/revocations',{})

    def delivery_stage(self,scope,ack):
        body={'stream_id':ack['stream_id'],'stream_seq':str(ack['stream_seq']),'stage':ack['stage'],'receipt_id':ack['receipt_id'],'retry_failed':bool(ack.get('retry_intent_id'))}
        if ack.get('client_time'):body['client_time']=ack['client_time'].isoformat()
        return self.request(scope,'/relay/v1/delivery-stages',body,ack.get('retry_intent_id') or ack['id'])


class NativeDeviceCredential:
    """Memory-only possession key/token; native secure storage supplies initial material."""
    def __init__(self,pairing_id,private_key,device_token,ttl_ms,*,provider_test=False,now_ns=time.monotonic_ns):
        self.pairing_id=pairing_id;self._key=private_key;self._token=device_token;self.provider_test=provider_test
        self.now_ns=now_ns;self._deadline=now_ns()+max(1000,ttl_ms-120000)*1000000 if device_token else now_ns();self._lock=threading.RLock()

    def __call__(self,scope):
        if self.provider_test and scope['run_purpose']=='production':raise DomainError('TEST_PROVIDER_FORBIDDEN','Synthetic device credentials cannot enter production',403)
        obj={'device_token':self._token,'authorization_id':scope['authorization_id'],'session_id':scope['session_id']}
        return base64.urlsafe_b64encode(__import__('json').dumps(obj,separators=(',',':')).encode()).rstrip(b'=').decode()

    def refresh_if_due(self,port,scope):
        with self._lock:
            if self.now_ns()<self._deadline:return
            parsed=urlsplit(port.target(scope)['url'])
            def request(operation,body):
                url=urlunsplit(('https' if parsed.scheme=='wss' else 'http',parsed.netloc,'/relay/v1/device-tokens/'+operation,'',''))
                headers={'X-BE-Extension-Version':'LW-BE/1.1.0'}
                if self._token:headers['Authorization']='Bearer '+self._token
                with port.client_factory() as client:response=client.post(url,headers=headers,json=body)
                if response.status_code!=200:raise DomainError('DEVICE_AUTHORIZATION_EXPIRED','Native device token renewal failed',403)
                result=response.json()
                if not isinstance(result,dict) or result.get('be_extension_version')!='LW-BE/1.1.0':raise DomainError('BE_EXTENSION_VERSION_UNSUPPORTED','Invalid device token extension response',409)
                return result
            challenge=request('challenges',{'pairing_id':self.pairing_id})
            try:raw=base64.b64decode(challenge['challenge_b64'],validate=True)
            except (ValueError,KeyError):raise DomainError('INPUT_INVALID','Invalid device challenge',422) from None
            verified=strict_json(raw)
            if verified.get('domain')!='wordheat-live.device-token' or verified.get('pairing_id')!=self.pairing_id or verified.get('challenge_id')!=challenge['challenge_id']:raise DomainError('INSTALLATION_MISMATCH','Device challenge scope mismatch',403)
            signature=base64.urlsafe_b64encode(self._key.sign(raw)).rstrip(b'=').decode()
            renewed=request('rotate',{'challenge_id':challenge['challenge_id'],'signature':signature})
            if renewed.get('pairing_id')!=self.pairing_id or type(renewed.get('ttl_ms')) is not int or not 0<renewed['ttl_ms']<=900000 or not isinstance(renewed.get('device_token'),str):raise DomainError('INPUT_INVALID','Invalid renewed device credential',422)
            self._token=renewed['device_token'];self._deadline=self.now_ns()+max(1000,renewed['ttl_ms']-120000)*1000000


class ConnectorSupervisor:
    """One active native WSS, independent bounded dispatch/monitor/ACK loops."""
    def __init__(self,coordinator,control_port,*,now_ns=time.monotonic_ns):
        self.owner=coordinator;self.port=control_port;self.db=coordinator.db;self.now_ns=now_ns
        self._running=threading.Lock();self._anchors={}
        self._polled={}

    async def run(self,stop):
        if not self._running.acquire(False):raise DomainError('OWNER_CONFLICT','Connector supervisor already owns the native WSS',409)
        async def monitor():
            while not stop.is_set():
                try:await asyncio.to_thread(self.owner.monitor)
                except Exception:self.owner.dependency_failed=True
                await asyncio.sleep(.5)
        async def dispatch():
            while not stop.is_set():
                try:await asyncio.to_thread(self.owner.worker_step)
                except Exception:self.owner.dependency_failed=True
                await asyncio.sleep(.2)
        async def historical_acks():
            while not stop.is_set():
                try:await asyncio.to_thread(self.flush_historical_ack)
                except Exception:self.owner.dependency_failed=True
                await asyncio.sleep(.2)
        async def delivery_observations():
            while not stop.is_set():
                try:await asyncio.to_thread(self.poll_delivery_status)
                except Exception:self.owner.dependency_failed=True
                await asyncio.sleep(1)
        tasks=[asyncio.create_task(monitor()),asyncio.create_task(dispatch()),asyncio.create_task(historical_acks()),asyncio.create_task(delivery_observations())]
        try:
            while not stop.is_set():
                try:
                    with self.db.engine.connect() as c:
                        candidates=c.execute(sa.select(m.bindings).join(s.sessions,m.bindings.c.session_id==s.sessions.c.id).where(m.bindings.c.boot_id==self.owner.boot_id,m.bindings.c.state.in_(['CONNECTING','AUTHENTICATING','CONNECTED','RECONNECTING'])).order_by(m.bindings.c.created_at).limit(2)).mappings().all()
                except Exception:
                    self.owner.dependency_failed=True;await asyncio.sleep(.2);continue
                if len(candidates)>1:
                    for item in candidates:
                        with self.db.engine.begin() as c:
                            session=self.owner.lock_session(c,item['session_id']);self.owner.interrupt(c,session,'OWNER_CONFLICT')
                    await asyncio.sleep(.2);continue
                if not candidates:await asyncio.sleep(.2);continue
                sid=candidates[0]['session_id']
                try:await self.connection(sid,stop)
                except Exception as exc:
                    if isinstance(exc,DomainError) and exc.code in ('LIVE_AUTHORIZATION_REVOKED','CANDIDATE_MISMATCH','EVIDENCE_REVOKED','FRAME_INTEGRITY_FAILURE','RELAY_GAP'):
                        with self.db.engine.begin() as c:
                            session=self.owner.lock_session(c,sid);self.owner.interrupt(c,session,exc.code)
                    try:await asyncio.to_thread(self.owner.disconnected,sid)
                    except Exception:self.owner.dependency_failed=True
                    await asyncio.sleep(.2)
        finally:
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            self._running.release()

    def snapshot(self,sid):
        with self.db.engine.connect() as c:
            state=c.execute(sa.select(m.bindings).where(m.bindings.c.session_id==sid)).mappings().one()
            return dict(state),state['scope']|{'session_id':sid,'connector_id':state['connector_id']}

    async def refresh_proof(self,sid,scope,connection):
        with self.db.engine.begin() as c:
            session=self.owner.lock_session(c,sid)
            challenge=self.owner.trust.challenge(c,session,connection)
        sent=self.now_ns();proof=await asyncio.to_thread(self.port.freshness,scope,challenge);received=self.now_ns()
        with self.db.engine.connect() as c:keys=self.owner.trust.effective_keys(c)
        verified=verify_signed_envelope(proof,keys,expected_type='freshness_proof')
        with self.db.engine.connect() as c:revision=self.owner.trust.registry(c).revocation_revision
        if verified.payload['revocation_revision']>revision:
            updates=await asyncio.to_thread(self.port.revocations,scope)
            if not isinstance(updates.get('items'),list) or len(updates['items'])>1000:raise DomainError('REVOCATION_STATE_INCOMPLETE','Invalid signed revocation update batch',409)
            with self.db.engine.begin() as c:
                self.owner.lock_session(c,sid)
                for envelope in updates['items']:self.owner.trust.import_envelope(c,envelope)
        server=parse_time(verified.payload['server_time']);old=self._anchors.get(sid)
        clock_trusted=received-sent<=500000000
        if old:
            expected=old[0]+timedelta(seconds=(sent-old[1])/1000000000)
            clock_trusted=clock_trusted and abs((server-expected).total_seconds())<=.5
        if clock_trusted:self._anchors[sid]=(server,sent)
        with self.db.engine.begin() as c:
            session=self.owner.lock_session(c,sid)
            self.owner.accept_proof(c,session,proof,clock_trusted=clock_trusted)

    async def renew(self,sid,scope):
        state,_=self.snapshot(sid);sent=self.now_ns()
        response=await asyncio.to_thread(self.port.execute,scope,'renew',{'lease':state['lease']})
        ttl=min(int(response['lease_ttl_ms']),int((parse_time(response['expires_at'])-parse_time(response['server_time'])).total_seconds()*1000))-3000
        deadline=sent+ttl*1000000
        if ttl<=0 or self.now_ns()>=deadline:raise DomainError('LEASE_EXPIRED','Lease renewal consumed its safety budget',409)
        with self.db.engine.begin() as c:
            session=self.owner.lock_session(c,sid);current=self.owner.store.binding(c,sid)
            if current['lease'].get('lease_id')!=response['lease_id']:raise DomainError('LEASE_STALE','Lease changed during renewal',409)
            c.execute(m.bindings.update().where(m.bindings.c.id==current['id']).values(lease=current['lease']|response|{'mono_sent_ns':sent},lease_deadline_ns=deadline))
            self.owner.publish_transport(c,session)

    def claim_ack(self,sid):
        stamp=datetime.now(timezone.utc)
        with self.db.engine.begin() as c:
            self.owner.lock_session(c,sid)
            state=self.owner.store.binding(c,sid)
            old_source=sa.exists(sa.select(m.deliveries.c.id).where(m.deliveries.c.session_id==sid,m.deliveries.c.stream_id==m.acks.c.stream_id,m.deliveries.c.stream_seq==m.acks.c.stream_seq,m.deliveries.c.body['original_generation'].astext!=str(state['lease'].get('owner_generation'))))
            row=c.execute(sa.select(m.acks).where(m.acks.c.session_id==sid,m.acks.c.stream_id==state['stream_id'],m.acks.c.stage=='transport.persisted',m.acks.c.cycle_attempts<6,sa.or_(m.acks.c.next_attempt_at.is_(None),m.acks.c.next_attempt_at<=stamp),sa.or_(m.acks.c.status.in_(['pending','retry']),sa.and_(m.acks.c.status=='running',m.acks.c.lease_until<stamp))).order_by(m.acks.c.created_at,m.acks.c.stream_seq).with_for_update(skip_locked=True).limit(1)).mappings().first()
            if not row:return None
            token=str(uuid4());c.execute(m.acks.update().where(m.acks.c.id==row['id']).values(status='running',claim_token=token,lease_until=stamp+timedelta(seconds=5),attempts=row['attempts']+1,cycle_attempts=row['cycle_attempts']+1))
            self.owner.store.observe(c,sid)
            return dict(row)|{'claim_token':token}

    def flush_historical_ack(self):
        stamp=datetime.now(timezone.utc)
        old_source=sa.exists(sa.select(m.deliveries.c.id).where(m.deliveries.c.session_id==m.acks.c.session_id,m.deliveries.c.stream_id==m.acks.c.stream_id,m.deliveries.c.stream_seq==m.acks.c.stream_seq,m.deliveries.c.body['original_generation'].astext!=m.bindings.c.lease['owner_generation'].astext))
        query=sa.select(m.acks).join(m.bindings,m.bindings.c.session_id==m.acks.c.session_id).join(s.sessions,s.sessions.c.id==m.acks.c.session_id).where(sa.or_(m.acks.c.stage.in_(['display.received','display.processed']),m.bindings.c.state.not_in(['CONNECTED','CONNECTING','AUTHENTICATING','RECONNECTING']),m.acks.c.stream_id!=m.bindings.c.stream_id),sa.or_(m.acks.c.status.in_(['pending','retry']),sa.and_(m.acks.c.status=='running',m.acks.c.lease_until<stamp)),sa.or_(m.acks.c.next_attempt_at.is_(None),m.acks.c.next_attempt_at<=stamp)).order_by(m.acks.c.created_at).limit(1)
        with self.db.engine.connect() as c:chosen=c.execute(query).mappings().first()
        if not chosen:return False
        with self.db.engine.begin() as c:
            session=self.owner.lock_session(c,chosen['session_id']);state=self.owner.store.binding(c,chosen['session_id'])
            row=c.execute(sa.select(m.acks).where(m.acks.c.id==chosen['id']).with_for_update(skip_locked=True)).mappings().first()
            if not row or row['status'] not in ('pending','retry','running') or row['status']=='running' and row['lease_until'] and row['lease_until']>stamp:return False
            token=str(uuid4());ack=dict(row);scope=state['scope']|{'session_id':row['session_id'],'connector_id':state['connector_id']}
            c.execute(m.acks.update().where(m.acks.c.id==row['id']).values(status='running',attempts=row['attempts']+1,cycle_attempts=row['cycle_attempts']+1,claim_token=token,lease_until=stamp+timedelta(seconds=10)))
        try:
            result=self.port.delivery_stage(scope,ack)
            if result.get('status')!='accepted' or result.get('stream_id')!=ack['stream_id'] or result.get('stream_seq')!=str(ack['stream_seq']) or result.get('stage')!=ack['stage']:raise DomainError('INPUT_INVALID','Historical acknowledgement response mismatch',422)
            error=None
        except Exception as exc:result={};error=exc.code if isinstance(exc,DomainError) else 'RELAY_UNAVAILABLE'
        with self.db.engine.begin() as c:
            self.owner.lock_session(c,ack['session_id']);self.owner.store.binding(c,ack['session_id'])
            if error:
                retry=ack['cycle_attempts']+1<6 and error in ('RELAY_UNAVAILABLE','DEPENDENCY_UNAVAILABLE','RATE_LIMITED')
                c.execute(m.acks.update().where(m.acks.c.id==ack['id'],m.acks.c.claim_token==token).values(status='retry' if retry else 'failed',last_error=error,next_attempt_at=stamp+timedelta(seconds=min(30,2**(ack['attempts']+1))),claim_token=None,lease_until=None))
            else:c.execute(m.acks.update().where(m.acks.c.id==ack['id'],m.acks.c.claim_token==token).values(status='succeeded',response=result,revision=m.acks.c.revision+1,claim_token=None,lease_until=None))
            self.owner.store.observe(c,ack['session_id'])
            if self.owner.hooks:self.owner.hooks.touch(c,ack['session_id'])
        return True

    def poll_delivery_status(self):
        with self.db.engine.connect() as c:
            query=sa.select(m.bindings.c.session_id).join(s.sessions,s.sessions.c.id==m.bindings.c.session_id).where(sa.or_(m.bindings.c.remote_pending.is_(None),m.bindings.c.remote_pending>0),sa.exists(sa.select(m.acks.c.id).where(m.acks.c.session_id==m.bindings.c.session_id,m.acks.c.status=='succeeded'))).order_by(m.bindings.c.created_at.desc()).limit(256)
            sids=c.execute(query).scalars().all()
        if not sids:return False
        sid=min(sids,key=lambda value:self._polled.get(value,0));self._polled[sid]=self.now_ns()
        with self.db.engine.connect() as c:
            state=c.execute(sa.select(m.bindings).where(m.bindings.c.session_id==sid)).mappings().one()
            ack=c.execute(sa.select(m.acks).where(m.acks.c.session_id==sid,m.acks.c.status=='succeeded').order_by(m.acks.c.created_at.desc()).limit(1)).mappings().one()
            revision=state['revision'];scope=state['scope']|{'session_id':sid,'connector_id':state['connector_id']}
        try:observed=self.port.delivery_stage(scope,dict(ack))
        except DomainError:return False
        count=observed.get('remote_fulfillment_pending')
        if count is not None and (type(count) is not int or count<0):raise DomainError('INPUT_INVALID','Invalid relay fulfillment observation',422)
        with self.db.engine.begin() as c:
            self.owner.lock_session(c,sid);current=self.owner.store.binding(c,sid)
            if current['revision']!=revision:return False
            if current['remote_pending']==count:return False
            c.execute(m.bindings.update().where(m.bindings.c.id==current['id']).values(remote_pending=count))
            self.owner.store.observe(c,sid)
            if self.owner.hooks:self.owner.hooks.touch(c,sid)
        return True

    def accept_ack(self,sid,ack,response):
        if ack['stage']=='transport.persisted':
            valid=response.get('kind')=='transport.persisted.accepted' and cursor(response.get('persisted_seq'))>=ack['stream_seq']
        else:valid=response.get('kind')=='delivery.stage.accepted' and response.get('stage')==ack['stage'] and response.get('status')=='pending'
        if not valid:raise DomainError('INPUT_INVALID','Relay ACK response does not match pending stage',422)
        with self.db.engine.begin() as c:
            self.owner.lock_session(c,sid)
            c.execute(m.acks.update().where(m.acks.c.id==ack['id'],m.acks.c.claim_token==ack['claim_token']).values(status='succeeded',claim_token=None,lease_until=None,response=response,revision=m.acks.c.revision+1))
            self.owner.store.observe(c,sid)
            if self.owner.hooks:self.owner.hooks.touch(c,sid)

    def contact(self,sid,connection):
        with self.db.engine.begin() as c:
            session=self.owner.lock_session(c,sid);state=self.owner.store.binding(c,sid)
            if state['connection'].get('connection_nonce')==connection['connection_nonce'] and state['state'] in ('CONNECTED','AUTHENTICATING'):
                c.execute(m.bindings.update().where(m.bindings.c.id==state['id']).values(last_contact_ns=self.now_ns(),connection=state['connection']|{'_last_confirmed_at':datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')}))
                self.owner.publish_transport(c,session)

    async def connection(self,sid,stop):
        state,scope=self.snapshot(sid);target=self.port.target(scope)
        async with RegisteredConnect(target['url'],additional_headers={'Authorization':'Bearer '+self.port.token(scope),'X-BE-Extension-Version':'LW-BE/1.1.0'},proxy=None,max_size=65536,ping_interval=None,ping_timeout=None,open_timeout=5,close_timeout=2) as ws:
            await ws.send(__import__('json').dumps({'kind':'resume','lease_id':state['lease']['lease_id'],'persisted_seq':str(state['persisted_seq'])}))
            ready=strict_json(await asyncio.wait_for(ws.recv(),5))
            required={'kind','lease_id','installation_id','delivery_generation','connection_nonce','persisted_seq'}
            if set(ready)!=required or ready['kind']!='resume_ready':raise DomainError('RELAY_GAP','Relay resume was not accepted',409)
            for key in ('delivery_generation','persisted_seq'):cursor(ready[key])
            if cursor(ready['persisted_seq'])>state['persisted_seq']:raise DomainError('RELAY_GAP','Cloud cursor exceeds local durable watermark',409)
            connection={k:v for k,v in ready.items() if k!='kind'}
            with self.db.engine.begin() as c:
                session=self.owner.lock_session(c,sid);self.owner.connection_ready(c,session,connection)
            await self.refresh_proof(sid,scope,connection)
            last_renew=self.now_ns();inflight=None
            async def heartbeat():
                while not stop.is_set():
                    try:
                        pong=await asyncio.wait_for(ws.ping(),1)
                        await asyncio.wait_for(pong,2)
                        await asyncio.to_thread(self.contact,sid,connection)
                    except Exception:
                        await asyncio.to_thread(self.owner.disconnected,sid)
                    await asyncio.sleep(2)
            beat=asyncio.create_task(heartbeat())
            try:
              while not stop.is_set():
                current,_=self.snapshot(sid)
                if current['state'] in ('STOPPED','INTERRUPTED','GAP','RECOVERING'):return
                if current['connection'].get('connection_nonce')!=connection['connection_nonce']:return
                if self.now_ns()-last_renew>=2000000000:
                    await self.renew(sid,scope);await self.refresh_proof(sid,scope,connection);last_renew=self.now_ns()
                if inflight is None:
                    inflight=await asyncio.to_thread(self.claim_ack,sid)
                    if inflight:
                        outgoing={'kind':inflight['stage']}
                        outgoing|={'persisted_seq':str(inflight['stream_seq'])} if inflight['stage']=='transport.persisted' else {'stream_seq':str(inflight['stream_seq']),'receipt_id':inflight['receipt_id']}
                        await ws.send(__import__('json').dumps(outgoing))
                try:incoming=strict_json(await asyncio.wait_for(ws.recv(),.1))
                except asyncio.TimeoutError:incoming=None
                if incoming:
                    await asyncio.to_thread(self.contact,sid,connection)
                    if incoming.get('kind')=='relay.frame':await asyncio.to_thread(self.owner.receive,sid,incoming,connection)
                    elif incoming.get('kind') in ('transport.persisted.accepted','delivery.stage.accepted') and inflight:
                        await asyncio.to_thread(self.accept_ack,sid,inflight,incoming);inflight=None
                    elif incoming.get('kind') in ('stream.gap','relay.error'):raise DomainError(incoming.get('reason_code','RELAY_GAP'),'Relay rejected current stream',409)
                    else:raise DomainError('INPUT_INVALID','Unknown relay control frame',422)
                for _ in range(100):
                    if not await asyncio.to_thread(self.owner.process_one,sid):break
            finally:
                beat.cancel();await asyncio.gather(beat,return_exceptions=True)
