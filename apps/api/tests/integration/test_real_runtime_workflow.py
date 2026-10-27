"""Actual offline encoder + IPC dev bootstrap + Runtime + isolated PG + HTTP.

Owned lexical fixtures exercise execution, not human G1-Q qualification.
"""
import asyncio,importlib.util,os,secrets,time
from pathlib import Path
import sqlalchemy as sa
from test_local_flow import environment


def test_real_bootstrap_runtime_http_wordbank_and_multiround(environment):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.modules.scoring.runtime import ScorerRuntime
    from app.infrastructure.db import schema as s
    db,actor=environment
    root=Path(__file__).resolve().parents[4]
    manifest=root/'resources/model-manifest.json'
    model=Path(os.environ.get('WORDHEAT_TEST_MODEL_ROOT','/Users/jiachenclaw/code/wordheat-live/.local/models/bge-small-zh-v1.5'))
    helper=root/'scripts/backend/20261025-run-local.py'
    spec=importlib.util.spec_from_file_location('workflow_dev_bootstrap',helper);host=importlib.util.module_from_spec(spec);spec.loader.exec_module(host)
    identity,sha=host.manifest_identity(manifest); secret=secrets.token_bytes(32); owned=[]
    command=[str(root/'apps/scorer/app/runtime_entry.py'),'--source-root',str(root),'--config-root',str(root),'--manifest',str(manifest),'--model-root',str(model),'--fds']
    try:
        child,fd,boot=host.spawn_child('scorer',Path('/Users/jiachenclaw/code/wordheat-live/apps/scorer/.venv/bin/python'),root/'apps/scorer',command,secret,secrets.token_hex(32),sha,'127.0.0.1:8124',owned)
        host.await_ack(child,fd,boot,secret,120)
        runtime=ScorerRuntime(secret,identity['manifest_id'],sha,identity['dimension'],manifest_metadata=identity)
        app=runtime.wrap_lifespan(create_app(db.settings,scorer=runtime))
        with TestClient(app,headers={'Origin':'http://testserver','X-Contract-Version':'LW-C1/1.0.0','X-BE-Extension-Version':'LW-BE/1.1.0'}) as operator:
            logged=operator.post('/api/v1/auth/sessions',json={'username':'testoperator','password':'private-test-password-123'})
            assert logged.status_code==200,logged.text
            operator.headers['X-CSRF-Token']=logged.json()['data']['csrf_token']
            counter=[0]
            def post(path,body):
                counter[0]+=1
                response=operator.post(path,json=body,headers={'Idempotency-Key':'real-workflow-'+str(counter[0])})
                assert response.status_code in {200,202},response.text
                return response.json()['data']
            def wait_job(jid):
                deadline=time.monotonic()+20
                while time.monotonic()<deadline:
                    result=operator.get('/api/v1/scoring-evaluations/'+jid).json()['data']
                    if result['status'] in {'succeeded','failed'}:
                        assert result['status']=='succeeded',result
                        return result
                    time.sleep(.05)
                raise AssertionError('evaluation did not terminate')
            puzzle=post('/api/v1/puzzles',{'word':'苹果','aliases':['林檎'],'category_id':'real-workflow','difficulty':1,'hints':[{'stage':1,'text':'一种水果'}],'explanation':'蔷薇科水果，执行验证用词。','source_ref':'owned lexical execution fixture; not G1-Q human annotations'})
            pairs=[{'guess':word,'human_rating':rating} for word,rating in [('电脑','cold'),('石头','cold'),('果园','warm'),('水果','hot'),('苹果树','hot')]]
            calibration=post('/api/v1/scoring-evaluations',{'puzzle_version_id':puzzle['id'],'manifest_id':identity['manifest_id'],'split':'calibration','pairs':pairs});wait_job(calibration['job_id'])
            blindpairs=[{'guess':word,'human_rating':rating} for word,rating in [('水泥','cold'),('火山','cold'),('梨子','warm'),('红苹果','hot'),('青苹果','hot')]]
            blind=post('/api/v1/scoring-evaluations',{'puzzle_version_id':puzzle['id'],'manifest_id':identity['manifest_id'],'split':'blind','pairs':blindpairs}); evaluated=wait_job(blind['job_id'])
            assert evaluated['result']['metrics']['pairwise_accuracy']>=.9,evaluated['result']
            post('/api/v1/puzzles/'+puzzle['puzzle_id']+'/commands',{'command':'approve','expected_version':1,'args':{'evaluation_id':blind['job_id'],'decision':'approve','notes':'Execution fixture approval only; no global quality assertion'}})
            post('/api/v1/puzzles/'+puzzle['puzzle_id']+'/commands',{'command':'publish','expected_version':1,'args':{'approved_version':1}})
            session=post('/api/v1/live-sessions',{'label':'Real Runtime workflow','mode':'rehearsal','run_purpose':'local','rules':{'category_id':'real-workflow','duration_seconds':30,'intermission_seconds':3,'auto_next':True}});sid=session['session_id']
            ticket=post('/api/v1/display-tickets',{'session_id':sid,'purpose':'primary'})
            from contextlib import nullcontext
            with nullcontext(TestClient(app,headers={'Origin':'http://testserver'})) as display:
                exchanged=display.post('/api/v1/display-sessions',json={'ticket':ticket['ticket']});assert exchanged.status_code==200
                ds=exchanged.json()['data'];display.headers['X-CSRF-Token']=ds['receipt_csrf_token']
                snap=display.get('/api/v1/display/sessions/'+sid+'/snapshot').json()['data']
                with display.websocket_connect('/api/v1/display/ws') as ws:
                    ws.send_json({'kind':'resume','schema_version':1,'contract_version':'LW-C1/1.0.0','session_id':sid,'after_seq':snap['as_of_seq']})
                    ready=ws.receive_json(); assert ready['kind']=='resume_ready'
                    post('/api/v1/live-sessions/'+sid+'/commands',{'command':'start','expected_version':session['version'],'args':{'run_purpose':'local'}})
                    with db.engine.connect() as c: first=dict(c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid)).mappings().one())
                    accepted=post('/api/v1/rehearsal/guesses',{'session_id':sid,'round_id':first['id'],'short_code':first['short_code'],'word':'香蕉','display_name':'演练执行验证'})
                    deadline=time.monotonic()+5
                    while time.monotonic()<deadline:
                        with db.engine.connect() as c: guess=c.execute(sa.select(s.guesses).where(s.guesses.c.id==accepted['guess_id'])).mappings().one()
                        if guess['status']!='queued': break
                        time.sleep(.05)
                    assert guess['status']=='scored' and 0<=guess['display_score']<=99.9 and not guess['solved']
                    # Protocol consumer really receives this frame before receipts.
                    effect=None
                    for _ in range(30):
                        frame=ws.receive_json()
                        for item in frame.get('payload',{}).get('effects',[]):
                            if item['feedback_id']==accepted['guess_id']: effect=item
                        if effect: break
                    assert effect
                    for stage in ('received','processed'):
                        receipt={'display_instance_id':ds['display_instance_id'],'render_generation':ready['render_generation'],'batch_id':'real-'+stage,'stage':stage,'deliveries':[{'delivery_id':effect['delivery_ids'][0],'feedback_id':effect['feedback_id'],'client_time':'2026-10-26T00:00:00.000Z'}]}
                        assert display.post('/api/v1/display/receipts',json=receipt).status_code==200
                    time.sleep(1.05)
                    hit=post('/api/v1/rehearsal/guesses',{'session_id':sid,'round_id':first['id'],'short_code':first['short_code'],'word':'林檎','display_name':'演练执行验证'})
                    assert hit['accepted']
                    deadline=time.monotonic()+6;second=None
                    while time.monotonic()<deadline:
                        with db.engine.connect() as c: second=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid,s.rounds.c.id!=first['id'])).mappings().first()
                        if second: break
                        time.sleep(.1)
                    assert second and second['short_code']!=first['short_code']
                    assert display.get('/api/v1/display/sessions/'+sid+'/snapshot').json()['data']['latest_feedbacks']==[]
                    rid=second['id']
                    paused=post('/api/v1/rounds/'+rid+'/commands',{'command':'pause','expected_version':second['version'],'args':{}})
                    hint=post('/api/v1/rounds/'+rid+'/commands',{'command':'reveal_hint','expected_version':paused['resource_version'],'args':{'stage':1}})
                    resumed=post('/api/v1/rounds/'+rid+'/commands',{'command':'resume','expected_version':hint['resource_version'],'args':{}})
                    post('/api/v1/rounds/'+rid+'/commands',{'command':'void','expected_version':resumed['resource_version'],'args':{'reason':'Execution workflow finish'}})
                    current=operator.get('/api/v1/live-sessions/'+sid).json()['data']
                    ended=post('/api/v1/live-sessions/'+sid+'/commands',{'command':'end','expected_version':current['version'],'args':{'reason':'done'}})
                    assert ended['state']=='ENDED'
                    traces=operator.get('/api/v1/messages',params={'session_id':sid}).json()['data']['items'];assert len(traces)==2
                    assert all(operator.get('/api/v1/messages/'+m['message_id']+'/trace').status_code==200 for m in traces)
                    with db.engine.connect() as c:
                        points=c.execute(sa.select(sa.func.sum(s.ledger.c.delta)).join(s.rounds,s.rounds.c.id==s.ledger.c.round_id).where(s.rounds.c.session_id==sid)).scalar_one()
                        assert points>=21
            replay=post('/api/v1/replays',{'label':'Real Runtime replay','events':[{'source_id':'replay-one','offset_ms':1000,'guess':'香蕉','participant_key':'viewer-a','display_name':'回放甲'},{'source_id':'replay-two','offset_ms':3000,'guess':'苹果','participant_key':'viewer-b','display_name':'回放乙'}]})
            jid=replay['job_id']; replay_sid=replay['result']['replay_session_id']
            replay_ticket=post('/api/v1/display-tickets',{'session_id':replay_sid,'purpose':'primary'})
            replay_display=TestClient(app,headers={'Origin':'http://testserver'})
            assert replay_display.post('/api/v1/display-sessions',json={'ticket':replay_ticket['ticket']}).status_code==200
            replay_snap=replay_display.get('/api/v1/display/sessions/'+replay_sid+'/snapshot').json()['data']
            with replay_display.websocket_connect('/api/v1/display/ws') as replay_ws:
                replay_ws.send_json({'kind':'resume','schema_version':1,'contract_version':'LW-C1/1.0.0','session_id':replay_sid,'after_seq':replay_snap['as_of_seq']})
                assert replay_ws.receive_json()['kind']=='resume_ready'
                post('/api/v1/replays/'+jid+'/commands',{'command':'start','expected_version':replay['version'],'args':{}})
                deadline=time.monotonic()+8
                while time.monotonic()<deadline:
                    status=operator.get('/api/v1/replays/'+jid).json()['data']
                    if status['status'] in {'succeeded','failed'}: break
                    time.sleep(.05)
                assert status['status']=='succeeded' and status['result']['events_applied']==2,status
                with db.engine.connect() as c:
                    assert c.execute(sa.select(sa.func.count()).select_from(s.participants).where(s.participants.c.session_id==replay_sid)).scalar_one()==2
                    assert set(c.execute(sa.select(s.guesses.c.source_mode).where(s.guesses.c.session_id==replay_sid)).scalars())=={'replay'}
            with db.engine.begin() as c:
                bundle=app.state.service.export_verified_bundle(c)
                assert app.state.service.validate_verified_bundle(c,bundle)['model_manifest_sha256']==sha
                permit=app.state.service.bundle_permit_issuer()(bundle,actor,'real-workflow-native-confirmed-fixture')
                activated=app.state.service.activate_verified_bundle(c,permit)
                assert activated['bundle_digest']
                resources=app.state.service.export_verified_bundle_resources(c,bundle)
                import json,hashlib
                raw=json.dumps(resources,ensure_ascii=False).encode()
                resource_permit=app.state.service.bundle_resource_permit_issuer()(bundle,raw,hashlib.sha256(raw).hexdigest(),actor,'native-import-roundtrip')
                imported=app.state.service.import_verified_bundle_resources(c,resource_permit)
                assert imported['puzzle_versions']==[puzzle['id']]
                assert app.state.service.import_verified_bundle_resources(c,resource_permit)==imported
                # New immutable IDs are absent from DB: native import must not
                # require a pre-existing catalog or Web evaluation during maintenance.
                import copy
                from app.modules.wordbank.bundle import digest
                fresh=copy.deepcopy(resources);entry=fresh['puzzles'][0]
                entry['puzzle_version_id']='native-new-puzzle-version';entry['puzzle_id']='native-new-puzzle';entry['calibration_id']='native-new-calibration';entry['evaluation_id']='native-new-evaluation'
                source=entry['evaluation_input'];source['puzzle_version_id']=entry['puzzle_version_id'];source['frozen_puzzle']['id']=entry['puzzle_version_id'];source['frozen_puzzle']['puzzle_id']=entry['puzzle_id']
                source['puzzle_input_sha256']=digest(source['frozen_puzzle']);source['evaluation_input_sha256']=digest({key:value for key,value in source.items() if key!='evaluation_input_sha256'})
                entry['scoring_artifact']['review']['evaluation_id']=entry['evaluation_id'];entry['evaluation_result']['calibration_id']=entry['calibration_id']
                fresh_identity=app.state.service.describe_bundle_resources(fresh);fresh_bytes=json.dumps(fresh,ensure_ascii=False).encode()
                assert not c.execute(sa.select(s.puzzles.c.id).where(s.puzzles.c.id==entry['puzzle_version_id'])).first()
                fresh_permit=app.state.service.bundle_resource_permit_issuer()(fresh_identity,fresh_bytes,hashlib.sha256(fresh_bytes).hexdigest(),actor,'native-import-absent-versions')
                fresh_import=app.state.service.import_verified_bundle_resources(c,fresh_permit)
                assert fresh_import['imported_count']==1
                fresh_activation=app.state.service.activate_verified_bundle(c,app.state.service.bundle_permit_issuer()(fresh_identity,actor,'native-activate-absent-versions'))
                assert fresh_activation['changed'] and fresh_activation['bundle_digest']!=activated['bundle_digest']

            print('REAL_RUNTIME_HTTP_WORKFLOW: IPC boot, model calibration/blind, approval/publish, primary resume, async scoring, exact alias, two rounds, receipt, pause/hint/resume/void/end, private trace, isolated multi-participant replay')
    finally: host.stop_children(owned)

# Native resources round-trip is exercised inside the real Runtime workflow above.
