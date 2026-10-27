"""Private diagnostics project only recorded phases; no inferred green stages."""
import base64
import json
import hmac
import hashlib
from datetime import timedelta,datetime
import sqlalchemy as sa
from fastapi import APIRouter,Request,Query
from app.errors import DomainError
from app.infrastructure.db import schema as s
from app.modules.auth.service import now
from app.modules.analytics import models as analytics
from app.modules.analytics.service import stamp as parse_stamp
from app.dto import MessagePage,MessageTrace
from app.modules.wordbank.models import Envelope

def stamp(value): return value.isoformat(timespec='milliseconds').replace('+00:00','Z')

def create_router(auth,core):
    router=APIRouter()
    def actor(request): return auth.operator(request.cookies.get('lw_operator'),'diagnostics.read')
    def ok(request,data): return {'data':data,'request_id':request.state.request_id,'contract_version':'LW-C1/1.0.0'}
    def summary(row):
        return {'message_id':row['id'],'session_id':row['session_id'],'round_id':row['round_id'],'source_mode':row['source_mode'],'received_at':stamp(row['created_at']),'status':row['status'],'reason_code':row['reason_code'],'normalized_word':None if row['solved'] else row['normalized_word'],'display_name':row['display_name']}
    def query():
        guesses=sa.select(s.guesses.c.id,s.guesses.c.created_at,s.guesses.c.session_id,s.guesses.c.round_id,s.guesses.c.source_mode,s.guesses.c.status,s.score_jobs.c.error_code.label('reason_code'),s.guesses.c.normalized_word,s.guesses.c.solved,s.participants.c.display_name).join(s.participants,s.participants.c.id==s.guesses.c.participant_id).outerjoin(s.score_jobs,s.score_jobs.c.guess_id==s.guesses.c.id)
        rejects=sa.select(s.rejections.c.id,s.rejections.c.created_at,s.rejections.c.session_id,s.rejections.c.round_id,s.rejections.c.source_mode,sa.literal('rejected').label('status'),s.rejections.c.reason_code,sa.null().label('normalized_word'),sa.literal(False).label('solved'),sa.literal('').label('display_name')).join(s.sessions,s.sessions.c.id==s.rejections.c.session_id)
        return sa.union_all(guesses,rejects).subquery()

    @router.get('/api/v1/messages',response_model=Envelope[MessagePage])
    def messages(request:Request,session_id:str,status:str|None=None,reason_code:str|None=None,cursor:str|None=None,page_size:int=Query(50,ge=1,le=100)):
        who=actor(request)
        with core.db.engine.connect() as c:
            core.authorize_session(c,session_id,who['actor_id'])
            rows=query(); statement=sa.select(rows).where(rows.c.session_id==session_id,rows.c.created_at>=now()-timedelta(days=7))
            if status: statement=statement.where(rows.c.status==status)
            if reason_code: statement=statement.where(rows.c.reason_code==reason_code)
            if cursor:
                try:
                    if len(cursor)>1024: raise ValueError()
                    value=json.loads(base64.urlsafe_b64decode(cursor+'='*((4-len(cursor)%4)%4)))
                    if value['session_id']!=session_id or value['status']!=status or value['reason_code']!=reason_code: raise ValueError()
                    moment=datetime.fromisoformat(value['time']); identity=value['id']
                except Exception: raise DomainError('INPUT_INVALID','Invalid message cursor',422) from None
                statement=statement.where(sa.tuple_(rows.c.created_at,rows.c.id)>sa.tuple_(moment,identity))
            page=list(c.execute(statement.order_by(rows.c.created_at,rows.c.id).limit(page_size+1)).mappings())
            next_cursor=None
            if len(page)>page_size:
                last=page[page_size-1]; value={'session_id':session_id,'status':status,'reason_code':reason_code,'time':last['created_at'].isoformat(),'id':last['id']}
                next_cursor=base64.urlsafe_b64encode(json.dumps(value,separators=(',',':')).encode()).decode().rstrip('=')
            return ok(request,{'items':[summary(row) for row in page[:page_size]],'next_cursor':next_cursor})

    @router.get('/api/v1/messages/{message_id}/trace',response_model=Envelope[MessageTrace])
    def trace(request:Request,message_id:str):
        who=actor(request)
        with core.db.engine.connect() as c:
            # Round trace refs are opaque server-issued identifiers, separate from guesses.
            round_trace=c.execute(sa.select(analytics.round_facts.c.round_id,analytics.round_facts.c.session_id,analytics.round_facts.c.payload,s.rounds.c.created_at,s.sessions.c.mode).join(s.rounds,s.rounds.c.id==analytics.round_facts.c.round_id).join(s.sessions,s.sessions.c.id==analytics.round_facts.c.session_id).where(analytics.round_facts.c.payload['trace_ref'].astext==message_id)).mappings().first()
            if not round_trace and len(message_id)==64 and all(x in '0123456789abcdef' for x in message_id):
                service=getattr(core,'analytics',None)
                if service is not None:
                    # Historical refs predate the observer. Scan only currently owned rounds
                    # through a bounded DB cursor; never expose another actor's existence.
                    legacy=sa.select(s.rounds.c.id,s.rounds.c.session_id,s.rounds.c.created_at,s.sessions.c.mode).join(s.sessions,s.sessions.c.id==s.rounds.c.session_id).where(s.sessions.c.actor_id==who['actor_id'])
                    for item in c.execute(legacy.execution_options(stream_results=True,yield_per=100)).mappings():
                        expected=hmac.new(service.signer.key,('trace:'+item['session_id']+':'+item['id']).encode(),'sha256').hexdigest()
                        if hmac.compare_digest(expected,message_id):
                            round_trace={'round_id':item['id'],'session_id':item['session_id'],'created_at':item['created_at'],'mode':item['mode'],'payload':{}}
                            break
            if round_trace:
                sid=round_trace['session_id']; rid=round_trace['round_id']
                core.authorize_session(c,sid,who['actor_id'])
                archived=c.execute(sa.select(analytics.traces).where(analytics.traces.c.round_id==rid,analytics.traces.c.session_id==sid,analytics.traces.c.trace_ref==message_id)).mappings().first()
                started=parse_stamp(round_trace['payload'].get('started_at')) or round_trace['created_at']
                expires=archived['expires_at'] if archived else parse_stamp(round_trace['payload'].get('trace_expires_at')) or started+timedelta(days=30)
                if expires<=now(): raise DomainError('TRACE_EXPIRED','Round trace expired',410,details={'reason':'EXPIRED'})
                if archived:
                    steps=archived['payload']['steps']
                else:
                    identities=sa.union(sa.select(s.guesses.c.id).where(s.guesses.c.session_id==sid,s.guesses.c.round_id==rid),sa.select(s.rejections.c.id).where(s.rejections.c.session_id==sid,s.rejections.c.round_id==rid))
                    rows=c.execute(sa.select(s.trace_steps).where(s.trace_steps.c.message_id.in_(identities)).order_by(s.trace_steps.c.time,s.trace_steps.c.id)).mappings()
                    steps=[{'stage':x['stage'],'state':x['state'],'time':stamp(x['time']),'reason_code':x['reason_code']} for x in rows]
                return ok(request,{'message_id':message_id,'source_mode':{'rehearsal':'manual','replay':'replay'}.get(round_trace['mode'],'official'),'round_id':rid,'stream_id':None,'stream_seq':None,'steps':steps,'feedback_refs':[]})
            rows=query(); row=c.execute(sa.select(rows).where(rows.c.id==message_id)).mappings().first()
            if not row: raise DomainError('RESOURCE_NOT_FOUND','Message not found',404)
            core.authorize_session(c,row['session_id'],who['actor_id'])
            if row['created_at']<now()-timedelta(days=30): raise DomainError('TRACE_EXPIRED','Message trace expired',410,details={'reason':'EXPIRED'})
            metadata_port=getattr(core,'connector_trace_metadata',None)
            metadata=metadata_port(c,message_id) if callable(metadata_port) else None
            metadata=metadata or {}
            steps=c.execute(sa.select(s.trace_steps).where(s.trace_steps.c.message_id==message_id).order_by(s.trace_steps.c.time,s.trace_steps.c.id)).mappings()
            return ok(request,{'message_id':message_id,'source_mode':row['source_mode'],'round_id':row['round_id'],'stream_id':metadata.get('stream_id'),'stream_seq':metadata.get('stream_seq'),'steps':[{'stage':step['stage'],'state':step['state'],'time':stamp(step['time']),'reason_code':step['reason_code']} for step in steps],'feedback_refs':[message_id] if row['status']=='scored' else []})
    return router
