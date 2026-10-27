"""Fixed native control routes. Neither imports keys nor accepts target URLs."""
import asyncio
import hashlib
import json
from psycopg.types.json import Jsonb
from fastapi import Request
from .core import RelayError,bounded_json,canonical


def install_control_routes(app,service,plane,*,trusted,bound,platform_adapter=None):
    async def body(request):
        if request.headers.get('x-be-extension-version')!='LW-BE/1.1.0':raise RelayError('BE_EXTENSION_VERSION_UNSUPPORTED',409)
        data=await request.body()
        return bounded_json(data,max_bytes=65536)
    def response(value):return value|{'be_extension_version':'LW-BE/1.1.0'}

    @app.post('/relay/v1/device-pairings')
    async def pairing(request:Request):
        authorization=request.headers.get('authorization','')
        if not authorization.startswith('Bearer '):raise RelayError('AUTH_REQUIRED',401)
        data=await body(request)
        return response(await asyncio.to_thread(plane.pair,authorization[7:],data))

    @app.post('/relay/v1/freshness')
    async def freshness(request:Request):
        grant=trusted(request.headers.get('authorization',''));data=await body(request)
        return response({'proof':plane.freshness(grant,data)})

    @app.post('/relay/v1/device-tokens/{operation}')
    async def device_token(operation:str,request:Request):
        authorization=request.headers.get('authorization','')
        if authorization and (not authorization.startswith('Bearer ') or len(authorization)>512):raise RelayError('AUTH_REQUIRED',401)
        data=await body(request);token=authorization[7:] if authorization else None
        if operation=='challenges':return response(plane.token_challenge(token,data.get('pairing_id')))
        if operation=='rotate':return response(plane.rotate_token(token,data.get('challenge_id'),data.get('signature')))
        raise RelayError('RESOURCE_NOT_FOUND',404)

    @app.post('/relay/v1/revocations')
    async def revocations(request:Request):
        trusted(request.headers.get('authorization',''))
        await body(request)
        with service.tx() as c:
            rows=c.execute("SELECT envelope FROM relay.trusted_objects WHERE type='revocation' ORDER BY issuer_id,issuer_revision LIMIT 1001").fetchall()
            if len(rows)>1000:raise RelayError('REVOCATION_STATE_INCOMPLETE',503)
            return response({'items':[row['envelope'] for row in rows]})

    @app.post('/relay/v1/streams/bootstrap')
    async def bootstrap(request:Request):
        grant=trusted(request.headers.get('authorization',''));data=await body(request);bound(grant,data['lease_id'])
        key=request.headers.get('idempotency-key')
        if not key:raise RelayError('IDEMPOTENCY_KEY_REQUIRED',422)
        return response(service.bootstrap_stream(grant.app_id,grant.room_id,data['lease_id'],expected_stream_id=data['expected_stream_id'],command_id=key,reason=data['reason']))

    @app.post('/relay/v1/delivery-stages')
    async def historical_stage(request:Request):
        authorization=request.headers.get('authorization','')
        if not authorization.startswith('Bearer ') or len(authorization)>16384:raise RelayError('AUTH_REQUIRED',401)
        data=await body(request)
        from .historical import accept
        return response(accept(plane,authorization[7:],data,intent_id=request.headers.get('idempotency-key')))

    def state(grant,lease):
        bound(grant,lease)
        with service.tx() as c:
            o,w,stream,now=service.lock(c,grant.app_id,grant.room_id);service.expire(c,o,w,stream,now)
            pending=c.execute("SELECT count(*) AS n FROM relay.relay_outbox WHERE app_id=%s AND room_id=%s AND status NOT IN ('succeeded','cancelled')",(grant.app_id,grant.room_id)).fetchone()['n']
            return {'lease':service.lease_view(o,c),'stream_id':stream['stream_id'],'latest_seq':str(stream['last_seq']),'retained_from':str(stream['retained_from']),'window':{'round_id':w['round_id'],'epoch':str(w['epoch']),'version':w['version'],'state':w['state'],'close_seq':str(w['close_seq']) if w['close_seq'] is not None else None,'barrier_seq':str(w['barrier_seq']) if w['barrier_seq'] is not None else None} if w else None,'remote_fulfillment_pending':pending if platform_adapter is not None else None}

    @app.post('/relay/v1/state')
    async def query_state(request:Request):
        grant=trusted(request.headers.get('authorization',''));data=await body(request)
        value=state(grant,data['lease_id'])
        if 'command_id' in data:
            if data.get('operation') not in ('open','pause','resume','close') or not isinstance(data['command_id'],str) or not 0<len(data['command_id'])<=128:raise RelayError('INPUT_INVALID',422)
            with service.tx() as c:
                row=c.execute('SELECT response FROM relay.control_results WHERE app_id=%s AND room_id=%s AND installation_id=%s AND session_id=%s AND operation=%s AND command_id=%s',(grant.app_id,grant.room_id,grant.installation_id,grant.session_id,data['operation'],data['command_id'])).fetchone()
                value['command_result']=row['response'] if row else None
        return response(value)

    @app.post('/relay/v1/tasks/{operation}')
    async def tasks(operation:str,request:Request):
        if operation not in ('start','stop','query'):raise RelayError('RESOURCE_NOT_FOUND',404)
        grant=trusted(request.headers.get('authorization',''));data=await body(request);lease=data.get('lease_id');bound(grant,lease)
        if operation=='stop':service.revoke(grant.app_id,grant.room_id,lease)
        job_id=('stop:' if operation=='stop' else 'start:')+lease
        with service.tx() as c:
            if operation=='start':
                o,w,stream,now=service.lock(c,grant.app_id,grant.room_id);service.expire(c,o,w,stream,now);service.authorize(o,lease,now)
                service.start_session_budget(c,o,now)
                service.enqueue_job(c,o,'platform.start',job_id=job_id,payload={'lease_id':lease})
            row=c.execute('SELECT * FROM relay.relay_outbox WHERE job_id=%s AND app_id=%s AND room_id=%s',(job_id,grant.app_id,grant.room_id)).fetchone()
            if not row:raise RelayError('RESOURCE_NOT_FOUND',404)
            retry=data.get('retry_failed',False)
            if type(retry) is not bool:raise RelayError('INPUT_INVALID',422)
            row=service.retry_job(c,job_id,request.headers.get('idempotency-key'),retry)
            return response({'job_id':job_id,'status':row['status'],'attempts':row['attempts'],'cycle_attempts':row['cycle_attempts'],'retry_cycle':row['retry_cycle'],'reason_code':row['last_error'],'platform_fulfilled':row['status']=='succeeded','provider_test':platform_adapter.provider_test if platform_adapter is not None else None})


def idempotent(service,grant,operation,command_id,request,perform):
    if not command_id:return perform()
    if not isinstance(command_id,str) or not 0<len(command_id)<=128:raise RelayError('INPUT_INVALID',422)
    digest=hashlib.sha256(canonical(request)).hexdigest()
    key=(grant.app_id,grant.room_id,grant.installation_id,grant.session_id,operation,command_id)
    # Only the idempotency advisory lock is held while the kernel uses its own
    # short room transaction. Kernel retries are themselves idempotent.
    with service.tx() as c:
        c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(json.dumps(key),))
        old=c.execute('SELECT * FROM relay.control_results WHERE app_id=%s AND room_id=%s AND installation_id=%s AND session_id=%s AND operation=%s AND command_id=%s',key).fetchone()
        if old:
            if old['request_hash']!=digest:raise RelayError('IDEMPOTENCY_KEY_REUSED')
            return old['response']
        result=perform()
        c.execute('INSERT INTO relay.control_results(app_id,room_id,installation_id,session_id,operation,command_id,request_hash,response) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',(*key,digest,Jsonb(result)))
        return result
