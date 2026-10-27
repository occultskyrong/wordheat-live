"""POSIX development host. This is NOT the Windows native Launcher.

Only API/scorer children are owned. No shell, installation, download, migration,
port-owner guessing, PostgreSQL control, or OBS control occurs here.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import select
import signal
import subprocess
import socket
import threading
import sys
import time

ACK_DOMAIN = b'wordheat-dev-bootstrap/1\0child-ready\0'
ENV_FIELDS = frozenset({'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'SYSTEMROOT'})
IDENTITY_FIELDS = frozenset({'pid','role','boot_id','manifest_sha256','challenge','endpoint'})

class BootstrapError(Exception): pass

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')

def require_posix(platform=None):
    if (platform or sys.platform) not in {'darwin','linux'}:
        raise BootstrapError('WINDOWS_NATIVE_REQUIRED')

def child_environment(parent=None):
    parent = os.environ if parent is None else parent
    env = {key:parent[key] for key in ENV_FIELDS if key in parent}
    env.update({'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','HF_HUB_DISABLE_TELEMETRY':'1',
                'TOKENIZERS_PARALLELISM':'false','PYTHONDONTWRITEBYTECODE':'1','OMP_NUM_THREADS':'2'})
    return env

def read_packet(fd, timeout=10.):
    deadline=time.monotonic()+timeout
    raw=bytearray()
    try:
        while True:
            remaining=deadline-time.monotonic()
            if remaining<=0 or not select.select([fd],[],[],remaining)[0]:
                raise BootstrapError('BOOTSTRAP_TIMEOUT')
            chunk=os.read(fd, min(4096,8193-len(raw)))
            if not chunk: break
            raw.extend(chunk)
            if len(raw)>8192: raise BootstrapError('BOOTSTRAP_INVALID')
        def unique(pairs):
            value={}
            for key,item in pairs:
                if key in value:raise ValueError()
                value[key]=item
            return value
        value=json.loads(raw,object_pairs_hook=unique)
        if not isinstance(value,dict):raise ValueError()
        return value
    except (OSError,ValueError,TypeError):raise BootstrapError('BOOTSTRAP_INVALID') from None
    finally:os.close(fd)

def receive_bootstrap(fd, role):
    try:
        boot=read_packet(fd)
        if not {'identity','parent_pid','secret'}<=set(boot) or set(boot)-{'identity','parent_pid','secret','control_fd','runtime_context'}:raise ValueError()
        if 'runtime_context' in boot and not isinstance(boot['runtime_context'],dict):raise ValueError()
        if 'control_fd' in boot and (role!='api' or type(boot['control_fd']) is not int or boot['control_fd']<3):raise ValueError()
        identity=boot['identity']
        if set(identity)!=IDENTITY_FIELDS:raise ValueError()
        if identity['pid']!=os.getpid() or boot['parent_pid']!=os.getppid() or identity['role']!=role:raise ValueError()
        if identity['endpoint']!={'api':'127.0.0.1:8765','scorer':'127.0.0.1:8124'}[role]:raise ValueError()
        if not all(isinstance(identity[k],str) and re.fullmatch('[0-9a-f]{64}',identity[k]) for k in ('boot_id','challenge','manifest_sha256')):raise ValueError()
        secret=base64.b64decode(boot['secret'],validate=True)
        if len(secret)!=32:raise ValueError()
        boot['secret']=secret
        return boot
    except (ValueError,TypeError,KeyError):raise BootstrapError('BOOTSTRAP_INVALID') from None

def make_ack(secret, identity):
    return {'identity':identity,'mac':hmac.new(secret,ACK_DOMAIN+canonical(identity),hashlib.sha256).hexdigest()}

def verify_ack(secret, packet, identity):
    try:
        return (set(packet)=={'identity','mac'} and packet['identity']==identity and
                hmac.compare_digest(packet['mac'],make_ack(secret,identity)['mac']))
    except (KeyError,TypeError,ValueError):return False

def send_ack(fd, boot):
    try:write_packet(fd,make_ack(boot['secret'],boot['identity']))
    finally:os.close(fd)

def write_packet(fd, value):
    raw=canonical(value)
    if len(raw)>8192:raise BootstrapError('BOOTSTRAP_INVALID')
    while raw:
        count=os.write(fd,raw)
        if count<=0:raise BootstrapError('BOOTSTRAP_WRITE_FAILED')
        raw=raw[count:]

def spawn_child(role, interpreter, cwd, command, secret, boot_id, manifest_sha256, endpoint, owned, *, control=False, runtime_context=None,diagnostic=False):
    """command is fixed by main; test injection is only a Python API, never HTTP."""
    incoming,writer=os.pipe();reader,outgoing=os.pipe()
    process=None; control_parent=None;control_child=None
    if control: control_parent,control_child=socket.socketpair()
    try:
        process=subprocess.Popen([str(interpreter),'-I',*command,str(incoming),str(outgoing)],start_new_session=True,
            cwd=str(cwd),env=child_environment(),shell=False,close_fds=True,
            pass_fds=(incoming,outgoing)+((control_child.fileno(),) if control_child else ()),stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,stderr=subprocess.PIPE if diagnostic else subprocess.DEVNULL)
        process._wordheat_owned_session=True
        owned.append(process)
        identity={'pid':process.pid,'role':role,'boot_id':boot_id,'manifest_sha256':manifest_sha256,
                  'challenge':secrets.token_hex(32),'endpoint':endpoint}
        boot={'identity':identity,'parent_pid':os.getpid(),'secret':base64.b64encode(secret).decode('ascii')}
        if control_child:boot['control_fd']=control_child.fileno()
        if role=='api' and runtime_context is not None:boot['runtime_context']=runtime_context|{'api_pid':process.pid,'parent_pid':os.getpid(),'boot_id':boot_id}
        write_packet(writer,boot)
        if control_parent:boot['_control_socket']=control_parent
        return process,reader,boot
    except BaseException:
        os.close(reader)
        if control_parent:control_parent.close()
        raise
    finally:
        os.close(incoming);os.close(outgoing);os.close(writer)
        if control_child:control_child.close()

def emit_child_diagnostic(process):
    stream=getattr(process,'stderr',None)
    if stream is None:return
    try:
        if not select.select([stream],[],[],.05)[0]:return
        raw=os.read(stream.fileno(),8192)
        for line in raw.decode('utf-8','replace').splitlines():
            if re.fullmatch('(API|SCORER)_ENTRY_FAILED:[A-Z_]{1,32}:[A-Z0-9_]{1,64}',line):print(line,file=sys.stderr,flush=True)
    except Exception:pass

def await_ack(process, fd, boot, secret, timeout):
    packet=read_packet(fd,timeout)
    if process.poll() is not None or not verify_ack(secret,packet,boot['identity']):
        raise BootstrapError('CHILD_IDENTITY_FAILED')

def stop_children(owned,grace=5.):
    # API first, then scorer; each gets stop -> wait -> kill -> wait.
    for process in reversed(owned):
        if process.poll() is not None:continue
        owned_signal(process) if 'owned_signal' in globals() else process.terminate()
        try:process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:process.kill()
            except ProcessLookupError:pass
            process.wait(timeout=grace)

CONTROL_DOMAIN=b'wordheat-dev-host/1\0api-stop-intent\0'
CONTROL_ACK_DOMAIN=b'wordheat-dev-host/1\0parent-stop-ack\0'

def make_stop_intent(secret,identity,command_id,nonce,*,phase=None,deadline_ns=None):
    body={'intent':'stop','identity':identity,'command_id':command_id,'nonce':nonce}
    if phase is not None:body.update(phase=phase,deadline_ns=deadline_ns)
    return body|{'mac':hmac.new(secret,CONTROL_DOMAIN+canonical(body),hashlib.sha256).hexdigest()}

def verify_stop_intent(secret,packet,identity):
    try:
        if set(packet) not in ({'intent','identity','command_id','nonce','mac'},{'intent','identity','command_id','nonce','mac','phase','deadline_ns'}) or packet['intent']!='stop' or identity['role']!='api' or packet['identity']!=identity:return False
        if not all(isinstance(packet[k],str) and 1<=len(packet[k])<=128 for k in ('command_id','nonce')):return False
        if 'phase' in packet and (packet['phase'] not in {'arm','complete','force'} or type(packet['deadline_ns']) is not int):return False
        expected=make_stop_intent(secret,identity,packet['command_id'],packet['nonce'],phase=packet.get('phase'),deadline_ns=packet.get('deadline_ns'))
        return hmac.compare_digest(expected['mac'],packet['mac'])
    except (ValueError,TypeError,KeyError):return False

def socket_packet(channel,value=None,timeout=15):
    channel.settimeout(timeout)
    if value is not None:
        raw=canonical(value)
        if len(raw)>8192:raise BootstrapError('CONTROL_PACKET_INVALID')
        channel.sendall(raw+b'\n');return
    raw=bytearray()
    while b'\n' not in raw:
        chunk=channel.recv(8193-len(raw))
        if not chunk:raise BootstrapError('HOST_CHILD_EXITED')
        if len(raw)+len(chunk)>8192:raise BootstrapError('CONTROL_PACKET_INVALID')
        raw.extend(chunk)
    if raw.count(b'\n')!=1 or not raw.endswith(b'\n'):raise BootstrapError('CONTROL_PACKET_INVALID')
    try:return json.loads(raw)
    except (ValueError,TypeError):raise BootstrapError('CONTROL_PACKET_INVALID') from None

class ApiControlClient:
    def __init__(self,channel,boot):self.channel=channel;self.boot=boot;self.exit_callback=None;self.lock=threading.RLock();self.armed_deadlines={}
    @classmethod
    def from_boot(cls,boot):
        if 'control_fd' not in boot or boot.get('identity',{}).get('role')!='api':raise BootstrapError('API_CONTROL_UNAVAILABLE')
        return cls(socket.socket(fileno=boot['control_fd']),boot)
    def bind_manifest(self,manifest,installation_id):
        context=self.boot.setdefault('runtime_context',{})
        if context.get('installation_id') not in {None,installation_id} or context.get('model_manifest_id') not in {None,manifest['manifest_id']}:raise BootstrapError('API_CONTROL_IDENTITY_FAILED')
        context.update(installation_id=installation_id,model_manifest_id=manifest['manifest_id'],model_manifest_sha256=self.boot['identity']['manifest_sha256'],api_pid=os.getpid(),parent_pid=os.getppid(),boot_id=self.boot['identity']['boot_id'])
    def verified_boot_context(self):
        context=dict(self.boot.get('runtime_context') or {})
        return {key:context.get(key) for key in ['boot_id','installation_id','model_manifest_id','model_manifest_sha256','api_pid','parent_pid','restart']}
    def bind_exit(self,callback):self.exit_callback=callback
    def prepare_stop(self,command_id,boot_id,timeout):
        identity=self.boot['identity']
        if self.exit_callback is None:raise BootstrapError('API_EXIT_HOOK_MISSING')
        if identity['pid']!=os.getpid() or identity['boot_id']!=boot_id:raise BootstrapError('API_CONTROL_IDENTITY_FAILED')
        nonce=secrets.token_hex(32);packet=make_stop_intent(self.boot['secret'],identity,command_id,nonce,phase='complete',deadline_ns=self.armed_deadlines.get(command_id,time.monotonic_ns()+int(timeout*1e9)))
        try:
            socket_packet(self.channel,packet,timeout=timeout)
            ack=socket_packet(self.channel,timeout=timeout)
            body={'intent':'stop_ack','identity':identity,'command_id':command_id,'nonce':nonce,'authorized':True,'scorer_stopped':True}
            expected=hmac.new(self.boot['secret'],CONTROL_ACK_DOMAIN+canonical(body),hashlib.sha256).hexdigest()
            if set(ack)!=set(body)|{'mac'} or any(ack[k]!=v for k,v in body.items()) or not hmac.compare_digest(ack['mac'],expected):raise BootstrapError('HOST_STOP_ACK_INVALID')
            return {'authorized':True,'scorer_stopped':True}
        except (OSError,ValueError,TypeError):raise BootstrapError('HOST_STOP_FAILED') from None
    def finish_exit(self):
        if self.exit_callback is None:raise BootstrapError('API_EXIT_HOOK_MISSING')
        self.exit_callback()

def authorize_host_stop(channel,packet,boot,secret,api,scorer,seen,*,deadline=None):
    if not verify_stop_intent(secret,packet,boot['identity']) or packet['nonce'] in seen or api.pid!=boot['identity']['pid'] or api.poll() is not None:
        raise BootstrapError('HOST_STOP_UNAUTHORIZED')
    seen.add(packet['nonce']);deadline=deadline if deadline is not None else time.monotonic()+15
    if scorer.poll() is None:
        scorer.terminate()
        try:scorer.wait(timeout=max(.001,deadline-time.monotonic()))
        except subprocess.TimeoutExpired:
            scorer.kill();scorer.wait(timeout=1);raise BootstrapError('SHUTDOWN_DRAIN_TIMEOUT') from None
    body={'intent':'stop_ack','identity':boot['identity'],'command_id':packet['command_id'],'nonce':packet['nonce'],'authorized':True,'scorer_stopped':True}
    body['mac']=hmac.new(secret,CONTROL_ACK_DOMAIN+canonical(body),hashlib.sha256).hexdigest()
    socket_packet(channel,body,timeout=max(.001,deadline-time.monotonic()))
    try:code=api.wait(timeout=max(.001,deadline-time.monotonic()))
    except subprocess.TimeoutExpired:
        api.kill();api.wait(timeout=1);raise BootstrapError('SHUTDOWN_DRAIN_TIMEOUT') from None
    if code!=0:raise BootstrapError('HOST_CHILD_EXITED')

ARM_ACK_DOMAIN=b'wordheat-dev-host/1\0parent-stop-armed\0'

def _api_arm_stop(self,command_id,boot_id,deadline_ns):
    if self.boot['identity']['boot_id']!=boot_id or self.boot['identity']['pid']!=os.getpid():raise BootstrapError('API_CONTROL_IDENTITY_FAILED')
    with self.lock:
        nonce=secrets.token_hex(32);packet=make_stop_intent(self.boot['secret'],self.boot['identity'],command_id,nonce,phase='arm',deadline_ns=deadline_ns)
        socket_packet(self.channel,packet,timeout=2);ack=socket_packet(self.channel,timeout=2)
        body={'intent':'stop_armed','identity':self.boot['identity'],'command_id':command_id,'nonce':nonce,'deadline_ns':ack.get('deadline_ns'),'authorized':True}
        mac=hmac.new(self.boot['secret'],ARM_ACK_DOMAIN+canonical(body),hashlib.sha256).hexdigest()
        if set(ack)!=set(body)|{'mac'} or any(ack[k]!=v for k,v in body.items()) or not hmac.compare_digest(mac,ack['mac']) or type(body['deadline_ns']) is not int or body['deadline_ns']>deadline_ns:raise BootstrapError('HOST_WATCHDOG_ACK_INVALID')
        self.armed_deadlines[command_id]=body['deadline_ns']
        return {'authorized':True,'deadline_ns':body['deadline_ns']}
ApiControlClient.arm_stop=_api_arm_stop

def _api_force_stop(self,command_id,boot_id,reason):
    with self.lock:
        deadline=self.armed_deadlines.get(command_id,time.monotonic_ns())
        packet=make_stop_intent(self.boot['secret'],self.boot['identity'],command_id,secrets.token_hex(32),phase='force',deadline_ns=deadline)
        socket_packet(self.channel,packet,timeout=.2)
ApiControlClient.force_stop=_api_force_stop

def owned_signal(process,force=False):
    if process.poll() is not None:return
    signum=signal.SIGKILL if force else signal.SIGTERM
    try:
        if getattr(process,'_wordheat_owned_session',False) and os.getpgid(process.pid)==process.pid:os.killpg(process.pid,signum)
        else:process.send_signal(signum)
    except ProcessLookupError:pass

class ParentControlSupervisor:
    def __init__(self,channel,boot,secret,api,scorer,*,reason_root,registry):
        self.channel=channel;self.boot=boot;self.secret=secret;self.api=api;self.scorer=scorer
        self.reason_root=reason_root;self.registry=registry;self.seen=set();self.armed=None
        self.done=threading.Event();self.result=None;self.error=None;self.restart_plan=None
        self.thread=threading.Thread(target=self.run,daemon=True)
    def start(self):self.thread.start();return self
    def write_reason(self,code):
        def write():
            try:
                root=Path(self.reason_root)
                if any(p.is_symlink() for p in [root,*root.parents]):return
                root.mkdir(parents=True,mode=0o700,exist_ok=True)
                if root.stat().st_uid!=os.getuid() or root.stat().st_mode&0o077:return
                path=root/'host-last-exit.json';temporary=root/('exit-'+secrets.token_hex(8)+'.part')
                data={'protocol':'wordheat-dev-host/1','code':code,'command_id':self.armed['command_id'] if self.armed else None,'boot_id':self.boot['identity']['boot_id'],'api_pid':self.api.pid,'scorer_pid':self.scorer.pid,'completion_received':False,'credentials_included':False}
                fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'w') as stream:json.dump(data,stream)
                os.replace(temporary,path)
            except Exception:pass
        logger=threading.Thread(target=write,daemon=True);logger.start();logger.join(.05)
    def force(self):
        # No SQL/network/log dependency can postpone the owned-process deadline.
        owned_signal(self.scorer,True);owned_signal(self.api,True)
        for child in [self.scorer,self.api]:
            try:child.wait(timeout=1)
            except subprocess.TimeoutExpired:pass
        self.write_reason('SHUTDOWN_DRAIN_TIMEOUT');self.result=17;self.done.set()
    def arm(self,packet):
        if packet['nonce'] in self.seen or len(self.seen)>=32:raise BootstrapError('HOST_STOP_UNAUTHORIZED')
        self.seen.add(packet['nonce'])
        deadline=min(packet['deadline_ns'],time.monotonic_ns()+15_000_000_000)
        if self.armed:
            if self.armed['command_id']!=packet['command_id']:raise BootstrapError('HOST_STOP_UNAUTHORIZED')
            deadline=min(deadline,self.armed['deadline_ns'])
        self.armed={'command_id':packet['command_id'],'deadline_ns':deadline}
        body={'intent':'stop_armed','identity':self.boot['identity'],'command_id':packet['command_id'],'nonce':packet['nonce'],'deadline_ns':deadline,'authorized':True}
        body['mac']=hmac.new(self.secret,ARM_ACK_DOMAIN+canonical(body),hashlib.sha256).hexdigest();socket_packet(self.channel,body,timeout=.5)
    def run(self):
        try:
            while True:
                if self.armed and time.monotonic_ns()>=self.armed['deadline_ns']:self.force();return
                if self.api.poll() is not None or self.scorer.poll() is not None:raise BootstrapError('HOST_CHILD_EXITED')
                wait=.1 if not self.armed else max(0,min(.1,(self.armed['deadline_ns']-time.monotonic_ns())/1e9))
                if not select.select([self.channel],[],[],wait)[0]:continue
                packet=socket_packet(self.channel,timeout=.5)
                if packet.get('intent')=='stop':
                    if not verify_stop_intent(self.secret,packet,self.boot['identity']):raise BootstrapError('HOST_STOP_UNAUTHORIZED')
                    phase=packet.get('phase','complete')
                    if phase=='arm':self.arm(packet);continue
                    if self.armed and self.armed['command_id']!=packet['command_id']:raise BootstrapError('HOST_STOP_UNAUTHORIZED')
                    if phase=='force':self.force();return
                    deadline=self.armed['deadline_ns']/1e9 if self.armed else None
                    authorize_host_stop(self.channel,packet,self.boot,self.secret,self.api,self.scorer,self.seen,deadline=deadline)
                    self.result=0;self.done.set();return
                if packet.get('intent')=='restart_bundle':
                    if self.armed:raise BootstrapError('HOST_RESTART_UNAUTHORIZED')
                    self.restart_plan=authorize_host_restart(self.channel,packet,self.boot,self.secret,self.api,self.scorer,self.seen,**self.registry)
                    self.result=0;self.done.set();return
                raise BootstrapError('HOST_CONTROL_ACTION_REJECTED')
        except BootstrapError as error:
            if str(error)=='SHUTDOWN_DRAIN_TIMEOUT':self.force();return
            self.error=error;self.done.set()
        except Exception:
            self.error=BootstrapError('HOST_CONTROL_FAILED');self.done.set()

RESTART_DOMAIN=b'wordheat-dev-host/1\0api-restart-bundle\0'
RESTART_ACK_DOMAIN=b'wordheat-dev-host/1\0parent-restart-ack\0'

def make_restart_intent(secret,identity,plan,nonce):
    body={'intent':'restart_bundle','identity':identity,'plan':plan,'nonce':nonce}
    return body|{'mac':hmac.new(secret,RESTART_DOMAIN+canonical(body),hashlib.sha256).hexdigest()}

def verify_restart_intent(secret,packet,identity):
    try:
        if set(packet)!={'intent','identity','plan','nonce','mac'} or packet['intent']!='restart_bundle' or packet['identity']!=identity or identity['role']!='api':return False
        expected=make_restart_intent(secret,identity,packet['plan'],packet['nonce'])
        return hmac.compare_digest(expected['mac'],packet['mac'])
    except (ValueError,TypeError,KeyError):return False

def _prepare_restart(self,plan,boot_id,timeout):
    identity=self.boot['identity']
    if identity['pid']!=os.getpid() or identity['boot_id']!=boot_id or self.exit_callback is None:raise BootstrapError('API_CONTROL_IDENTITY_FAILED')
    nonce=secrets.token_hex(32);packet=make_restart_intent(self.boot['secret'],identity,plan,nonce)
    try:
        socket_packet(self.channel,packet,timeout=timeout);ack=socket_packet(self.channel,timeout=timeout)
        body={'intent':'restart_ack','identity':identity,'command_id':plan['command_id'],'plan_fingerprint':plan['plan_fingerprint'],'nonce':nonce,'authorized':True,'scorer_stopped':True}
        mac=hmac.new(self.boot['secret'],RESTART_ACK_DOMAIN+canonical(body),hashlib.sha256).hexdigest()
        if set(ack)!=set(body)|{'mac'} or any(ack[k]!=v for k,v in body.items()) or not hmac.compare_digest(mac,ack['mac']):raise BootstrapError('HOST_RESTART_ACK_INVALID')
        return {'authorized':True,'scorer_stopped':True}
    except (OSError,ValueError,TypeError):raise BootstrapError('HOST_RESTART_FAILED') from None
ApiControlClient.prepare_restart=_prepare_restart

def validate_restart_plan(plan,*,installation_id,bundle_root,allowed_model_roots,source_root):
    expected={'command_id','plan_fingerprint','confirmation_id','installation_id','manifest_path','manifest_file_sha256','model_root','model_manifest_id','model_manifest_sha256'}
    if not isinstance(plan,dict) or set(plan)!=expected or installation_id is None or plan['installation_id']!=installation_id or bundle_root is None:raise BootstrapError('HOST_RESTART_PLAN_INVALID')
    if not all(isinstance(plan[k],str) and 1<=len(plan[k])<=128 for k in ['command_id','confirmation_id']) or not all(isinstance(plan[k],str) and re.fullmatch('[0-9a-f]{64}',plan[k]) for k in ['plan_fingerprint','manifest_file_sha256','model_manifest_sha256']):raise BootstrapError('HOST_RESTART_PLAN_INVALID')
    manifest=Path(plan['manifest_path']);model=Path(plan['model_root']);root=Path(bundle_root)
    if not root.is_dir() or any(p.is_symlink() for p in [root,*root.parents]) or root.stat().st_uid!=os.getuid() or root.stat().st_mode&0o077:raise BootstrapError('HOST_RESTART_PATH_REJECTED')
    if not manifest.is_absolute() or manifest.is_symlink() or not manifest.resolve(strict=True).is_relative_to(root.resolve(strict=True)) or model.is_symlink() or model.resolve(strict=True) not in allowed_model_roots:raise BootstrapError('HOST_RESTART_PATH_REJECTED')
    # Reuse the data-only verifier; it does not import torch or model code.
    api_path=str(Path(source_root)/'apps/api');sys.path.insert(0,api_path)
    try:
        from app.modules.operations.runtime_artifacts import verify_model_data
        checked=verify_model_data(manifest,model)
    except Exception:raise BootstrapError('HOST_RESTART_ARTIFACT_INVALID') from None
    finally:sys.path.remove(api_path)
    if checked['file_sha256']!=plan['manifest_file_sha256'] or checked['manifest_sha256']!=plan['model_manifest_sha256'] or checked['manifest']['manifest_id']!=plan['model_manifest_id']:raise BootstrapError('HOST_RESTART_ARTIFACT_INVALID')
    return dict(plan)

def authorize_host_restart(channel,packet,boot,secret,api,scorer,seen,**registry):
    if not verify_restart_intent(secret,packet,boot['identity']) or packet['nonce'] in seen or api.pid!=boot['identity']['pid'] or api.poll() is not None:raise BootstrapError('HOST_RESTART_UNAUTHORIZED')
    plan=validate_restart_plan(packet['plan'],**registry);seen.add(packet['nonce']);deadline=time.monotonic()+15
    if scorer.poll() is None:
        scorer.terminate()
        try:scorer.wait(timeout=max(.001,deadline-time.monotonic()))
        except subprocess.TimeoutExpired:scorer.kill();scorer.wait(timeout=1);raise BootstrapError('SHUTDOWN_DRAIN_TIMEOUT') from None
    body={'intent':'restart_ack','identity':boot['identity'],'command_id':plan['command_id'],'plan_fingerprint':plan['plan_fingerprint'],'nonce':packet['nonce'],'authorized':True,'scorer_stopped':True}
    body['mac']=hmac.new(secret,RESTART_ACK_DOMAIN+canonical(body),hashlib.sha256).hexdigest();socket_packet(channel,body,timeout=max(.001,deadline-time.monotonic()))
    try:code=api.wait(timeout=max(.001,deadline-time.monotonic()))
    except subprocess.TimeoutExpired:api.kill();api.wait(timeout=1);raise BootstrapError('SHUTDOWN_DRAIN_TIMEOUT') from None
    if code!=0:raise BootstrapError('HOST_CHILD_EXITED')
    return plan

def nonsecret_host_fields(config_root):
    path=Path(config_root)/'.env'
    if not path.is_file():return {}
    if path.stat().st_mode&0o077:raise BootstrapError('HOST_CONFIG_UNVERIFIED')
    result={}
    for line in path.read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key,value=line.split('=',1)
            if key.strip() in {'WORDHEAT_INSTANCE_ID','LOCAL_MAINTENANCE_ROOT'}:result[key.strip()]=value.strip().strip('"').strip("'")
    return result

def _serialized(function):
    def invoke(self,*args,**kwargs):
        with self.lock:return function(self,*args,**kwargs)
    return invoke
ApiControlClient.prepare_stop=_serialized(ApiControlClient.prepare_stop)
ApiControlClient.prepare_restart=_serialized(ApiControlClient.prepare_restart)

def manifest_identity(path):
    import rfc8785
    try:
        data=json.loads(path.read_bytes())
        if data.get('dimension')!=512 or not isinstance(data.get('manifest_id'),str):raise ValueError()
        return data,hashlib.sha256(rfc8785.dumps(data)).hexdigest()
    except Exception:raise BootstrapError('MANIFEST_INVALID') from None

def serve(app, boot, ack_fd, port):
    """Acknowledge only after lifespan AND actual loopback socket binding succeed."""
    import uvicorn
    class Server(uvicorn.Server):
        async def startup(self,sockets=None):
            await super().startup(sockets)
            if not self.started or self.should_exit:raise BootstrapError('CHILD_START_FAILED')
            bound=[sock.getsockname()[:2] for server in self.servers for sock in server.sockets]
            if ('127.0.0.1',port) not in bound:raise BootstrapError('CHILD_ENDPOINT_FAILED')
            send_ack(ack_fd,boot)
    # Suppress raw library exceptions; parent reports only fixed reason codes.
    server=Server(uvicorn.Config(app,host='127.0.0.1',port=port,workers=1,loop='asyncio',
        reload=False,proxy_headers=False,access_log=False,log_config=None,log_level='critical'))
    controller=getattr(app.state,'runtime_controller',None)
    if controller and callable(getattr(controller.stop_port,'bind_exit',None)):
        controller.stop_port.bind_exit(lambda:setattr(server,'should_exit',True))
    server.run()

def main(argv=None):
    owned=[];channel=None;supervisor=None
    try:
        require_posix();parser=argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--root',required=True,type=Path);parser.add_argument('--config-root',type=Path)
        parser.add_argument('--manifest',required=True,type=Path);parser.add_argument('--model-root',required=True,type=Path)
        parser.add_argument('--api-python',required=True,type=Path);parser.add_argument('--scorer-python',required=True,type=Path)
        parser.add_argument('--installation-id');parser.add_argument('--bundle-root',type=Path)
        parser.add_argument('--model-root-allowlist',type=Path,action='append',default=[])
        args=parser.parse_args(argv)
        for path in (args.root,args.manifest,args.model_root,args.api_python,args.scorer_python,args.config_root or args.root):
            if not path.is_absolute() or not path.exists():raise BootstrapError('EXPLICIT_PATH_REQUIRED')
        root=args.root.resolve(strict=True);config=args.config_root or root
        fields=nonsecret_host_fields(config);installation=args.installation_id or fields.get('WORDHEAT_INSTANCE_ID')
        bundle=args.bundle_root or (Path(fields['LOCAL_MAINTENANCE_ROOT']) if fields.get('LOCAL_MAINTENANCE_ROOT') else config/'.local/maintenance')
        if not bundle.is_absolute() or any(p.is_symlink() for p in [bundle,*bundle.parents]):raise BootstrapError('HOST_RESTART_PATH_REJECTED')
        if bundle.exists() and (bundle.stat().st_uid!=os.getuid() or bundle.stat().st_mode&0o077):raise BootstrapError('HOST_RESTART_PATH_REJECTED')
        bundle=bundle.resolve(strict=True) if bundle.exists() else bundle
        allowed_roots={p.resolve(strict=True) for p in [args.model_root,*args.model_root_allowlist]}
        entries={role:root/f'apps/{role}/app/runtime_entry.py' for role in ('api','scorer')}
        if any(not path.is_file() or not path.resolve().is_relative_to(root) for path in entries.values()):raise BootstrapError('SOURCE_ENTRY_INVALID')
        def stop_signal(signum,frame):raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM,stop_signal)
        restart=None
        while True:
            supervisor=None
            manifest,sha=manifest_identity(args.manifest);secret=secrets.token_bytes(32);boot_id=secrets.token_hex(32)
            for role,python,port,timeout in (('scorer',args.scorer_python,8124,120.),('api',args.api_python,8765,30.)):
                command=[str(entries[role]),'--source-root',str(root),'--config-root',str(config),'--manifest',str(args.manifest),'--model-root',str(args.model_root),'--fds']
                context={'installation_id':installation,'model_manifest_id':manifest['manifest_id'],'model_manifest_sha256':sha,'restart':restart}
                child,fd,boot=spawn_child(role,python,root/f'apps/{role}',command,secret,boot_id,sha,f'127.0.0.1:{port}',owned,control=role=='api',runtime_context=context if role=='api' else None,diagnostic=True)
                if role=='api':
                    channel=boot.get('_control_socket')
                    supervisor=ParentControlSupervisor(channel,boot,secret,owned[-1],owned[0],reason_root=config/'.local/runtime',registry={'installation_id':installation,'bundle_root':bundle,'allowed_model_roots':allowed_roots,'source_root':root}).start()
                try:await_ack(child,fd,boot,secret,timeout)
                except BootstrapError:
                    emit_child_diagnostic(child)
                    if supervisor and supervisor.done.wait(.15) and supervisor.result==17:return 17
                    if supervisor and supervisor.done.is_set() and supervisor.result==0:return 0
                    raise
            print('DEV_SERVICES_READY',flush=True)
            while not supervisor.done.wait(.1):pass
            if supervisor.error:raise supervisor.error
            if supervisor.result==17:raise BootstrapError('SHUTDOWN_DRAIN_TIMEOUT')
            next_plan=supervisor.restart_plan
            if next_plan is None:
                print('DEV_SERVICES_STOPPED',flush=True);return 0
            if next_plan is None:raise BootstrapError('HOST_CHILD_EXITED')
            channel.close();channel=None;owned.clear()
            args.manifest=Path(next_plan['manifest_path']);args.model_root=Path(next_plan['model_root']);restart=next_plan
            print('DEV_BUNDLE_RESTARTING',flush=True)
    except KeyboardInterrupt:return 0
    except Exception as error:
        code=str(error) if isinstance(error,BootstrapError) else 'DEV_START_FAILED'
        print(code,file=sys.stderr,flush=True);return 17 if code=='SHUTDOWN_DRAIN_TIMEOUT' else 1
    finally:
        if channel:channel.close()
        stop_children(owned)

if __name__=='__main__':raise SystemExit(main())
