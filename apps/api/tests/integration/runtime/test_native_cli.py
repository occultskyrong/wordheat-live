"""Actual Darwin/Linux peer identity + registered CLI. Fixture credentials are synthetic."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import tempfile
import pytest
import sqlalchemy as sa
from app.modules.auth.service import hash_password

spec=importlib.util.spec_from_file_location('native_cli_runtime_fixture',Path(__file__).with_name('test_runtime_commands.py'))
h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
env=h.env
PASSWORD='Synthetic-native-password-123'

@pytest.fixture
def native_api(env,postgres_isolation,tmp_path,request):
    e=env
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    release_key=Ed25519PrivateKey.generate();release_public=release_key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    wrong_uid=getattr(request,'param',None)=='wrong_uid'
    with e.db.engine.begin() as c:c.execute(h.s.operators.update().where(h.s.operators.c.id==e.actor).values(password_hash=hash_password(PASSWORD),permissions=['maintenance.request','session.control','diagnostics.read']))
    candidate=Path(__file__).resolve().parents[5];source=tmp_path/'source';entry=source/'apps/api/app/runtime_entry.py'
    shutil.copytree(candidate/'apps/api/app',entry.parent,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    shutil.copytree(candidate/'apps/api/migrations',source/'apps/api/migrations',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    shutil.copy2(candidate/'apps/api/app/cli.py',entry.parent/'cli.py')
    config=tmp_path/'config';config.mkdir(mode=0o700);private=config/'.env'
    private.write_text('LOCAL_DATABASE_URL='+e.db.settings.database_url+'\nWORDHEAT_INSTANCE_ID=wordheat-local-development\n');private.chmod(0o600)
    maintenance=config/'.local/maintenance';maintenance.mkdir(parents=True,mode=0o700)
    socket_root=Path(tempfile.mkdtemp(prefix='wh-native-',dir='/private/tmp'))
    code=f'''import sys,os
from pathlib import Path
sys.path.insert(0,{str(source/'apps/api')!r})
import sqlalchemy as sa
from sqlalchemy.engine import Engine
@sa.event.listens_for(Engine,'engine_connect')
def isolate(c):
 raw=c.connection.dbapi_connection;before=raw.autocommit
 try:
  raw.autocommit=True
  with raw.cursor() as cur:cur.execute("SELECT set_config('search_path',%s,false)",({postgres_isolation.schema!r},))
 finally:raw.autocommit=before
from fastapi import FastAPI
from app.config import load_settings
from app.infrastructure.db.database import Database
from app.modules.auth.service import AuthService
from app.modules.sessions.service import LocalService
from app.modules.operations.runtime_artifacts import ArtifactRegistry
from app.modules.operations.runtime_native import NativeMaintenancePort
from app.modules.operations.runtime_backup import BackupExecutor
from app.modules.operations.runtime_postgres import PostgresTools
from app.modules.operations.runtime_router import bind_runtime,LocalServerStop
settings=load_settings(Path({str(config)!r}));db=Database(settings);core=LocalService(db)
registry=ArtifactRegistry(Path({str(maintenance)!r}),{{'fixture':{release_public!r}}},settings.instance_id)
tools=PostgresTools.from_registry(Path({os.environ.get('WORDHEAT_MAINTENANCE_REGISTRY_FILE')!r}),settings.instance_id)
with db.engine.connect() as c:marker=c.execute(sa.text("SELECT obj_description(oid,'pg_namespace') FROM pg_namespace WHERE nspname=:s"),{{'s':{postgres_isolation.schema!r}}}).scalar_one()
port=NativeMaintenancePort(registry,lambda attest:BackupExecutor(attest,tools=tools,registry=registry,database=db,source_key='local',source_schema={postgres_isolation.schema!r},namespace_marker=marker))
app=FastAPI();app.state.database=db;app.state.service=core;app.state.auth=AuthService(db)
stop=LocalServerStop()
controller=bind_runtime(app,stop_port=stop,native_port=port,native_root=Path({str(socket_root)!r}),source_root=Path({str(source)!r}),program_entry=Path(__file__),configuration_root=Path({str(config)!r}))
from contextlib import asynccontextmanager
original=app.router.lifespan_context
@asynccontextmanager
async def diagnosed(value):
 try:
  async with original(value):yield
 except Exception as error:
  import json,traceback
  trace=traceback.extract_tb(error.__traceback__)[-1]
  Path({str(config/'failure.json')!r}).write_text(json.dumps({{'type':type(error).__name__,'code':getattr(error,'code',None),'where':Path(trace.filename).name,'line':trace.lineno}}))
  raise
app.router.lifespan_context=diagnosed
if {wrong_uid!r}:app.state.native_control_server.expected_peer_uid=os.getuid()+1
import uvicorn
server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=8876,loop='asyncio',workers=1,reload=False,access_log=False,log_config=None,log_level='critical'))
stop.bind_exit(lambda:setattr(server,'should_exit',True));server.run()
'''
    entry.write_text(code)
    process=subprocess.Popen([sys.executable,'-I',str(entry)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    registration=socket_root/'native.json'
    deadline=time.monotonic()+8
    while not registration.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.02)
    assert registration.exists() and process.poll() is None,(config/'failure.json').read_text() if (config/'failure.json').exists() else 'native fixture did not start'
    result=dict(e=e,source=source,config=config,process=process,candidate=candidate,registration=registration,maintenance=maintenance,socket_root=socket_root,release_key=release_key)
    try:yield result
    finally:
        if process.poll() is None:process.terminate();process.wait(timeout=8)
        private.unlink(missing_ok=True)
        shutil.rmtree(socket_root)

def invoke(api,action,extra=(),confirmation=None):
    read,write=os.pipe();os.write(write,(PASSWORD+'\n').encode());os.close(write)
    confirm_read=None
    command=[sys.executable,'-I',str(api['source']/'apps/api/app/cli.py'),'--root',str(api['config']),'native-control',action,'--username',api['e'].actor,'--source-root',str(api['source']),'--native-root',str(api['socket_root']),'--password-fd',str(read),*extra]
    inherited=[read]
    if confirmation is not None:
        confirm_read,confirm_write=os.pipe();os.write(confirm_write,(confirmation+'\n').encode());os.close(confirm_write)
        command+=['--confirmation-fd',str(confirm_read)];inherited.append(confirm_read)
    try:
        result=subprocess.run(command,cwd=api['candidate']/'apps/api',env=dict(os.environ,PYTHONPATH=str(api['candidate']/'apps/api')),pass_fds=tuple(inherited),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=8)
        left=os.read(read,1024)
        return result,left
    finally:
        os.close(read)
        if confirm_read is not None:os.close(confirm_read)

def request_prepare(api):
    result,_=invoke(api,'request',['--operation','backup_local','--reason','Synthetic native CLI backup'])
    assert result.returncode==0,result.stderr
    cid=json.loads(result.stdout)['command_id']
    from app.modules.operations import runtime_models as m
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        with api['e'].db.engine.connect() as c:stage=c.execute(sa.select(m.commands.c.stage).where(m.commands.c.id==cid)).scalar_one()
        if stage=='AWAITING_NATIVE_CONFIRMATION':break
        time.sleep(.02)
    result,_=invoke(api,'prepare',['--command-id',cid]);assert result.returncode==0,result.stderr
    return cid,json.loads(result.stdout)

def test_real_native_cli_prepare_cancel_and_confirm_execute(native_api):
    api=native_api;cid,review=request_prepare(api)
    result,_=invoke(api,'cancel',['--command-id',cid]);assert result.returncode==0,result.stderr
    from app.modules.operations import runtime_models as m
    with api['e'].db.engine.connect() as c:assert c.execute(sa.select(m.commands.c.status).where(m.commands.c.id==cid)).scalar_one()=='cancelled'
    cid,review=request_prepare(api)
    result,_=invoke(api,'confirm',['--command-id',cid,'--fingerprint',review['fingerprint'],'--nonce',review['confirmation_nonce'],'--instance-id',review['installation_id']],confirmation=review['installation_id']+' '+review['fingerprint'])
    assert result.returncode==0,result.stderr
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        with api['e'].db.engine.connect() as c:record=c.execute(sa.select(m.commands).where(m.commands.c.id==cid)).mappings().one()
        if record['status']!='pending':break
        time.sleep(.05)
    assert record['status']=='applied',record['error']
    with api['e'].db.engine.connect() as c:assert c.execute(sa.select(m.backups.c.status).where(m.backups.c.command_id==cid)).scalar_one()=='completed'

def test_cli_rejects_changed_peer_pid_before_password_fd_is_read(native_api):
    api=native_api;record=json.loads(api['registration'].read_text());saved=dict(record);record['pid']=os.getpid()
    api['registration'].write_text(json.dumps(record))
    try:
        result,left=invoke(api,'status')
        assert result.returncode!=0 and 'NATIVE_PEER_UNVERIFIED' in result.stderr
        assert left.decode()==PASSWORD+'\n'
        assert api['process'].poll() is None
    finally:api['registration'].write_text(json.dumps(saved))

@pytest.mark.parametrize('native_api',['wrong_uid'],indirect=True)
def test_real_kernel_uid_mismatch_is_rejected_before_password(native_api):
    result,left=invoke(native_api,'status')
    assert result.returncode!=0 and 'NATIVE_PEER_UNVERIFIED' in result.stderr
    assert left.decode()==PASSWORD+'\n' and native_api['process'].poll() is None

def test_native_cli_refuses_artifact_changed_after_prepare(native_api):
    import base64,hashlib,rfc8785
    api=native_api;data=b'Synthetic signed release data'
    target=api['maintenance']/'data.bin';target.write_bytes(data)
    payload={'installation_id':'wordheat-local-development','operation':'upgrade_app','app_version':'0.2.0','compatibility':{'pg_major':16,'db_schema_read_versions':[1,2],'db_schema_write_version':2,'upgrade_from_schema_versions':[1]},'files':[{'path':'data.bin','bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}],'bundle':{'version':1,'model_manifest_id':'fixture-model','model_manifest_sha256':'a'*64,'wordbank_version':'sha256:'+'b'*64,'calibration_version':'sha256:'+'c'*64,'puzzles':[]}}
    envelope={'key_id':'fixture','domain':'wordheat-live.maintenance','version':1,'payload':payload}
    envelope['signature']=base64.urlsafe_b64encode(api['release_key'].sign(rfc8785.dumps(envelope))).decode().rstrip('=')
    (api['maintenance']/'tamper.json').write_text(json.dumps(envelope))
    result,_=invoke(api,'request',['--operation','upgrade_app','--reason','Synthetic artifact verification'])
    assert result.returncode==0,result.stderr
    cid=json.loads(result.stdout)['command_id']
    result,_=invoke(api,'prepare',['--command-id',cid,'--artifact-id','tamper']);assert result.returncode==0,result.stderr
    review=json.loads(result.stdout);target.write_bytes(b'Changed after native review')
    result,_=invoke(api,'confirm',['--command-id',cid,'--fingerprint',review['fingerprint'],'--nonce',review['confirmation_nonce'],'--instance-id',review['installation_id']],confirmation=review['installation_id']+' '+review['fingerprint'])
    assert result.returncode!=0 and 'MAINTENANCE_ARTIFACT_CHANGED' in result.stderr
    result,_=invoke(api,'cancel',['--command-id',cid]);assert result.returncode==0,result.stderr
