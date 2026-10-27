"""C1 runtime mutation adapter. Native confirmation is deliberately not HTTP."""
from contextlib import asynccontextmanager
import asyncio
import uuid
import os
import sys
import socket
import json
import subprocess
import signal
import threading
from pathlib import Path
from typing import Literal
from fastapi import APIRouter,Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel,ConfigDict,Field
from app import dto
from app.errors import DomainError
from app.modules.wordbank.models import Envelope
from app.modules.operations.runtime_controller import RuntimeController

class Args(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    reason:str=Field(min_length=1,max_length=200)
    operation:Literal['backup_local','verify_local_backup','restore_new_instance','upgrade_app','switch_model']|None=None
class RuntimeCommand(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    command:Literal['stop','request_maintenance']
    expected_version:int=Field(ge=0)
    args:Args

def create_router(auth,controller,settings):
    router=APIRouter()
    @router.post('/api/v1/runtime/commands',response_model=Envelope[dto.CommandResult],response_model_exclude_unset=True,status_code=202)
    def command(request:Request,body:RuntimeCommand):
        if request.headers.get('host') not in settings.allowed_hosts or request.headers.get('origin') not in settings.allowed_origins:
            raise DomainError('FORBIDDEN','Exact local Host and Origin required',403)
        actor=auth.operator(request.cookies.get('lw_operator'),'maintenance.request',csrf=request.headers.get('x-csrf-token',''))
        result=controller.command(actor['actor_id'],request.headers.get('idempotency-key'),body.model_dump(exclude_none=True))
        result=dto.validated(dto.CommandResult,result)
        return JSONResponse({'data':result,'request_id':getattr(request.state,'request_id',str(uuid.uuid4())),'contract_version':'LW-C1/1.0.0'},status_code=202 if result['status']=='pending' else 200)
    return router

class LocalServerStop:
    """Stops only the Uvicorn process to which this object has been bound."""
    def __init__(self,reason_file=None):self.callback=None;self.reason_file=reason_file;self.watchdog=None;self.watchdog_channel=None
    def bind_exit(self,callback):
        self.callback=callback
        if os.name=='posix' and threading.current_thread() is threading.main_thread():signal.signal(signal.SIGUSR1,lambda signum,frame:os._exit(17))
    def arm_stop(self,command_id,boot_id,deadline_ns):
        if self.callback is None:raise DomainError('RUNTIME_STOP_UNAVAILABLE','Current server exit hook is not connected',503)
        if self.watchdog is not None:
            if self.watchdog.poll() is not None:raise DomainError('RUNTIME_STOP_UNAVAILABLE','Independent watchdog exited before stop completed',503)
            return {'authorized':True,'deadline_ns':deadline_ns}
        if os.name!='posix':raise DomainError('WINDOWS_NATIVE_REQUIRED','Native Windows Job watchdog required',503)
        parent,child=socket.socketpair()
        executable=Path(__file__).with_name('runtime_watchdog.py')
        self.watchdog=subprocess.Popen([sys.executable,'-I',str(executable),str(child.fileno())],pass_fds=(child.fileno(),),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True)
        child.close();parent.settimeout(3)
        parent.sendall((json.dumps({'parent_pid':os.getpid(),'command_id':command_id,'boot_id':boot_id,'deadline_ns':deadline_ns,'reason_file':str(self.reason_file) if self.reason_file else None})+'\n').encode())
        if parent.recv(16)!=b'ARMED\n':raise DomainError('RUNTIME_STOP_UNAVAILABLE','Independent local watchdog did not arm',503)
        self.watchdog_channel=parent
        return {'authorized':True,'deadline_ns':deadline_ns}
    def force_stop(self,command_id,boot_id,reason):
        if os.name=='posix':os.kill(os.getpid(),signal.SIGUSR1)
        raise DomainError('WINDOWS_NATIVE_REQUIRED','Native process owner must force stop',503)
    def prepare_stop(self,command_id,boot_id,timeout):
        if self.callback is None:raise DomainError('RUNTIME_STOP_UNAVAILABLE','Current server exit hook is not connected',503)
        return {'authorized':True,'owned_api_only':True}
    def finish_exit(self):self.callback()

class RuntimeWorker:
    def __init__(self,controller,interval_seconds=.1):
        if not 0<interval_seconds<=1:raise ValueError('Runtime control interval must be within one second')
        self.controller=controller;self.interval=interval_seconds;self.task=None;self.stopping=None;self.last_error=None
    async def start(self):
        if self.task is not None:return
        await asyncio.to_thread(self.controller.initialize)
        self.stopping=asyncio.Event();self.task=asyncio.create_task(self.run())
    async def run(self):
        while not self.stopping.is_set():
            try:
                await asyncio.to_thread(self.controller.tick);self.last_error=None
            except Exception:self.last_error='RUNTIME_CONTROL_FAILED'
            try:await asyncio.wait_for(self.stopping.wait(),timeout=self.interval)
            except TimeoutError:pass
    async def stop(self):
        if self.task is None:return
        self.stopping.set();await self.task;self.task=None

def bind_runtime(app,*,stop_port=None,native_port=None,platform_projection=None,boot_id=None,bundle_issuer=None,resource_issuer=None,control_importer=None,boot_context=None,native_root=None,source_root=None,program_entry=None,configuration_root=None,pairing_bridge=None):
    """Idempotent composition helper; root owns when and with which ports it is called."""
    existing=getattr(app.state,'runtime_controller',None)
    if existing:
        if stop_port is not None:existing.stop_port=stop_port
        if boot_id is not None:existing.boot_id=boot_id
        if boot_context is not None:existing.boot_context=boot_context
        if native_port is not None:existing.native_port=native_port
        if platform_projection is not None:existing.platform_projection=platform_projection
        if resource_issuer is not None:existing.resource_issuer=resource_issuer
        if bundle_issuer is not None:existing.bundle_issuer=bundle_issuer
        if control_importer is not None:existing.control_importer=control_importer
        if pairing_bridge is not None:existing.pairing_bridge=pairing_bridge
        return existing
    core=app.state.service
    controller=RuntimeController(core,stop_port=stop_port,native_port=native_port,platform_projection=platform_projection,boot_id=boot_id,bundle_issuer=bundle_issuer,resource_issuer=resource_issuer,control_importer=control_importer,boot_context=boot_context,pairing_bridge=pairing_bridge)
    core.runtime_guard=controller.require_running
    core.runtime_recovery_completed=controller.recovery_completed
    if not hasattr(core,'command_resolvers'):core.command_resolvers=[]
    core.command_resolvers.append(controller.lookup_command)
    if pairing_bridge is not None:core.command_resolvers.append(pairing_bridge.lookup_command)
    core.readiness.runtime_controller=controller
    app.state.runtime_controller=controller
    worker=RuntimeWorker(controller);app.state.runtime_worker=worker
    if native_root is not None and isinstance(stop_port,LocalServerStop):stop_port.reason_file=Path(native_root)/'local-stop-reason.json'
    app.include_router(create_router(app.state.auth,controller,app.state.database.settings))
    native_server=None
    if native_root is not None:
        if source_root is None:raise ValueError('Explicit native source root required')
        from app.modules.operations.runtime_socket import NativeControlServer
        native_server=NativeControlServer(controller,native_root,source_root,program_entry=program_entry,configuration_root=configuration_root)
        app.state.native_control_server=native_server
    original=app.router.lifespan_context
    @asynccontextmanager
    async def lifecycle(value):
        await asyncio.to_thread(app.state.database.verify_runtime)
        await asyncio.to_thread(controller.initialize)
        async with original(value):
            try:
                if native_server is not None:native_server.start()
                await worker.start()
                yield
            finally:
                if native_server is not None:native_server.stop()
                await worker.stop()
    app.router.lifespan_context=lifecycle
    return controller
