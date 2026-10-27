from fastapi import APIRouter,Request
from app.modules.replays.models import ReplayCreate
from app.modules.wordbank.models import ReplayCommand,Envelope,JobResult
from app.dto import CommandResult

def create_router(auth,replay):
    router=APIRouter()
    def who(request,mutation=False): return auth.operator(request.cookies.get('lw_operator'),'replay.control',request.headers.get('x-csrf-token','') if mutation else None)
    def ok(request,data): return {'data':data,'request_id':request.state.request_id,'contract_version':'LW-C1/1.0.0'}
    @router.post('/api/v1/replays',status_code=202,response_model=Envelope[JobResult],response_model_exclude_unset=True)
    def create(request:Request,body:ReplayCreate):
        actor=who(request,True); data=body.model_dump()
        return ok(request,replay.mutate(actor['actor_id'],request.method,request.url.path,request.headers.get('idempotency-key'),data,lambda c:replay.create(c,actor['actor_id'],data)))
    @router.get('/api/v1/replays/{job_id}',response_model=Envelope[JobResult],response_model_exclude_unset=True)
    def status(request:Request,job_id:str):
        actor=who(request)
        with replay.db.engine.connect() as c: return ok(request,replay.dto(replay.job(c,actor['actor_id'],job_id)))
    @router.post('/api/v1/replays/{job_id}/commands',response_model=Envelope[CommandResult],response_model_exclude_unset=True)
    def command(request:Request,job_id:str,body:ReplayCommand):
        actor=who(request,True); data=body.model_dump()
        return ok(request,replay.mutate(actor['actor_id'],request.method,request.url.path,request.headers.get('idempotency-key'),data,lambda c:replay.command(c,actor['actor_id'],job_id,data),job_id))
    return router
