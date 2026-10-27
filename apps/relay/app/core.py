"""Durable relay protocol kernel. No official provider implementation is assumed."""
from __future__ import annotations
import asyncio
import base64
import hashlib
import hmac
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol, Mapping
from uuid import uuid4
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row
from psycopg import sql
from psycopg.types.json import Jsonb
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

CONTRACT = 'LW-C1/1.0.0'

class RelayError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def dsn(value):
    try:
        value=value.strip().strip('"\'').replace('postgresql+psycopg://','postgresql://')
        parsed=urlsplit(value)
        if '?' in value or '#' in value or parsed.scheme!='postgresql' or not parsed.hostname or not parsed.path.strip('/'):
            raise ValueError()
    except (ValueError,TypeError,AttributeError): raise RelayError('DATABASE_URL_INVALID',503) from None
    return value


@contextmanager
def safe_connect(value, **kwargs):
    """Normalize first; suppress raw driver exceptions at every connection boundary."""
    target=dsn(value)
    try:
        with psycopg.connect(target,**kwargs) as connection:
            yield connection
    except (psycopg.Error,OSError,ValueError,TypeError):
        raise RelayError('DATABASE_UNAVAILABLE',503) from None


def validate_database(c, expected_database, expected_marker, migration=False):
    row=c.execute("SELECT current_database() AS db,current_user AS role,shobj_description(oid,'pg_database') AS marker FROM pg_database WHERE datname=current_database()").fetchone()
    if row['db']!=expected_database or row['marker']!=expected_marker:
        raise RelayError('DATABASE_IDENTITY_MISMATCH',503)
    privileged=c.execute("SELECT rolsuper OR rolcreatedb OR rolcreaterole AS privileged FROM pg_roles WHERE rolname=current_user").fetchone()['privileged']
    if privileged: raise RelayError('DATABASE_ROLE_UNSAFE',503)
    owner=c.execute("SELECT pg_has_role(current_user,datdba,'USAGE') AS owner FROM pg_database WHERE datname=current_database()").fetchone()['owner']
    if not migration and owner: raise RelayError('RUNTIME_ROLE_IS_OWNER',503)


def migrate(url, *, expected_database='wordheat_relay_test', expected_marker='wordheat-live:test:relay', runtime_role='wordheat_relay_test_app'):
    target=dsn(url)
    if urlsplit(target).path!='/'+expected_database: raise RelayError('DATABASE_IDENTITY_MISMATCH',503)
    with safe_connect(target, row_factory=dict_row) as c:
        validate_database(c,expected_database,expected_marker,True)
        c.execute('SELECT pg_advisory_xact_lock(786542391)')
        for migration in sorted((Path(__file__).parents[1]/'migrations').glob('*.sql')):
            c.execute(migration.read_text())
        c.execute(sql.SQL('GRANT USAGE ON SCHEMA relay TO {}').format(sql.Identifier(runtime_role)))
        c.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA relay TO {}').format(sql.Identifier(runtime_role)))
        c.execute(sql.SQL('REVOKE DELETE ON ALL TABLES IN SCHEMA relay FROM {}').format(sql.Identifier(runtime_role)))
        c.execute(sql.SQL('REVOKE INSERT,UPDATE ON relay.schema_versions FROM {}').format(sql.Identifier(runtime_role)))


@dataclass(frozen=True)
class SourceMessage:
    source_msg_id: str
    type: str
    payload: dict
    participant_key: str | None = None
    display_name: str | None = None
    provider_test: bool | None = None
    source_fingerprint: str | None = None
    source_metadata: dict | None = None


@dataclass(frozen=True)
class VerifiedGrant:
    """Only a trusted GrantVerifier may create this at a network boundary."""
    app_id: str
    room_id: str
    installation_id: str
    session_id: str
    run_purpose: str
    expires_at: datetime
    authorization_id: str | None = None
    candidate_digest: str | None = None
    live_profile: str | None = None
    grant_digest: str | None = None
    room_binding_id: str | None = None
    revocation_revision: int = 0
    device_binding_id: str | None = None
    trusted: bool = False
    provider_test: bool = False
    max_session_duration_ms: int = 0


class GrantVerifier(Protocol):
    """Must verify signed scope, revocation and registered-device/platform-room binding."""
    def verify(self, credential: str) -> VerifiedGrant: ...


class ProviderAdapter(Protocol):
    provider_test: bool
    def verify_and_decode(self, raw: bytes, headers: Mapping[str,str]) -> tuple[str,str,list[SourceMessage]]: ...
    def success_response(self, sequences: list[str]) -> dict: ...


class UnavailableProvider:
    """Control/pairing can run while the unconfigured official ingress rejects all input."""
    provider_test=False
    available=False
    def verify_and_decode(self,raw,headers):raise RelayError('PLATFORM_ADAPTER_UNSUPPORTED',503)
    def success_response(self,sequences):raise RelayError('PLATFORM_ADAPTER_UNSUPPORTED',503)


class UnavailableGrantVerifier:
    def verify(self,credential):raise RelayError('TRUSTED_CONTROL_PLANE_REQUIRED',403)


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def opaque_id(value,max_bytes=256):
    try: valid=isinstance(value,str) and 0<len(value.encode('utf-8'))<=max_bytes
    except UnicodeError: valid=False
    if not valid: raise RelayError('INPUT_INVALID',422)
    return value


def bounded_json(raw,*,max_bytes=65536,max_nodes=4096):
    try:
        encoded=raw.encode('utf-8') if isinstance(raw,str) else raw
        if not isinstance(encoded,bytes): raise ValueError()
        if len(encoded)>max_bytes: raise RelayError('PAYLOAD_TOO_LARGE',413)
        def pairs(items):
            obj={}
            for key,value in items:
                if key in obj:raise RelayError('INPUT_INVALID',422)
                obj[key]=value
            return obj
        value=json.loads(encoded,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        pending=[(value,0)];nodes=0
        while pending:
            current,depth=pending.pop();nodes+=1
            if depth>16 or nodes>max_nodes: raise RelayError('INPUT_INVALID',422)
            if isinstance(current,dict):
                if len(current)>1000: raise RelayError('INPUT_INVALID',422)
                pending.extend((v,depth+1) for v in current.values())
            elif isinstance(current,list):
                if len(current)>1000: raise RelayError('INPUT_INVALID',422)
                pending.extend((v,depth+1) for v in current)
        if not isinstance(value,dict): raise ValueError()
        return value
    except (ValueError,TypeError,UnicodeError,RecursionError):
        raise RelayError('INPUT_INVALID',422) from None


async def receive_ws_json(ws):
    message=await ws.receive()
    if message['type']=='websocket.disconnect': raise WebSocketDisconnect(message.get('code',1000))
    raw=message.get('text') if message.get('text') is not None else message.get('bytes')
    return bounded_json(raw)


def decimal_cursor(value):
    if not isinstance(value,str) or not re.fullmatch(r'0|[1-9][0-9]{0,18}',value) or int(value)>9223372036854775807:raise RelayError('INPUT_INVALID',422)
    return int(value)


class SyntheticTestProvider:
    """Explicit component fixture, not a Douyin signature or production adapter."""
    provider_test=True
    def __init__(self,key:bytes):
        if len(key)<16: raise RelayError('SYNTHETIC_KEY_REQUIRED',503)
        self.key=key
    def sign(self,raw): return hmac.new(self.key,raw,hashlib.sha256).hexdigest()
    def verify_and_decode(self,raw,headers):
        if not hmac.compare_digest(self.sign(raw),headers.get('x-synthetic-signature','')):
            raise RelayError('SIGNATURE_INVALID',403)
        try:
            body=bounded_json(raw,max_bytes=1048576,max_nodes=20000)
            messages=[SourceMessage(x['source_msg_id'],x['type'],x['payload']) for x in body['messages']]
            return body['app_id'],body['room_id'],messages
        except (ValueError,KeyError,TypeError,AttributeError): raise RelayError('INPUT_INVALID',422) from None
    def success_response(self,sequences): return {'accepted':True,'provider_test':True,'stream_seqs':sequences}


class RelayService:
    def __init__(self,url,*,clock=None,expected_database='wordheat_relay_test',expected_marker='wordheat-live:test:relay',max_messages=1_000_000,max_bytes=1_073_741_824,schema='relay'):
        self.url=dsn(url)
        if urlsplit(self.url).path!='/'+expected_database: raise RelayError('DATABASE_IDENTITY_MISMATCH',503)
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.expected_database=expected_database;self.expected_marker=expected_marker
        self.max_messages=max_messages;self.max_bytes=max_bytes
        if schema!='relay' and not re.fullmatch('wordheat_relay_pytest_[0-9a-f]{32}',schema):raise RelayError('DATABASE_NAMESPACE_INVALID',503)
        self.schema=schema
        self.owner_guard=None
        self.fulfillment=None
        self.official_ingress_ready=False
        self.platform_ready=False
        self.platform_adapter_configured=False
        with self.tx() as c:
            if c.execute('SELECT max(version) AS version FROM relay.schema_versions').fetchone()['version']!=2:
                raise RelayError('SCHEMA_UNSUPPORTED',503)
            if c.execute("SELECT has_database_privilege(current_user,current_database(),'CREATE') OR EXISTS(SELECT 1 FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname<>'information_schema' AND has_schema_privilege(current_user,oid,'CREATE')) AS allowed").fetchone()['allowed']:
                raise RelayError('RUNTIME_ROLE_CAN_DDL',503)

    @contextmanager
    def tx(self):
        try:
            with safe_connect(self.url,row_factory=dict_row) as c:
                validate_database(c,self.expected_database,self.expected_marker)
                try:
                    if self.schema=='relay':yield c
                    else:
                        class NamespaceConnection:
                            def execute(inner,query,params=None):
                                if not isinstance(query,str):raise RelayError('DATABASE_NAMESPACE_INVALID',503)
                                return c.execute(query.replace('relay.',self.schema+'.'),params)
                            def commit(inner):return c.commit()
                        yield NamespaceConnection()
                except RelayError as exc:
                    if exc.code in ('CLOCK_UNCERTAIN','LEASE_EXPIRED','LEASE_REVOKED'):
                        c.commit()  # Preserve only conservative rejection barriers.
                    raise
        except psycopg.Error:
            raise RelayError('DATABASE_UNAVAILABLE',503) from None

    def lock(self,c,app,room):
        owner=c.execute('SELECT * FROM relay.room_owners WHERE app_id=%s AND room_id=%s FOR UPDATE',(app,room)).fetchone()
        if not owner: raise RelayError('ROOM_UNREGISTERED',403)
        window=c.execute('SELECT * FROM relay.relay_windows WHERE app_id=%s AND room_id=%s AND epoch=%s FOR UPDATE',(app,room,owner['window_epoch'])).fetchone()
        stream=c.execute('SELECT * FROM relay.relay_streams WHERE app_id=%s AND room_id=%s FOR UPDATE',(app,room)).fetchone()
        now=self.clock()
        if now.tzinfo is None: raise RelayError('CLOCK_UNCERTAIN')
        if now < owner['last_trusted_at']-timedelta(milliseconds=500):
            if window and window['state']!='CLOSED':
                self._close(c,owner,window,stream,owner['last_trusted_at'],'CLOCK_UNCERTAIN')
            self.enqueue_job(c,owner,'platform.stop',job_id='stop:'+owner['lease_id'])
            raise RelayError('CLOCK_UNCERTAIN')
        c.execute('UPDATE relay.room_owners SET last_trusted_at=%s WHERE app_id=%s AND room_id=%s',(max(now,owner['last_trusted_at']),app,room))
        return owner,window,stream,max(now,owner['last_trusted_at'])

    def authorize(self,o,lease,now):
        if o.get('revoked_at'): raise RelayError('LEASE_REVOKED')
        if o['lease_id']!=lease: raise RelayError('LEASE_STALE')
        if now>=min(o['expires_at'],o['authorization_expires_at']): raise RelayError('LEASE_EXPIRED')

    def enqueue_job(self,c,owner,kind,*,job_id=None,stream_seq=None,payload=None):
        context={key:owner.get(key) for key in ('app_id','room_id','installation_id','session_id','lease_id','owner_generation','run_purpose','authorization_id','candidate_digest','live_profile')}
        job=job_id or str(uuid4())
        c.execute('INSERT INTO relay.relay_outbox(job_id,app_id,room_id,stream_seq,kind,payload) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',(job,owner['app_id'],owner['room_id'],stream_seq,kind,Jsonb(dict(payload or {})|{'context':context})))
        return job

    def retry_job(self,c,job_id,intent_id,requested):
        row=c.execute('SELECT * FROM relay.relay_outbox WHERE job_id=%s FOR UPDATE',(job_id,)).fetchone()
        if intent_id:
            opaque_id(intent_id,128)
            if row['status']=='failed' and requested and row['retry_intent_id']!=intent_id:
                c.execute("UPDATE relay.relay_outbox SET status='pending',cycle_attempts=0,retry_cycle=retry_cycle+1,retry_intent_id=%s,next_attempt_at=NULL,last_error=NULL,claim_token=NULL,lease_until=NULL,completed_at=NULL WHERE job_id=%s",(intent_id,job_id))
            elif row['retry_intent_id'] is None and row['status']!='failed':c.execute('UPDATE relay.relay_outbox SET retry_intent_id=%s WHERE job_id=%s',(intent_id,job_id))
        return c.execute('SELECT * FROM relay.relay_outbox WHERE job_id=%s',(job_id,)).fetchone()

    def start_session_budget(self,c,owner,now):
        if not owner.get('authorization_id'):return owner['authorization_expires_at']
        if type(owner.get('max_session_duration_ms')) is not int or owner['max_session_duration_ms']<=0:raise RelayError('LIVE_AUTHORIZATION_INVALID',403)
        key=(owner['app_id'],owner['room_id'],owner['installation_id'],owner['session_id'],owner['authorization_id'])
        row=c.execute('SELECT * FROM relay.authorization_sessions WHERE app_id=%s AND room_id=%s AND installation_id=%s AND session_id=%s AND authorization_id=%s FOR UPDATE',key).fetchone()
        if not row:
            end=min(owner['authorization_expires_at'],now+timedelta(milliseconds=owner['max_session_duration_ms']))
            c.execute('INSERT INTO relay.authorization_sessions VALUES(%s,%s,%s,%s,%s,%s,%s)',(*key,now,end))
        else:end=row['deadline_at']
        if now>=end:raise RelayError('LIVE_AUTHORIZATION_EXPIRED',403)
        c.execute('UPDATE relay.room_owners SET authorization_expires_at=least(authorization_expires_at,%s) WHERE app_id=%s AND room_id=%s',(end,owner['app_id'],owner['room_id']))
        owner['authorization_expires_at']=min(owner['authorization_expires_at'],end)
        return end

    def acquire(self,g:VerifiedGrant):
        now=self.clock()
        if isinstance(g,VerifiedGrant):
            for value in (g.app_id,g.room_id,g.installation_id,g.session_id): opaque_id(value)
        if not isinstance(g,VerifiedGrant) or g.run_purpose not in ('production','test_live') or g.expires_at<=now:
            raise RelayError('LIVE_AUTHORIZATION_INVALID',403)
        with self.tx() as c:
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(json.dumps([g.app_id,g.room_id]),))
            existing=c.execute('SELECT * FROM relay.room_owners WHERE app_id=%s AND room_id=%s FOR UPDATE',(g.app_id,g.room_id)).fetchone()
            budget=c.execute('SELECT deadline_at FROM relay.authorization_sessions WHERE app_id=%s AND room_id=%s AND installation_id=%s AND session_id=%s AND authorization_id=%s',(g.app_id,g.room_id,g.installation_id,g.session_id,g.authorization_id)).fetchone() if g.trusted else None
            if budget and budget['deadline_at']<=now:raise RelayError('LIVE_AUTHORIZATION_EXPIRED',403)
            if existing and existing['expires_at']>now:
                if existing['installation_id']!=g.installation_id or existing['session_id']!=g.session_id:
                    raise RelayError('OWNER_CONFLICT')
                if existing.get('revoked_at'): raise RelayError('LEASE_REVOKED')
                if existing['run_purpose']!=g.run_purpose or (g.trusted and any(existing.get(k)!=getattr(g,k) for k in ('authorization_id','candidate_digest','live_profile','grant_digest','room_binding_id'))):raise RelayError('LIVE_AUTHORIZATION_INVALID',403)
                return self.lease_view(existing,c)
            generation=1 if not existing else existing['owner_generation']+1
            if existing:
                o,w,st,t=self.lock(c,g.app_id,g.room_id)
                if w and w['state']!='CLOSED': self._close(c,o,w,st,t,'LEASE_EXPIRED')
            authorization_end=min(g.expires_at,budget['deadline_at']) if budget else g.expires_at
            lease=str(uuid4());expires=min(now+timedelta(seconds=15),authorization_end)
            c.execute('''INSERT INTO relay.room_owners(app_id,room_id,installation_id,session_id,lease_id,owner_generation,expires_at,authorization_expires_at,run_purpose,last_trusted_at)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(app_id,room_id) DO UPDATE SET installation_id=excluded.installation_id,session_id=excluded.session_id,lease_id=excluded.lease_id,owner_generation=excluded.owner_generation,expires_at=excluded.expires_at,authorization_expires_at=excluded.authorization_expires_at,run_purpose=excluded.run_purpose,connection_nonce=NULL,revoked_at=NULL,last_trusted_at=excluded.last_trusted_at''',
              (g.app_id,g.room_id,g.installation_id,g.session_id,lease,generation,expires,authorization_end,g.run_purpose,now))
            c.execute('INSERT INTO relay.relay_streams(app_id,room_id,stream_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(g.app_id,g.room_id,str(uuid4())))
            c.execute("INSERT INTO relay.stream_segments(stream_id,app_id,room_id,start_seq,installation_id,session_id,owner_generation,reason) SELECT stream_id,app_id,room_id,1,%s,%s,%s,'INITIAL_CONTINUOUS_STREAM' FROM relay.relay_streams WHERE app_id=%s AND room_id=%s ON CONFLICT DO NOTHING",(g.installation_id,g.session_id,generation,g.app_id,g.room_id))
            c.execute('INSERT INTO relay.relay_leases(lease_id,app_id,room_id,owner_generation,installation_id,expires_at) VALUES(%s,%s,%s,%s,%s,%s)',(lease,g.app_id,g.room_id,generation,g.installation_id,expires))
            if g.trusted:
                c.execute('UPDATE relay.room_owners SET authorization_id=%s,candidate_digest=%s,live_profile=%s,grant_digest=%s,room_binding_id=%s,revocation_revision=%s,max_session_duration_ms=%s,device_binding_id=%s WHERE app_id=%s AND room_id=%s',(g.authorization_id,g.candidate_digest,g.live_profile,g.grant_digest,g.room_binding_id,g.revocation_revision,g.max_session_duration_ms,g.device_binding_id,g.app_id,g.room_id))
            return self.lease_view(c.execute('SELECT * FROM relay.room_owners WHERE app_id=%s AND room_id=%s',(g.app_id,g.room_id)).fetchone(),c)

    def lease_view(self,o,c=None):
        if c is None:
            with self.tx() as own:return self.lease_view(o,own)
        now=self.clock()
        stream=c.execute('SELECT st.stream_id,sg.start_seq,sg.bootstrap_seq,sg.previous_stream_id,sg.rpo FROM relay.relay_streams st JOIN relay.stream_segments sg USING(stream_id) WHERE st.app_id=%s AND st.room_id=%s',(o['app_id'],o['room_id'])).fetchone()
        fence=c.execute("SELECT owner_lease_id,state FROM relay.platform_task_fences WHERE app_id=%s AND room_id=%s AND msg_type='live_comment'",(o['app_id'],o['room_id'])).fetchone()
        platform_ready=self.platform_ready and not (fence and fence['owner_lease_id']!=o['lease_id'] and fence['state']!='STOPPED')
        return {'lease_id':o['lease_id'],'owner_generation':str(o['owner_generation']),'expires_at':o['expires_at'].isoformat(),'server_time':now.isoformat(),'lease_ttl_ms':max(0,min(15000,int((min(o['expires_at'],o['authorization_expires_at'])-now).total_seconds()*1000))),'official_ingress_ready':self.official_ingress_ready,'platform_ready':platform_ready,**({'stream_id':stream['stream_id'],'start_seq':str(stream['start_seq']),'bootstrap_seq':str(stream['bootstrap_seq']) if stream['bootstrap_seq'] else None,'previous_stream_id':stream['previous_stream_id'],'rpo':stream['rpo']} if stream else {})}

    def bootstrap_stream(self,app,room,lease,**kwargs):
        from .segments import bootstrap
        return bootstrap(self,app,room,lease,**kwargs)

    def task_scope_guard(self,kind,context):
        from .platform_state import validate
        return validate(self,kind,context)

    def append(self,c,o,w,st,now,kind,payload,eligible=False,provider_test=False,source_mode=None):
        seq=st['last_seq']+1
        body=canonical({'schema_version':1,'contract_version':CONTRACT,'stream_id':st['stream_id'],'stream_seq':str(seq),'original_generation':str(o['owner_generation']),'round_id':w['round_id'] if w else None,'window_epoch':str(w['epoch']) if w else '0','relay_received_at':now.isoformat(timespec='milliseconds').replace('+00:00','Z'),'type':kind,'eligible':eligible,'provider_test':provider_test,'run_purpose':o['run_purpose'],'source_mode':source_mode or ('synthetic' if provider_test else 'official'),'payload':payload})
        # Include base64/envelope expansion in the 64KiB transport budget.
        if len(body)>47000: raise RelayError('PAYLOAD_TOO_LARGE',413)
        if kind not in ('window.close','window.pause') and (st['retained_messages']+1>self.max_messages or st['payload_bytes']+len(body)>self.max_bytes): raise RelayError('RELAY_QUOTA_EXCEEDED',429)
        c.execute('INSERT INTO relay.relay_frames(app_id,room_id,stream_seq,body,immutable_sha256,received_at,original_generation,window_epoch,eligible,stream_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(o['app_id'],o['room_id'],seq,body,hashlib.sha256(body).hexdigest(),now,o['owner_generation'],w['epoch'] if w else 0,eligible,st['stream_id']))
        context={key:o.get(key) for key in ('app_id','room_id','installation_id','session_id','lease_id','owner_generation','run_purpose','authorization_id','candidate_digest','live_profile','grant_digest','room_binding_id','device_binding_id')}
        context.update(provider_test=provider_test,source_mode=source_mode or ('synthetic' if provider_test else 'official'))
        c.execute('INSERT INTO relay.delivery_contexts VALUES(%s,%s,%s,%s,%s,%s,%s)',(o['app_id'],o['room_id'],seq,st['stream_id'],Jsonb(context),kind,eligible))
        c.execute('UPDATE relay.relay_streams SET last_seq=%s,payload_bytes=payload_bytes+%s,retained_messages=retained_messages+1 WHERE app_id=%s AND room_id=%s',(seq,len(body),o['app_id'],o['room_id']))
        st['last_seq']=seq;st['payload_bytes']+=len(body);st['retained_messages']+=1
        return seq

    def _close(self,c,o,w,st,now,reason):
        boundary=st['last_seq'];w.update(state='CLOSED',reason=reason,close_seq=boundary,version=w['version']+1)
        barrier=self.append(c,o,w,st,now,'window.close',{'close_seq':str(boundary),'reason':reason,'window_version':w['version']})
        w['barrier_seq']=barrier
        c.execute("UPDATE relay.relay_windows SET state='CLOSED',reason=%s,close_seq=%s,barrier_seq=%s,version=%s WHERE app_id=%s AND room_id=%s AND epoch=%s",(reason,boundary,barrier,w['version'],o['app_id'],o['room_id'],w['epoch']))

    def expire(self,c,o,w,st,now):
        failure=self.owner_guard(c,o,now) if self.owner_guard else None
        if failure and not o.get('revoked_at'):
            o['revoked_at']=now
            c.execute('UPDATE relay.room_owners SET revoked_at=%s WHERE app_id=%s AND room_id=%s',(now,o['app_id'],o['room_id']))
        stop_reason=failure or ('LEASE_REVOKED' if o.get('revoked_at') else 'LEASE_EXPIRED' if now>=min(o['expires_at'],o['authorization_expires_at']) else None)
        if w and w['state']!='CLOSED':
            if stop_reason:self._close(c,o,w,st,now,stop_reason)
            elif w['state']=='OPEN' and now>=w['deadline']: self._close(c,o,w,st,now,'TIMEOUT')
        if stop_reason:
            self.enqueue_job(c,o,'platform.stop',job_id='stop:'+o['lease_id'])

    def open_window(self,app,room,lease,round_id,deadline):
        with self.tx() as c:
            o,w,st,now=self.lock(c,app,room);self.expire(c,o,w,st,now);self.authorize(o,lease,now)
            budget_end=self.start_session_budget(c,o,now)
            deadline=min(deadline,budget_end)
            if w and w['state']!='CLOSED':
                if w['round_id']==round_id:
                    return {'epoch':str(w['epoch']),'version':w['version'],'stream_seq':str(w.get('open_seq') or 0),'deadline_at':w['deadline'].isoformat(),'remaining_ms':max(0,int((w['deadline']-now).total_seconds()*1000))}
                raise RelayError('WINDOW_ALREADY_OPEN')
            opaque_id(round_id)
            if deadline<=now or deadline>now+timedelta(seconds=600) or deadline>o['authorization_expires_at']: raise RelayError('DEADLINE_INVALID',422)
            epoch=o['window_epoch']+1
            c.execute('UPDATE relay.room_owners SET window_epoch=%s WHERE app_id=%s AND room_id=%s',(epoch,app,room))
            c.execute("INSERT INTO relay.relay_windows(app_id,room_id,epoch,round_id,original_generation,version,state,opened_at,deadline) VALUES(%s,%s,%s,%s,%s,1,'OPEN',%s,%s)",(app,room,epoch,round_id,o['owner_generation'],now,deadline))
            w={'epoch':epoch,'round_id':round_id,'version':1,'state':'OPEN','deadline':deadline}
            seq=self.append(c,o,w,st,now,'window.open',{'deadline_at':deadline.isoformat(),'window_version':1})
            c.execute('UPDATE relay.relay_windows SET open_seq=%s,last_control_seq=%s WHERE app_id=%s AND room_id=%s AND epoch=%s',(seq,seq,app,room,epoch))
            return {'epoch':str(epoch),'version':1,'stream_seq':str(seq),'deadline_at':deadline.isoformat(),'remaining_ms':max(0,int((deadline-now).total_seconds()*1000))}

    @staticmethod
    def bulk_insert(c,table,columns,types,rows):
        if not rows:return
        arrays=[[row[index] for row in rows] for index in range(len(columns))]
        casts=','.join('%s::'+kind+'[]' for kind in types)
        c.execute('INSERT INTO relay.'+table+'('+','.join(columns)+') SELECT * FROM UNNEST('+casts+')',arrays)

    def ingest(self,app,room,messages,raw,*,provider_test=False):
        if not isinstance(raw,bytes) or len(raw)>1048576 or len(messages)>1000:raise RelayError('PAYLOAD_TOO_LARGE',413)
        if not messages:raise RelayError('INPUT_INVALID',422)
        prepared=[]
        for message in messages:
            opaque_id(message.source_msg_id)
            if message.type!='comment' or not isinstance(message.payload,dict):raise RelayError('INPUT_INVALID',422)
            try:canonical(message.payload)
            except (ValueError,TypeError):raise RelayError('INPUT_INVALID',422) from None
            if not provider_test and (not isinstance(message.participant_key,str) or not re.fullmatch(r'p:[0-9a-f]{64}',message.participant_key) or not isinstance(message.display_name,str) or not 0<len(message.display_name)<=80 or any(ord(ch)<32 for ch in message.display_name)):raise RelayError('PLATFORM_IDENTITY_REQUIRED',403)
            if not provider_test and (set(message.payload)!={'text'} or not isinstance(message.payload['text'],str)):raise RelayError('PLATFORM_MESSAGE_INVALID',403)
            if message.provider_test is not None and type(message.provider_test) is not bool:raise RelayError('INPUT_INVALID',422)
            if provider_test and message.provider_test is False:raise RelayError('TEST_PROVIDER_FORBIDDEN',403)
            test=provider_test if message.provider_test is None else message.provider_test
            digest=message.source_fingerprint or hashlib.sha256(canonical({'type':message.type,'payload':message.payload,'participant_key':message.participant_key,'display_name':message.display_name,'provider_test':test})).hexdigest()
            if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):raise RelayError('INPUT_INVALID',422)
            meta=message.source_metadata or {}
            if not isinstance(meta,dict) or len(canonical(meta))>4096:raise RelayError('INPUT_INVALID',422)
            prepared.append((message,test,digest,meta))
        with self.tx() as c:
            owner,window,stream,now=self.lock(c,app,room)
            if provider_test and owner['run_purpose']!='test_live':raise RelayError('TEST_PROVIDER_FORBIDDEN',403)
            self.expire(c,owner,window,stream,now)
            ids=[item[0].source_msg_id for item in prepared]
            old={row['source_msg_id']:row for row in c.execute("SELECT source_msg_id,source_hash,stream_seq,source_schema_version FROM relay.relay_inbox WHERE app_id=%s AND room_id=%s AND event_type='comment' AND source_msg_id=ANY(%s)",(app,room,ids)).fetchall()}
            fresh={};result=[]
            for message,test,digest,meta in prepared:
                previous=old.get(message.source_msg_id)
                if previous:
                    legacy=hashlib.sha256(canonical({'type':message.type,'payload':message.payload})).hexdigest()
                    if previous['source_hash']!=digest and not (previous['source_schema_version']==0 and previous['source_hash']==legacy):raise RelayError('SOURCE_ID_CONFLICT')
                    result.append(str(previous['stream_seq']));continue
                if message.source_msg_id in fresh:
                    if fresh[message.source_msg_id][2]!=digest:raise RelayError('SOURCE_ID_CONFLICT')
                    result.append(str(fresh[message.source_msg_id][4]));continue
                seq=stream['last_seq']+len(fresh)+1
                fresh[message.source_msg_id]=(message,test,digest,meta,seq);result.append(str(seq))
            if not fresh:return result
            raw_hash=hashlib.sha256(raw).hexdigest()
            batch=c.execute('SELECT raw_body FROM relay.raw_batches WHERE app_id=%s AND room_id=%s AND raw_hash=%s',(app,room,raw_hash)).fetchone()
            raw_cost=len(raw) if not batch or batch['raw_body'] is None else 0
            frame_rows=[];context_rows=[];inbox_rows=[];payload_cost=raw_cost
            base_context={key:owner.get(key) for key in ('app_id','room_id','installation_id','session_id','lease_id','owner_generation','run_purpose','authorization_id','candidate_digest','live_profile','grant_digest','room_binding_id','device_binding_id')}
            for message,test,digest,meta,seq in fresh.values():
                mode='synthetic' if provider_test else 'official'
                eligible=bool(window and window['state']=='OPEN' and not owner.get('revoked_at') and now<window['deadline'] and now<min(owner['expires_at'],owner['authorization_expires_at']) and not (test and owner['run_purpose']=='production'))
                payload={'source_msg_id':message.source_msg_id,'participant_key':message.participant_key or 'synthetic:'+message.source_msg_id,'display_name':message.display_name or 'Synthetic','content':message.payload}
                body=canonical({'schema_version':1,'contract_version':CONTRACT,'stream_id':stream['stream_id'],'stream_seq':str(seq),'original_generation':str(owner['owner_generation']),'round_id':window['round_id'] if window else None,'window_epoch':str(window['epoch']) if window else '0','relay_received_at':now.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z'),'type':message.type,'eligible':eligible,'provider_test':test,'run_purpose':owner['run_purpose'],'source_mode':mode,'payload':payload})
                if len(body)>47000:raise RelayError('PAYLOAD_TOO_LARGE',413)
                payload_cost+=len(body)
                frame_rows.append((app,room,seq,body,hashlib.sha256(body).hexdigest(),now,owner['owner_generation'],window['epoch'] if window else 0,eligible,stream['stream_id']))
                context_rows.append((app,room,seq,stream['stream_id'],Jsonb(base_context|{'provider_test':test,'source_mode':mode}),message.type,eligible))
                inbox_rows.append((app,room,message.type,message.source_msg_id,digest,raw_hash,None,seq,now,2 if message.source_fingerprint else 1,Jsonb(meta)))
            if stream['retained_messages']+len(fresh)>self.max_messages or stream['payload_bytes']+payload_cost>self.max_bytes:raise RelayError('RELAY_QUOTA_EXCEEDED',429)
            if raw_cost:c.execute('INSERT INTO relay.raw_batches VALUES(%s,%s,%s,%s,%s) ON CONFLICT(app_id,room_id,raw_hash) DO UPDATE SET raw_body=excluded.raw_body,received_at=excluded.received_at',(app,room,raw_hash,raw,now))
            self.bulk_insert(c,'relay_frames',('app_id','room_id','stream_seq','body','immutable_sha256','received_at','original_generation','window_epoch','eligible','stream_id'),('text','text','bigint','bytea','text','timestamptz','bigint','bigint','boolean','text'),frame_rows)
            self.bulk_insert(c,'delivery_contexts',('app_id','room_id','stream_seq','stream_id','context','event_type','eligible'),('text','text','bigint','text','jsonb','text','boolean'),context_rows)
            self.bulk_insert(c,'relay_inbox',('app_id','room_id','event_type','source_msg_id','source_hash','raw_hash','raw_body','stream_seq','received_at','source_schema_version','source_metadata'),('text','text','text','text','text','text','bytea','bigint','timestamptz','integer','jsonb'),inbox_rows)
            c.execute('UPDATE relay.relay_streams SET last_seq=last_seq+%s,payload_bytes=payload_bytes+%s,retained_messages=retained_messages+%s WHERE app_id=%s AND room_id=%s',(len(fresh),payload_cost,len(fresh),app,room))
            return result

    def close_window(self,app,room,lease,expected_version,reason,*,expected_epoch,winner_seq=None):
        if reason not in ('SOLVED','TIMEOUT','VOID','INTERRUPTED'): raise RelayError('INPUT_INVALID',422)
        with self.tx() as c:
            o,w,st,now=self.lock(c,app,room);self.expire(c,o,w,st,now)
            self.authorize(o,lease,now)
            if not w: raise RelayError('WINDOW_NOT_FOUND',404)
            if str(w['epoch'])!=str(expected_epoch): raise RelayError('WINDOW_EPOCH_CONFLICT')
            if w['state']!='CLOSED' and w['version']!=expected_version: raise RelayError('WINDOW_VERSION_CONFLICT')
            if winner_seq is not None and reason!='SOLVED': raise RelayError('INPUT_INVALID',422)
            if w['state']=='CLOSED' and w['reason'] not in ('TIMEOUT','SOLVED') and winner_seq is not None:
                raise RelayError('WINDOW_TERMINAL_CONFLICT')
            if w['state']!='CLOSED' and reason=='SOLVED' and winner_seq is None: raise RelayError('WINNER_REQUIRED',422)
            if winner_seq is not None:
                frame=c.execute('SELECT * FROM relay.relay_frames WHERE app_id=%s AND room_id=%s AND stream_seq=%s',(app,room,winner_seq)).fetchone()
                boundary=w['close_seq'] if w['state']=='CLOSED' else st['last_seq']
                if not frame or not frame['eligible'] or frame['window_epoch']!=w['epoch'] or frame['original_generation']!=o['owner_generation'] or winner_seq>boundary: raise RelayError('WINNER_OUTSIDE_WINDOW')
                if w.get('winner_seq') not in (None,winner_seq): raise RelayError('WINNER_CONFLICT')
            if w['state']!='CLOSED': self._close(c,o,w,st,now,reason)
            if winner_seq is not None:
                c.execute('UPDATE relay.relay_windows SET winner_seq=%s WHERE app_id=%s AND room_id=%s AND epoch=%s',(winner_seq,app,room,w['epoch']))
                w['winner_seq']=winner_seq
            return {'state':'CLOSED','reason':w['reason'],'version':w['version'],'close_seq':str(w['close_seq']),'barrier_seq':str(w['barrier_seq']),'winner_seq':str(w['winner_seq']) if w.get('winner_seq') is not None else None}

    def pause_window(self,app,room,lease,expected_version,*,expected_epoch):
        return self._transition_window(app,room,lease,expected_version,'PAUSED',expected_epoch)

    def resume_window(self,app,room,lease,expected_version,*,expected_epoch):
        return self._transition_window(app,room,lease,expected_version,'OPEN',expected_epoch)

    def _transition_window(self,app,room,lease,expected_version,target,expected_epoch):
        with self.tx() as c:
            o,w,st,now=self.lock(c,app,room);self.expire(c,o,w,st,now);self.authorize(o,lease,now)
            if not w or w['state']=='CLOSED': raise RelayError('ROUND_CLOSED')
            if str(w['epoch'])!=str(expected_epoch): raise RelayError('WINDOW_EPOCH_CONFLICT')
            if w['state']==target and w['version']==expected_version+1:
                return {'state':target,'version':w['version'],'epoch':str(w['epoch']),'stream_seq':str(w['last_control_seq']),'deadline_at':w['deadline'].isoformat(),'remaining_ms':w['remaining_ms']}
            if w['version']!=expected_version: raise RelayError('WINDOW_VERSION_CONFLICT')
            if (w['state'],target) not in (('OPEN','PAUSED'),('PAUSED','OPEN')): raise RelayError('WINDOW_STATE_CONFLICT')
            remaining=max(0,int((w['deadline']-now).total_seconds()*1000)) if target=='PAUSED' else w['remaining_ms']
            deadline=w['deadline'] if target=='PAUSED' else now+timedelta(milliseconds=remaining)
            if deadline<=now or deadline>now+timedelta(seconds=600) or deadline>o['authorization_expires_at']: raise RelayError('DEADLINE_INVALID',422)
            version=w['version']+1
            c.execute('UPDATE relay.relay_windows SET state=%s,remaining_ms=%s,deadline=%s,version=%s WHERE app_id=%s AND room_id=%s AND epoch=%s',(target,remaining,deadline,version,app,room,w['epoch']))
            boundary=st['last_seq']
            seq=self.append(c,o,w,st,now,'window.pause' if target=='PAUSED' else 'window.resume',{'window_version':version,'deadline_at':deadline.isoformat()})
            c.execute('UPDATE relay.relay_windows SET last_control_seq=%s WHERE app_id=%s AND room_id=%s AND epoch=%s',(seq,app,room,w['epoch']))
            return {'state':target,'version':version,'epoch':str(w['epoch']),'stream_seq':str(seq),'deadline_at':deadline.isoformat(),'remaining_ms':remaining,'close_seq':str(boundary)}

    def revoke(self,app,room,lease):
        with self.tx() as c:
            o,w,st,now=self.lock(c,app,room)
            if o['lease_id']!=lease: raise RelayError('LEASE_STALE')
            if w and w['state']!='CLOSED': self._close(c,o,w,st,now,'LEASE_REVOKED')
            c.execute('UPDATE relay.room_owners SET revoked_at=%s,connection_nonce=NULL WHERE app_id=%s AND room_id=%s',(now,app,room))
            c.execute('UPDATE relay.relay_leases SET revoked_at=%s WHERE lease_id=%s',(now,lease))
            self.enqueue_job(c,o,'platform.stop',job_id='stop:'+lease)
            # Keep expires_at: a replacement cannot overlap the old conservative TTL.
            return {'status':'revoked','replacement_not_before':o['expires_at'].isoformat()}

    def renew(self,grant,lease):
        with self.tx() as c:
            o,w,st,now=self.lock(c,grant.app_id,grant.room_id);self.expire(c,o,w,st,now);self.authorize(o,lease,now)
            if (o['installation_id'],o['session_id'],o['run_purpose'])!=(grant.installation_id,grant.session_id,grant.run_purpose) or grant.expires_at<=now: raise RelayError('LIVE_AUTHORIZATION_INVALID',403)
            if grant.trusted and any(o.get(k)!=getattr(grant,k) for k in ('authorization_id','candidate_digest','live_profile','grant_digest','room_binding_id')):raise RelayError('LIVE_AUTHORIZATION_INVALID',403)
            budget=c.execute('SELECT deadline_at FROM relay.authorization_sessions WHERE app_id=%s AND room_id=%s AND installation_id=%s AND session_id=%s AND authorization_id=%s',(grant.app_id,grant.room_id,grant.installation_id,grant.session_id,grant.authorization_id)).fetchone() if grant.trusted else None
            end=min(grant.expires_at,budget['deadline_at']) if budget else grant.expires_at
            if end<=now:raise RelayError('LIVE_AUTHORIZATION_EXPIRED',403)
            expires=min(now+timedelta(seconds=15),end)
            c.execute('UPDATE relay.room_owners SET expires_at=%s,authorization_expires_at=%s,revocation_revision=%s WHERE app_id=%s AND room_id=%s',(expires,end,grant.revocation_revision,grant.app_id,grant.room_id))
            c.execute('UPDATE relay.relay_leases SET expires_at=%s WHERE lease_id=%s',(expires,lease))
            return self.lease_view(c.execute('SELECT * FROM relay.room_owners WHERE app_id=%s AND room_id=%s',(grant.app_id,grant.room_id)).fetchone(),c)

    def prune_payloads(self,app,room,*,before):
        with self.tx() as c:
            o,w,st,now=self.lock(c,app,room)
            cutoff=min(before,now-timedelta(hours=24))
            rows=c.execute("UPDATE relay.relay_frames f SET body=NULL WHERE app_id=%s AND room_id=%s AND body IS NOT NULL AND received_at<%s AND NOT EXISTS(SELECT 1 FROM relay.relay_outbox j WHERE j.app_id=f.app_id AND j.room_id=f.room_id AND j.stream_seq=f.stream_seq AND j.status IN ('pending','running','retry')) RETURNING stream_seq",(app,room,cutoff)).fetchall()
            c.execute('UPDATE relay.relay_inbox SET raw_body=NULL WHERE app_id=%s AND room_id=%s AND stream_seq=ANY(%s)',(app,room,[r['stream_seq'] for r in rows]))
            c.execute('''UPDATE relay.raw_batches b SET raw_body=NULL WHERE app_id=%s AND room_id=%s AND received_at<%s AND NOT EXISTS(SELECT 1 FROM relay.relay_inbox i JOIN relay.relay_frames f USING(app_id,room_id,stream_seq) WHERE i.app_id=b.app_id AND i.room_id=b.room_id AND i.raw_hash=b.raw_hash AND f.body IS NOT NULL)''',(app,room,cutoff))
            floor=c.execute('SELECT min(stream_seq) AS floor FROM relay.relay_frames WHERE app_id=%s AND room_id=%s AND stream_id=%s AND body IS NOT NULL',(app,room,st['stream_id'])).fetchone()['floor']
            c.execute('UPDATE relay.relay_streams SET retained_from=%s,payload_bytes=(SELECT coalesce(sum(octet_length(body)),0) FROM relay.relay_frames WHERE app_id=%s AND room_id=%s)+(SELECT coalesce(sum(octet_length(raw_body)),0) FROM relay.relay_inbox WHERE app_id=%s AND room_id=%s)+(SELECT coalesce(sum(octet_length(raw_body)),0) FROM relay.raw_batches WHERE app_id=%s AND room_id=%s) WHERE app_id=%s AND room_id=%s',(floor or st['last_seq']+1,app,room,app,room,app,room,app,room))
            c.execute('UPDATE relay.relay_streams SET retained_messages=(SELECT count(*) FROM relay.relay_frames WHERE app_id=%s AND room_id=%s AND body IS NOT NULL) WHERE app_id=%s AND room_id=%s',(app,room,app,room))
            # Source hashes, IDs, window/terminal and confirmation metadata are deliberately retained.
            return len(rows)

    def record_stage(self,app,room,conn,seq,stage,receipt_id,*,client_time=None):
        if stage not in ('display.received','display.processed'): raise RelayError('INPUT_INVALID',422)
        opaque_id(receipt_id)
        # A display observation owns its actual client timestamp. Validate before
        # writing either its stage or the durable official-report intent.
        if not isinstance(client_time,str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z',client_time):raise RelayError('INPUT_INVALID',422)
        try:
            actual_time=datetime.fromisoformat(client_time.replace('Z','+00:00'))
            if actual_time<datetime(1970,1,1,tzinfo=timezone.utc):raise ValueError()
        except ValueError:raise RelayError('INPUT_INVALID',422) from None
        with self.tx() as c:
            o,st,cp=self.connection(c,app,room,conn)
            if seq<=0 or seq>cp['persisted_seq']: raise RelayError('ACK_OUT_OF_RANGE')
            source=c.execute('''SELECT f.body,f.immutable_sha256,f.window_epoch,f.original_generation,w.round_id,i.source_msg_id,i.event_type FROM relay.relay_inbox i JOIN relay.relay_frames f USING(app_id,room_id,stream_seq) JOIN relay.relay_windows w ON (w.app_id=f.app_id AND w.room_id=f.room_id AND w.epoch=f.window_epoch) WHERE i.app_id=%s AND i.room_id=%s AND i.stream_seq=%s AND i.source_msg_id<>'' AND i.event_type='comment' AND f.eligible AND f.original_generation=%s AND w.original_generation=f.original_generation AND w.round_id<>'' ''',(app,room,seq,o['owner_generation'])).fetchone()
            if not source: raise RelayError('DISPLAY_SOURCE_DELIVERY_REQUIRED')
            # Historical effects retain their original window mapping; a new active epoch
            # neither re-admits that source nor cancels its independent delivery evidence.
            if source['body'] is not None:
                body=bytes(source['body'])
                if hashlib.sha256(body).hexdigest()!=source['immutable_sha256']: raise RelayError('FRAME_INTEGRITY_FAILURE')
                immutable=bounded_json(body)
                if (immutable.get('stream_seq'),immutable.get('original_generation'),immutable.get('window_epoch'),immutable.get('round_id'),immutable.get('eligible'),immutable.get('type'),immutable.get('payload',{}).get('source_msg_id'))!=(str(seq),str(source['original_generation']),str(source['window_epoch']),source['round_id'],True,source['event_type'],source['source_msg_id']):
                    raise RelayError('DISPLAY_SOURCE_DELIVERY_REQUIRED')
            if stage=='display.processed' and not c.execute("SELECT 1 FROM relay.delivery_stages WHERE app_id=%s AND room_id=%s AND stream_seq=%s AND stage='display.received'",(app,room,seq)).fetchone(): raise RelayError('DISPLAY_RECEIVED_REQUIRED')
            existing=c.execute('SELECT payload FROM relay.relay_outbox WHERE app_id=%s AND room_id=%s AND stream_seq=%s AND kind=%s',(app,room,seq,stage)).fetchone()
            if existing:
                try:previous=datetime.fromisoformat(existing['payload']['client_time'].replace('Z','+00:00'))
                except (KeyError,ValueError,TypeError,AttributeError):raise RelayError('RECEIPT_CONFLICT') from None
                # HTTP history keeps its original offset/precision. Compare the
                # actual millisecond sent to the SDK without rewriting history.
                if previous.tzinfo is None:raise RelayError('RECEIPT_CONFLICT')
                previous=previous.astimezone(timezone.utc)
                if previous.replace(microsecond=previous.microsecond//1000*1000)!=actual_time:raise RelayError('RECEIPT_CONFLICT')
            c.execute('INSERT INTO relay.delivery_stages VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',(app,room,seq,stage,receipt_id,self.clock()))
            self.enqueue_job(c,o,stage,stream_seq=seq,payload={'receipt_id':receipt_id,'client_time':client_time})
            return {'status':'pending','stage':stage}

    def fulfill_pending(self):
        if self.fulfillment is not None:return self.fulfillment.step()
        from .fulfillment import FulfillmentWorker
        result={'unsupported':0,'completed':0};worker=FulfillmentWorker(self)
        for _ in range(100):
            item=worker.step()
            for key in result:result[key]+=item[key]
            if not item['unsupported'] and not item['completed']:break
        return result

    def connect(self,app,room,lease):
        with self.tx() as c:
            o,w,st,now=self.lock(c,app,room);self.expire(c,o,w,st,now);self.authorize(o,lease,now)
            segment=c.execute('SELECT session_id,installation_id FROM relay.stream_segments WHERE stream_id=%s',(st['stream_id'],)).fetchone()
            if segment['session_id']!=o['session_id'] or segment['installation_id']!=o['installation_id']:raise RelayError('STREAM_BOOTSTRAP_REQUIRED')
            nonce=str(uuid4());generation=o['delivery_generation']+1
            c.execute('UPDATE relay.room_owners SET connection_nonce=%s,delivery_generation=%s WHERE app_id=%s AND room_id=%s',(nonce,generation,app,room))
            c.execute('''INSERT INTO relay.relay_checkpoints(app_id,room_id,installation_id,connection_nonce,stream_id,session_id) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(app_id,room_id,installation_id) DO UPDATE SET connection_nonce=excluded.connection_nonce,delivered_seq=relay.relay_checkpoints.persisted_seq''',(app,room,o['installation_id'],nonce,st['stream_id'],o['session_id']))
            persisted=c.execute('SELECT persisted_seq FROM relay.relay_checkpoints WHERE app_id=%s AND room_id=%s AND installation_id=%s',(app,room,o['installation_id'])).fetchone()['persisted_seq']
            return {'lease_id':lease,'installation_id':o['installation_id'],'delivery_generation':str(generation),'connection_nonce':nonce,'persisted_seq':str(persisted)}

    def connection(self,c,app,room,conn):
        o,w,st,now=self.lock(c,app,room);self.expire(c,o,w,st,now);self.authorize(o,conn['lease_id'],now)
        if conn['connection_nonce']!=o['connection_nonce'] or conn['delivery_generation']!=str(o['delivery_generation']): raise RelayError('CONNECTION_STALE')
        cp=c.execute('SELECT * FROM relay.relay_checkpoints WHERE app_id=%s AND room_id=%s AND installation_id=%s FOR UPDATE',(app,room,o['installation_id'])).fetchone()
        return o,st,cp

    def frames(self,app,room,conn,after,limit=100):
        with self.tx() as c:
            o,st,cp=self.connection(c,app,room,conn)
            if after<cp['persisted_seq'] or after>cp['delivered_seq'] or after<st['retained_from']-1 or after>st['last_seq']: raise RelayError('RELAY_GAP')
            rows=c.execute('SELECT * FROM relay.relay_frames WHERE app_id=%s AND room_id=%s AND stream_id=%s AND stream_seq>%s ORDER BY stream_seq LIMIT %s',(app,room,st['stream_id'],after,min(limit,100))).fetchall()
            expected=after+1;result=[]
            for r in rows:
                if r['stream_seq']!=expected or r['body'] is None: raise RelayError('RELAY_GAP')
                body=bytes(r['body'])
                if hashlib.sha256(body).hexdigest()!=r['immutable_sha256']: raise RelayError('FRAME_INTEGRITY_FAILURE')
                result.append({'kind':'relay.frame','stream_id':st['stream_id'],'stream_seq':str(expected),'body_b64':base64.b64encode(body).decode(),'immutable_sha256':r['immutable_sha256'],**conn});expected+=1
            if not rows and after<st['last_seq']: raise RelayError('RELAY_GAP')
            c.execute('UPDATE relay.relay_checkpoints SET delivered_seq=greatest(delivered_seq,%s) WHERE app_id=%s AND room_id=%s AND installation_id=%s',(expected-1,app,room,o['installation_id']))
            return result

    def ack(self,app,room,conn,seq):
        with self.tx() as c:
            o,st,cp=self.connection(c,app,room,conn)
            if seq<0 or seq>cp['delivered_seq']: raise RelayError('ACK_OUT_OF_RANGE')
            if seq<=cp['persisted_seq']: return cp['persisted_seq']
            c.execute('''INSERT INTO relay.delivery_stages SELECT app_id,room_id,stream_seq,'transport.persisted',%s,%s FROM relay.relay_frames WHERE app_id=%s AND room_id=%s AND stream_id=%s AND stream_seq>%s AND stream_seq<=%s ON CONFLICT DO NOTHING''',(conn['connection_nonce'],self.clock(),app,room,st['stream_id'],cp['persisted_seq'],seq))
            c.execute('UPDATE relay.relay_checkpoints SET persisted_seq=%s WHERE app_id=%s AND room_id=%s AND installation_id=%s',(seq,app,room,o['installation_id']))
            return seq

    def stages(self,app,room,seq):
        with self.tx() as c:
            return [r['stage'] for r in c.execute('SELECT stage FROM relay.delivery_stages WHERE app_id=%s AND room_id=%s AND stream_seq=%s ORDER BY stage',(app,room,seq)).fetchall()]


def create_app(service,*,provider:ProviderAdapter|None=None,grant_verifier:GrantVerifier|None=None,test_mode=False,control_plane=None,platform_adapter=None,run_workers=False):
    if provider is None: raise RelayError('PROVIDER_ADAPTER_REQUIRED',503)
    if provider.provider_test and not test_mode: raise RelayError('TEST_PROVIDER_FORBIDDEN',503)
    if grant_verifier is None: raise RelayError('TRUSTED_CONTROL_PLANE_REQUIRED',503)
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def lifespan(app):
        task=None;stop=asyncio.Event()
        if run_workers:
            from .fulfillment import FulfillmentWorker,RelayRuntime
            service.fulfillment=FulfillmentWorker(service,platform_adapter)
            task=asyncio.create_task(RelayRuntime(service,service.fulfillment).run(stop))
        try:yield
        finally:
            stop.set()
            if task:
                task.cancel();await asyncio.gather(task,return_exceptions=True)
    app=FastAPI(lifespan=lifespan)
    service.official_ingress_ready=bool(getattr(provider,'available',True) and not provider.provider_test)
    service.platform_ready=bool(platform_adapter is not None and not platform_adapter.provider_test and getattr(platform_adapter,'available',True))
    service.platform_adapter_configured=platform_adapter is not None
    if control_plane is not None:service.owner_guard=control_plane.owner_failure
    @app.exception_handler(RelayError)
    async def error(request,exc): return JSONResponse(status_code=exc.status,content={'error':{'code':exc.code,'message':exc.code,'retryable':False}})
    @app.post('/relay/v1/douyin/callback')
    async def callback(request:Request):
        chunks=[];size=0
        async for part in request.stream():
            size+=len(part)
            if size>1048576: raise RelayError('PAYLOAD_TOO_LARGE',413)
            chunks.append(part)
        raw=b''.join(chunks)
        a,r,messages=provider.verify_and_decode(raw,request.headers)
        sequences=service.ingest(a,r,messages,raw,provider_test=provider.provider_test)
        return provider.success_response(sequences)
    def trusted(header):
        if grant_verifier is None: raise RelayError('TRUSTED_CONTROL_PLANE_REQUIRED',403)
        if len(header.encode('utf-8'))>16384: raise RelayError('AUTH_REQUIRED',401)
        if not header.startswith('Bearer '): raise RelayError('AUTH_REQUIRED',401)
        grant=grant_verifier.verify(header[7:])
        if not isinstance(grant,VerifiedGrant): raise RelayError('LIVE_AUTHORIZATION_INVALID',403)
        for value in (grant.app_id,grant.room_id,grant.installation_id,grant.session_id): opaque_id(value)
        if grant.expires_at<=service.clock(): raise RelayError('LIVE_AUTHORIZATION_EXPIRED',403)
        if not provider.provider_test and (not grant.trusted or not all((grant.authorization_id,grant.candidate_digest,grant.live_profile,grant.grant_digest,grant.room_binding_id,grant.device_binding_id))):raise RelayError('TRUSTED_CONTROL_PLANE_REQUIRED',403)
        if provider.provider_test and grant.run_purpose!='test_live': raise RelayError('TEST_PROVIDER_FORBIDDEN',403)
        return grant
    def native_response(request,value):
        extension=request.headers.get('x-be-extension-version')
        if extension is not None and extension!='LW-BE/1.1.0':raise RelayError('BE_EXTENSION_VERSION_UNSUPPORTED',409)
        if extension:return value|{'be_extension_version':'LW-BE/1.1.0'}
        if 'lease_ttl_ms' in value:return {k:value[k] for k in ('lease_id','owner_generation','expires_at') if k in value}
        return value
    def native_request(request):
        extension=request.headers.get('x-be-extension-version')
        if extension is not None and extension!='LW-BE/1.1.0':raise RelayError('BE_EXTENSION_VERSION_UNSUPPORTED',409)
    @app.post('/relay/v1/leases')
    async def leases(request:Request):
        grant=trusted(request.headers.get('authorization',''));native_request(request)
        return native_response(request,service.acquire(grant))
    def bound(grant,lease_id):
        opaque_id(lease_id,128)
        with service.tx() as c:
            owner=c.execute('SELECT * FROM relay.room_owners WHERE app_id=%s AND room_id=%s',(grant.app_id,grant.room_id)).fetchone()
            if not owner or (owner['installation_id'],owner['session_id'],owner['lease_id'])!=(grant.installation_id,grant.session_id,lease_id) or grant.expires_at<=service.clock():
                raise RelayError('INSTALLATION_MISMATCH',403)
            if owner['run_purpose']!=grant.run_purpose or (grant.trusted and any(owner.get(k)!=getattr(grant,k) for k in ('authorization_id','candidate_digest','live_profile','grant_digest','room_binding_id'))):raise RelayError('LIVE_AUTHORIZATION_INVALID',403)
    @app.post('/relay/v1/windows/{operation}')
    async def windows(operation:str,request:Request):
        grant=trusted(request.headers.get('authorization',''))
        try:
            data=bounded_json(await request.body());lease=data['lease_id'];bound(grant,lease);native_request(request)
            if operation in ('pause','resume','close'):
                if type(data.get('expected_version')) is not int or data['expected_version']<1:raise RelayError('INPUT_INVALID',422)
                decimal_cursor(data.get('window_epoch'))
            def perform():
              if operation=='open':
                if not getattr(provider,'available',True):raise RelayError('PLATFORM_ADAPTER_UNSUPPORTED',503)
                if grant.trusted:
                    with service.tx() as c:
                        task=c.execute("SELECT state,owner_lease_id FROM relay.platform_task_fences WHERE app_id=%s AND room_id=%s AND msg_type='live_comment'",(grant.app_id,grant.room_id)).fetchone()
                        if not service.platform_ready or not task or task['owner_lease_id']!=lease or task['state']!='RUNNING':raise RelayError('PLATFORM_TASK_NOT_RUNNING',409)
                deadline=datetime.fromisoformat(data['deadline_at'].replace('Z','+00:00'))
                if deadline.tzinfo is None: raise ValueError()
                return service.open_window(grant.app_id,grant.room_id,lease,data['round_id'],deadline)
              if operation=='pause': return service.pause_window(grant.app_id,grant.room_id,lease,data['expected_version'],expected_epoch=data['window_epoch'])
              if operation=='resume': return service.resume_window(grant.app_id,grant.room_id,lease,data['expected_version'],expected_epoch=data['window_epoch'])
              if operation=='close': return service.close_window(grant.app_id,grant.room_id,lease,data['expected_version'],data['reason'],expected_epoch=data['window_epoch'],winner_seq=decimal_cursor(data['winner_seq']) if data.get('winner_seq') is not None else None)
              raise RelayError('RESOURCE_NOT_FOUND',404)
            from .control_routes import idempotent
            return native_response(request,idempotent(service,grant,operation,request.headers.get('idempotency-key'),data,perform))
        except (ValueError,KeyError,TypeError,AttributeError): raise RelayError('INPUT_INVALID',422) from None
    @app.post('/relay/v1/leases/{operation}')
    async def lease_operation(operation:str,request:Request):
        grant=trusted(request.headers.get('authorization',''))
        try:
            data=bounded_json(await request.body());lease=data['lease_id'];bound(grant,lease);native_request(request)
            if operation=='renew': return native_response(request,service.renew(grant,lease))
            if operation=='revoke': return native_response(request,service.revoke(grant.app_id,grant.room_id,lease))
            raise RelayError('RESOURCE_NOT_FOUND',404)
        except (ValueError,KeyError,TypeError): raise RelayError('INPUT_INVALID',422) from None
    @app.websocket('/relay/v1/streams')
    async def streams(ws:WebSocket):
        try:
            if ws.url.query: raise RelayError('QUERY_CREDENTIAL_FORBIDDEN',403)
            grant=trusted(ws.headers.get('authorization',''))
        except RelayError:
            await ws.close(code=1008);return
        await ws.accept()
        try:
            command=await receive_ws_json(ws)
            grant=trusted(ws.headers.get('authorization',''))
            if command.get('kind')!='resume' or set(command)!={'kind','lease_id','persisted_seq'}: raise RelayError('INPUT_INVALID',422)
            claimed=decimal_cursor(command.get('persisted_seq'))
            bound(grant,command['lease_id'])
            with service.tx() as c:
                segment=c.execute('SELECT sg.previous_stream_id FROM relay.relay_streams st JOIN relay.stream_segments sg USING(stream_id) WHERE st.app_id=%s AND st.room_id=%s',(grant.app_id,grant.room_id)).fetchone()
                if segment['previous_stream_id'] is not None and ws.headers.get('x-be-extension-version')!='LW-BE/1.1.0':raise RelayError('BE_EXTENSION_VERSION_UNSUPPORTED',409)
            conn=service.connect(grant.app_id,grant.room_id,command['lease_id'])
            after=int(conn['persisted_seq'])
            if claimed<after: raise RelayError('RELAY_GAP')
            # ACK loss replays from relay's durable cursor; a client claim never advances it.
            frames=service.frames(grant.app_id,grant.room_id,conn,after)
            await ws.send_json({'kind':'resume_ready',**conn})
            while True:
                for frame in frames:
                    await ws.send_json(frame);after=int(frame['stream_seq'])
                try: incoming=await asyncio.wait_for(receive_ws_json(ws),timeout=.1)
                except asyncio.TimeoutError: incoming=None
                current_grant=trusted(ws.headers.get('authorization',''))
                if (current_grant.app_id,current_grant.room_id,current_grant.installation_id,current_grant.session_id,current_grant.run_purpose)!=(grant.app_id,grant.room_id,grant.installation_id,grant.session_id,grant.run_purpose): raise RelayError('LIVE_AUTHORIZATION_INVALID',403)
                bound(current_grant,conn['lease_id'])
                if incoming:
                    if incoming.get('kind')=='transport.persisted':
                        if set(incoming)!={'kind','persisted_seq'}:raise RelayError('INPUT_INVALID',422)
                        acknowledged=service.ack(grant.app_id,grant.room_id,conn,decimal_cursor(incoming['persisted_seq']))
                        await ws.send_json({'kind':'transport.persisted.accepted','persisted_seq':str(acknowledged)})
                    elif incoming.get('kind') in ('display.received','display.processed'):
                        if ws.headers.get('x-be-extension-version')!='LW-BE/1.1.0':raise RelayError('BE_EXTENSION_VERSION_UNSUPPORTED',409)
                        if set(incoming)!={'kind','stream_seq','receipt_id','client_time'}:raise RelayError('INPUT_INVALID',422)
                        receipt=service.record_stage(grant.app_id,grant.room_id,conn,decimal_cursor(incoming['stream_seq']),incoming['kind'],incoming['receipt_id'],client_time=incoming['client_time'])
                        await ws.send_json({'kind':'delivery.stage.accepted',**receipt})
                    else: raise RelayError('INPUT_INVALID',422)
                frames=service.frames(grant.app_id,grant.room_id,conn,after)
        except WebSocketDisconnect: pass
        except (RelayError,KeyError,ValueError,TypeError) as exc:
            await ws.send_json({'kind':'stream.gap' if isinstance(exc,RelayError) and exc.code=='RELAY_GAP' else 'relay.error','reason_code':exc.code if isinstance(exc,RelayError) else 'INPUT_INVALID'})
            await ws.close(code=1009 if isinstance(exc,RelayError) and exc.code=='PAYLOAD_TOO_LARGE' else 1008)
    if control_plane is not None:
        from .control_routes import install_control_routes
        install_control_routes(app,service,control_plane,trusted=trusted,bound=bound,platform_adapter=platform_adapter)
    return app
