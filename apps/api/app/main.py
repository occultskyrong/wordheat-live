"""Local loopback HTTP adapter. Unimplemented capabilities have no routes."""
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal
import time
import uuid
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from app.config import Settings
from app import dto
from app.errors import DomainError
from app.infrastructure.db.database import Database
from app.infrastructure.db import schema as s
from app.modules.auth.service import AuthService
from app.modules.sessions.service import LocalService
from app.modules.rounds.worker import LocalRoundWorker
from app.modules.scoring.worker import ScoreWorker
from app.modules.realtime.service import RealtimeService
from app.modules.realtime.router import create_router as realtime_router
from app.modules.realtime.protocol import parse_receipt,Receipt
from app.modules.wordbank.models import Envelope
from app.http_contract import C1_ERROR_RESPONSES,install_protocol_schema
from app.modules.wordbank.service import WordbankService
from app.modules.wordbank.worker import WordbankWorker
from app.modules.wordbank.router import create_router as wordbank_router
from app.modules.replays.service import ReplayService
from app.modules.replays.worker import ReplayWorker
from app.modules.replays.router import create_router as replay_router
from app.modules.operations.messages import create_router as messages_router

CONTRACT='LW-C1/1.0.0'
class Model(BaseModel): model_config=ConfigDict(extra='forbid',strict=True)
class Login(Model): username:str=Field(min_length=1,max_length=80); password:str=Field(min_length=1,max_length=256)
class SessionRules(Model):
    duration_seconds:int=Field(default=240,ge=30,le=600,strict=True)
    intermission_seconds:int=Field(default=4,ge=3,le=30,strict=True)
    auto_next:bool=True
    category_id:str|None=None
    difficulty_min:int=Field(default=1,ge=1,le=5,strict=True)
    difficulty_max:int=Field(default=5,ge=1,le=5,strict=True)
    strict_round_code:bool=True
class SessionCreate(Model):
    label:str=Field(min_length=1,max_length=80)
    mode:Literal['rehearsal','official','replay']
    run_purpose:Literal['local','test_live','production']
    room_binding_id:str|None=None
    live_profile:Literal['beta','g2_evidence','production']|None=None
    rules:SessionRules=Field(default_factory=SessionRules)
class Command(Model): command:str; expected_version:int=Field(ge=0); args:dict=Field(default_factory=dict)
class Selection(Model): strategy:Literal['random','specific']; puzzle_id:str|None=None
class RoundCreate(Model):
    session_id:str
    expected_session_version:int=Field(ge=0)
    selection:Selection|None=None
    selection_id:str|None=None
    expected_selection_version:int|None=None
class ConnectorArgs(Model): session_id:str; reason:str|None=Field(default=None,max_length=200); fresh_start:bool=False; retry_failed_deliveries:bool=False
class ConnectorCommand(Model):
    command:Literal['connect','start','stop','query']
    expected_version:int=Field(ge=0,strict=True)
    args:ConnectorArgs
class Ticket(Model): session_id:str; purpose:Literal['primary','preview']
class Exchange(Model): ticket:str=Field(min_length=1,max_length=128)
class Guess(Model):
    session_id:str; round_id:str; short_code:str=Field(min_length=2,max_length=4)
    word:str=Field(max_length=32); display_name:str=Field(min_length=1,max_length=80)


def create_app(settings:Settings, *, scorer=None,display_probe=None,official=None):
    db=Database(settings); auth=AuthService(db); service=LocalService(db,scorer,display_probe,official=official)
    realtime=RealtimeService(service); service.realtime=realtime
    if display_probe is None: service.readiness.bind_display_probe(realtime.is_ready,in_transaction=realtime.is_ready_in_transaction)
    worker=LocalRoundWorker(service)
    score_worker=ScoreWorker(service)
    service.score_worker=score_worker
    wordbank=WordbankService(service); wordbank_worker=WordbankWorker(wordbank)
    replay=ReplayService(service,wordbank); replay_worker=ReplayWorker(replay)
    @asynccontextmanager
    async def lifespan(app):
        db.verify_runtime()
        worker.start()
        score_worker.start()
        wordbank_worker.start()
        replay_worker.start()
        try: yield
        finally:
            worker.stop()
            score_worker.stop()
            wordbank_worker.stop()
            replay_worker.stop()
            db.engine.dispose()
    app=FastAPI(title='WordHeat local API',version='0.1.0',lifespan=lifespan,docs_url=None,redoc_url=None,responses=C1_ERROR_RESPONSES)
    app.state.database=db; app.state.service=service; app.state.auth=auth; app.state.realtime=realtime
    app.state.score_worker=score_worker
    app.state.wordbank=wordbank; app.state.wordbank_worker=wordbank_worker
    app.state.replay=replay; app.state.replay_worker=replay_worker
    app.include_router(realtime_router(db,auth,service,realtime,settings))
    app.include_router(wordbank_router(db,auth,wordbank))
    app.include_router(replay_router(auth,replay))
    app.include_router(messages_router(auth,service))
    attempts={}
    def fail(request,error):
        return JSONResponse({'error':{'code':error.code,'message':error.message,'retryable':error.retryable,'details':error.details},'request_id':getattr(request.state,'request_id',str(uuid.uuid4())),'contract_version':CONTRACT},status_code=error.status_code)
    @app.exception_handler(DomainError)
    async def domain_error(request,error): return fail(request,error)
    @app.exception_handler(SQLAlchemyError)
    async def database_error(request,error): return fail(request,DomainError('DEPENDENCY_UNAVAILABLE','Local database operation failed',503,True))
    from starlette.exceptions import HTTPException
    @app.exception_handler(HTTPException)
    async def http_error(request,error):
        return fail(request,DomainError('RESOURCE_NOT_FOUND' if error.status_code==404 else 'INPUT_INVALID','Route unavailable' if error.status_code==404 else 'Request rejected',error.status_code))
    from fastapi.exceptions import RequestValidationError
    @app.exception_handler(RequestValidationError)
    async def validation_error(request,error): return fail(request,DomainError('INPUT_INVALID','Invalid request payload',422,details=[{'field':'.'.join(str(p) for p in item['loc'] if p!='body'),'reason':item['type']} for item in error.errors()]))
    from fastapi.exceptions import ResponseValidationError
    @app.exception_handler(ResponseValidationError)
    async def response_validation_error(request,error): return fail(request,DomainError('RESPONSE_CONTRACT_INVALID','Response contract validation failed',500))
    @app.middleware('http')
    async def boundary(request:Request,call_next):
        request.state.request_id=str(uuid.uuid4())
        if request.headers.get('host') not in settings.allowed_hosts: return fail(request,DomainError('FORBIDDEN','Host denied',403))
        origin=request.headers.get('origin')
        if origin is not None and origin not in settings.allowed_origins: return fail(request,DomainError('FORBIDDEN','Origin denied',403))
        version=request.headers.get('x-contract-version')
        if version is not None and version!=CONTRACT: return fail(request,DomainError('CONTRACT_VERSION_UNSUPPORTED','Unsupported contract version',409))
        if request.method not in {'GET','HEAD','OPTIONS'}:
            if origin not in settings.allowed_origins: return fail(request,DomainError('FORBIDDEN','Exact Origin required',403))
            upload=request.method=='POST' and request.url.path=='/api/v1/puzzle-imports'
            media=request.headers.get('content-type','').split(';')[0]
            if request.method!='DELETE' and media!=('multipart/form-data' if upload else 'application/json'): return fail(request,DomainError('INPUT_INVALID','Required request content type missing',422))
            limit=2*1024*1024+65536 if upload else 65536
            length=request.headers.get('content-length')
            if length and (not length.isdigit() or int(length)>limit): return fail(request,DomainError('PAYLOAD_TOO_LARGE','Payload too large',413))
            chunks=[]; size=0
            async for chunk in request.stream():
                size+=len(chunk)
                if size>limit: return fail(request,DomainError('PAYLOAD_TOO_LARGE','Payload too large',413))
                chunks.append(chunk)
            request._body=b''.join(chunks)
            if request.url.path in {'/api/v1/auth/sessions','/api/v1/display-sessions'}:
                key=(request.client.host if request.client else 'local',request.url.path); moment=time.monotonic()
                recent=[t for t in attempts.get(key,[]) if moment-t<60]
                if len(recent)>=20: return fail(request,DomainError('RATE_LIMITED','Bootstrap rate exceeded',429,True))
                attempts[key]=recent+[moment]
        if request.method not in {'GET','HEAD','OPTIONS'} and request.url.path not in {'/api/v1/auth/sessions','/api/v1/display-sessions','/api/v1/display/receipts'}:
            try:
                auth.operator(request.cookies.get('lw_operator'),csrf=request.headers.get('x-csrf-token',''))
            except DomainError as error:
                return fail(request,error)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        return response
    def ok(request,data):
        path=request.url.path; model=None
        if path=='/api/v1/health/live': model=dto.Liveness
        elif path=='/api/v1/auth/sessions/current' and request.method=='DELETE': model=dto.LoggedOut
        elif path.startswith('/api/v1/auth/sessions/') and request.method=='DELETE': model=dto.Revoked
        elif path.startswith('/api/v1/display-tickets/') and request.method=='DELETE': model=dto.Revoked
        if path=='/api/v1/auth/sessions' and request.method=='GET': model=dto.OperatorSessions
        elif path=='/api/v1/auth/sessions' or (path=='/api/v1/auth/sessions/current' and request.method=='GET'): model=dto.AuthSession
        elif path=='/api/v1/display-sessions' or path=='/api/v1/display-sessions/current': model=dto.DisplaySession
        elif path=='/api/v1/display-tickets': model=dto.TicketResult
        elif path=='/api/v1/health/ready': model=dto.Readiness
        elif path=='/api/v1/runtime/status': model=dto.RuntimeStatus
        elif path.endswith('/private-state'): model=dto.PrivateRound
        elif path.startswith('/api/v1/studio/') and path.endswith('/snapshot'): model=dto.StudioSnapshot
        elif path.endswith('/snapshot'): model=dto.PublicSnapshot
        elif path.startswith('/api/v1/puzzles/') and path.endswith('/commands'): model=dto.CommandResult
        elif path.endswith('/commands'): model=dto.CommandResult
        elif path=='/api/v1/rounds': model=dto.RoundCreated
        elif path.startswith('/api/v1/live-sessions'): model=dto.SessionDetail
        elif path=='/api/v1/rehearsal/guesses': model=dto.RehearsalResult
        elif path=='/api/v1/display/receipts': model=dto.ReceiptResult
        if model: data=dto.validated(model,data)
        return {'data':data,'request_id':request.state.request_id,'contract_version':CONTRACT}
    def actor(request,permission=None,mutation=False):
        csrf=request.headers.get('x-csrf-token','') if mutation else None
        return auth.operator(request.cookies.get('lw_operator'),permission,csrf)
    def command(request,who,body,op):
        extension=(request.url.path in {'/api/v1/live-sessions','/api/v1/rounds'}
                   or body.get('command') in {'start','configure','select_next'}
                   or body.get('selection_id') is not None
                   or body.get('args',{}).get('retry_failed_deliveries') is True
                   or (request.url.path.startswith('/api/v1/connectors/') and body.get('args',{}).get('fresh_start') is True))
        original=op
        def guarded(c):
            if extension and request.headers.get('x-be-extension-version')!='LW-BE/1.1.0':
                raise DomainError('BE_EXTENSION_VERSION_UNSUPPORTED','Backend extension negotiation required',409)
            result=original(c)
            if extension: result=result|{'be_extension_version':'LW-BE/1.1.0'}
            return result
        result=service.command(who['actor_id'],request.method,request.url.path,request.headers.get('idempotency-key'),body,guarded,request_id=request.state.request_id)
        value=ok(request,result)
        return JSONResponse(value,status_code=202) if result.get('status')=='pending' or result.get('command',{}).get('status')=='pending' else value
    def cookie(response,name,value): response.set_cookie(name,value,httponly=True,secure=settings.secure_cookies,samesite='strict',max_age=28800,path='/api/v1')
    @app.get('/api/v1/health/live',response_model=Envelope[dto.Liveness],response_model_exclude_unset=True)
    def live(request:Request): return ok(request,{'status':'alive'})
    def extension(request):
        value=request.headers.get('x-be-extension-version')
        if value is not None and value!='LW-BE/1.1.0': raise DomainError('BE_EXTENSION_VERSION_UNSUPPORTED','Unsupported backend extension',409)
        return value is not None
    @app.get('/api/v1/health/ready',response_model=Envelope[dto.Readiness],response_model_exclude_unset=True)
    def ready(request:Request,session_id:str|None=None):
        who=actor(request,'session.control' if session_id else None); extended=extension(request)
        with db.engine.begin() as c:
            session=service.session(c,session_id,who['actor_id'],lock=False) if session_id else None
            result=service.readiness.evaluate(c,session,who['permissions'])
            return ok(request,result if extended else service.readiness.c1(result))
    @app.get('/api/v1/runtime/status',response_model=Envelope[dto.RuntimeStatus],response_model_exclude_unset=True)
    def runtime(request:Request,session_id:str|None=None):
        who=actor(request,'session.control' if session_id else None); extended=extension(request)
        with db.engine.begin() as c:
            session=service.session(c,session_id,who['actor_id'],lock=False) if session_id else None
            result=service.readiness.evaluate(c,session,who['permissions'])
            return ok(request,service.readiness.runtime(c,result,extended))
    @app.post('/api/v1/auth/sessions',response_model=Envelope[dto.AuthSession],response_model_exclude_unset=True)
    def login(request:Request,body:Login):
        token,data=auth.login(body.username,body.password); response=JSONResponse(ok(request,data)); cookie(response,'lw_operator',token); return response
    @app.get('/api/v1/auth/sessions/current',response_model=Envelope[dto.AuthSession],response_model_exclude_unset=True)
    def current(request:Request):
        who=actor(request); return ok(request,{'actor':{key:who[key] for key in ('actor_id','display_name','permissions')},'csrf_token':who['csrf_token'],'expires_at':who['expires_at']})
    @app.get('/api/v1/auth/sessions',response_model=Envelope[dto.OperatorSessions],response_model_exclude_unset=True)
    def operator_sessions(request:Request):
        who=actor(request); return ok(request,{'sessions':auth.sessions(who,request.cookies.get('lw_operator'))})
    @app.delete('/api/v1/auth/sessions/current',response_model=Envelope[dto.LoggedOut],response_model_exclude_unset=True)
    def logout(request:Request):
        actor(request,mutation=True); auth.logout(request.cookies['lw_operator']); response=JSONResponse(ok(request,{'logged_out':True})); response.delete_cookie('lw_operator',path='/api/v1'); return response
    @app.delete('/api/v1/auth/sessions/{session_id}',response_model=Envelope[dto.Revoked],response_model_exclude_unset=True)
    def revoke_operator_session(request:Request,session_id:str):
        who=actor(request,mutation=True); return ok(request,auth.revoke(who,session_id))
    @app.post('/api/v1/live-sessions',response_model=Envelope[dto.SessionDetail],response_model_exclude_unset=True)
    def session_create(request:Request,body:SessionCreate):
        who=actor(request,'session.control',True); data=body.model_dump(); return command(request,who,data,lambda c:service.create_session(c,who['actor_id'],data))
    @app.get('/api/v1/live-sessions/{session_id}',response_model=Envelope[dto.SessionDetail],response_model_exclude_unset=True)
    def session_get(request:Request,session_id:str):
        who=actor(request,'session.control')
        with db.read_snapshot() as c:
            row=service.session(c,session_id,who['actor_id'],lock=False)
            result=service.session_detail(c,row)
            extension=request.headers.get('x-be-extension-version')
            if extension:
                if extension!='LW-BE/1.1.0': raise DomainError('BE_EXTENSION_VERSION_UNSUPPORTED','Unsupported backend extension',409)
                result|={'be_extension_version':extension,'next_selection':service.next_selection(c,session_id)}
            return ok(request,result)
    @app.post('/api/v1/live-sessions/{session_id}/commands',response_model=Envelope[dto.CommandResult],response_model_exclude_unset=True)
    def session_command(request:Request,session_id:str,body:Command):
        who=actor(request,'session.control',True); data=body.model_dump(); return command(request,who,data,lambda c:service.session_command(c,who['actor_id'],session_id,data))
    @app.post('/api/v1/connectors/{connector_id}/commands',response_model=Envelope[dto.CommandResult],response_model_exclude_unset=True)
    def connector_command(request:Request,connector_id:str,body:ConnectorCommand):
        who=actor(request,'session.control',True)
        if service.official is None: raise DomainError('DEPENDENCY_UNAVAILABLE','Official coordinator unavailable',503)
        data=body.model_dump(); data['session_id']=body.args.session_id
        return command(request,who,data,lambda c:service.official.connector_command(c,who['actor_id'],connector_id,data))
    @app.post('/api/v1/display-tickets',response_model=Envelope[dto.TicketResult],response_model_exclude_unset=True)
    def ticket_create(request:Request,body:Ticket):
        who=actor(request,'display.manage',True); data=body.model_dump(); return command(request,who,data,lambda c:service.ticket(c,who['actor_id'],data))
    @app.post('/api/v1/display-sessions',response_model=Envelope[dto.DisplaySession],response_model_exclude_unset=True)
    def exchange(request:Request,body:Exchange):
        token,data=service.exchange(body.ticket); response=JSONResponse(ok(request,auth.display_dto(data))); cookie(response,'lw_display',token); return response
    @app.get('/api/v1/display-sessions/current',response_model=Envelope[dto.DisplaySession],response_model_exclude_unset=True)
    def display_current(request:Request): return ok(request,auth.display_dto(auth.display(request.cookies.get('lw_display'))))
    @app.delete('/api/v1/display-tickets/{ticket_id}',response_model=Envelope[dto.Revoked],response_model_exclude_unset=True)
    def revoke(request:Request,ticket_id:str):
        who=actor(request,'display.manage',True)
        def operation(c):
            ticket=c.execute(sa.select(s.tickets).where(s.tickets.c.id==ticket_id)).mappings().first()
            if not ticket: raise DomainError('RESOURCE_NOT_FOUND','Ticket not found',404)
            service.session(c,ticket['session_id'],who['actor_id'])
            c.execute(s.tickets.update().where(s.tickets.c.id==ticket_id).values(revoked=True)); c.execute(s.displays.update().where(s.displays.c.ticket_id==ticket_id).values(revoked=True)); service.touch(c,ticket['session_id']); return {'revoked':True}
        return command(request,who,{},operation)
    @app.post('/api/v1/rounds',response_model=Envelope[dto.RoundCreated],response_model_exclude_unset=True)
    def round_create(request:Request,body:RoundCreate):
        who=actor(request,'session.control',True); data=body.model_dump(exclude_none=True); return command(request,who,data,lambda c:service.create_round(c,who['actor_id'],data))
    @app.post('/api/v1/rounds/{round_id}/commands',response_model=Envelope[dto.CommandResult],response_model_exclude_unset=True)
    def round_command(request:Request,round_id:str,body:Command):
        who=actor(request,'session.control',True); data=body.model_dump(); return command(request,who,data,lambda c:service.round_command(c,who['actor_id'],round_id,data))
    @app.get('/api/v1/rounds/{round_id}/private-state',response_model=Envelope[dto.PrivateRound],response_model_exclude_unset=True)
    def private_state(request:Request,round_id:str):
        who=actor(request,'session.control')
        with db.read_snapshot() as c:
            _,row=service.round(c,round_id,who['actor_id'],lock=False); return ok(request,service.private_round(row,c))
    @app.post('/api/v1/rehearsal/guesses',response_model=Envelope[dto.RehearsalResult],response_model_exclude_unset=True)
    def guess(request:Request,body:Guess):
        who=actor(request,'session.control',True); data=body.model_dump()
        result=service.submit_guess(who['actor_id'],request.headers.get('idempotency-key'),data)
        return ok(request,{'accepted':result['status'] in {'queued','scored'},'reason':result['message'] or None,'guess_id':result['feedback_id']})
    @app.post('/api/v1/display/receipts',response_model=Envelope[dto.ReceiptResult],response_model_exclude_unset=True)
    def receipt(request:Request,body:Receipt):
        display=auth.display(request.cookies.get('lw_display'))
        import hmac
        if not hmac.compare_digest(request.headers.get('x-csrf-token',''),display['csrf']): raise DomainError('FORBIDDEN','Display CSRF required',403)
        try: parsed=parse_receipt(body.model_dump())
        except DomainError: raise
        except Exception: raise DomainError('INPUT_INVALID','Invalid receipt body',422) from None
        return ok(request,realtime.receipt(display['id'],parsed))
    @app.get('/api/v1/studio/sessions/{session_id}/snapshot',response_model=Envelope[dto.StudioSnapshot],response_model_exclude_unset=True)
    def studio_snapshot(request:Request,session_id:str):
        who=actor(request,'session.control'); extended=extension(request)
        with db.read_snapshot() as c:
            row=service.session(c,session_id,who['actor_id'],lock=False); public=service.snapshot(c,session_id,who['actor_id'],lock=False)
            active=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==session_id).order_by(s.rounds.c.created_at.desc()).limit(1)).mappings().first()
            ready=service.readiness.evaluate(c,row,who['permissions'])
            result={'schema_version':1,'contract_version':CONTRACT,'session_id':session_id,'mode':row['mode'],'run_purpose':row['run_purpose'],'as_of_seq':str(row['studio_seq']),'server_time':public['server_time'],'public_view':public,'private_round':service.private_round(active,c) if active else None,'session_rules':row['rules'],'next_rules':service.session_detail(c,row)['next_rules'],'readiness':ready if extended else service.readiness.c1(ready),'pending_commands':service.pending_commands(c,session_id)}
            if extended: result|={'be_extension_version':'LW-BE/1.1.0','next_selection':service.next_selection(c,session_id)}
            return ok(request,result)
    @app.get('/api/v1/commands/{command_id}',response_model=Envelope[dto.CommandResult],response_model_exclude_unset=True)
    def command_status(request:Request,command_id:str):
        who=actor(request)
        with db.read_snapshot() as c:
            for resolver in service.command_resolvers:
                resolved=resolver(c,command_id,who['actor_id'],who['permissions'])
                if resolved is not None: return ok(request,dto.validated(dto.CommandResult,resolved))
            row=c.execute(sa.select(s.commands).where(s.commands.c.id==command_id)).mappings().first()
            if not row:
                cached=c.execute(sa.select(s.idempotency).where(s.idempotency.c.response['command_id'].astext==command_id)).mappings().first()
                if not cached: raise DomainError('RESOURCE_NOT_FOUND','Command not found',404)
                path=cached['path']; required='puzzle.review' if path.startswith('/api/v1/puzzles/') else 'puzzle.edit' if path.startswith('/api/v1/puzzle-imports/') else 'replay.control' if path.startswith('/api/v1/replays/') else None
                if cached['actor_id']!=who['actor_id'] or required is None or required not in who['permissions']: raise DomainError('FORBIDDEN','Command permission required',403)
                if required=='replay.control': replay.job(c,who['actor_id'],path.split('/')[4])
                if required=='puzzle.edit': return ok(request,dto.validated(dto.CommandResult,wordbank.import_command_result(c,who['actor_id'],path.split('/')[4],cached['response'])))
                return ok(request,dto.validated(dto.CommandResult,cached['response']))
            if 'session.control' not in who['permissions']: raise DomainError('FORBIDDEN','Session permission required',403)
            service.authorize_session(c,row['session_id'],who['actor_id'])
            if row['actor_id'] not in {who['actor_id'],'system:selection'}: raise DomainError('FORBIDDEN','Command actor mismatch',403)
            response=row['response']
            if service.official is not None and isinstance(response.get('state'),dict) and response['state'].get('intent_id'):
                session=service.session(c,row['session_id'],who['actor_id'],lock=False)
                response=service.official.status(c,session,response['state']['intent_id'])
            return ok(request,dto.validated(dto.CommandResult,response))
    @app.get('/api/v1/display/sessions/{session_id}/snapshot',response_model=Envelope[dto.PublicSnapshot],response_model_exclude_unset=True)
    def display_snapshot(request:Request,session_id:str):
        who=auth.display(request.cookies.get('lw_display'))
        if who['session_id']!=session_id: raise DomainError('DISPLAY_UNAUTHORIZED','Display session scope mismatch',403)
        with db.read_snapshot() as c: return ok(request,service.snapshot(c,session_id,lock=False))
    install_protocol_schema(app)
    return app
