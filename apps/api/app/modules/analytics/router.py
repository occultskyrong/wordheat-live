"""Private analytics HTTP adapter; every request rechecks operator authorization."""
from contextlib import ExitStack
import uuid
import time
import anyio
from fastapi import APIRouter, Request
from starlette.responses import FileResponse
from app.errors import DomainError
from app.modules.analytics.service import AnalyticsService,stamp,moment

class ExportResponse(FileResponse):
    def __init__(self,path,stack,started,expires_at=None):
        self.export_path=path; self.export_stack=stack; self.export_started=started
        self.export_deadline=started+30
        if expires_at is not None: self.export_deadline=min(self.export_deadline,time.monotonic()+max(0,(stamp(expires_at)-moment()).total_seconds()))
        super().__init__(path,media_type='text/csv; charset=utf-8',filename='wordheat-analytics.csv',headers={'Content-Length':str(path.stat().st_size),'Cache-Control':'no-store'})
    async def __call__(self,scope,receive,send):
        try:
            with anyio.fail_after(max(0.001,self.export_deadline-time.monotonic())): await super().__call__(scope,receive,send)
        finally:
            self.export_path.unlink(missing_ok=True)
            self.export_stack.close()

def create_router(db,auth,service,cursor_key:bytes):
    analytics=service if isinstance(service,AnalyticsService) else AnalyticsService(db,cursor_key)
    router=APIRouter()
    def actor(request):
        if not request.cookies.get('lw_operator'): raise DomainError('FORBIDDEN','Diagnostics operator permission required',403)
        return auth.operator(request.cookies.get('lw_operator'),'diagnostics.read')['actor_id']
    def extension(request):
        if request.headers.get('x-be-extension-version')!='LW-BE/1.1.0':
            raise DomainError('BE_EXTENSION_VERSION_UNSUPPORTED','BE extension version required',409)
    def ok(request,data):
        return {'data':data,'request_id':getattr(request.state,'request_id',str(uuid.uuid4())),'contract_version':'LW-C1/1.0.0'}
    @router.get('/api/v1/live-sessions/{session_id}/analytics')
    def read(request:Request,session_id:str,snapshot_id:str|None=None,section:str|None=None,cursor:str|None=None,limit:int=50,format:str='json',fresh:bool=False):
        who=actor(request)
        with db.engine.connect() as c: analytics.authorize(c,session_id,who)
        if snapshot_id or section or cursor or fresh or format=='csv': extension(request)
        if format not in {'json','csv'} or section not in {None,'minutes'} or not 1<=limit<=100:
            raise DomainError('INPUT_INVALID','Invalid analytics query',422)
        if cursor and (not snapshot_id or not section): raise DomainError('ANALYTICS_CURSOR_INVALID','Cursor requires a frozen section',422)
        if (format=='csv' or section) and not snapshot_id: raise DomainError('INPUT_INVALID','snapshot_id required',422)
        if format=='csv':
            stack=ExitStack(); path=None
            try:
                payload=stack.enter_context(analytics.pinned(session_id,who,snapshot_id))
                stack.enter_context(analytics.export_slot(who))
                started=time.monotonic(); path=analytics.csv_file(payload)
                # Revalidate current authority and expiry after the complete-file preflight.
                actor(request)
                with db.engine.connect() as c: analytics.authorize(c,session_id,who)
                if stamp(payload['expires_at'])<=moment(): raise analytics.unavailable('EXPIRED')
                return ExportResponse(path,stack,started,payload['expires_at'])
            except BaseException:
                if path: path.unlink(missing_ok=True)
                stack.close(); raise
        if snapshot_id:
            with analytics.pinned(session_id,who,snapshot_id) as payload:
                data=analytics.page(payload,session_id,'minutes',limit,cursor) if section else analytics.aggregate(payload,session_id,limit)
        else:
            payload=analytics.snapshot(session_id,who,fresh=fresh)
            data=analytics.aggregate(payload,session_id,limit)
        return ok(request,data)
    @router.get('/api/v1/live-sessions/{session_id}/analytics/rounds')
    def rounds(request:Request,session_id:str,snapshot_id:str,cursor:str|None=None,limit:int=50):
        who=actor(request); extension(request)
        with analytics.pinned(session_id,who,snapshot_id) as payload:
            return ok(request,analytics.page(payload,session_id,'rounds',limit,cursor))
    return router
