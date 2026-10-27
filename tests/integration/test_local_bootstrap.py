"""Development bootstrap tests. Echo children are not service/model acceptance."""
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/backend/20261025-run-local.py'

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

@pytest.fixture
def host(): return load(SCRIPT, 'local_host')

def test_environment_and_platform_fail_closed(host):
    env = host.child_environment({'PATH':'/bin','HOME':'/private/home','AWS_SECRET_ACCESS_KEY':'sentinel','PYTHONPATH':'evil','LOCAL_DATABASE_URL':'secret','HTTPS_PROXY':'secret'})
    assert 'AWS_SECRET_ACCESS_KEY' not in env and 'PYTHONPATH' not in env
    assert 'LOCAL_DATABASE_URL' not in env and 'HTTPS_PROXY' not in env
    assert env['HF_HUB_OFFLINE'] == '1'
    with pytest.raises(host.BootstrapError, match='WINDOWS_NATIVE_REQUIRED'): host.require_posix('win32')

def test_private_pipe_rejects_bad_or_truncated_boot(host):
    for raw in (b'{}', b'x' * 9000, b'not-json'):
        read, write = os.pipe()
        os.write(write, raw); os.close(write)
        with pytest.raises(host.BootstrapError): host.receive_bootstrap(read, 'api')

def test_handshake_is_bound_and_domain_separated(host):
    secret = b'a' * 32
    identity = {'pid':123,'role':'api','boot_id':'boot','manifest_sha256':'a'*64,'challenge':'nonce','endpoint':'127.0.0.1:8765'}
    packet = host.make_ack(secret, identity)
    assert host.verify_ack(secret, packet, identity)
    for field in identity:
        changed = identity | {field: 'wrong'}
        assert not host.verify_ack(secret, packet, changed)
    assert packet['mac'] != __import__('hmac').new(secret, host.canonical(identity), hashlib.sha256).hexdigest()

def test_spawn_real_echo_child_and_cleanup_only_owned(host, tmp_path):
    child = tmp_path/'echo.py'
    child.write_text('import importlib.util,sys,time\ns=importlib.util.spec_from_file_location("host",sys.argv[1]); m=importlib.util.module_from_spec(s);sys.modules["host"]=m;s.loader.exec_module(m)\nb=m.receive_bootstrap(int(sys.argv[2]),"api");m.send_ack(int(sys.argv[3]),b);time.sleep(60)\n')
    bystander = subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
    owned=[]
    try:
        process, fd, boot = host.spawn_child('api', Path(sys.executable), child.parent, [str(child),str(SCRIPT)], b'z'*32, 'b'*64, 'a'*64, '127.0.0.1:8765', owned)
        host.await_ack(process,fd,boot, b'z'*32,2)
        assert process.poll() is None
        host.stop_children(owned, grace=.1)
        assert process.poll() is not None and bystander.poll() is None
    finally:
        host.stop_children(owned,grace=.1)
        bystander.terminate();bystander.wait()

def test_failed_ack_and_timeout_clean_owned_process(host,tmp_path):
    child=tmp_path/'sleep.py';child.write_text('import time;time.sleep(60)')
    owned=[]
    try:
        process,fd,boot=host.spawn_child('api',Path(sys.executable),tmp_path,[str(child)],b'z'*32,'b'*64,'a'*64,'127.0.0.1:8765',owned)
        with pytest.raises(host.BootstrapError):host.await_ack(process,fd,boot,b'z'*32,.1)
    finally:host.stop_children(owned,grace=.1)
    assert process.poll() is not None

def test_runtime_clients_are_per_call_and_monitor_is_cached():
    runtime=load(ROOT/'apps/api/app/modules/scoring/runtime.py','runtime_test')
    clients=[]; clock=[0.]
    class Client:
        def __init__(self,*args,**kwargs):self.closed=False;clients.append(self)
        async def embeddings(self,texts):return [[1.,0.] for _ in texts]
        async def score(self,*args):return 42.
        async def aclose(self):self.closed=True
    proxy=runtime.ScorerRuntime(b'x'*32,'real-model','a'*64,2,client_factory=Client,clock=lambda:clock[0])
    assert proxy.readiness()['ready'] is False and len(clients)==0
    for word in ('first-uncached','second-uncached'): assert asyncio.run(proxy.score(word,{}))==42.
    assert len(clients)==2 and all(c.closed for c in clients)
    asyncio.run(proxy.probe())
    assert proxy.readiness()=={'ready':True,'manifest_id':'real-model'}
    count=len(clients);clock[0]=2.
    # readiness is a pure read: it never opens a client of its own, and a slow but
    # successful probe still counts as healthy inside the freshness window.
    assert proxy.readiness()=={'ready':True,'manifest_id':'real-model'} and len(clients)==count
    clock[0]=runtime.OBSERVATION_FRESHNESS_SECONDS
    assert proxy.readiness()['ready'] is False and len(clients)==count
    async def monitor():
        await proxy.start();await asyncio.sleep(.02);await proxy.close()
    asyncio.run(monitor())
    assert proxy.readiness()['ready'] is False

def test_probe_failure_clears_readiness_and_always_closes_client():
    runtime=load(ROOT/'apps/api/app/modules/scoring/runtime.py','runtime_failure')
    closed=[]
    class Client:
        def __init__(self,*args,**kwargs):pass
        async def embeddings(self,texts):raise RuntimeError('private-sentinel')
        async def aclose(self):closed.append(True)
    proxy=runtime.ScorerRuntime(b'x'*32,'real-model','a'*64,512,client_factory=Client)
    asyncio.run(proxy.probe())
    assert proxy.readiness()=={'ready':False,'manifest_id':None}
    assert closed==[True]

def test_monitor_wraps_and_preserves_application_lifespan():
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    runtime=load(ROOT/'apps/api/app/modules/scoring/runtime.py','runtime_lifespan')
    steps=[]
    @asynccontextmanager
    async def original(app):
        steps.append('enter')
        try:yield
        finally:steps.append('exit')
    app=SimpleNamespace(router=SimpleNamespace(lifespan_context=original))
    class Client:
        def __init__(self,*args,**kwargs):pass
        async def embeddings(self,texts):return [[1.]]
        async def aclose(self):pass
    proxy=runtime.ScorerRuntime(b'x'*32,'real-model','a'*64,1,client_factory=Client)
    proxy.wrap_lifespan(app)
    async def run():
        async with app.router.lifespan_context(app):
            await asyncio.sleep(.01)
            assert proxy.readiness()['ready'] is True
        assert proxy._monitor is None and proxy.readiness()['ready'] is False
    asyncio.run(run())
    assert steps==['enter','exit']

def test_child_entry_imports_never_start_processes_or_read_env():
    assert load(ROOT/'apps/api/app/runtime_entry.py','api_entry').main
    assert load(ROOT/'apps/scorer/app/runtime_entry.py','scorer_entry').main

def test_inflight_probe_preserves_recent_verified_observation():
    runtime=load(ROOT/'apps/api/app/modules/scoring/runtime.py','runtime_inflight')
    clock=[0.]
    class Client:
        def __init__(self,*args,**kwargs):pass
        async def embeddings(self,texts):
            await asyncio.sleep(.05)
            return [[1.]]
        async def aclose(self):pass
    proxy=runtime.ScorerRuntime(b'x'*32,'real-model','a'*64,1,client_factory=Client,clock=lambda:clock[0])
    async def run():
        await proxy.probe()
        clock[0]=1.
        task=asyncio.create_task(proxy.probe())
        await asyncio.sleep(.01)
        assert proxy.readiness()['ready'] is True
        await task
    asyncio.run(run())

@pytest.mark.parametrize('action', ['failure','interrupt'])
def test_host_main_cleans_both_children_on_failure_or_interrupt(tmp_path,action):
    import select
    import signal
    root=tmp_path/'source'
    for role in ('api','scorer'):
        folder=root/f'apps/{role}/app';folder.mkdir(parents=True)
        source=f'''import argparse,importlib.util,sys,time,os
from pathlib import Path
p=argparse.ArgumentParser()
for name in ('source-root','config-root','manifest','model-root'):p.add_argument('--'+name)
p.add_argument('--fds',type=int,nargs=2)
a=p.parse_args()
s=importlib.util.spec_from_file_location('host',{str(SCRIPT)!r});h=importlib.util.module_from_spec(s);s.loader.exec_module(h)
b=h.receive_bootstrap(a.fds[0],{role!r})
Path(a.source_root,{role+'.pid'!r}).write_text(str(os.getpid()))
h.send_ack(a.fds[1],b)
time.sleep({'.2' if role=='api' and action=='failure' else '60'})
'''
        (folder/'runtime_entry.py').write_text(source)
    manifest=tmp_path/'manifest.json';manifest.write_text(json.dumps({'dimension':512,'manifest_id':'synthetic-echo-only'}))
    process=subprocess.Popen([sys.executable,str(SCRIPT),'--root',str(root),'--config-root',str(root),
        '--manifest',str(manifest),'--model-root',str(root),'--api-python',sys.executable,
        '--scorer-python',sys.executable],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert select.select([process.stdout],[],[],3)[0]
        assert process.stdout.readline().strip()=='DEV_SERVICES_READY'
        if action=='interrupt':process.send_signal(signal.SIGINT)
        stdout,stderr=process.communicate(timeout=4)
        assert process.returncode==(0 if action=='interrupt' else 1)
        assert stderr==('' if action=='interrupt' else 'HOST_CHILD_EXITED\n')
        for role in ('api','scorer'):
            pid=int((root/f'{role}.pid').read_text())
            with pytest.raises(ProcessLookupError):os.kill(pid,0)
    finally:
        if process.poll() is None:process.terminate();process.wait(timeout=6)
