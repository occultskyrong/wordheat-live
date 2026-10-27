"""Real own relay child and explicit owned PG namespace; no official API calls."""
import json,os,socket,subprocess,sys,time
from pathlib import Path
from urllib.parse import urlsplit
import httpx
import pytest
from psycopg.rows import dict_row
from app.core import safe_connect
from app.server_config import load_runtime,load_migration
from app.composition import migrate_server


def config_for(tmp_path,service):
    fields={}
    for line in Path(os.environ['RELAY_TEST_ENV_FILE']).read_text().splitlines():
        key,sep,value=line.partition('=')
        if sep and key in {'RELAY_DATABASE_URL','RELAY_MIGRATION_DATABASE_URL'}:fields[key]=value.strip().strip('"\'')
    with service.tx() as c:
        marker=c.execute("SELECT obj_description(oid) AS marker FROM pg_namespace WHERE nspname=%s",(service.schema,)).fetchone()['marker']
    directory=tmp_path/'private';directory.mkdir(mode=0o700)
    values=fields|{'RELAY_DATABASE_NAME':'wordheat_relay_test','RELAY_DATABASE_MARKER':'wordheat-live:test:relay','WORDHEAT_ENV':'test','RELAY_RUNTIME_ROLE':urlsplit(fields['RELAY_DATABASE_URL'].replace('postgresql+psycopg://','postgresql://')).username,'RELAY_TEST_NAMESPACE':service.schema,'RELAY_TEST_NAMESPACE_MARKER':marker}
    env=directory/'.env';env.write_text(''.join(k+'='+v+'\n' for k,v in values.items()));env.chmod(0o600)
    return directory,fields


def test_owned_migrate_and_real_child_without_official_keys(relay_environment,tmp_path):
    service,*_=relay_environment;directory,fields=config_for(tmp_path,service)
    config=load_runtime(directory);migration=load_migration(directory)
    assert 'RELAY_MIGRATION_DATABASE_URL' not in repr(config) and fields['RELAY_MIGRATION_DATABASE_URL'] not in repr(config)
    console=Path(sys.executable).with_name('wordheat-relay')
    assert console.is_file(),'Install this relay package before entrypoint verification'
    run_env={k:v for k,v in os.environ.items() if not any(word in k for word in ('TOKEN','SECRET','PASSWORD','DATABASE','PRIVATE_KEY'))}
    run_env['PYTHONPATH']=str(Path(__file__).parents[1])
    migrated=subprocess.run([str(console),'migrate','--config-root',str(directory)],env=run_env,capture_output=True,text=True,timeout=10)
    assert migrated.returncode==0 and migrated.stdout.strip()=='RELAY_MIGRATION_COMPLETE'
    assert all(secret not in migrated.stdout+migrated.stderr for secret in fields.values())
    # Start the real fixed CLI in an own process, on a dynamically selected
    # loopback port. Parent PG/runtime/model processes are never touched.
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    assert port!=8124
    environment={k:v for k,v in os.environ.items() if not any(word in k for word in ('TOKEN','SECRET','PASSWORD','DATABASE','PRIVATE_KEY'))}
    environment['PYTHONPATH']=str(Path(__file__).parents[1])
    child=subprocess.Popen([str(console),'serve','--config-root',str(directory),'--port',str(port)],env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    observed=False
    try:
        with httpx.Client(base_url='http://127.0.0.1:'+str(port),timeout=.3,trust_env=False) as client:
            for _ in range(70):
                if child.poll() is not None:break
                try:
                    response=client.get('/relay/v1/health/live')
                    if response.status_code==200:observed=True;break
                except httpx.HTTPError:pass
                time.sleep(.05)
            assert observed,'Real child did not become live'
            assert response.json()=={'status':'alive'}
            denied=client.post('/relay/v1/leases',headers={'Authorization':'Bearer synthetic-ephemeral-token'})
            assert denied.status_code in (401,403) and 'lease_id' not in denied.text
            callback=client.post('/relay/v1/douyin/callback',content=b'[]')
            assert callback.status_code==503
    finally:
        child.terminate()
        try:out,err=child.communicate(timeout=5)
        except subprocess.TimeoutExpired:child.kill();out,err=child.communicate(timeout=5)
    for secret in [*fields.values(),'synthetic-ephemeral-token']:
        assert secret not in out+err
    assert observed


def test_real_cli_configuration_error_does_not_echo_ephemeral_secret(tmp_path):
    p=tmp_path/'private';p.mkdir();f=p/'.env';sentinel='never-write-token-to-output'
    f.write_text('ACCESS_TOKEN='+sentinel+'\n');f.chmod(0o600)
    environment={'PATH':os.environ['PATH'],'PYTHONPATH':str(Path(__file__).parents[1])}
    result=subprocess.run([sys.executable,'-m','app.cli','serve','--config-root',str(p)],env=environment,capture_output=True,text=True,timeout=5)
    assert result.returncode==1 and result.stderr.strip()=='RELAY_COMMAND_FAILED' and sentinel not in result.stdout+result.stderr
