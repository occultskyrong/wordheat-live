"""Actual BGE deployment/manifest cold restart; same weights, no G1 quality claim."""
import base64,copy,hashlib,json,os,select,subprocess,tempfile,time
from pathlib import Path
import pytest,rfc8785
import sqlalchemy as sa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from app.infrastructure.db import schema as s
from app.modules.auth.service import bootstrap_operator
from app.modules.wordbank.resources import describe

PASSWORD='Synthetic-cold-restart-password-123'

def digest(value):return hashlib.sha256(rfc8785.dumps(value)).hexdigest()
def private(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2));path.chmod(0o600)
def read_line(process,seconds):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        if select.select([process.stdout],[],[],.1)[0]:return process.stdout.readline().strip()
        if process.poll() is not None:break
    return ''

def test_actual_two_manifest_cold_restart_imports_new_resources_and_activates(postgres_isolation):
    required=['WORDHEAT_TEST_MODEL_ROOT','WORDHEAT_TEST_API_PYTHON','WORDHEAT_TEST_SCORER_PYTHON']
    if any(not os.environ.get(key) for key in required):pytest.skip('Explicit registered real model/interpreters required')
    import httpx
    source=Path(__file__).resolve().parents[5]
    api_python=Path(os.environ[required[1]]);scorer_python=Path(os.environ[required[2]]);model=Path(os.environ[required[0]])
    first=json.loads((source/'resources/model-manifest.json').read_text());second=copy.deepcopy(first);second['manifest_id']=first['manifest_id']+'-deployment-v2';sha2=digest(second)
    signer=Ed25519PrivateKey.generate();public=signer.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    with postgres_isolation.temporary_schema() as namespace,tempfile.TemporaryDirectory(prefix='wh-cold-',dir='/private/tmp') as directory:
        root=Path(directory);root.chmod(0o700);maintenance=root/'.local/maintenance';maintenance.mkdir(parents=True,mode=0o700)
        owner,marker=postgres_isolation.owned[namespace]
        with postgres_isolation.admin.begin() as c:
            c.execute(sa.text("SELECT set_config('search_path',:name,true)"),{'name':namespace})
            installation=c.execute(sa.select(s.instance.c.instance_id)).scalar_one();actor=bootstrap_operator(c,'cold-operator',PASSWORD,'Cold deployment fixture')
        ns=root/'namespace.json';private(ns,{'version':1,'database':'wordheat_test','database_marker':'wordheat-live:test:local','installation_id':installation,'namespace':namespace,'owner':owner,'comment':marker})
        keys=root/'release-keys.json';private(keys,{'version':1,'keys':[{'key_id':'fixture-release','public_key_b64url':base64.urlsafe_b64encode(public).decode().rstrip('='),'purposes':['release_artifact']}]})
        env=root/'.env';env.write_text('WORDHEAT_ENV=test\nWORDHEAT_INSTANCE_ID='+installation+'\nLOCAL_DATABASE_URL='+postgres_isolation.runtime.render_as_string(hide_password=False)+'\nLOCAL_ANALYTICS_CURSOR_KEY='+('c'*64)+'\nLOCAL_TEST_NAMESPACE_FILE='+str(ns)+'\nLOCAL_MAINTENANCE_ROOT='+str(maintenance)+'\nLOCAL_RELEASE_PUBLIC_KEYS_FILE='+str(keys)+'\n');env.chmod(0o600)
        command=[str(api_python),'-B',str(source/'scripts/backend/20261025-run-local.py'),'--root',str(source),'--config-root',str(root),'--manifest',str(source/'resources/model-manifest.json'),'--model-root',str(model),'--api-python',str(api_python),'--scorer-python',str(scorer_python),'--installation-id',installation,'--bundle-root',str(maintenance)]
        parent=subprocess.Popen(command,cwd=source,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env={'PATH':os.environ.get('PATH',''),'LANG':'en_US.UTF-8','PYTHONDONTWRITEBYTECODE':'1'})
        try:
            first_ready=read_line(parent,150)
            if first_ready!='DEV_SERVICES_READY':
                safe=parent.stderr.read() if parent.poll() is not None else 'HOST_STARTUP_TIMEOUT'
                raise AssertionError('Registered first generation failed: '+safe)
            registration=json.loads((root/'.local/runtime/native.json').read_text());old_pid=registration['pid'];old_boot=registration['boot_id']
            def native(action,extra=(),confirm=None):
                read,write=os.pipe();os.write(write,(PASSWORD+'\n').encode());os.close(write);fds=[read];confirm_read=None
                argv=[str(api_python),'-I',str(source/'apps/api/app/cli.py'),'--root',str(root),'native-control',action,'--source-root',str(source),'--username','cold-operator','--password-fd',str(read),*extra]
                if confirm:
                    confirm_read,writer=os.pipe();os.write(writer,(confirm+'\n').encode());os.close(writer);fds.append(confirm_read);argv+=['--confirmation-fd',str(confirm_read)]
                try:
                    result=subprocess.run(argv,cwd=source/'apps/api',pass_fds=tuple(fds),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=15,env={'PATH':os.environ.get('PATH',''),'LANG':'en_US.UTF-8'})
                    assert result.returncode==0,result.stderr
                    assert PASSWORD not in result.stdout+result.stderr
                    return json.loads(result.stdout)
                finally:
                    for fd in fds:os.close(fd)
            with httpx.Client(base_url='http://127.0.0.1:8765',trust_env=False,headers={'Origin':'http://127.0.0.1:8765','X-Contract-Version':'LW-C1/1.0.0','X-BE-Extension-Version':'LW-BE/1.1.0'},timeout=10) as client:
                logged=client.post('/api/v1/auth/sessions',json={'username':'cold-operator','password':PASSWORD});assert logged.status_code==200
                client.headers['X-CSRF-Token']=logged.json()['data']['csrf_token'];counter=[0]
                def post(path,body):
                    counter[0]+=1;response=client.post(path,json=body,headers={'Idempotency-Key':'cold-'+str(counter[0])});assert response.status_code in {200,202},response.text;return response.json()['data']
                def job(jid):
                    end=time.monotonic()+25
                    while time.monotonic()<end:
                        data=client.get('/api/v1/scoring-evaluations/'+jid).json()['data']
                        if data['status'] in {'succeeded','failed'}:assert data['status']=='succeeded',data['error'];return data
                        time.sleep(.05)
                    raise AssertionError('Actual evaluation did not terminate')
                end=time.monotonic()+10
                while time.monotonic()<end:
                    ready=client.get('/api/v1/health/ready').json()['data']
                    if ready['checks']['model']['status']=='pass':break
                    time.sleep(.05)
                puzzle=post('/api/v1/puzzles',{'word':'苹果','aliases':['林檎'],'category_id':'cold-fixture','difficulty':1,'hints':[],'explanation':'Deployment execution fixture','source_ref':'owned lexical labels; not G1 qualification'})
                calibration=post('/api/v1/scoring-evaluations',{'puzzle_version_id':puzzle['id'],'manifest_id':first['manifest_id'],'split':'calibration','pairs':[{'guess':a,'human_rating':b} for a,b in [('电脑','cold'),('石头','cold'),('果园','warm'),('水果','hot'),('苹果树','hot')]]});job(calibration['job_id'])
                blind=post('/api/v1/scoring-evaluations',{'puzzle_version_id':puzzle['id'],'manifest_id':first['manifest_id'],'split':'blind','pairs':[{'guess':a,'human_rating':b} for a,b in [('水泥','cold'),('火山','cold'),('梨子','warm'),('红苹果','hot'),('青苹果','hot')]]});evaluated=job(blind['job_id'])
                assert evaluated['result']['metrics']['pairwise_accuracy']>=.9
                post('/api/v1/puzzles/'+puzzle['puzzle_id']+'/commands',{'command':'approve','expected_version':1,'args':{'evaluation_id':blind['job_id'],'decision':'approve','notes':'Execution fixture, not global G1'}})
                post('/api/v1/puzzles/'+puzzle['puzzle_id']+'/commands',{'command':'publish','expected_version':1,'args':{'approved_version':1}})
            with postgres_isolation.admin.connect() as c:
                c.execute(sa.text("SELECT set_config('search_path',:name,false)"),{'name':namespace})
                p=c.execute(sa.select(s.puzzles).where(s.puzzles.c.id==puzzle['id'])).mappings().one();evaluation=c.execute(sa.select(s.wordbank_jobs).where(s.wordbank_jobs.c.id==blind['job_id'])).mappings().one()
                definition={k:p[k] for k in ['word','aliases','category_id','difficulty','hints','explanation','source_ref']}
                entry={'puzzle_version_id':p['id'],'puzzle_id':p['puzzle_id'],'version':p['version'],'definition':definition,'calibration_id':p['calibration_id'],'evaluation_id':p['evaluation_id'],'scoring_artifact':p['scoring_artifact'],'evaluation_input':evaluation['input'],'evaluation_result':evaluation['result'],'annotations_locked_at':evaluation['annotations_locked_at'].isoformat()}
            entry=copy.deepcopy(entry);entry.update(puzzle_version_id='cold-v2-puzzle-version',puzzle_id='cold-v2-puzzle',calibration_id='cold-v2-calibration',evaluation_id='cold-v2-evaluation')
            body=entry['evaluation_input'];body.update(puzzle_version_id=entry['puzzle_version_id'],manifest_id=second['manifest_id'],model_manifest_sha256=sha2,model_manifest=second)
            body['frozen_puzzle'].update(id=entry['puzzle_version_id'],puzzle_id=entry['puzzle_id']);body['puzzle_input_sha256']=digest(body['frozen_puzzle']);body['evaluation_input_sha256']=digest({k:v for k,v in body.items() if k!='evaluation_input_sha256'})
            entry['scoring_artifact']['manifest_sha256']=sha2;entry['scoring_artifact']['review']['evaluation_id']=entry['evaluation_id']
            entry['evaluation_result'].update(manifest_id=second['manifest_id'],manifest_sha256=sha2,calibration_id=entry['calibration_id'])
            resources={'version':1,'model_manifest':second,'puzzles':[entry]};identity=describe(resources)
            manifest2=maintenance/'model-v2.json';private(manifest2,second);resource=maintenance/'resources-v2.json';private(resource,resources)
            files=[{'path':p.name,'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in [manifest2,resource]]
            payload={'installation_id':installation,'operation':'switch_model','compatibility':{'pg_major':16,'db_schema_read_versions':[1],'db_schema_write_version':1},'model_manifest':manifest2.name,'model_root_id':'current','bundle_resource':{'path':resource.name,'sha256':files[1]['sha256']},'files':files,'bundle':identity}
            envelope={'key_id':'fixture-release','domain':'wordheat-live.maintenance','version':1,'payload':payload};envelope['signature']=base64.urlsafe_b64encode(signer.sign(rfc8785.dumps(envelope))).decode().rstrip('=');private(maintenance/'switch-v2.json',envelope)
            requested=native('request',['--operation','switch_model','--reason','Same BGE files, different frozen deployment manifest']);cid=requested['command_id']
            review=native('prepare',['--command-id',cid,'--artifact-id','switch-v2'])
            native('confirm',['--command-id',cid,'--fingerprint',review['fingerprint'],'--nonce',review['confirmation_nonce'],'--instance-id',installation],confirm=installation+' '+review['fingerprint'])
            assert read_line(parent,30)=='DEV_BUNDLE_RESTARTING'
            assert read_line(parent,150)=='DEV_SERVICES_READY'
            new_registration=json.loads((root/'.local/runtime/native.json').read_text())
            assert new_registration['pid']!=old_pid and new_registration['boot_id']!=old_boot
            with pytest.raises(ProcessLookupError):os.kill(old_pid,0)
            end=time.monotonic()+25;record=None
            from app.modules.operations import runtime_models as runtime
            while time.monotonic()<end:
                with postgres_isolation.admin.connect() as c:
                    c.execute(sa.text("SELECT set_config('search_path',:name,false)"),{'name':namespace})
                    record=c.execute(sa.select(runtime.commands).where(runtime.commands.c.id==cid)).mappings().one()
                if record['status']!='pending':break
                time.sleep(.05)
            assert record['status']=='applied',{'error':record['error'],'observation':record['result']}
            with postgres_isolation.admin.connect() as c:
                c.execute(sa.text("SELECT set_config('search_path',:name,false)"),{'name':namespace})
                assert c.execute(sa.select(s.puzzles.c.manifest_id).where(s.puzzles.c.id==entry['puzzle_version_id'])).scalar_one()==second['manifest_id']
                assert c.execute(sa.select(s.catalog.c.joint_identity).where(s.catalog.c.id=='global')).scalar_one()==identity
            pointer=json.loads((maintenance/'.active-bundle.json').read_text());assert pointer['bundle']==identity
            native('request',['--runtime-command','stop','--reason','Complete cold-deployment execution fixture'])
            assert read_line(parent,20)=='DEV_SERVICES_STOPPED' and parent.wait(timeout=5)==0
            assert postgres_isolation.sentinel_exists()
        finally:
            if parent.poll() is None:parent.terminate();parent.wait(timeout=20)
            parent.stdout.close();parent.stderr.close()
