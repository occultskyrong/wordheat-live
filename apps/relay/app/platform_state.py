"""Durable per-room SDK operation fence, separate from live input ownership."""
from datetime import timedelta
import threading
from psycopg.types.json import Jsonb
from .core import RelayError

_ACTIVE=set();_LOCK=threading.Lock()


def active(token):
    with _LOCK:return token in _ACTIVE


def active_tokens():
    with _LOCK:return list(_ACTIVE)


def claim(service,c,job,context,token,now):
    if job['kind'] not in ('platform.start','platform.stop'):return None
    row=c.execute("SELECT * FROM relay.platform_task_fences WHERE app_id=%s AND room_id=%s AND msg_type='live_comment' FOR UPDATE",(context['app_id'],context['room_id'])).fetchone()
    if row and row['claim_token'] and (active(row['claim_token']) or row['claim_until'] and row['claim_until']>now):return 'PLATFORM_OPERATION_IN_FLIGHT'
    if job['kind']=='platform.start':
        previous=c.execute("SELECT 1 FROM relay.relay_outbox WHERE app_id=%s AND room_id=%s AND kind='platform.stop' AND status NOT IN ('succeeded','cancelled') AND coalesce(payload->'context'->>'lease_id','')<>%s AND coalesce(last_error,'')<>'PLATFORM_SCOPE_SUPERSEDED' LIMIT 1",(context['app_id'],context['room_id'],context['lease_id'])).fetchone()
        if previous:return 'PREVIOUS_PLATFORM_STOP_UNCONFIRMED'
    if row and row['owner_lease_id']!=context['lease_id']:
        if job['kind']=='platform.stop':return 'PLATFORM_SCOPE_SUPERSEDED'
        if row['state']!='STOPPED':
            service.enqueue_job(c,row['owner_context'],'platform.stop',job_id='stop:'+row['owner_lease_id'])
            return 'PREVIOUS_PLATFORM_STOP_UNCONFIRMED'
    uncertain=bool(row and (row['query_required'] or row['claim_token']))
    c.execute("INSERT INTO relay.platform_task_fences(app_id,room_id,msg_type,owner_lease_id,owner_context,state,claim_token,claim_until,query_required) VALUES(%s,%s,'live_comment',%s,%s,%s,%s,%s,%s) ON CONFLICT(app_id,room_id,msg_type) DO UPDATE SET owner_lease_id=excluded.owner_lease_id,owner_context=excluded.owner_context,state=excluded.state,claim_token=excluded.claim_token,claim_until=excluded.claim_until,query_required=excluded.query_required,updated_at=now()",(context['app_id'],context['room_id'],context['lease_id'],Jsonb({k:v for k,v in context.items() if not k.startswith('_')}),'STARTING' if job['kind']=='platform.start' else 'STOPPING',token,now+timedelta(seconds=15),uncertain))
    context['_task_fence_token']=token
    context['_task_query_required']=uncertain
    return None


def validate(service,kind,context):
    token=context.get('_task_fence_token')
    if not token:return False
    with service.tx() as c:
        row=c.execute("SELECT * FROM relay.platform_task_fences WHERE app_id=%s AND room_id=%s AND msg_type='live_comment'",(context['app_id'],context['room_id'])).fetchone()
        if not row or row['claim_token']!=token or row['owner_lease_id']!=context['lease_id'] or row['claim_until']<=service.clock():return False
        if kind=='platform.start':
            owner=c.execute('SELECT * FROM relay.room_owners WHERE app_id=%s AND room_id=%s',(context['app_id'],context['room_id'])).fetchone()
            if owner['lease_id']!=context['lease_id'] or owner.get('revoked_at') or service.clock()>=min(owner['expires_at'],owner['authorization_expires_at']):return False
        return True


def begin(service,kind,context):
    if not kind.startswith('platform.'):return
    if not validate(service,kind,context):raise RelayError('PLATFORM_TASK_FENCE_LOST',409)
    with _LOCK:_ACTIVE.add(context['_task_fence_token'])


def end(context):
    with _LOCK:_ACTIVE.discard(context.get('_task_fence_token'))


def finish(service,c,job,context,token,status,now):
    if job['kind'] not in ('platform.start','platform.stop'):return
    row=c.execute("SELECT * FROM relay.platform_task_fences WHERE app_id=%s AND room_id=%s AND msg_type='live_comment' FOR UPDATE",(context['app_id'],context['room_id'])).fetchone()
    if not row or row['claim_token']!=token or row['owner_lease_id']!=context['lease_id']:return
    pending=active(token)
    state='RUNNING' if status=='succeeded' and job['kind']=='platform.start' else 'STOPPED' if status=='succeeded' else 'UNKNOWN'
    c.execute("UPDATE relay.platform_task_fences SET state=%s,claim_token=%s,claim_until=%s,query_required=%s,updated_at=%s WHERE app_id=%s AND room_id=%s AND msg_type='live_comment'",(state,token if pending else None,row['claim_until'] if pending else None,status!='succeeded',now,context['app_id'],context['room_id']))
