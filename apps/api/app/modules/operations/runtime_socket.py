"""Mac/Linux local native control. OS peer credentials are never JSON claims.

Darwin constants below are from the system SDK sys/un.h, sys/sysctl.h and
libproc.h. Only argv entries are parsed; process environment bytes are discarded.
This development port is not Windows SID/DPAPI/UAC equivalence.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import struct
import sys
import threading
from dataclasses import dataclass,field
import time
import sqlalchemy as sa
from app.errors import DomainError
from app.infrastructure.db import schema as s
from app.modules.auth.service import verify_password
from app.modules.operations.runtime_artifacts import sha_file,unique

MAX_FRAME=64*1024
PROTOCOL='wordheat-native-control/1'

def denied(code='NATIVE_PEER_UNVERIFIED'):raise DomainError(code,'Local native control identity or request rejected',403)

def peer_identity(channel):
    if sys.platform=='darwin':
        libc=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
        uid=ctypes.c_uint();gid=ctypes.c_uint()
        libc.getpeereid.argtypes=[ctypes.c_int,ctypes.POINTER(ctypes.c_uint),ctypes.POINTER(ctypes.c_uint)]
        libc.getpeereid.restype=ctypes.c_int
        if libc.getpeereid(channel.fileno(),ctypes.byref(uid),ctypes.byref(gid))!=0:denied()
        pid=struct.unpack('i',channel.getsockopt(0,0x002,4))[0]  # SOL_LOCAL / LOCAL_PEERPID
        if pid<=0:denied()
        return {'uid':uid.value,'gid':gid.value,'pid':pid}
    if sys.platform=='linux':
        pid,uid,gid=struct.unpack('3i',channel.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        return {'uid':uid,'gid':gid,'pid':pid}
    denied('WINDOWS_NATIVE_REQUIRED')

def process_identity(pid):
    if sys.platform=='darwin':
        libc=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
        path=ctypes.create_string_buffer(4096)
        libc.proc_pidpath.argtypes=[ctypes.c_int,ctypes.c_void_p,ctypes.c_uint32]
        libc.proc_pidpath.restype=ctypes.c_int
        if libc.proc_pidpath(pid,path,len(path))<=0:denied()
        mib=(ctypes.c_int*3)(1,49,pid)  # CTL_KERN/KERN_PROCARGS2
        raw=ctypes.create_string_buffer(MAX_FRAME);size=ctypes.c_size_t(MAX_FRAME)
        libc.sysctl.argtypes=[ctypes.POINTER(ctypes.c_int),ctypes.c_uint,ctypes.c_void_p,ctypes.POINTER(ctypes.c_size_t),ctypes.c_void_p,ctypes.c_size_t]
        libc.sysctl.restype=ctypes.c_int
        try:
            if libc.sysctl(mib,3,raw,ctypes.byref(size),None,0)!=0:denied()
            data=raw.raw[:size.value];argc=struct.unpack_from('i',data)[0]
            if not 1<=argc<=256:denied()
            pos=data.index(b'\0',4)+1
            while pos<len(data) and data[pos]==0:pos+=1
            argv=[]
            for _ in range(argc):
                end=data.index(b'\0',pos);argv.append(data[pos:end].decode('utf-8'));pos=end+1
            executable=Path(path.value.decode()).resolve(strict=True)
        except (ValueError,UnicodeError,OSError):denied()
        finally:ctypes.memset(raw,0,len(raw))
    elif sys.platform=='linux':
        executable=Path(os.readlink('/proc/'+str(pid)+'/exe')).resolve(strict=True)
        with open('/proc/'+str(pid)+'/cmdline','rb') as stream:data=stream.read(MAX_FRAME+1)
        if len(data)>MAX_FRAME:denied()
        argv=[x.decode('utf-8') for x in data.split(b'\0') if x]
    else:denied('WINDOWS_NATIVE_REQUIRED')
    return {'executable':str(executable),'executable_sha256':sha_file(executable),'argv':argv}

def program_matches(identity,source_root,role):
    root=Path(source_root).resolve(strict=True);args=identity['argv']
    if '-c' in args:return False
    entry=root/('apps/api/app/runtime_entry.py' if role=='api' else 'apps/api/app/cli.py')
    try:
        direct_index=next((i for i in (1,2) if i<len(args) and not args[i].startswith('-') and Path(args[i]).is_absolute() and Path(args[i]).resolve()==entry),None)
        module=role in {'cli','serve'} and len(args)>2 and args[1:3]==['-m','app.cli']
        if not entry.is_file() or direct_index is None and not module:return False
        if role=='api':return direct_index is not None
        rest=args[3:] if module else args[direct_index+1:]
        while rest and rest[0].startswith('-'):
            if rest[0]=='--root' and len(rest)>1:rest=rest[2:]
            elif rest[0].startswith('--root='):rest=rest[1:]
            else:return False
        return bool(rest) and rest[0]==('native-control' if role=='cli' else 'serve')
    except (OSError,ValueError):return False

def send_frame(channel,value):
    raw=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
    if len(raw)>MAX_FRAME:denied('NATIVE_FRAME_TOO_LARGE')
    channel.sendall(raw+b'\n')

def receive_frame(channel):
    raw=bytearray()
    while not raw.endswith(b'\n'):
        chunk=channel.recv(min(4096,MAX_FRAME+1-len(raw)))
        if not chunk:denied('NATIVE_CHANNEL_CLOSED')
        raw.extend(chunk)
        if len(raw)>MAX_FRAME or b'\n' in raw[:-1]:denied('NATIVE_FRAME_TOO_LARGE')
    try:
        value=json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:denied('NATIVE_FRAME_INVALID'))
        if not isinstance(value,dict):denied('NATIVE_FRAME_INVALID')
        return value
    except (ValueError,TypeError,RecursionError):denied('NATIVE_FRAME_INVALID')

def private_directory(path):
    path=Path(path)
    if not path.is_absolute():denied('NATIVE_STORAGE_REJECTED')
    for parent in [*reversed(path.parents),path]:
        if parent.is_symlink():denied('NATIVE_STORAGE_REJECTED')
    path.mkdir(parents=True,mode=0o700,exist_ok=True)
    info=path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:denied('NATIVE_STORAGE_REJECTED')
    return path

@dataclass(frozen=True)
class NativeCallContext:
    peer_uid:int
    peer_pid:int
    actor_id:str
    installation_id:str
    boot_id:str
    expires_ns:int
    _issuer:object=field(repr=False)

class NativeControlServer:
    def __init__(self,controller,root,source_root,*,program_entry=None,expected_peer_uid=None,configuration_root=None):
        self.controller=controller;self.root=Path(root);self.source_root=Path(source_root).resolve(strict=True)
        self.program_entry=Path(program_entry or sys.argv[0]).resolve(strict=True)
        self.configuration_root=Path(configuration_root).resolve(strict=True) if configuration_root is not None else self.root.parent.parent.resolve(strict=True) if self.root.name=='runtime' and self.root.parent.name=='.local' else None
        self.expected_peer_uid=os.getuid() if expected_peer_uid is None else expected_peer_uid
        self.socket_path=self.root/'native.sock';self.registration_path=self.root/'native.json'
        self.listener=None;self.thread=None;self.stopping=threading.Event();self.limit=threading.BoundedSemaphore(4)
        self.connections=set();self.lock=threading.Lock();self.last_error=None;self.failures=[];self._native_issuer=object()

    def validate_native_context(self,value,actor_id):
        return isinstance(value,NativeCallContext) and value._issuer is self._native_issuer and value.actor_id==actor_id and value.installation_id==self.controller.installation_id and value.boot_id==self.controller.boot_id and value.peer_uid==self.expected_peer_uid and value.peer_pid>0 and time.monotonic_ns()<value.expires_ns

    def start(self):
        if getattr(self.controller,'pairing_bridge',None) is not None:self.controller.pairing_bridge.bind_native_validator(self.validate_native_context)
        private_directory(self.root)
        if self.socket_path.exists() or self.socket_path.is_symlink():
            if self.socket_path.is_symlink() or not self.registration_path.is_file():denied('NATIVE_CONTROL_ALREADY_RUNNING')
            info=self.socket_path.lstat()
            try:old=json.loads(self.registration_path.read_bytes())
            except (ValueError,OSError):denied('NATIVE_STORAGE_REJECTED')
            if info.st_uid!=os.getuid() or not stat.S_ISSOCK(info.st_mode) or old.get('installation_id')!=self.controller.installation_id or old.get('socket_inode')!=info.st_ino:denied('NATIVE_STORAGE_REJECTED')
            try:os.kill(old['pid'],0)
            except ProcessLookupError:self.socket_path.unlink()
            else:denied('NATIVE_CONTROL_ALREADY_RUNNING')
        if len(os.fsencode(self.socket_path))>100:raise DomainError('NATIVE_SOCKET_PATH_TOO_LONG','Registered native socket path must use a short private directory',503)
        self.listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.listener.bind(str(self.socket_path));os.chmod(self.socket_path,0o600);self.listener.listen(4);self.listener.settimeout(.1)
        info=self.socket_path.stat();self.inode=info.st_ino
        process=process_identity(os.getpid())
        self.approved_interpreter_sha=process['executable_sha256']
        cli=self.source_root/'apps/api/app/cli.py'
        self.approved_cli_sha=sha_file(cli) if cli.is_file() else None
        registration={'protocol':PROTOCOL,'uid':os.getuid(),'pid':os.getpid(),'boot_id':self.controller.boot_id,'installation_id':self.controller.installation_id,'socket_inode':self.inode,'source_root':str(self.source_root),'configuration_root':str(self.configuration_root) if self.configuration_root else None,'program_entry':str(self.program_entry),'program_sha256':sha_file(self.program_entry),'executable':process['executable'],'executable_sha256':process['executable_sha256']}
        temporary=self.root/('native-'+secrets.token_hex(8)+'.json')
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as stream:json.dump(registration,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,self.registration_path)
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()

    def run(self):
        while not self.stopping.is_set():
            try:channel,_=self.listener.accept()
            except (socket.timeout,TimeoutError):continue
            except OSError:break
            if not self.limit.acquire(blocking=False):channel.close();continue
            with self.lock:self.connections.add(channel)
            threading.Thread(target=self.connection,args=(channel,),daemon=True).start()

    def authenticate(self,username,password):
        if not isinstance(username,str) or not isinstance(password,str) or not 1<=len(username)<=80 or not 1<=len(password)<=256:denied('AUTH_REQUIRED')
        with self.controller.db.engine.begin() as c:
            c.execute(sa.text("SET LOCAL statement_timeout='2500ms'"))
            actor=c.execute(sa.select(s.operators).where(s.operators.c.username==username)).mappings().first()
        if not actor or actor['disabled'] or not verify_password(password,actor['password_hash']):denied('AUTH_REQUIRED')
        return actor['id']

    def connection(self,channel):
        try:
            channel.settimeout(5)
            peer=peer_identity(channel)
            if peer['uid']!=self.expected_peer_uid or peer['pid']<=0:denied('NATIVE_PEER_UNAUTHORIZED')
            identity=process_identity(peer['pid'])
            if identity['executable_sha256']!=self.approved_interpreter_sha or self.approved_cli_sha is None or sha_file(self.source_root/'apps/api/app/cli.py')!=self.approved_cli_sha or not program_matches(identity,self.source_root,'cli'):denied('NATIVE_PEER_PROGRAM_REJECTED')
            challenge=secrets.token_hex(32)
            send_frame(channel,{'protocol':PROTOCOL,'boot_id':self.controller.boot_id,'installation_id':self.controller.installation_id,'challenge':challenge})
            packet=receive_frame(channel)
            if set(packet)!={'action','challenge','username','password'} or packet['action']!='auth' or packet['challenge']!=challenge:denied('NATIVE_AUTH_INVALID')
            with self.lock:
                self.failures=[t for t in self.failures if time.monotonic()-t<60]
                if len(self.failures)>=10:denied('NATIVE_AUTH_RATE_LIMITED')
            try:actor_id=self.authenticate(packet['username'],packet['password'])
            except DomainError:
                with self.lock:self.failures.append(time.monotonic())
                raise
            packet.clear();send_frame(channel,{'authenticated':True})
            request=receive_frame(channel)
            if set(request)!={'action','payload'} or not isinstance(request['payload'],dict):denied('NATIVE_FRAME_INVALID')
            context=NativeCallContext(peer['uid'],peer['pid'],actor_id,self.controller.installation_id,self.controller.boot_id,time.monotonic_ns()+10_000_000_000,self._native_issuer)
            data=self.dispatch(actor_id,request['action'],request['payload'],native_context=context)
            send_frame(channel,{'data':data})
        except DomainError as error:
            self.last_error=error.code
            try:send_frame(channel,{'error':{'code':error.code,'message':error.message}})
            except Exception:pass
        except Exception:
            self.last_error='NATIVE_CONTROL_FAILED'
            try:send_frame(channel,{'error':{'code':'NATIVE_CONTROL_FAILED','message':'Private native control failed'}})
            except Exception:pass
        finally:
            with self.lock:self.connections.discard(channel)
            channel.close();self.limit.release()

    def dispatch(self,actor_id,action,payload,*,native_context=None):
        controller=self.controller;port=controller.native_control_port()
        with controller.db.engine.connect() as c:
            actor=c.execute(sa.select(s.operators).where(s.operators.c.id==actor_id)).mappings().one()
            if actor['disabled']:denied('AUTH_REQUIRED')
            if action!='status':controller.authorize(c,actor_id)
            if action not in {'status','request','device.prepare','device.confirm','pair.prepare','pair.confirm'} and controller.native_port is None:raise DomainError('NATIVE_CONTROL_UNAVAILABLE','Registered native maintenance port is not configured',503)
            if action not in {'status','request','trust.prepare','device.prepare','device.confirm','pair.prepare','pair.confirm'}:
                if not isinstance(payload.get('command_id'),str):denied('NATIVE_FRAME_INVALID')
                controller.lookup_command(c,payload['command_id'],actor_id,actor['permissions'])
        if action in {'device.prepare','device.confirm','pair.prepare','pair.confirm'}:
            bridge=getattr(controller,'pairing_bridge',None)
            if bridge is None or not self.validate_native_context(native_context,actor_id):raise DomainError('NATIVE_CONTROL_UNAVAILABLE','Registered native pairing bridge unavailable',503)
            if action=='device.prepare':
                if payload:denied('NATIVE_FRAME_INVALID')
                return bridge.prepare_device_key(actor_id,native_context=native_context)
            if action=='pair.prepare':
                if set(payload)!={'source_id'}:denied('NATIVE_FRAME_INVALID')
                return bridge.prepare_pair(actor_id,payload['source_id'],native_context=native_context)
            required={'command_id','fingerprint','nonce','installation_id'}|({'launch_token'} if action=='pair.confirm' else set())
            if set(payload)!=required:denied('NATIVE_FRAME_INVALID')
            if action=='device.confirm':return bridge.confirm_device_key(actor_id,payload['command_id'],payload['fingerprint'],payload['nonce'],payload['installation_id'],native_context=native_context)
            return bridge.confirm_pair(actor_id,payload['command_id'],payload['fingerprint'],payload['nonce'],payload['installation_id'],payload['launch_token'],native_context=native_context)
        if action=='status':
            if payload:denied('NATIVE_FRAME_INVALID')
            with controller.db.engine.begin() as c:
                readiness=controller.core.readiness.evaluate(c,None,actor['permissions'])
                return controller.status(c,readiness,extended=True)
        if action=='request':
            if set(payload)-{'command','operation','reason'}:denied('NATIVE_FRAME_INVALID')
            with controller.db.engine.connect() as c:version=c.execute(sa.select(__import__('app.modules.operations.runtime_models',fromlist=['instances']).instances.c.version).where(__import__('app.modules.operations.runtime_models',fromlist=['instances']).instances.c.installation_id==controller.installation_id)).scalar_one()
            args={'reason':payload.get('reason','Native operator request')}
            if 'operation' in payload:args['operation']=payload['operation']
            return controller.command(actor_id,secrets.token_urlsafe(24),{'command':payload.get('command','request_maintenance'),'expected_version':version,'args':args})
        if action=='prepare':
            if set(payload)-{'command_id','artifact_id','target_id','allow_data_loss'}:denied('NATIVE_FRAME_INVALID')
            artifact=payload.get('artifact_id')
            if artifact and artifact not in controller.native_port.registry.artifacts:
                if not __import__('re').fullmatch('[A-Za-z0-9_-]{1,80}',artifact):denied('NATIVE_FRAME_INVALID')
                controller.native_port.registry.register(artifact,controller.native_port.registry.root/(artifact+'.json'))
            return port.prepare(payload['command_id'],artifact,target_id=payload.get('target_id'),allow_data_loss=payload.get('allow_data_loss',False))
        if action in {'confirm','trust.confirm'}:
            if set(payload)!={'command_id','fingerprint','nonce','installation_id'}:denied('NATIVE_FRAME_INVALID')
            fn=port.confirm if action=='confirm' else port.confirm_key_import
            return fn(payload['command_id'],payload['fingerprint'],payload['nonce'],payload['installation_id'])
        if action=='cancel':
            if set(payload)!={'command_id'}:denied('NATIVE_FRAME_INVALID')
            return port.cancel(payload['command_id'])
        if action=='recover':
            if set(payload)!={'command_id'}:denied('NATIVE_FRAME_INVALID')
            return port.recover(payload['command_id'])
        if action=='trust.prepare':
            if set(payload)!={'source_id'} or not __import__('re').fullmatch('[A-Za-z0-9_-]{1,80}',payload['source_id']):denied('NATIVE_FRAME_INVALID')
            registry=controller.native_port.registry
            if payload['source_id'] not in registry.controls:registry.register_control(payload['source_id'],registry.control_root/(payload['source_id']+'.json'))
            return port.prepare_key_import(actor_id,payload['source_id'])
        denied('NATIVE_ACTION_REJECTED')

    def stop(self):
        self.stopping.set()
        if self.listener:self.listener.close()
        with self.lock:channels=list(self.connections)
        for channel in channels:
            try:channel.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        if self.thread:self.thread.join(timeout=1)
        if self.socket_path.exists() and self.socket_path.lstat().st_ino==getattr(self,'inode',None):self.socket_path.unlink()
        if self.registration_path.exists():
            try:registered=json.loads(self.registration_path.read_bytes())
            except (ValueError,OSError):registered={}
            if registered.get('pid')==os.getpid() and registered.get('boot_id')==self.controller.boot_id:self.registration_path.unlink()

class NativeControlClient:
    def __init__(self,root,source_root,*,configuration_root=None):self.root=Path(root);self.source_root=Path(source_root).resolve(strict=True);self.configuration_root=Path(configuration_root).resolve(strict=True) if configuration_root else None;self.channel=None
    def connect(self):
        private_directory(self.root)
        record=self.root/'native.json';path=self.root/'native.sock'
        if record.is_symlink() or path.is_symlink() or record.stat().st_uid!=os.getuid() or record.stat().st_mode&0o077 or record.stat().st_size>8192 or path.stat().st_uid!=os.getuid() or path.stat().st_mode&0o077:denied('NATIVE_STORAGE_REJECTED')
        registration=json.loads(record.read_bytes(),object_pairs_hook=unique)
        if registration.get('protocol')!=PROTOCOL or self.configuration_root is not None and registration.get('configuration_root')!=str(self.configuration_root) or registration.get('source_root')!=str(self.source_root) or registration.get('socket_inode')!=path.stat().st_ino:denied('NATIVE_PEER_UNVERIFIED')
        channel=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);channel.settimeout(5)
        try:
            channel.connect(str(path));peer=peer_identity(channel);identity=process_identity(peer['pid'])
            entry=Path(registration['program_entry']).resolve(strict=True)
            expected_entries={self.source_root/'apps/api/app/runtime_entry.py',self.source_root/'apps/api/app/cli.py'}
            if peer['uid']!=os.getuid() or peer['uid']!=registration['uid'] or peer['pid']!=registration['pid'] or identity['executable']!=registration['executable'] or identity['executable_sha256']!=registration['executable_sha256'] or identity['executable_sha256']!=sha_file(Path(sys.executable).resolve()) or entry not in expected_entries or sha_file(entry)!=registration['program_sha256'] or not program_matches(identity,self.source_root,'api' if entry==self.source_root/'apps/api/app/runtime_entry.py' else 'serve'):denied('NATIVE_PEER_UNVERIFIED')
            hello=receive_frame(channel)
            if hello.get('protocol')!=PROTOCOL or hello.get('boot_id')!=registration['boot_id'] or hello.get('installation_id')!=registration['installation_id']:denied('NATIVE_PEER_UNVERIFIED')
            self.channel=channel;self.hello=hello;return self
        except BaseException:channel.close();raise
    def call(self,username,password,action,payload):
        try:
            send_frame(self.channel,{'action':'auth','challenge':self.hello['challenge'],'username':username,'password':password})
            auth=receive_frame(self.channel)
            if auth.get('authenticated') is not True:denied(auth.get('error',{}).get('code','AUTH_REQUIRED'))
            send_frame(self.channel,{'action':action,'payload':payload})
            response=receive_frame(self.channel)
            if 'error' in response:raise DomainError(response['error']['code'],'Private native command rejected',409)
            return response['data']
        finally:self.channel.close();self.channel=None
