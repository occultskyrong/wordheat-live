"""Dedicated-PG runtime intent/CAS/recovery; no privileged native action is faked."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest
import sqlalchemy as sa
from app.errors import DomainError
from app.infrastructure.db import schema as s
from app.modules.sessions.service import LocalService
from app.modules.operations import runtime_controller as module
spec=importlib.util.spec_from_file_location('runtime_analytics_fixture',Path(__file__).parents[1]/'test_analytics.py')
h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
@pytest.fixture
def env():
    fixture=h.env.__wrapped__(); e=next(fixture)
    e.db.settings.instance_id='wordheat-local-development'
    try: yield e
    finally:
        if hasattr(module,'RuntimeController'):
            from app.modules.operations import runtime_models as m
            with e.db.engine.begin() as c:
                c.execute(m.native_requests.delete().where(m.native_requests.c.actor_id==e.actor))
                ids=sa.select(m.commands.c.id).where(m.commands.c.actor_id==e.actor)
                c.execute(m.steps.delete().where(m.steps.c.command_id.in_(ids)))
                c.execute(m.commands.delete().where(m.commands.c.actor_id==e.actor))
                c.execute(m.backups.delete().where(m.backups.c.installation_id==e.db.settings.instance_id))
                if not c.execute(sa.select(m.commands.c.id).where(m.commands.c.installation_id==e.db.settings.instance_id).limit(1)).first():
                    c.execute(m.scopes.delete().where(m.scopes.c.installation_id==e.db.settings.instance_id))
                    c.execute(m.instances.delete().where(m.instances.c.installation_id==e.db.settings.instance_id))
        try: next(fixture)
        except StopIteration: pass

def controller(e,**kwargs):
    assert hasattr(module,'RuntimeController'),'portable runtime controller is missing'
    with e.db.engine.begin() as c:
        c.execute(s.operators.update().where(s.operators.c.id==e.actor).values(permissions=['maintenance.request','session.control','diagnostics.read']))
    e.db.settings.instance_id='wordheat-local-development'
    core=LocalService(e.db); value=module.RuntimeController(core,boot_id=e.prefix+'boot',**kwargs);value.initialize()
    return value

def request(value,e,command='stop',operation=None,key='once'):
    args={'reason':'operator requested controlled shutdown'}
    if operation:args['operation']=operation
    with e.db.engine.connect() as c: version=value.status(c,{'checks':{},'can_test_live':False,'can_go_live':False},True)['version']
    return value.command(e.actor,key,{'command':command,'expected_version':version,'args':args})

def test_persistent_stop_is_idempotent_and_no_stop_port_is_explicit_failure(env):
    e=env; value=controller(e)
    first=request(value,e); assert first['status']=='pending'
    value.tick()
    with e.db.engine.connect() as c:
        result=value.lookup_command(c,first['command_id'],e.actor,['maintenance.request'])
        assert result['status']=='failed' and result['error']['code']=='RUNTIME_STOP_UNAVAILABLE'
        assert c.execute(sa.select(s.sessions.c.status).where(s.sessions.c.id==e.sid)).scalar_one()=='ENDED'
    repeat=value.command(e.actor,'once',{'command':'stop','expected_version':first['resource_version']-1,'args':{'reason':'operator requested controlled shutdown'}})
    assert repeat['command_id']==first['command_id'] and repeat['status']=='failed'

def test_runtime_request_rejects_web_confirmation_paths_and_stale_cas(env):
    e=env; value=controller(e)
    for args in [{'reason':'x','operation':'shell'},{'reason':'x','operation':'backup_local','path':'/tmp/x'},{'reason':'x','confirmed':True}]:
        with pytest.raises(DomainError) as error:value.command(e.actor,'bad'+str(len(args)),{'command':'request_maintenance','expected_version':1,'args':args})
        assert error.value.code=='INPUT_INVALID'
    with pytest.raises(DomainError) as error:value.command(e.actor,'stale',{'command':'stop','expected_version':0,'args':{'reason':'stop'}})
    assert error.value.code=='RUNTIME_VERSION_CONFLICT'

def test_maintenance_without_private_native_port_never_silently_executes(env):
    e=env; value=controller(e)
    result=request(value,e,'request_maintenance','backup_local'); value.tick()
    with e.db.engine.connect() as c:
        current=value.lookup_command(c,result['command_id'],e.actor,['maintenance.request'])
        assert current['status']=='failed' and current['error']['code']=='NATIVE_CONTROL_UNAVAILABLE'
        assert value.status(c,{'checks':{},'can_test_live':False,'can_go_live':False},True)['maintenance_status']['requires_native_confirmation'] is True

def test_cross_boot_unfinished_command_requires_explicit_recovery_and_current_permission(env):
    e=env; value=controller(e)
    result=request(value,e)
    restarted=module.RuntimeController(value.core,boot_id=e.prefix+'nextboot'); restarted.initialize()
    with e.db.engine.connect() as c:
        assert restarted.status(c,{'checks':{},'can_test_live':False,'can_go_live':False},True)['launcher_state']=='RECOVERY_REQUIRED'
        current=restarted.lookup_command(c,result['command_id'],e.actor,['maintenance.request'])
        assert current['status']=='failed' and current['error']['code']=='RUNTIME_RECOVERY_REQUIRED'
    with e.db.engine.begin() as c:c.execute(s.operators.update().where(s.operators.c.id==e.actor).values(permissions=[]))
    with e.db.engine.connect() as c:
        with pytest.raises(DomainError) as error:restarted.lookup_command(c,result['command_id'],e.actor,['maintenance.request'])
        assert error.value.status_code==403

def native(e,tmp_path,executor_factory=None):
    from app.modules.operations.runtime_artifacts import ArtifactRegistry
    from app.modules.operations.runtime_native import NativeMaintenancePort
    return NativeMaintenancePort(ArtifactRegistry(tmp_path,{},'wordheat-local-development'),executor_factory)

def test_native_confirmation_is_bound_and_cancelled_command_remains_queryable(env,tmp_path):
    e=env; value=controller(e,native_port=native(e,tmp_path))
    result=request(value,e,'request_maintenance','backup_local');value.tick()
    port=value.native_control_port();review=port.prepare(result['command_id'])
    with pytest.raises(DomainError) as error:port.confirm(result['command_id'],review['fingerprint'],'wrong',review['installation_id'])
    assert error.value.code=='NATIVE_CONFIRMATION_INVALID'
    port.confirm(result['command_id'],review['fingerprint'],review['confirmation_nonce'],review['installation_id'])
    with pytest.raises(DomainError):port.confirm(result['command_id'],review['fingerprint'],review['confirmation_nonce'],review['installation_id'])
    port.cancel(result['command_id'])
    with e.db.engine.connect() as c:
        assert value.lookup_command(c,result['command_id'],e.actor,['maintenance.request'])['status']=='cancelled'
        assert value.status(c,{'checks':{},'can_test_live':False,'can_go_live':False},True)['maintenance_status']['stage']=='CANCELLED'

def test_json_execution_claim_is_not_native_evidence(env,tmp_path):
    e=env
    class Fake:
        def run_stage(self,*args):return {'completed':True,'backup_verified':True}
    value=controller(e,native_port=native(e,tmp_path,lambda attest:Fake()))
    result=request(value,e,'request_maintenance','backup_local');value.tick()
    port=value.native_control_port();review=port.prepare(result['command_id']);port.confirm(result['command_id'],review['fingerprint'],review['confirmation_nonce'],review['installation_id']);value.tick();value.tick()
    with e.db.engine.connect() as c:
        current=value.lookup_command(c,result['command_id'],e.actor,['maintenance.request'])
        assert current['status']=='failed' and current['error']['code']=='NATIVE_EXECUTION_UNVERIFIED'

def test_platform_stop_pending_blocks_process_stop_and_uses_same_fifteen_second_budget(env):
    e=env;clock=[1];calls=[]
    class Stop:
        def prepare_stop(self,*args):calls.append('prepare');return {'authorized':True}
        def finish_exit(self):calls.append('exit')
    pending=[True]
    value=controller(e,clock=lambda:clock[0],stop_port=Stop(),platform_projection=lambda c:{'platform_stop_pending':pending[0],'tasks':[]})
    result=request(value,e);value.tick();assert calls==[]
    with e.db.engine.connect() as c:assert value.lookup_command(c,result['command_id'],e.actor,['maintenance.request'])['status']=='pending'
    clock[0]=15_000_000_002;value.tick()
    with e.db.engine.connect() as c:
        result=value.lookup_command(c,result['command_id'],e.actor,['maintenance.request'])
        assert result['status']=='failed' and result['error']['code']=='SHUTDOWN_DRAIN_TIMEOUT'
    assert calls==[]

@pytest.mark.parametrize('blocked',[False,True])
def test_real_http_authorized_stop_reaps_api_and_scorer_only(env,postgres_isolation,tmp_path,blocked):
    """Real runtime route/controller/PG/socketpair; scorer child is an identity-only fixture."""
    import json,os,select,shutil,subprocess,sys,time
    import httpx
    e=env;e.db.settings.instance_id='wordheat-local-development'
    with e.db.engine.begin() as c:c.execute(s.operators.update().where(s.operators.c.id==e.actor).values(permissions=['maintenance.request','session.control','diagnostics.read']))
    root=tmp_path/'host';(root/'scripts/backend').mkdir(parents=True)
    candidate=Path(__file__).resolve().parents[5]
    script=candidate/'scripts/backend/20261025-run-local.py'
    shutil.copy2(script,root/'scripts/backend/20261025-run-local.py')
    config=tmp_path/'config';config.mkdir(mode=0o700)
    private=config/'.env';private.write_text('LOCAL_DATABASE_URL='+e.db.settings.database_url+'\nWORDHEAT_INSTANCE_ID=wordheat-local-development\n');private.chmod(0o600)
    schema=postgres_isolation.schema
    common='''import argparse,importlib.util,sys,os,time
from pathlib import Path
p=argparse.ArgumentParser()
for name in ('source-root','config-root','manifest','model-root'):p.add_argument('--'+name)
p.add_argument('--fds',type=int,nargs=2)
a=p.parse_args();root=Path(a.source_root)
s=importlib.util.spec_from_file_location('host',root/'scripts/backend/20261025-run-local.py');h=importlib.util.module_from_spec(s);s.loader.exec_module(h)
'''
    for role in ['api','scorer']:(root/f'apps/{role}/app').mkdir(parents=True)
    (root/'apps/scorer/app/runtime_entry.py').write_text(common+"b=h.receive_bootstrap(a.fds[0],'scorer')\n(root/'scorer.pid').write_text(str(os.getpid()))\nh.send_ack(a.fds[1],b)\ntime.sleep(60)\n")
    api=common+f'''b=h.receive_bootstrap(a.fds[0],'api')
(root/'api.pid').write_text(str(os.getpid()))
sys.path.insert(0,{str(candidate/'apps/api')!r})
import sqlalchemy as sa
from sqlalchemy.engine import Engine
@sa.event.listens_for(Engine,'engine_connect')
def isolate(c):
 raw=c.connection.dbapi_connection;before=raw.autocommit
 try:
  raw.autocommit=True
  with raw.cursor() as cur:cur.execute("SELECT set_config('search_path',%s,false)",({schema!r},))
 finally:raw.autocommit=before
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from app.config import load_settings
from app.infrastructure.db.database import Database
from app.modules.auth.service import AuthService
from app.modules.sessions.service import LocalService
from app.modules.operations.runtime_router import bind_runtime
from app.errors import DomainError
settings=load_settings(Path(a.config_root));db=Database(settings);core=LocalService(db);core.boot_id=b['identity']['boot_id']
app=FastAPI();app.state.database=db;app.state.service=core;app.state.auth=AuthService(db)
@app.exception_handler(DomainError)
async def failure(request,error):return JSONResponse({{'error':{{'code':error.code}}}},status_code=error.status_code)
controller=bind_runtime(app,stop_port=h.ApiControlClient.from_boot(b),boot_id=b['identity']['boot_id'])
if {blocked!r}:
 def blocked_drain(command):
  with db.engine.connect() as c:c.execute(sa.text('SELECT pg_sleep(30)'))
  return False
 controller.drain_sessions=blocked_drain
h.serve(app,b,a.fds[1],8765)
'''
    (root/'apps/api/app/runtime_entry.py').write_text(api)
    manifest=tmp_path/'manifest.json';manifest.write_text(json.dumps({'dimension':512,'manifest_id':'synthetic-process-control-only'}))
    bystander=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
    host=subprocess.Popen([sys.executable,str(script),'--root',str(root),'--config-root',str(config),'--manifest',str(manifest),'--model-root',str(root),'--api-python',sys.executable,'--scorer-python',sys.executable],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert select.select([host.stdout],[],[],8)[0], 'host did not report identity-bound startup'
        assert host.stdout.readline().strip()=='DEV_SERVICES_READY'
        from app.modules.operations import runtime_models as m
        with e.db.engine.connect() as c:version=c.execute(sa.select(m.instances.c.version).where(m.instances.c.installation_id=='wordheat-local-development')).scalar_one()
        started=time.monotonic()
        response=httpx.post('http://127.0.0.1:8765/api/v1/runtime/commands',json={'command':'stop','expected_version':version,'args':{'reason':'owned subprocess HTTP stop'}},headers={'Origin':'http://127.0.0.1:8765','X-CSRF-Token':'csrf','Idempotency-Key':e.prefix+'host-stop'},cookies={'lw_operator':e.token},timeout=5)
        assert response.status_code==202,response.text
        stdout,stderr=host.communicate(timeout=18)
        elapsed=time.monotonic()-started
        if blocked:
            assert host.returncode==17 and 'SHUTDOWN_DRAIN_TIMEOUT' in stderr
            assert 'DEV_SERVICES_STOPPED' not in stdout and 14<=elapsed<17
            reason=json.loads((config/'.local/runtime/host-last-exit.json').read_text())
            assert reason['code']=='SHUTDOWN_DRAIN_TIMEOUT' and reason['command_id']==response.json()['data']['command_id']
            assert reason['credentials_included'] is False
        else:
            assert host.returncode==0 and 'DEV_SERVICES_STOPPED' in stdout and stderr==''
            assert elapsed<15
        assert bystander.poll() is None
        for role in ['api','scorer']:
            with pytest.raises(ProcessLookupError):os.kill(int((root/(role+'.pid')).read_text()),0)
        with e.db.engine.connect() as c:
            command=c.execute(sa.select(m.commands).where(m.commands.c.id==response.json()['data']['command_id'])).mappings().one()
            if blocked:
                assert command['status']=='pending' and command['stage']=='DRAINING'
                assert c.execute(sa.select(s.sessions.c.status).where(s.sessions.c.id==e.sid)).scalar_one()=='ACTIVE'
            else:assert command['status']=='applied' and command['stage']=='STOPPED'
            assert c.execute(sa.text('SELECT 1')).scalar_one()==1
    finally:
        if host.poll() is None:host.terminate();host.wait(timeout=6)
        bystander.terminate();bystander.wait(timeout=3)
        private.unlink(missing_ok=True)

def test_http_maintenance_intent_does_not_stop_business_before_native_confirmation(env,tmp_path):
    e=env;value=controller(e,native_port=native(e,tmp_path))
    result=request(value,e,'request_maintenance','backup_local');value.tick()
    with e.db.engine.connect() as c:
        assert c.execute(sa.select(s.sessions.c.status).where(s.sessions.c.id==e.sid)).scalar_one()=='ACTIVE'
        assert value.lookup_command(c,result['command_id'],e.actor,['maintenance.request'])['status']=='pending'
    permit=value.native_control_port();review=permit.prepare(result['command_id'])
    permit.confirm(result['command_id'],review['fingerprint'],review['confirmation_nonce'],review['installation_id'])
    value.tick()
    with e.db.engine.connect() as c:assert c.execute(sa.select(s.sessions.c.status).where(s.sessions.c.id==e.sid)).scalar_one()=='ENDED'

def test_confirmed_private_restart_resumes_only_bound_plan_and_waits_for_real_probe(env):
    from app.modules.operations import runtime_models as m
    from app.modules.operations.runtime_controller import PrivateRuntimeBoot
    import os
    e=env;clock=[10];value=controller(e,clock=lambda:clock[0])
    with e.db.engine.begin() as c:c.execute(s.sessions.update().where(s.sessions.c.id==e.sid).values(status='ENDED'))
    response=request(value,e,'request_maintenance','switch_model')
    expected={'command_id':response['command_id'],'plan_fingerprint':'f'*64,'confirmation_id':'native-confirmation','installation_id':value.installation_id,'manifest_path':'registered-v2.json','manifest_file_sha256':'e'*64,'model_root':'registered-model-root','model_manifest_id':'model-v2','model_manifest_sha256':'a'*64}
    with e.db.engine.begin() as c:
        c.execute(m.commands.update().where(m.commands.c.id==response['command_id']).values(stage='RESTART_REQUIRED',plan={'confirmed':True,'confirmation_id':'native-confirmation','restart_expected':expected}))
        c.execute(m.instances.update().where(m.instances.c.installation_id==value.installation_id).values(state='MAINTENANCE'))
    class Probe:
        manifest_sha256='a'*64
        ready=False
        def readiness(self):return {'ready':self.ready,'manifest_id':'model-v2' if self.ready else None}
    value.core.scorer=Probe()
    context=PrivateRuntimeBoot('new-verified-boot',value.installation_id,'model-v2','a'*64,os.getpid(),os.getppid(),expected)
    new=module.RuntimeController(value.core,boot_id=context.boot_id,boot_context=context,clock=lambda:clock[0]);new.initialize();new.tick()
    with e.db.engine.connect() as c:
        current=new.lookup_command(c,response['command_id'],e.actor,['maintenance.request'])
        assert current['status']=='pending' and current['state']=='WARMING_NEW_BUNDLE'
        assert c.execute(sa.select(m.instances.c.state).where(m.instances.c.installation_id==new.installation_id)).scalar_one()=='MAINTENANCE'
    value.core.scorer.ready=True;new.tick()
    with e.db.engine.connect() as c:assert new.lookup_command(c,response['command_id'],e.actor,['maintenance.request'])['state']=='CONFIRMED'
