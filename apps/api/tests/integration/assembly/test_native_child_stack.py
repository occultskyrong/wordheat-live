"""Actual standard API child and fixed Native CLI, under a parent-owned PG schema."""
import json,os,select,subprocess,tempfile,time
from pathlib import Path
import pytest
import sqlalchemy as sa
import httpx


def read_line(process,deadline):
    while time.monotonic()<deadline:
        if select.select([process.stdout],[],[],.2)[0]:return process.stdout.readline().strip()
        if process.poll() is not None:break
    return ''

def test_actual_host_api_child_native_identity_and_owned_stop(postgres_isolation):
    required=['WORDHEAT_TEST_MODEL_ROOT','WORDHEAT_TEST_SCORER_PYTHON','WORDHEAT_TEST_API_PYTHON']
    if any(not os.environ.get(key) for key in required):pytest.skip('Explicit owned child interpreters/model root required')
    source=Path(__file__).resolve().parents[5];api_python=Path(os.environ[required[2]]);scorer_python=Path(os.environ[required[1]]);model=Path(os.environ[required[0]])
    from app.infrastructure.db import schema as s
    from app.modules.auth.service import bootstrap_operator
    with postgres_isolation.temporary_schema() as namespace,tempfile.TemporaryDirectory(prefix='wh-child-',dir='/private/tmp') as directory:
        root=Path(directory);root.chmod(0o700);owner,marker=postgres_isolation.owned[namespace]
        with postgres_isolation.admin.begin() as c:
            c.execute(sa.text("SELECT set_config('search_path',:name,true)"),{'name':namespace})
            installation=c.execute(sa.select(s.instance.c.instance_id)).scalar_one()
            bootstrap_operator(c,'child-operator','private-child-test-password','Native child fixture')
        registration={'version':1,'database':'wordheat_test','database_marker':'wordheat-live:test:local','installation_id':installation,'namespace':namespace,'owner':owner,'comment':marker}
        file=root/'namespace.json';file.write_text(json.dumps(registration));file.chmod(0o600)
        # Signed beta-grant fixture only: no room binding/candidate/gates, so it
        # cannot authorize official production. Exercise the real native importer.
        import base64
        from datetime import datetime,timedelta,timezone
        import rfc8785
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        signer=Ed25519PrivateKey.generate();encode=lambda data:base64.urlsafe_b64encode(data).rstrip(b'=').decode()
        keyfile=root/'gate-roots.json'
        keyfile.write_text(json.dumps({'version':1,'keys':[{'key_id':'native-fixture','public_key_b64url':encode(signer.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)),'domains':['wordheat-live.readiness'],'types':['live_grant'],'test_only':True}]}));keyfile.chmod(0o600)
        clock=datetime.now(timezone.utc)
        envelope={'domain':'wordheat-live.readiness','type':'live_grant','version':1,'key_id':'native-fixture','payload':{'object_id':'native-grant-fixture','issuer_id':'native-fixture','issuer_revision':1,'authorization_id':'native-beta-fixture','app_id':'fixture-app','room_binding_id':'unbound-fixture','installation_id':installation,'candidate_digest':'fixture-no-candidate','kind':'controlled_beta','allowed_profiles':['beta'],'not_before':(clock-timedelta(seconds=1)).isoformat(),'expires_at':(clock+timedelta(minutes=10)).isoformat(),'max_session_duration_ms':60000}}
        envelope['signature']=encode(signer.sign(rfc8785.dumps(envelope)))
        control=root/'.local/control';control.mkdir(parents=True,mode=0o700)
        sourcefile=control/'native-grant.json';sourcefile.write_text(json.dumps(envelope));sourcefile.chmod(0o600)
        configuration=root/'.env'
        dsn=postgres_isolation.runtime.render_as_string(hide_password=False)
        configuration.write_text('WORDHEAT_ENV=test\nWORDHEAT_INSTANCE_ID='+installation+'\nLOCAL_DATABASE_URL='+dsn+'\nLOCAL_ANALYTICS_CURSOR_KEY='+('a'*64)+'\nLOCAL_TEST_NAMESPACE_FILE='+str(file)+'\nLOCAL_GATE_TRUST_ROOTS_FILE='+str(keyfile)+'\n');configuration.chmod(0o600)
        command=[str(api_python),'-B',str(source/'scripts/backend/20261025-run-local.py'),'--root',str(source),'--config-root',str(root),'--manifest',str(source/'resources/model-manifest.json'),'--model-root',str(model),'--api-python',str(api_python),'--scorer-python',str(scorer_python)]
        parent=subprocess.Popen(command,cwd=source,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env={'PATH':os.environ.get('PATH',''),'LANG':'en_US.UTF-8','PYTHONDONTWRITEBYTECODE':'1'})
        try:
            ready=read_line(parent,time.monotonic()+150)
            if ready!='DEV_SERVICES_READY':
                reason=parent.stderr.read() if parent.poll() is not None else 'HOST_STARTUP_TIMEOUT'
                pytest.fail('Actual child startup failed: '+reason)
            with httpx.Client(base_url='http://127.0.0.1:8765',trust_env=False,headers={'Origin':'http://127.0.0.1:8765','X-Contract-Version':'LW-C1/1.0.0','X-BE-Extension-Version':'LW-BE/1.1.0'},timeout=5) as client:
                login=client.post('/api/v1/auth/sessions',json={'username':'child-operator','password':'private-child-test-password'})
                assert login.status_code==200,login.text
                client.headers['X-CSRF-Token']=login.json()['data']['csrf_token']
                created=client.post('/api/v1/live-sessions',json={'label':'Real standard child','mode':'rehearsal','run_purpose':'local','rules':{}},headers={'Idempotency-Key':'actual-child-create'})
                assert created.status_code==200,created.text
                assert client.get('/api/v1/runtime/status').status_code==200
            # Native branch must not read .env. The running API already loaded its
            # explicit private configuration, while the CLI uses only OS socket identity.
            def native(action,extra=(),confirmation=None):
                readfd,writefd=os.pipe();confirmationfd=None
                try:
                    os.write(writefd,b'private-child-test-password\n');os.close(writefd);writefd=None
                    argv=[str(api_python),'-I',str(source/'apps/api/app/cli.py'),'--root',str(root),'native-control',action,'--source-root',str(source),'--username','child-operator','--password-fd',str(readfd),*extra]
                    descriptors=[readfd]
                    if confirmation is not None:
                        confirmationfd,confirmationwrite=os.pipe();os.write(confirmationwrite,(confirmation+'\n').encode());os.close(confirmationwrite)
                        argv.extend(['--confirmation-fd',str(confirmationfd)]);descriptors.append(confirmationfd)
                    result=subprocess.run(argv,cwd=source/'apps/api',pass_fds=tuple(descriptors),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=15,env={'PATH':os.environ.get('PATH',''),'LANG':'en_US.UTF-8','PYTHONDONTWRITEBYTECODE':'1'})
                    assert result.returncode==0, 'Native CLI rejected actual owned process: '+result.stderr
                    assert 'private-child-test-password' not in result.stdout+result.stderr
                    return json.loads(result.stdout)
                finally:
                    os.close(readfd)
                    if confirmationfd is not None:os.close(confirmationfd)
                    if writefd is not None:os.close(writefd)
            keyplan=native('device.prepare')
            initialized=native('device.confirm',('--command-id',keyplan['command_id'],'--fingerprint',keyplan['fingerprint'],'--nonce',keyplan['confirmation_nonce'],'--instance-id',installation),confirmation=installation+' '+keyplan['fingerprint'])
            assert initialized['status']=='device_key_ready' and len(initialized['device_key_sha256'])==64
            assert configuration.stat().st_mode&0o777==0o600
            text=configuration.read_text();assert text.count('LOCAL_DEVICE_PRIVATE_KEY_B64URL=')==1
            assert 'device_token' not in text and 'launch_token' not in text
            configuration.rename(root/'server-config-held-private')
            status=native('status');assert status['installation_id']==installation
            prepared=native('trust.prepare',('--source-id','native-grant'))
            confirmed=native('trust.confirm',('--command-id',prepared['command_id'],'--fingerprint',prepared['fingerprint'],'--nonce',prepared['confirmation_nonce'],'--instance-id',installation),confirmation=installation+' '+prepared['fingerprint'])
            assert confirmed['status']=='applied' and confirmed['object_id']=='native-grant-fixture'
            assert len(confirmed['signature_input_digest'])==64 and confirmed['signature_input_digest']!=confirmed['file_sha256']
            from app.modules.connectors import models as connector_models
            with postgres_isolation.admin.connect() as c:
                c.execute(sa.text("SELECT set_config('search_path',:name,true)"),{'name':namespace})
                stored=c.execute(sa.select(connector_models.trust_objects).where(connector_models.trust_objects.c.object_id=='native-grant-fixture')).mappings().one()
                assert stored['type']=='live_grant' and stored['digest']==confirmed['signature_input_digest']
            stopped=native('request',('--runtime-command','stop','--reason','owned child integration stop'))
            assert stopped['status'] in {'pending','applied'}
            assert read_line(parent,time.monotonic()+20)=='DEV_SERVICES_STOPPED'
            assert parent.wait(timeout=5)==0
            assert postgres_isolation.sentinel_exists()
            with postgres_isolation.admin.connect() as c:
                c.execute(sa.text("SELECT set_config('search_path',:name,true)"),{'name':namespace})
                assert c.execute(sa.select(sa.func.count()).select_from(s.sessions)).scalar_one()==1
            print('STANDARD_CHILD_NATIVE_WORKFLOW: real parent/API/scorer, owned namespace, HTTP preparation, OS-verified CLI without env, native key initialization, signed beta grant import, acknowledged owned stop')
        finally:
            if parent.poll() is None:
                parent.terminate()
                try:parent.wait(timeout=15)
                except subprocess.TimeoutExpired:parent.kill();parent.wait(timeout=5)
            parent.stdout.close();parent.stderr.close()
