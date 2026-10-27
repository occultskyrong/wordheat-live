"""Durable task/receipt execution, distinct from transport and display evidence."""
import asyncio
from dataclasses import dataclass,field
from datetime import timedelta
from typing import Protocol
from queue import Queue,Empty
import threading
from uuid import uuid4
from psycopg.types.json import Jsonb
from .core import RelayError,bounded_json


@dataclass(frozen=True)
class PlatformOutcome:
    status:str
    code:str|None=None
    response:dict=field(default_factory=dict)
    retryable:bool=False


class PlatformAdapter(Protocol):
    provider_test:bool
    def perform(self,kind:str,context:dict,payload:dict)->PlatformOutcome:...


class FulfillmentWorker:
    def __init__(self,service,adapter=None,*,max_attempts=6,call_timeout=5):
        self.service=service;self.adapter=adapter;self.max_attempts=max_attempts
        self.call_timeout=call_timeout;self._slots=threading.BoundedSemaphore(2)

    def perform(self,kind,context,payload,*,batch=False):
        if not self._slots.acquire(False):return PlatformOutcome('retry','PLATFORM_WORKER_BUSY',retryable=True)
        result=Queue(maxsize=1)
        def run():
            from . import platform_state
            try:
                platform_state.begin(self.service,kind,context)
                result.put((True,self.adapter.perform_batch(kind,context,payload) if batch else self.adapter.perform(kind,context,payload)))
            except Exception:result.put((False,None))
            finally:platform_state.end(context);self._slots.release()
        threading.Thread(target=run,daemon=True,name='wordheat-platform-port').start()
        try:
            ok,value=result.get(timeout=self.call_timeout)
            return value if ok else PlatformOutcome('retry','PLATFORM_TEMPORARY_FAILURE',retryable=True)
        except Empty:return PlatformOutcome('retry','PLATFORM_TIMEOUT',retryable=True)

    def step(self):
        now=self.service.clock();token=str(uuid4())
        use_batch=False
        from . import platform_state
        active_tokens=platform_state.active_tokens()
        with self.service.tx() as c:
            row=c.execute("SELECT * FROM relay.relay_outbox WHERE (status IN ('pending','retry') OR (status='running' AND lease_until<%s)) AND (next_attempt_at IS NULL OR next_attempt_at<=%s) AND (claim_token IS NULL OR NOT(claim_token=ANY(%s))) ORDER BY created_at,job_id FOR UPDATE SKIP LOCKED LIMIT 1",(now,now,active_tokens)).fetchone()
            if not row:return {'completed':0,'unsupported':0}
            use_batch=row['kind'] in ('display.received','display.processed') and callable(getattr(self.adapter,'perform_batch',None))
            if not use_batch:c.execute("UPDATE relay.relay_outbox SET status='running',claim_token=%s,lease_until=%s,attempts=attempts+1,cycle_attempts=cycle_attempts+1 WHERE job_id=%s",(token,now+timedelta(seconds=15),row['job_id']))
        if use_batch:return self.batch_step(row['app_id'],row['room_id'],row['kind'])
        now=self.service.clock()
        with self.service.tx() as c:
            owner=c.execute('SELECT * FROM relay.room_owners WHERE app_id=%s AND room_id=%s FOR UPDATE',(row['app_id'],row['room_id'])).fetchone()
            frame=c.execute('SELECT body FROM relay.relay_frames WHERE app_id=%s AND room_id=%s AND stream_seq=%s',(row['app_id'],row['room_id'],row['stream_seq'])).fetchone() if row['stream_seq'] is not None else None
            payload=dict(row['payload'])
            context=payload.get('context') or {k:owner.get(k) for k in ('app_id','room_id','installation_id','session_id','lease_id','run_purpose','authorization_id','candidate_digest','live_profile')}
            unsafe=not payload.get('context') and row['kind']=='platform.stop' and row['job_id']!='stop:'+owner['lease_id']
            if frame and frame['body'] is not None:payload['delivery']=bounded_json(bytes(frame['body']))
            if row['stream_seq'] is not None:
                source=c.execute('SELECT source_msg_id,event_type,source_hash,raw_hash FROM relay.relay_inbox WHERE app_id=%s AND room_id=%s AND stream_seq=%s',(row['app_id'],row['room_id'],row['stream_seq'])).fetchone()
                if source:payload['source_reference']=dict(source)|{'stream_seq':str(row['stream_seq'])}
            payload['job_id']=row['job_id']
            start=c.execute('SELECT response FROM relay.relay_outbox WHERE job_id=%s',('start:'+context['lease_id'],)).fetchone()
            if start:payload['task_reference']=start['response'].get('platform_task_id')
            from . import platform_state
            fence_error=platform_state.claim(self.service,c,row,context,token,now)
        try:
            if fence_error:outcome=PlatformOutcome('failed' if fence_error=='PLATFORM_SCOPE_SUPERSEDED' else 'retry',fence_error,retryable=fence_error!='PLATFORM_SCOPE_SUPERSEDED')
            elif unsafe:outcome=PlatformOutcome('failed','STALE_PLATFORM_TASK_CONTEXT')
            elif self.adapter is None:outcome=PlatformOutcome('failed','PLATFORM_ADAPTER_UNSUPPORTED')
            elif self.adapter.provider_test and context['run_purpose']=='production':outcome=PlatformOutcome('failed','TEST_PROVIDER_FORBIDDEN')
            elif row['kind']=='platform.start' and (row['attempts']>0 or context.get('_task_query_required')):
                observation=self.perform('platform.query',context,payload|{'original_kind':'platform.start'})
                target=observation.response.get('operation_status') if isinstance(observation,PlatformOutcome) else None
                live_owner=context['lease_id']==owner['lease_id'] and not owner.get('revoked_at') and now<min(owner['expires_at'],owner['authorization_expires_at'])
                if not isinstance(observation,PlatformOutcome) or observation.status!='succeeded':outcome=observation
                elif target=='succeeded':outcome=PlatformOutcome('succeeded',response=observation.response)
                elif target=='not_found' and live_owner:outcome=self.perform(row['kind'],context,payload)
                elif target=='not_found':outcome=PlatformOutcome('failed','LEASE_EXPIRED')
                elif target=='failed':outcome=PlatformOutcome('failed','PLATFORM_CONFIRMED_FAILURE')
                else:outcome=PlatformOutcome('retry','PLATFORM_START_UNCONFIRMED',retryable=True)
            elif row['kind']=='platform.start' and (context['lease_id']!=owner['lease_id'] or owner.get('revoked_at') or now>=min(owner['expires_at'],owner['authorization_expires_at'])):outcome=PlatformOutcome('failed','LEASE_EXPIRED')
            elif row['kind']=='platform.stop' and getattr(self.adapter,'requires_task_reference',True) and not self.adapter.provider_test and not payload.get('task_reference'):
                observation=self.perform('platform.query',context,payload|{'original_kind':'platform.stop'})
                target=observation.response.get('operation_status') if isinstance(observation,PlatformOutcome) else None
                reference=observation.response.get('platform_task_id') if isinstance(observation,PlatformOutcome) else None
                if not isinstance(observation,PlatformOutcome) or observation.status!='succeeded':outcome=observation
                elif target in ('succeeded','not_found'):outcome=PlatformOutcome('succeeded',response=observation.response)
                elif isinstance(reference,str) and reference:outcome=self.perform(row['kind'],context,payload|{'task_reference':reference})
                else:outcome=PlatformOutcome('retry','PLATFORM_STOP_UNCONFIRMED',retryable=True)
            else:outcome=self.perform(row['kind'],context,payload)
            if not isinstance(outcome,PlatformOutcome) or outcome.status not in ('succeeded','failed','retry'):raise ValueError()
            if row['kind']=='platform.start' and outcome.status=='succeeded' and getattr(self.adapter,'requires_task_reference',True) and not self.adapter.provider_test and not outcome.response.get('platform_task_id'):outcome=PlatformOutcome('failed','PLATFORM_TASK_REFERENCE_REQUIRED')
            if not fence_error and row['kind'] in ('platform.start','platform.stop') and not self.service.task_scope_guard('platform.query',context):outcome=PlatformOutcome('retry','PLATFORM_TASK_FENCE_LOST',retryable=True)
        except Exception:
            outcome=PlatformOutcome('retry','PLATFORM_TEMPORARY_FAILURE',retryable=True)
        now=self.service.clock()
        with self.service.tx() as c:
            # Room lock precedes task lock for stop completion and owner fencing.
            current=c.execute('SELECT * FROM relay.room_owners WHERE app_id=%s AND room_id=%s FOR UPDATE',(row['app_id'],row['room_id'])).fetchone()
            job=c.execute('SELECT * FROM relay.relay_outbox WHERE job_id=%s FOR UPDATE',(row['job_id'],)).fetchone()
            if job['claim_token']!=token or job['status']!='running':return {'completed':0,'unsupported':0}
            retry=(outcome.status=='retry' or outcome.retryable) and job['cycle_attempts']<self.max_attempts
            status='retry' if retry else 'succeeded' if outcome.status=='succeeded' else 'failed'
            code=outcome.code or ('PLATFORM_RETRY_EXHAUSTED' if outcome.status=='retry' else None)
            # Response must be an adapter-produced public status, never raw headers/tokens.
            safe={k:outcome.response[k] for k in ('platform_task_id','status','operation_status','receipt_reference','provider_test') if k in outcome.response and isinstance(outcome.response[k],(str,bool,int,type(None)))}
            c.execute('UPDATE relay.relay_outbox SET status=%s,last_error=%s,response=%s,next_attempt_at=%s,claim_token=NULL,lease_until=NULL,completed_at=%s WHERE job_id=%s',(status,code,Jsonb(safe),now+timedelta(seconds=min(30,2**job['attempts'])) if retry else None,None if retry else now,row['job_id']))
            if not fence_error:platform_state.finish(self.service,c,row,context,token,status,now)
            if status=='succeeded' and row['stream_seq'] is not None and row['kind'] in ('display.received','display.processed'):
                c.execute('INSERT INTO relay.delivery_stages VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',(row['app_id'],row['room_id'],row['stream_seq'],'platform.fulfilled' if row['kind']=='display.processed' else 'platform.received',row['job_id'],now))
            if status=='succeeded' and row['kind']=='platform.stop' and row['job_id']=='stop:'+current['lease_id']:
                c.execute('UPDATE relay.room_owners SET expires_at=least(expires_at,%s),revoked_at=coalesce(revoked_at,%s),connection_nonce=NULL WHERE app_id=%s AND room_id=%s',(now,now,row['app_id'],row['room_id']))
            c.execute('INSERT INTO relay.operational_events VALUES(%s,%s,%s,%s,%s,%s)',(str(uuid4()),row['app_id'],row['room_id'],'platform.job',Jsonb({'job_id':row['job_id'],'kind':row['kind'],'status':status,'reason_code':code,'attempt':job['attempts']}),now))
        return {'completed':int(status=='succeeded'),'unsupported':int(code=='PLATFORM_ADAPTER_UNSUPPORTED')}

    def batch_step(self,app,room,kind):
        now=self.service.clock();token=str(uuid4());payloads=[];selected=[];size=0
        with self.service.tx() as c:
            c.execute('SELECT 1 FROM relay.room_owners WHERE app_id=%s AND room_id=%s FOR UPDATE',(app,room))
            rows=c.execute("SELECT j.*,i.source_msg_id,i.event_type FROM relay.relay_outbox j JOIN relay.relay_inbox i ON i.app_id=j.app_id AND i.room_id=j.room_id AND i.stream_seq=j.stream_seq WHERE j.app_id=%s AND j.room_id=%s AND j.kind=%s AND (j.status IN ('pending','retry') OR (j.status='running' AND j.lease_until<%s)) AND (j.next_attempt_at IS NULL OR j.next_attempt_at<=%s) AND j.cycle_attempts<%s ORDER BY j.created_at,j.job_id FOR UPDATE OF j SKIP LOCKED LIMIT 1000",(app,room,kind,now,now,self.max_attempts)).fetchall()
            for row in rows:
                payload=dict(row['payload'])|{'job_id':row['job_id'],'source_reference':{'source_msg_id':row['source_msg_id'],'event_type':row['event_type'],'stream_seq':str(row['stream_seq'])}}
                # Budget the actual normalized report fields plus conservative
                # JSON-string/envelope expansion, never the full raw comment.
                cost=2*len(str(row['source_msg_id']).encode())+2*len(str(payload.get('client_time','')).encode())+256
                if size+cost>1048576:break
                size+=cost;selected.append(row);payloads.append(payload)
            if not selected:return {'completed':0,'unsupported':0}
            ids=[row['job_id'] for row in selected]
            c.execute("UPDATE relay.relay_outbox SET status='running',claim_token=%s,lease_until=%s,attempts=attempts+1,cycle_attempts=cycle_attempts+1 WHERE job_id=ANY(%s)",(token,now+timedelta(seconds=15),ids))
        context=payloads[0].get('context',{})
        if any(item.get('context',{}).get('run_purpose')=='production' for item in payloads) and self.adapter.provider_test:outcome=PlatformOutcome('failed','TEST_PROVIDER_FORBIDDEN')
        else:outcome=self.perform(kind,context,payloads,batch=True)
        if not isinstance(outcome,PlatformOutcome) or outcome.status not in ('succeeded','retry','failed'):outcome=PlatformOutcome('retry','PLATFORM_TEMPORARY_FAILURE',retryable=True)
        finished=self.service.clock();completed=0
        with self.service.tx() as c:
            c.execute('SELECT 1 FROM relay.room_owners WHERE app_id=%s AND room_id=%s FOR UPDATE',(app,room))
            current=c.execute('SELECT * FROM relay.relay_outbox WHERE job_id=ANY(%s) ORDER BY job_id FOR UPDATE',(ids,)).fetchall()
            for job in current:
                if job['status']!='running' or job['claim_token']!=token:continue
                retry=(outcome.status=='retry' or outcome.retryable) and job['cycle_attempts']<self.max_attempts
                status='succeeded' if outcome.status=='succeeded' else 'retry' if retry else 'failed'
                safe={key:value for key,value in outcome.response.items() if key in ('status','receipt_reference','provider_test') and isinstance(value,(str,int,bool,type(None)))}
                c.execute('UPDATE relay.relay_outbox SET status=%s,last_error=%s,response=%s,next_attempt_at=%s,claim_token=NULL,lease_until=NULL,completed_at=%s WHERE job_id=%s',(status,outcome.code,Jsonb(safe),finished+timedelta(seconds=min(30,2**job['cycle_attempts'])) if retry else None,None if retry else finished,job['job_id']))
                if status=='succeeded':
                    completed+=1
                    c.execute('INSERT INTO relay.delivery_stages VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',(app,room,job['stream_seq'],'platform.fulfilled' if kind=='display.processed' else 'platform.received',job['job_id'],finished))
            c.execute('INSERT INTO relay.operational_events VALUES(%s,%s,%s,%s,%s,%s)',(str(uuid4()),app,room,'platform.batch',Jsonb({'stage':kind,'jobs':len(selected),'completed':completed,'reason_code':outcome.code}),finished))
        return {'completed':completed,'unsupported':0}


class RelayRuntime:
    def __init__(self,service,worker):self.service=service;self.worker=worker

    def sweep(self):
        with self.service.tx() as c:rooms=c.execute('SELECT app_id,room_id FROM relay.room_owners').fetchall()
        for room in rooms:
            try:
                with self.service.tx() as c:
                    o,w,st,now=self.service.lock(c,room['app_id'],room['room_id']);self.service.expire(c,o,w,st,now)
                    if st['payload_bytes']>=self.service.max_bytes*.8 or st['retained_messages']>=self.service.max_messages*.8:
                        c.execute('INSERT INTO relay.operational_events VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',('quota:'+o['app_id']+':'+o['room_id']+':'+now.date().isoformat(),o['app_id'],o['room_id'],'relay.quota',Jsonb({'payload_bytes':st['payload_bytes'],'retained_messages':st['retained_messages']}),now))
            except RelayError:continue

    async def run(self,stop):
        async def sweep_loop():
            while not stop.is_set():
                try:await asyncio.to_thread(self.sweep)
                except RelayError:pass
                await asyncio.sleep(.2)
        async def fulfillment_loop():
            while not stop.is_set():
                try:await asyncio.to_thread(self.worker.step)
                except RelayError:pass
                await asyncio.sleep(.1)
        async def retention_loop():
            ticks=0
            while not stop.is_set():
                await asyncio.sleep(.2);ticks+=1
                if ticks%300:continue
                try:
                    with self.service.tx() as c:rooms=c.execute('SELECT app_id,room_id FROM relay.room_owners').fetchall()
                    for room in rooms:await asyncio.to_thread(self.service.prune_payloads,room['app_id'],room['room_id'],before=self.service.clock())
                except RelayError:pass
        tasks=[asyncio.create_task(sweep_loop()),asyncio.create_task(fulfillment_loop()),asyncio.create_task(retention_loop())]
        try:await asyncio.gather(*tasks)
        finally:
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
