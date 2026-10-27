"""Persistent official-session intents and commit guards.

The caller owns the session lock. Platform/WS IO is always outside transactions.
Business authority remains in the supplied LocalService hooks.
"""
from datetime import datetime,timedelta,timezone
import time
import re
from uuid import uuid4
import sqlalchemy as sa
from app.errors import DomainError
from app.infrastructure.db import schema as s
from . import models as m
from .store import ConnectorStore


def utc():return datetime.now(timezone.utc)
def uid():return str(uuid4())


class OfficialCoordinator:
    def __init__(self,database,trusted_control,*,control_port=None,hooks=None,registry=None,observer=None,now_ns=time.monotonic_ns):
        self.db=database;self.trust=trusted_control;self.port=control_port;self.hooks=hooks
        self.registry=registry or {};self.now_ns=now_ns;self.store=ConnectorStore(database,observer)
        self.boot_id=trusted_control.boot_id
        self.dependency_failed=False

    def authorize_create(self,c,actor,body):return self.trust.authorize_create(c,actor,body)

    def public_transport(self,c,session):
        """Read-only C1 projection. Never refreshes grants or touches network IO.

        Monotonic facts are usable only in their owning boot. Wall-clock contact
        timestamps are recorded by actual proof/contact writers, never inferred
        from a row update or the viewer's request time.
        """
        row=c.execute(sa.select(m.bindings).where(m.bindings.c.session_id==session['id'])).mappings().first()
        confirmed=None
        if row:
            value=row['connection'].get('_last_confirmed_at')
            if isinstance(value,str):
                try:
                    actual=datetime.fromisoformat(value.replace('Z','+00:00'))
                    if actual.tzinfo is not None: confirmed=actual.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
                except ValueError: pass
        def result(state,reason,recoverable=False):
            return {'state':state,'reason_code':reason,'recoverable':recoverable,'last_confirmed_at':confirmed}
        if not row:return result('DISCONNECTED','RELAY_NOT_CONNECTED')
        status=row['state']
        error=row['last_error']
        error=error if isinstance(error,str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}',error) else None
        if status=='STOPPED':return result('DISCONNECTED','CONNECTOR_STOPPED')
        if status in ('GAP','INTERRUPTED','RECOVERING'):
            return result('FAULTED',error or ('RELAY_GAP' if status=='GAP' else 'CONNECTOR_RECOVERY_REQUIRED'))
        if self.dependency_failed:return result('FAULTED','DATABASE_DEPENDENCY_LOST')
        if row['boot_id']!=self.boot_id:return result('FAULTED','BOOT_CHANGED')
        if any(row['scope'].get(key)!=session.get(key) for key in ('app_id','room_id','room_binding_id','installation_id','live_profile','authorization_id','candidate_digest','grant_digest','run_purpose')):
            return result('FAULTED','CANDIDATE_MISMATCH')
        if status=='CREATED':return result('DISCONNECTED',error or 'RELAY_NOT_CONNECTED')
        moment=self.now_ns()
        if not row['lease_deadline_ns'] or moment>=row['lease_deadline_ns']:return result('FAULTED','LEASE_EXPIRED')
        if row['disconnected_ns'] and moment-row['disconnected_ns']>=6000000000:return result('FAULTED','RELAY_DISCONNECTED')
        if status=='RECONNECTING':return result('RECONNECTING',error or 'RELAY_DISCONNECTED',True)
        if status in ('CONNECTING','AUTHENTICATING'):return result('CONNECTING',error or 'RELAY_CONNECTING',True)
        if status=='CONNECTED':
            if not row['last_contact_ns'] or moment-row['last_contact_ns']>=6000000000:return result('FAULTED','RELAY_DISCONNECTED')
            if row['lease'].get('official_ingress_ready') is not True or row['lease'].get('platform_ready') is not True:return result('FAULTED','READINESS_NOT_MET')
            return result('CONNECTED','OK')
        return result('FAULTED',error or 'CONNECTOR_NOT_READY')

    def publish_transport(self,c,session):
        """Publish once per committed semantic transition, under the session lock.

        Contact timestamps can advance on every pong without consuming audience
        sequence numbers. The outbox is the durable prior state across restart.
        """
        if session.get('mode')!='official' or not callable(getattr(self.hooks,'event',None)):return
        value=self.public_transport(c,session)
        previous=c.execute(sa.select(s.outbox.c.payload).where(s.outbox.c.session_id==session['id'],s.outbox.c.audience=='DISPLAY',s.outbox.c.type=='connection.status').order_by(s.outbox.c.seq.desc()).limit(1)).scalar_one_or_none()
        before=previous.get('transport',{}) if previous else {'state':'DISCONNECTED','reason_code':'RELAY_NOT_CONNECTED','recoverable':False}
        if any(before.get(key)!=value[key] for key in ('state','reason_code','recoverable')):
            self.hooks.event(c,session['id'],'connection.status',{'transport':value})
            self.hooks.touch(c,session['id'])

    def observations(self,c,session):
        state=c.execute(sa.select(m.bindings).where(m.bindings.c.session_id==session['id'])).mappings().first()
        scope={k:session.get(k) for k in ('app_id','room_binding_id','installation_id','candidate_digest')}
        room=c.execute(sa.select(m.room_bindings).where(m.room_bindings.c.id==session.get('room_binding_id'))).mappings().first()
        room_matches=bool(room and all(room[k]==session.get(k) for k in ('app_id','room_id','installation_id')) and (not state or room['connector_id']==state['connector_id']))
        actor_allowed=bool(room and session.get('actor_id') in room['authorization'].get('actor_ids',[]))
        room_ok=bool(room_matches and actor_allowed and room['status']=='pass' and room['expires_at'] and room['expires_at']>utc() and not room['authorization'].get('provider_test'))
        def item(ok,reason):return {'status':'pass' if ok else 'pending' if not state or state['state'] in ('CREATED','CONNECTING','AUTHENTICATING','RECONNECTING') else 'fail','reason_code':'OK' if ok else reason,'message':'','scope':scope}
        connected=bool(state and state['state']=='CONNECTED' and state['boot_id']==self.boot_id and state['lease'].get('official_ingress_ready') is True and state['lease'].get('platform_ready') is True)
        lease=bool(connected and self.now_ns()<state['lease_deadline_ns'])
        binding_failure=bool(room and (not room_matches or not actor_allowed or room['status']!='pass' or room['authorization'].get('provider_test')))
        return {'checks':{'relay':item(connected,'RELAY_NOT_CONNECTED'),'lease':item(lease,'LEASE_NOT_READY')},'platform_binding':{'status':'pass' if room_ok else 'fail' if binding_failure else 'pending','reason_code':'ROOM_BINDING_SCOPE_MISMATCH' if room and not room_matches else 'PLATFORM_AUTHORIZATION_REQUIRED','scope':{k:scope[k] for k in ('app_id','room_binding_id','installation_id')}}}

    def ensure_binding(self,c,session):
        row=c.execute(sa.select(m.bindings).where(m.bindings.c.session_id==session['id']).with_for_update()).mappings().first()
        fields=('app_id','room_id','room_binding_id','installation_id','live_profile','authorization_id','candidate_digest','grant_digest','run_purpose')
        scope={key:session.get(key) for key in fields}
        if any(scope[k] is None for k in fields):raise DomainError('LIVE_AUTHORIZATION_REQUIRED','Immutable session authorization is incomplete',403)
        if row:
            if row['scope']!=scope:raise DomainError('CANDIDATE_MISMATCH','Immutable session binding changed',409)
            return dict(row)
        room=c.execute(sa.select(m.room_bindings).where(m.room_bindings.c.id==scope['room_binding_id'])).mappings().first()
        if not room or room['status']!='pass' or any(room[k]!=scope[k] for k in ('app_id','room_id','installation_id')):raise DomainError('PLATFORM_AUTHORIZATION_REQUIRED','Trusted immutable room binding required',403)
        key=uid();c.execute(m.bindings.insert().values(id=key,session_id=session['id'],connector_id=room['connector_id'],scope=scope,state='CREATED',boot_id=self.boot_id))
        return self.store.binding(c,session['id'])

    def require_current(self,c,session,round=None,*,result=False):
        if self.dependency_failed:raise DomainError('DEPENDENCY_UNAVAILABLE','Database recovery must interrupt official work before reuse',503)
        state=self.ensure_binding(c,session)
        room=c.execute(sa.select(m.room_bindings).where(m.room_bindings.c.id==session['room_binding_id']).with_for_update(read=True)).mappings().first()
        if not room or any(room[k]!=session.get(k) for k in ('app_id','room_id','installation_id')) or room['connector_id']!=state['connector_id'] or room['status']!='pass' or not room['expires_at'] or room['expires_at']<=utc() or session['actor_id'] not in room['authorization'].get('actor_ids',[]) or room['authorization'].get('provider_test'):raise DomainError('PLATFORM_AUTHORIZATION_REQUIRED','Current platform binding no longer authorizes this immutable scene',403)
        if state['state'] in ('RECONNECTING','AUTHENTICATING') and state['boot_id']==self.boot_id and state['disconnected_ns'] and self.now_ns()-state['disconnected_ns']<6000000000 and self.now_ns()<state['lease_deadline_ns']:
            raise DomainError('CONNECTOR_RECONNECTING','Official commits are frozen within the bounded reconnect grace',409,True)
        if state['state']!='CONNECTED' or state['boot_id']!=self.boot_id or self.now_ns()>=state['lease_deadline_ns']:raise DomainError('LEASE_EXPIRED','Current conservative lease is required',409)
        if state['lease'].get('official_ingress_ready') is not True or state['lease'].get('platform_ready') is not True:raise DomainError('READINESS_NOT_MET','Current official relay/platform adapter is not ready',409)
        if round and state['lease'].get('window_epoch') is not None and str(round['epoch'])!=str(state['lease']['window_epoch']):raise DomainError('WINDOW_EPOCH_CONFLICT','Round window is no longer current',409)
        runtime=self.trust.require_current(c,session)
        if self.hooks is None:raise DomainError('DEPENDENCY_UNAVAILABLE','Business readiness hooks are not connected',503)
        readiness=getattr(self.hooks,'readiness',None)
        if readiness is None or readiness.trusted_control is not self.trust or readiness.connector is not self:raise DomainError('DEPENDENCY_UNAVAILABLE','Trusted readiness composition is incomplete',503)
        permissions=c.execute(sa.select(s.operators.c.permissions).where(s.operators.c.id==session['actor_id'],s.operators.c.disabled.is_(False))).scalar_one_or_none() or []
        result_state=readiness.evaluate(c,session,permissions)
        if not result_state['profiles'][session['live_profile']]['allowed']:raise DomainError('READINESS_NOT_MET','Current server observations do not authorize this official operation',409,details={'profile':session['live_profile'],'blockers':result_state['profiles'][session['live_profile']]['blockers']})
        return runtime

    def mark_started(self,c,session):return self.trust.mark_started(c,session)

    def connector_command(self,c,actor,connector_id,body):
        sid=body.get('session_id') or body.get('args',{}).get('session_id')
        session=self.lock_session(c,sid)
        if session['actor_id']!=actor:raise DomainError('FORBIDDEN','Session ownership required',403)
        if session['version']!=body['expected_version']:raise DomainError('ROUND_VERSION_CONFLICT','Session version changed',409)
        state=self.ensure_binding(c,session)
        if state['connector_id']!=connector_id:raise DomainError('FORBIDDEN','Connector binding mismatch',403)
        if body['command']=='query' and body.get('args',{}).get('retry_failed_deliveries') is True:
            command_id=uid()
            c.execute(m.acks.update().where(m.acks.c.session_id==sid,sa.or_(m.acks.c.status=='failed',sa.and_(m.acks.c.status=='succeeded',m.acks.c.stage.in_(['display.received','display.processed'])))).values(status='retry',cycle_attempts=0,retry_cycle=m.acks.c.retry_cycle+1,retry_intent_id=command_id,next_attempt_at=None,last_error=None,claim_token=None,lease_until=None))
            c.execute(m.bindings.update().where(m.bindings.c.id==state['id']).values(remote_pending=None))
            self.store.observe(c,sid)
            if self.hooks:self.hooks.touch(c,sid)
            return {'command_id':command_id,'status':'applied','resource_version':session['version'],'state':'DELIVERY_RETRY_QUEUED','error':None}
        active=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid,s.rounds.c.status.in_(['PREPARING','RUNNING','PAUSED','DRAINING','FAULTED'])).with_for_update()).mappings().first()
        return self.command(c,session,dict(active) if active else None,body['command'],body.get('args'))

    def queue(self,c,session,operation,args=None,round=None):
        self.ensure_binding(c,session)
        key=uid();command=uid()
        c.execute(m.intents.insert().values(id=key,session_id=session['id'],round_id=round['id'] if round else None,operation=operation,args=dict(args or {})|{'_queued_mono_ns':self.now_ns(),'_boot_id':self.boot_id},status='pending',command_id=command))
        return {'command_id':command,'status':'pending','resource_version':session['version'],'state':{'intent_id':key,'operation':operation},'error':None}

    def prepare_start(self,c,session,*,fresh_start=False):
        grant=self.trust.get_grant(c,session['authorization_id'])
        if grant.digest!=session.get('grant_digest'):raise DomainError('CANDIDATE_MISMATCH','Grant changed after create',409)
        state=self.ensure_binding(c,session)
        if state['last_error']=='CANDIDATE_MISMATCH':raise DomainError('CANDIDATE_MISMATCH','This immutable draft was invalidated; create a newly authorized session',409)
        actual=self.trust.candidate_digest() if callable(self.trust.candidate_digest) else self.trust.candidate_digest
        if actual!=session['candidate_digest']:raise DomainError('CANDIDATE_MISMATCH','Installed candidate changed after create',409)
        if state['state']=='CONNECTED':
            self.require_current(c,session)
            return self.queue(c,session,'prepare',{'reuse_connection':True})
        return self.queue(c,session,'prepare',{'fresh_start':fresh_start is True})

    def create_round(self,c,session,round):
        self.require_current(c,session)
        return self.queue(c,session,'open',{'duration_ms':round['frozen_rules']['duration_seconds']*1000},round)

    def command(self,c,session,round,operation,args=None):
        if operation not in ('pause','resume','close','end','recover','stop','query','connect','start'):raise DomainError('INPUT_INVALID','Unsupported connector operation',422)
        if operation in ('connect','start'):return self.prepare_start(c,session,fresh_start=operation=='start' or (args or {}).get('fresh_start') is True)
        if operation=='query':
            state=self.ensure_binding(c,session)
            return {'command_id':uid(),'status':'applied','resource_version':session['version'],'state':state['state'],'error':None}
        if operation=='recover':
            strategy=(args or {}).get('strategy')
            if strategy not in ('new_round','end_session'):raise DomainError('FORBIDDEN','Official recovery never resumes an old local round',403)
            state=self.ensure_binding(c,session)
            if strategy=='new_round' and (state['state']=='GAP' or state['boot_id']!=self.boot_id):raise DomainError('RELAY_GAP','This recovery requires ending the old session',409)
            if round and self.hooks:self.hooks.apply_window_result(c,session,round,{'operation':'interrupt','status':'failed','error_code':'OPERATOR_RECOVERY'})
            c.execute(m.bindings.update().where(m.bindings.c.id==state['id']).values(state='RECOVERING'))
            return self.queue(c,session,'recover',args,round)
        if operation not in ('end','stop','close'):self.require_current(c,session,round)
        request=dict(args or {})
        state=self.ensure_binding(c,session)
        if round:request|={'window_epoch':str(round['epoch']),'expected_version':state['lease'].get('window_version',1)}
        if operation in ('end','stop') and round and round['status'] in ('PREPARING','RUNNING','PAUSED','DRAINING','FAULTED'):
            close=self.queue(c,session,'close',request|{'reason':'INTERRUPTED'},round)
            request['after_intent_id']=close['state']['intent_id']
        return self.queue(c,session,'end' if operation=='stop' else operation,request,round)

    def status(self,c,session,intent_id):
        row=c.execute(sa.select(m.intents).where(m.intents.c.id==intent_id,m.intents.c.session_id==session['id'])).mappings().first()
        if not row:raise DomainError('RESOURCE_NOT_FOUND','Connector command not found',404)
        return {'command_id':row['command_id'],'status':'applied' if row['status']=='succeeded' else 'failed' if row['status']=='failed' else 'cancelled' if row['status']=='cancelled' else 'pending','resource_version':session['version'],'state':{'intent_id':row['id'],'operation':row['operation']},'error':{'code':row['last_error'],'message':row['last_error'],'retryable':False} if row['status']=='failed' else None}

    def lookup_command(self,c,command_id,actor_id,permissions):
        row=c.execute(sa.select(m.intents).where(m.intents.c.command_id==command_id)).mappings().first()
        if not row:return None
        session=c.execute(sa.select(s.sessions).where(s.sessions.c.id==row['session_id'])).mappings().one()
        if session['actor_id']!=actor_id or 'session.control' not in permissions:raise DomainError('FORBIDDEN','Current command permission and ownership required',403)
        return self.status(c,dict(session),row['id'])

    def pending_commands(self,c,session,round_id=None):
        query=sa.select(m.intents.c.id).where(m.intents.c.session_id==session['id'],m.intents.c.status.in_(['pending','waiting','running','retry','failed']))
        if round_id is not None:query=query.where(m.intents.c.round_id==round_id)
        return [self.status(c,session,key) for key in c.execute(query.order_by(m.intents.c.created_at)).scalars()]

    def stop_projection(self,c):
        if c.execute(sa.text("SELECT to_regclass('connector_intents')")).scalar_one() is None:raise DomainError('DEPENDENCY_UNAVAILABLE','Connector task persistence is unavailable',503)
        query=sa.select(m.intents.c.id,m.intents.c.session_id,m.intents.c.status,m.intents.c.last_error).join(m.bindings,m.bindings.c.session_id==m.intents.c.session_id).where(m.intents.c.operation.in_(['end','recover']),m.intents.c.status.not_in(['succeeded','cancelled']),m.bindings.c.state!='STOPPED')
        return {'platform_stop_pending':bool(rows:=c.execute(query).mappings().all()),'tasks':[dict(row) for row in rows]}

    def invalidate_bundle(self,c,identity,bundle_digest):
        """Trusted bundle activation calls under its existing catalog/session locks."""
        if not isinstance(bundle_digest,str) or len(bundle_digest)!=64:raise DomainError('INPUT_INVALID','Verified bundle digest required',422)
        rows=c.execute(sa.select(s.sessions).where(s.sessions.c.mode=='official',s.sessions.c.status.not_in(['ENDED'])).order_by(s.sessions.c.id).with_for_update()).mappings().all()
        changed=[]
        for raw in rows:
            session=dict(raw);sid=session['id']
            state=c.execute(sa.select(m.bindings).where(m.bindings.c.session_id==sid).with_for_update()).mappings().first()
            if not state:
                room=c.execute(sa.select(m.room_bindings).where(m.room_bindings.c.id==session['room_binding_id'])).mappings().first()
                scope={k:session.get(k) for k in ('app_id','room_id','room_binding_id','installation_id','live_profile','authorization_id','candidate_digest','grant_digest','run_purpose')}
                c.execute(m.bindings.insert().values(id=uid(),session_id=sid,connector_id=room['connector_id'] if room else 'scope-unavailable',scope=scope,state='INTERRUPTED',boot_id=self.boot_id,last_error='CANDIDATE_MISMATCH'))
                state=c.execute(sa.select(m.bindings).where(m.bindings.c.session_id==sid)).mappings().one()
            self.trust._runtime.pop(sid,None)
            if sid in self.trust._nonces:self.trust._nonces[sid].reset(self.boot_id,'invalidated:'+uid())
            if state:
                c.execute(m.bindings.update().where(m.bindings.c.id==state['id']).values(state='INTERRUPTED',last_error='CANDIDATE_MISMATCH',authorization_deadline_ns=0))
                c.execute(m.intents.update().where(m.intents.c.session_id==sid,m.intents.c.operation.not_in(['end']),m.intents.c.status.in_(['pending','waiting','running','retry'])).values(status='failed',last_error='CANDIDATE_MISMATCH',claim_token=None,lease_until=None))
                if state['lease'].get('lease_id'):self.queue(c,session,'end',{'reason':'CANDIDATE_MISMATCH'})
                self.store.observe(c,sid)
            c.execute(s.sessions.update().where(s.sessions.c.id==sid).values(status='RECOVERY_REQUIRED' if session['status']!='CREATED' else 'CREATED',version=s.sessions.c.version+1))
            self.publish_transport(c,session)
            changed.append(sid)
        return changed

    def record_display_receipt(self,c,display,body):
        sid=display['session_id']
        session=c.execute(sa.select(s.sessions).where(s.sessions.c.id==sid)).mappings().one()
        if session['mode']!='official':return
        for item in body['deliveries']:
            source=c.execute(sa.select(m.deliveries).where(m.deliveries.c.session_id==sid,m.deliveries.c.feedback_id==item['feedback_id'])).mappings().first()
            if not source:raise DomainError('DISPLAY_SOURCE_DELIVERY_REQUIRED','Official effect has no durable relay source mapping',409)
            self.store.receipt(c,sid,source['stream_id'],source['stream_seq'],'display.'+body['stage'],body['batch_id'],item['client_time'])

    def terminal_feedback(self,c,feedback_id):
        source=c.execute(sa.select(m.deliveries).where(m.deliveries.c.feedback_id==feedback_id)).mappings().first()
        if source:self.store.advance(c,source['session_id'],source['stream_seq'],terminal=True)

    def lock_session(self,c,sid):
        from app.modules.wordbank.selection import catalog_lock
        catalog_lock(c)
        row=c.execute(sa.select(s.sessions).where(s.sessions.c.id==sid).with_for_update()).mappings().first()
        if not row:raise DomainError('RESOURCE_NOT_FOUND','Session not found',404)
        return dict(row)

    def callback(self,c,session,row,result):
        if self.hooks is None:raise DomainError('DEPENDENCY_UNAVAILABLE','Business connector hooks are not connected',503)
        round=c.execute(sa.select(s.rounds).where(s.rounds.c.id==row['round_id']).with_for_update()).mappings().first() if row['round_id'] else None
        self.hooks.apply_window_result(c,session,dict(round) if round else None,result|{'intent_id':row['id'],'operation':row['operation'],'round_id':row['round_id']})

    def publish(self,c,session,intent):
        self.publish_transport(c,session)
        old=c.execute(sa.select(s.commands).where(s.commands.c.id==intent['command_id'])).mappings().first()
        if old:
            version=c.execute(sa.select(s.sessions.c.version).where(s.sessions.c.id==session['id'])).scalar_one()
            result=self.status(c,session|{'version':version},intent['id'])
            c.execute(s.commands.update().where(s.commands.c.id==intent['command_id']).values(response=result))
            if self.hooks:
                self.hooks.event(c,session['id'],'command.updated',{'command':result},audiences=('STUDIO',))
        if self.hooks:self.hooks.touch(c,session['id'])

    def worker_step(self):
        """Claim one intent; save network result before waiting for its durable barrier."""
        stamp=utc()
        with self.db.engine.begin() as c:
            row=c.execute(sa.select(m.intents).where(sa.or_(m.intents.c.status.in_(['pending','retry']),sa.and_(m.intents.c.status=='running',m.intents.c.lease_until<stamp)),sa.or_(m.intents.c.next_attempt_at.is_(None),m.intents.c.next_attempt_at<=stamp)).order_by(m.intents.c.created_at).with_for_update(skip_locked=True).limit(1)).mappings().first()
            if not row:return False
            if row['args'].get('after_intent_id'):
                parent=c.execute(sa.select(m.intents.c.status).where(m.intents.c.id==row['args']['after_intent_id'])).scalar_one_or_none()
                if parent!='succeeded':return False
            row=dict(row);token=uid()
            c.execute(m.intents.update().where(m.intents.c.id==row['id']).values(status='running',attempts=row['attempts']+1,claim_token=token,lease_until=stamp+timedelta(seconds=15)))
        sent=self.now_ns()
        try:
            with self.db.engine.begin() as c:
                session=self.lock_session(c,row['session_id']);state=self.store.binding(c,row['session_id'])
                scope=state['scope']|{'session_id':row['session_id'],'connector_id':state['connector_id']}
                if row['operation'] not in ('prepare','end','close','recover'):self.require_current(c,session)
                if row['operation'] in ('end','recover') and c.execute(sa.select(s.rounds.c.id).where(s.rounds.c.session_id==row['session_id'],s.rounds.c.status.in_(['PREPARING','RUNNING','PAUSED','DRAINING','FAULTED']))).first():
                    c.execute(m.intents.update().where(m.intents.c.id==row['id'],m.intents.c.claim_token==token).values(status='retry',next_attempt_at=utc()+timedelta(milliseconds=200),lease_until=None,claim_token=None))
                    return True
                args=row['args']|{'command_id':row['command_id'],'round_id':row['round_id'],'lease':state['lease']}
            if row['operation']=='prepare' and row['args'].get('reuse_connection'):
                response=state['lease']|{'_reuse_connection':True}
            else:
                if self.port is None:raise DomainError('PLATFORM_ADAPTER_UNSUPPORTED','Registered relay control adapter unavailable',503)
                response=self.port.execute(scope,row['operation'],args)
            if not isinstance(response,dict):raise DomainError('INPUT_INVALID','Invalid relay command result',422)
            if response.get('status')=='failed':raise DomainError(response.get('reason_code') or 'PLATFORM_TASK_FAILED','Platform task failed',503)
            if row['operation']=='end' and response.get('status')=='succeeded' and response.get('platform_fulfilled') is not True:raise DomainError('PLATFORM_TASK_UNCONFIRMED','Stop result was not confirmed',503)
            failure=None
        except Exception as exc:
            failure=exc.code if isinstance(exc,DomainError) else 'DEPENDENCY_UNAVAILABLE';response={}
        with self.db.engine.begin() as c:
            session=self.lock_session(c,row['session_id']);state=self.store.binding(c,row['session_id'])
            current=c.execute(sa.select(m.intents).where(m.intents.c.id==row['id']).with_for_update()).mappings().one()
            if current['claim_token']!=token or current['status']!='running':return True
            if failure:
                retry=failure in ('DEPENDENCY_UNAVAILABLE','RELAY_UNAVAILABLE','RATE_LIMITED','PLATFORM_TASK_PENDING') and current['attempts']<6
                c.execute(m.intents.update().where(m.intents.c.id==row['id']).values(status='retry' if retry else 'failed',last_error=failure,next_attempt_at=utc()+timedelta(seconds=min(30,2**current['attempts'])),claim_token=None,lease_until=None))
                if not retry:self.interrupt(c,session,failure,enqueue_stop=row['operation']!='end')
            elif row['operation']=='prepare' or (row['operation']=='recover' and row['args'].get('strategy')=='new_round'):
                if response.get('_reuse_connection'):
                    self.require_current(c,session)
                    self.callback(c,session,row,{'status':'succeeded',**state['lease']})
                    c.execute(m.intents.update().where(m.intents.c.id==row['id']).values(status='succeeded',response={k:v for k,v in response.items() if not k.startswith('_')},claim_token=None,lease_until=None))
                    self.store.observe(c,row['session_id']);self.publish(c,session,row);return True
                lease=response['lease'] if row['operation']=='recover' else response
                server=datetime.fromisoformat(lease['server_time'].replace('Z','+00:00'))
                remaining=min(int(lease['lease_ttl_ms']),int((datetime.fromisoformat(lease['expires_at'].replace('Z','+00:00'))-server).total_seconds()*1000))-3000
                if remaining<=0:raise DomainError('LEASE_EXPIRED','Lease response arrived too late',409)
                deadline=sent+remaining*1000000
                if self.now_ns()>=deadline:raise DomainError('LEASE_EXPIRED','Lease response consumed its safety budget',409)
                changes={'lease':lease|{'mono_sent_ns':sent},'state':'CONNECTING','lease_deadline_ns':deadline,'boot_id':self.boot_id,'connection':{}}
                if lease.get('stream_id') and state['stream_id']!=lease['stream_id']:
                    if state['stream_id'] and lease.get('fresh_start') is not True:raise DomainError('RELAY_GAP','A new stream requires an explicit fresh-session intent',409)
                    start=int(lease.get('start_seq','1'))
                    if start>1 and lease.get('fresh_start') is not True:raise DomainError('RELAY_GAP','Missing explicit stream bootstrap record',409)
                    if state['stream_id']:
                        c.execute(m.checkpoint_history.insert().values(id=uid(),session_id=session['id'],stream_id=state['stream_id'],persisted_seq=state['persisted_seq'],admitted_seq=state['admitted_seq'],terminal_seq=state['terminal_seq'],reason='EXPLICIT_FRESH_SESSION'))
                        c.execute(m.deliveries.update().where(m.deliveries.c.session_id==session['id'],m.deliveries.c.stream_id==state['stream_id'],m.deliveries.c.state.in_(['persisted','admitted'])).values(state='archived',reason_code='EXPLICIT_FRESH_SESSION'))
                    c.execute(m.baselines.insert().values(id=uid(),session_id=session['id'],stream_id=lease['stream_id'],start_seq=start,intent_id=row['id'],previous_stream_id=lease.get('previous_stream_id'),rpo=lease.get('rpo') or {}))
                    changes.update(stream_id=lease['stream_id'],persisted_seq=start-1,admitted_seq=start-1,terminal_seq=start-1)
                c.execute(m.bindings.update().where(m.bindings.c.id==state['id']).values(**changes))
                c.execute(m.intents.update().where(m.intents.c.id==row['id']).values(status='waiting',response=response,claim_token=None,lease_until=None))
            elif row['operation'] in ('open','pause','resume','close'):
                response=response|{'_wait_deadline_ns':self.now_ns()+5000000000,'_boot_id':self.boot_id}
                c.execute(m.intents.update().where(m.intents.c.id==row['id']).values(status='waiting',response=response,claim_token=None,lease_until=None))
                barrier=int(response.get('barrier_seq',response.get('stream_seq','-1')))
                source=c.execute(sa.select(m.deliveries).where(m.deliveries.c.session_id==row['session_id'],m.deliveries.c.stream_seq==barrier,m.deliveries.c.stream_seq<=state['admitted_seq'])).mappings().first()
                if source:self.apply_barrier(c,session,state,row|{'response':response},source['body'],barrier)
            else:
                # End only completes when the relay confirms platform stop, not merely enqueue.
                pending=response.get('status') in ('pending','retry','running')
                c.execute(m.intents.update().where(m.intents.c.id==row['id']).values(status='retry' if pending else 'succeeded',response=response,next_attempt_at=utc()+timedelta(seconds=2) if pending else None,claim_token=None,lease_until=None))
                if not pending:
                    if row['operation']=='end' or row['operation']=='recover' and row['args'].get('strategy')=='end_session':
                        c.execute(m.bindings.update().where(m.bindings.c.id==state['id']).values(state='STOPPED'))
                        getattr(self.trust,'_runtime',{}).pop(row['session_id'],None);getattr(self.trust,'_nonces',{}).pop(row['session_id'],None)
                    self.callback(c,session,row,response|{'status':'succeeded','strategy':row['args'].get('strategy')})
            self.store.observe(c,row['session_id'])
            self.publish(c,session,row)
        return True

    def connection_ready(self,c,session,connection):
        state=self.ensure_binding(c,session)
        if state['state'] in ('GAP','STOPPED','INTERRUPTED','RECOVERING'):raise DomainError('ROUND_CLOSED','Explicit recovery is required before replacing this connection',409)
        if connection.get('installation_id')!=session['installation_id'] or connection.get('lease_id')!=state['lease'].get('lease_id'):raise DomainError('INSTALLATION_MISMATCH','Relay connection binding mismatch',403)
        c.execute(m.bindings.update().where(m.bindings.c.id==state['id']).values(connection=connection,state='AUTHENTICATING',last_contact_ns=self.now_ns(),boot_id=self.boot_id))
        self.publish_transport(c,session)
        return self.trust.challenge(c,session,connection)

    def accept_proof(self,c,session,proof,*,clock_trusted):
        state=self.store.binding(c,session['id'])
        if state['state'] in ('GAP','STOPPED','INTERRUPTED','RECOVERING'):raise DomainError('ROUND_CLOSED','Late proof cannot revive an interrupted session',409)
        runtime=self.trust.accept_proof(c,session,proof,lease_deadline_ns=state['lease_deadline_ns'],clock_trusted=clock_trusted)
        c.execute(m.bindings.update().where(m.bindings.c.id==state['id']).values(state='CONNECTED',disconnected_ns=0,last_error=None,last_contact_ns=self.now_ns(),connection=state['connection']|{'_last_confirmed_at':utc().isoformat(timespec='milliseconds').replace('+00:00','Z')}))
        self.require_current(c,session)
        waiting=c.execute(sa.select(m.intents).where(m.intents.c.session_id==session['id'],m.intents.c.operation.in_(['prepare','recover']),m.intents.c.status=='waiting').order_by(m.intents.c.created_at).with_for_update()).mappings().all()
        for row in waiting:
            self.callback(c,session,row,{'status':'succeeded','strategy':row['args'].get('strategy'),'lease_mono_safe_deadline_ns':state['lease_deadline_ns'],**state['lease']})
            c.execute(m.intents.update().where(m.intents.c.id==row['id']).values(status='succeeded'))
            self.publish(c,session,row)
        self.store.observe(c,session['id'])
        self.publish_transport(c,session)
        return runtime

    def receive(self,sid,frame,connection):
        try:
            with self.db.engine.begin() as c:
                self.lock_session(c,sid);state=self.store.binding(c,sid)
                if state['connection'].get('connection_nonce')!=connection.get('connection_nonce'):return False
                self.store.persist(c,sid,frame,connection)
        except DomainError as exc:
            with self.db.engine.begin() as c:
                session=self.lock_session(c,sid);self.store.mark_gap(c,sid,exc.code)
                self.interrupt(c,session,exc.code)
            raise

    def process_one(self,sid):
        sample=None
        with self.db.engine.begin() as c:
            session=self.lock_session(c,sid);state=self.store.binding(c,sid)
            row=c.execute(sa.select(m.deliveries).where(m.deliveries.c.session_id==sid,m.deliveries.c.stream_id==state['stream_id'],m.deliveries.c.stream_seq==state['admitted_seq']+1,m.deliveries.c.stream_seq<=state['persisted_seq']).with_for_update()).mappings().first()
            if not row:return False
            body=row['body'];terminal=True;reason=None
            current_origin=body['original_generation']==str(state['lease'].get('owner_generation')) and body['run_purpose']==session['run_purpose']
            if body['type']=='stream.bootstrap':reason='EXPLICIT_STREAM_BASELINE'
            elif body['type'].startswith('window.'):
                matched=False
                for intent in c.execute(sa.select(m.intents).where(m.intents.c.session_id==sid,m.intents.c.status=='waiting',m.intents.c.operation.in_(['open','pause','resume','close'])).with_for_update()).mappings().all():
                    barrier=int(intent['response'].get('barrier_seq',intent['response'].get('stream_seq','-1')))
                    if barrier==row['stream_seq']:
                        matched=True;self.apply_barrier(c,session,state,dict(intent),body,row['stream_seq'])
                if not matched and body['type']=='window.close' and body['original_generation']==str(state['lease'].get('owner_generation')):
                    reason=body['payload'].get('reason')
                    if reason=='TIMEOUT' and body.get('round_id'):
                        pseudo={'id':'barrier:'+str(row['stream_seq']),'operation':'close','round_id':body['round_id']}
                        self.callback(c,session,pseudo,body['payload']|{'status':'succeeded','window_epoch':body['window_epoch'],'barrier_seq':str(row['stream_seq'])})
                    elif reason not in ('TIMEOUT','SOLVED','VOID'):self.interrupt(c,session,reason or 'RELAY_WINDOW_CLOSED')
            elif state['state'] not in ('GAP','INTERRUPTED','STOPPED','RECOVERING') and current_origin and not body['eligible'] and self.hooks:
                if body['provider_test'] and session['run_purpose']=='production':
                    self.hooks.observe(c,'source_message',sid,{'observation_id':'connector-source:'+body['stream_id']+':'+body['stream_seq'],'round_id':body['round_id'],'source_mode':'official' if body['source_mode']=='official' else 'bridge','provider_test':True,'received_at':body['relay_received_at'],'is_guess_attempt':False})
                    reason='PROVIDER_TEST_EXCLUDED'
                else:
                    # The core rejects eligibility before scoring. Ordinary
                    # chat and identifiable late guesses retain their facts.
                    result=self.hooks.admit_delivery(c,session,body)
                    feedback_id=result.get('feedback_id',result.get('guess_id'))
                    if feedback_id:c.execute(m.deliveries.update().where(m.deliveries.c.id==row['id']).values(feedback_id=feedback_id))
                    sample=self.commit_sample(sid,result)
            elif state['state'] in ('GAP','INTERRUPTED','STOPPED','RECOVERING') or not body['eligible'] or body['round_id'] is None:
                reason='ARCHIVED_OLD_DELIVERY'
            else:
                self.require_current(c,session)
                if body['run_purpose']!=session['run_purpose'] or body['provider_test'] and session['run_purpose']=='production' or body['original_generation']!=str(state['lease'].get('owner_generation')):reason='DELIVERY_SCOPE_MISMATCH'
                elif self.hooks is None:raise DomainError('DEPENDENCY_UNAVAILABLE','Admission hook unavailable',503)
                else:
                    result=self.hooks.admit_delivery(c,session,body);terminal=result.get('terminal',result.get('status') not in ('queued','running','waiting'))
                    feedback_id=result.get('feedback_id',result.get('guess_id'))
                    if feedback_id:c.execute(m.deliveries.update().where(m.deliveries.c.id==row['id']).values(feedback_id=feedback_id))
                    sample=self.commit_sample(sid,result)
            self.store.advance(c,sid,row['stream_seq'],terminal=terminal,reason=reason)
            if self.hooks:self.hooks.touch(c,sid)
        # Official latency is sampled after the admission actually committed, so a
        # rolled back delivery never contributes a fabricated sample.
        if sample is not None and self.hooks is not None:
            guess_id,status=sample
            self.hooks.committed(sid,guess_id,'admission')
            if status!='queued':self.hooks.committed(sid,guess_id,'feedback')
        return True

    @staticmethod
    def commit_sample(sid,result):
        """Only a persisted guess owns a sample; rejections have no guess row."""
        if not isinstance(result,dict) or not result.get('feedback_id') or not result.get('word'):return None
        if result.get('status') not in {'queued','scored','system_failed'}:return None
        return result['feedback_id'],result['status']

    def apply_barrier(self,c,session,state,intent,body,sequence):
        if body.get('type')!={'open':'window.open','pause':'window.pause','resume':'window.resume','close':'window.close'}[intent['operation']]:raise DomainError('WINDOW_EPOCH_CONFLICT','Wrong barrier type for this intent',409)
        if body.get('round_id')!=intent['round_id'] or body['original_generation']!=str(state['lease'].get('owner_generation')):raise DomainError('WINDOW_EPOCH_CONFLICT','Barrier does not belong to the current owner/round',409)
        if intent['operation'] in ('open','resume'):self.require_current(c,session)
        result={k:v for k,v in intent['response'].items() if not k.startswith('_')}|body['payload']|{'status':'succeeded','window_epoch':body['window_epoch'],'stream_id':body['stream_id'],'owner_generation':body['original_generation']}
        if intent['operation'] in ('open','resume'):
            from app.modules.auth.evidence import parse_time
            trusted_now=parse_time(state['lease']['server_time'])+timedelta(seconds=(self.now_ns()-state['lease']['mono_sent_ns'])/1000000000)
            result['remaining_ms']=max(0,int((parse_time(result['deadline_at'])-trusted_now).total_seconds()*1000))
        if intent['operation']=='pause':result.setdefault('close_seq',str(sequence-1))
        c.execute(m.bindings.update().where(m.bindings.c.id==state['id']).values(lease=state['lease']|{'window_epoch':body['window_epoch'],'window_version':result.get('version',body['payload'].get('window_version',1))}))
        self.callback(c,session,intent,result)
        c.execute(m.intents.update().where(m.intents.c.id==intent['id']).values(status='succeeded'))
        self.publish(c,session,intent)

    def interrupt(self,c,session,reason,*,enqueue_stop=True):
        state=self.store.binding(c,session['id'])
        c.execute(m.bindings.update().where(m.bindings.c.id==state['id']).values(state='INTERRUPTED',last_error=reason,authorization_deadline_ns=0))
        getattr(self.trust,'_runtime',{}).pop(session['id'],None);getattr(self.trust,'_nonces',{}).pop(session['id'],None)
        if self.hooks:
            active=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==session['id'],s.rounds.c.status.in_(['PREPARING','RUNNING','PAUSED','DRAINING','FAULTED'])).with_for_update()).mappings().first()
            self.hooks.apply_window_result(c,session,dict(active) if active else None,{'operation':'interrupt','status':'failed','error_code':reason})
        # Stop work remains durable even if the network/authorization has failed.
        existing=c.execute(sa.select(m.intents.c.id).where(m.intents.c.session_id==session['id'],m.intents.c.operation=='end',m.intents.c.status.in_(['pending','running','retry','waiting']))).first()
        if enqueue_stop and not existing:self.queue(c,session,'end',{'reason':reason})
        self.publish_transport(c,session)

    def disconnected(self,sid):
        with self.db.engine.begin() as c:
            session=self.lock_session(c,sid);state=self.store.binding(c,sid)
            if state['state'] in ('GAP','STOPPED','INTERRUPTED','RECOVERING'):return
            c.execute(m.bindings.update().where(m.bindings.c.id==state['id']).values(state='RECONNECTING',disconnected_ns=state['disconnected_ns'] or state['last_contact_ns'] or self.now_ns()))
            self.publish_transport(c,session)

    def monitor(self):
        with self.db.engine.connect() as c:sids=c.execute(sa.select(m.bindings.c.session_id).join(s.sessions,m.bindings.c.session_id==s.sessions.c.id).where(m.bindings.c.state.not_in(['CREATED','STOPPED','INTERRUPTED','GAP','RECOVERING']))).scalars().all()
        for sid in sids:
            with self.db.engine.begin() as c:
                session=self.lock_session(c,sid);state=self.store.binding(c,sid);reason=None
                if self.dependency_failed:reason='DATABASE_DEPENDENCY_LOST'
                elif state['boot_id']!=self.boot_id:reason='BOOT_CHANGED'
                elif state['disconnected_ns'] and self.now_ns()-state['disconnected_ns']>=6000000000:reason='RELAY_DISCONNECTED'
                elif self.now_ns()>=state['lease_deadline_ns']:reason='LEASE_EXPIRED'
                elif state['state']=='CONNECTED':
                    if state['last_contact_ns'] and self.now_ns()-state['last_contact_ns']>=6000000000:reason='RELAY_DISCONNECTED'
                    else:
                        try:self.require_current(c,session)
                        except DomainError as exc:reason=exc.code
                waiting=c.execute(sa.select(m.intents).where(m.intents.c.session_id==sid,m.intents.c.status.in_(['pending','waiting','running','retry']))).mappings().all()
                for intent in waiting:
                    budget=5000000000 if intent['operation'] in ('close','pause','resume') else 30000000000
                    deadline=intent['response'].get('_wait_deadline_ns',intent['args'].get('_queued_mono_ns',self.now_ns())+budget)
                    if intent['args'].get('_boot_id')!=self.boot_id or self.now_ns()>=deadline:
                        code='TRANSPORT_DRAIN_TIMEOUT' if intent['operation'] in ('close','pause','resume','open') else 'CONNECTOR_PREPARATION_TIMEOUT'
                        c.execute(m.intents.update().where(m.intents.c.id==intent['id']).values(status='failed',last_error=code,claim_token=None,lease_until=None))
                        self.publish(c,session,intent)
                        if intent['operation']!='end':reason=code
                if state['session_started_ns'] and state['boot_id']==self.boot_id:
                    elapsed=max(state['elapsed_ms'],max(0,(self.now_ns()-state['session_started_ns'])//1000000))
                    c.execute(m.bindings.update().where(m.bindings.c.id==state['id']).values(elapsed_ms=elapsed))
                    if 'authorization_elapsed_ms' in s.sessions.c:c.execute(s.sessions.update().where(s.sessions.c.id==sid).values(authorization_elapsed_ms=elapsed))
                if reason:self.interrupt(c,session,reason)
        self.dependency_failed=False
