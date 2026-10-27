"""Authenticated, private catalog and durable job HTTP endpoints."""
import base64
import sqlalchemy as sa
from fastapi import APIRouter, Request, UploadFile, File, Query
from app.errors import DomainError
from app.infrastructure.db import schema as s
from app.modules.wordbank.models import Draft,Patch,Evaluation,PuzzleCommand,ImportCommand,Envelope,JobResult,PuzzleResult,PuzzlePage
from app.dto import CommandResult
from app.modules.wordbank.service import puzzle_dto
from app.modules.wordbank.pagination import page as puzzle_page

def create_router(db,auth,wordbank):
    router=APIRouter()
    def actor(request,permission,mutation=False): return auth.operator(request.cookies.get('lw_operator'),permission,request.headers.get('x-csrf-token','') if mutation else None)
    def ok(request,data): return {'data':data,'request_id':request.state.request_id,'contract_version':'LW-C1/1.0.0'}
    def mutate(request,who,body,permission,operation,jid=None): return wordbank.mutate(who['actor_id'],request.method,request.url.path,request.headers.get('idempotency-key'),body,permission,operation,jid,request_id=request.state.request_id)
    def read_actor(request):
        who=actor(request,None)
        if not set(who['permissions'])&{'puzzle.edit','puzzle.review','scoring.evaluate'}: raise DomainError('FORBIDDEN','Wordbank permission required',403)
        return who

    @router.get('/api/v1/puzzles',response_model=Envelope[PuzzlePage])
    def puzzles(request:Request,page:int=Query(1,ge=1,le=100000,description='Legacy offset page; use next_cursor for stable continuation'),page_size:int=Query(25,ge=1,le=100),category_id:str|None=None,status:str|None=None,q:str|None=Query(None,max_length=80),all_versions:bool=False,difficulty:int|None=Query(None,ge=1,le=5),difficulty_min:int=Query(1,ge=1,le=5),difficulty_max:int=Query(5,ge=1,le=5),cursor:str|None=Query(None,max_length=4096)):
        who=read_actor(request)
        if difficulty_min>difficulty_max or (difficulty is not None and not difficulty_min<=difficulty<=difficulty_max): raise DomainError('INPUT_INVALID','Invalid difficulty range',422)
        filters={'category_id':category_id,'status':status,'q':q or None,'all_versions':all_versions,'difficulty':difficulty,'difficulty_min':difficulty_min,'difficulty_max':difficulty_max}
        with db.engine.connect().execution_options(isolation_level='REPEATABLE READ') as c, c.begin():
            return ok(request,puzzle_page(wordbank,c,who['actor_id'],filters,page_size,page,cursor))

    @router.post('/api/v1/puzzles',response_model=Envelope[PuzzleResult])
    def create(request:Request,body:Draft):
        who=actor(request,'puzzle.edit',True); data=body.model_dump()
        return ok(request,mutate(request,who,data,'puzzle.edit',lambda c:wordbank.create(c,data)))

    @router.patch('/api/v1/puzzles/{puzzle_id}',response_model=Envelope[PuzzleResult])
    def patch(request:Request,puzzle_id:str,body:Patch):
        who=actor(request,'puzzle.edit',True); data=body.model_dump()
        return ok(request,mutate(request,who,data,'puzzle.edit',lambda c:wordbank.patch(c,puzzle_id,data)))

    @router.post('/api/v1/puzzle-imports',status_code=202,response_model=Envelope[JobResult],response_model_exclude_unset=True)
    async def upload(request:Request,file:UploadFile=File(...)):
        who=actor(request,'puzzle.edit',True)
        raw=await file.read(2*1024*1024+1); await file.close()
        if len(raw)>2*1024*1024: raise DomainError('PAYLOAD_TOO_LARGE','CSV limit is 2 MiB',413)
        filename=(file.filename or 'upload.csv').replace('\\','/').rsplit('/',1)[-1][:160] or 'upload.csv'
        data={'csv_base64':base64.b64encode(raw).decode(),'filename':filename}
        return ok(request,mutate(request,who,data,'puzzle.edit',lambda c:wordbank.enqueue(c,who['actor_id'],'import',data)))

    @router.get('/api/v1/puzzle-imports/{job_id}',response_model=Envelope[JobResult],response_model_exclude_unset=True)
    def import_get(request:Request,job_id:str):
        who=actor(request,'puzzle.edit')
        with db.engine.connect() as c:
            row=wordbank.job(c,who['actor_id'],job_id)
            if row['kind']!='import': raise DomainError('RESOURCE_NOT_FOUND','Import job not found',404)
            return ok(request,wordbank.job_dto(row))

    @router.post('/api/v1/puzzle-imports/{job_id}/commands',response_model=Envelope[CommandResult],response_model_exclude_unset=True)
    def import_command(request:Request,job_id:str,body:ImportCommand):
        who=actor(request,'puzzle.edit',True); data=body.model_dump()
        return ok(request,mutate(request,who,data,'puzzle.edit',lambda c:wordbank.import_command(c,who['actor_id'],job_id,data),job_id))

    @router.post('/api/v1/scoring-evaluations',status_code=202,response_model=Envelope[JobResult],response_model_exclude_unset=True)
    def evaluation(request:Request,body:Evaluation):
        who=actor(request,'scoring.evaluate',True); data=body.model_dump()
        return ok(request,mutate(request,who,data,'scoring.evaluate',lambda c:wordbank.enqueue(c,who['actor_id'],'evaluation',data)))

    @router.get('/api/v1/scoring-evaluations/{job_id}',response_model=Envelope[JobResult],response_model_exclude_unset=True)
    def evaluation_get(request:Request,job_id:str):
        who=actor(request,None); review='puzzle.review' in who['permissions']
        if not review and 'scoring.evaluate' not in who['permissions']: raise DomainError('FORBIDDEN','Evaluation permission required',403)
        with db.engine.connect() as c:
            row=wordbank.job(c,who['actor_id'],job_id,review)
            if row['kind']!='evaluation': raise DomainError('RESOURCE_NOT_FOUND','Evaluation not found',404)
            return ok(request,wordbank.job_dto(row))

    @router.post('/api/v1/puzzles/{puzzle_id}/commands',response_model=Envelope[CommandResult|JobResult],response_model_exclude_unset=True,responses={202:{'model':Envelope[JobResult],'description':'Persisted evaluation job'}})
    def puzzle_command(request:Request,puzzle_id:str,body:PuzzleCommand):
        permission='scoring.evaluate' if body.command=='evaluate' else 'puzzle.review'
        who=actor(request,permission,True); data=body.model_dump()
        result=mutate(request,who,data,permission,lambda c:wordbank.puzzle_command(c,who['actor_id'],puzzle_id,data))
        from fastapi.responses import JSONResponse
        return JSONResponse(ok(request,result),status_code=202 if body.command=='evaluate' else 200)
    return router
