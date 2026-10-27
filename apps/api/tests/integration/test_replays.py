import sqlalchemy as sa
from test_local_flow import environment,ComponentScorer
from fastapi.testclient import TestClient

def client_for(db):
    from app.main import create_app
    class Score(ComponentScorer):
        async def score(self,word,frozen_puzzle): return 80.
    client=TestClient(create_app(db.settings,scorer=Score(),display_probe=lambda **kw:True),headers={'Origin':'http://testserver','X-BE-Extension-Version':'LW-BE/1.1.0'})
    logged=client.post('/api/v1/auth/sessions',json={'username':'testoperator','password':'private-test-password-123'}).json()['data']; client.headers['X-CSRF-Token']=logged['csrf_token']
    return client

def post(client,path,body,key): return client.post(path,json=body,headers={'Idempotency-Key':key})

def test_replay_durable_pause_resume_cancel_and_source_isolation(environment):
    from app.infrastructure.db import schema as s
    db,actor=environment; client=client_for(db)
    created=post(client,'/api/v1/replays',{'label':'Isolated fixture','events':[{'source_id':'one','offset_ms':1000,'guess':'香蕉'},{'source_id':'two','offset_ms':3000,'guess':'芒果'}]},'replay')
    assert created.status_code==202,created.text
    job=created.json()['data']; jid=job['job_id']; sid=job['result']['replay_session_id']
    ticket=post(client,'/api/v1/display-tickets',{'session_id':sid,'purpose':'primary'},'replay-ticket').json()['data']
    display=TestClient(client.app,headers={'Origin':'http://testserver'}); assert display.post('/api/v1/display-sessions',json={'ticket':ticket['ticket']}).status_code==200
    start=post(client,f'/api/v1/replays/{jid}/commands',{'command':'start','expected_version':job['version'],'args':{}},'start-replay')
    assert start.status_code==200,start.text
    client.app.state.replay_worker.tick()
    status=client.get('/api/v1/replays/'+jid).json()['data']; assert status['result']['events_applied']==1 and status['result']['replay_cursor']=='1'
    paused=post(client,f'/api/v1/replays/{jid}/commands',{'command':'pause','expected_version':status['version'],'args':{}},'pause-replay'); assert paused.status_code==200,paused.text
    snapshot=client.get('/api/v1/replays/'+jid).json()['data']; client.app.state.replay_worker.tick()
    assert client.get('/api/v1/replays/'+jid).json()['data']['result']==snapshot['result']
    resumed=post(client,f'/api/v1/replays/{jid}/commands',{'command':'resume','expected_version':snapshot['version'],'args':{}},'resume-replay'); assert resumed.status_code==200,resumed.text
    status=client.get('/api/v1/replays/'+jid).json()['data']
    cancelled=post(client,f'/api/v1/replays/{jid}/commands',{'command':'cancel','expected_version':status['version'],'args':{}},'cancel-replay'); assert cancelled.status_code==200,cancelled.text
    client.app.state.replay_worker.tick()
    with db.engine.connect() as c:
        modes=list(c.execute(sa.select(s.guesses.c.source_mode).where(s.guesses.c.session_id==sid)).scalars()); assert modes==['replay']
        assert c.execute(sa.select(s.sessions.c.mode).where(s.sessions.c.id==sid)).scalar_one()=='replay'
    messages=client.get('/api/v1/messages',params={'session_id':sid}).json()['data']['items']; assert len(messages)==1
    trace=client.get('/api/v1/messages/'+messages[0]['message_id']+'/trace').json()['data']; assert trace['steps'] and all(step['time'] for step in trace['steps'])
    assert 'identity_key' not in str(trace) and 'frozen_puzzle' not in str(trace)

def test_replay_rejects_answer_score_or_official_inputs(environment):
    db,actor=environment; client=client_for(db)
    for extra in ({'mode':'official'},{'answer':'苹果'},{'points':100}):
        r=post(client,'/api/v1/replays',{'label':'Forbidden','events':[{'source_id':'a','offset_ms':1,'guess':'香蕉'}]}|extra,'bad-'+next(iter(extra)))
        assert r.status_code==422,r.text

def prepared_replay(db,label,events):
    client=client_for(db)
    job=post(client,'/api/v1/replays',{'label':label,'events':events},label).json()['data']; jid=job['job_id']; sid=job['result']['replay_session_id']
    ticket=post(client,'/api/v1/display-tickets',{'session_id':sid,'purpose':'primary'},label+'-ticket').json()['data']
    display=TestClient(client.app,headers={'Origin':'http://testserver'}); display.post('/api/v1/display-sessions',json={'ticket':ticket['ticket']})
    response=post(client,f'/api/v1/replays/{jid}/commands',{'command':'start','expected_version':job['version'],'args':{}},label+'-start')
    assert response.status_code==200,response.text
    return client,jid,sid

def test_replay_cross_boot_requires_explicit_resume_and_no_double_admission(environment):
    from app.modules.replays.service import ReplayService
    from app.modules.replays.worker import ReplayWorker
    from app.modules.wordbank.service import WordbankService
    from app.infrastructure.db import schema as s
    from test_local_flow import component_service
    db,actor=environment
    client,jid,sid=prepared_replay(db,'reboot-replay',[{'source_id':'1','offset_ms':1000,'guess':'香蕉'},{'source_id':'2','offset_ms':3000,'guess':'芒果'}])
    client.app.state.replay_worker.tick()
    nextcore=component_service(db); nextcore.scorer=client.app.state.service.scorer
    replay=ReplayService(nextcore,WordbankService(nextcore)); worker=ReplayWorker(replay); worker.recover_boot(); worker.tick()
    with db.engine.connect() as c:
        job=replay.job(c,actor,jid); assert job['state']=='PAUSED' and job['cursor']==1
        assert c.execute(sa.select(sa.func.count()).select_from(s.guesses).where(s.guesses.c.session_id==sid)).scalar_one()==1
    replay.mutate(actor,'POST',f'/api/v1/replays/{jid}/commands','reboot-resume',{'command':'resume','expected_version':job['version'],'args':{}},lambda c:replay.command(c,actor,jid,{'command':'resume','expected_version':job['version'],'args':{}}),jid)
    worker.tick(); worker.tick()
    with db.engine.connect() as c:
        job=replay.job(c,actor,jid); assert job['status']=='succeeded' and job['cursor']==2
        assert c.execute(sa.select(sa.func.count()).select_from(s.guesses).where(s.guesses.c.session_id==sid)).scalar_one()==2

def test_replay_deadline_equality_never_admits_into_old_round(environment):
    from app.infrastructure.db import schema as s
    db,actor=environment
    client,jid,sid=prepared_replay(db,'deadline-replay',[{'source_id':'edge','offset_ms':240000,'guess':'香蕉'}])
    worker=client.app.state.replay_worker; worker.tick(); worker.tick(); worker.tick()
    with db.engine.connect() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(s.guesses).where(s.guesses.c.session_id==sid)).scalar_one()==0
        row=c.execute(sa.select(s.replay_jobs).where(s.replay_jobs.c.id==jid)).mappings().one()
        assert row['events_rejected']==1 and row['status']=='succeeded'

def test_replay_auth_revocation_precedes_cached_result_and_foreign_job(environment):
    from app.infrastructure.db import schema as s
    db,actor=environment; client=client_for(db)
    body={'label':'revoke-replay','events':[{'source_id':'1','offset_ms':0,'guess':'香蕉'}]}
    created=post(client,'/api/v1/replays',body,'revoke-key'); assert created.status_code==202
    with db.engine.begin() as c:
        permissions=c.execute(sa.select(s.operators.c.permissions).where(s.operators.c.id==actor)).scalar_one()
        c.execute(s.operators.update().where(s.operators.c.id==actor).values(permissions=['diagnostics.read']))
    try: assert post(client,'/api/v1/replays',body,'revoke-key').status_code==403
    finally:
        with db.engine.begin() as c: c.execute(s.operators.update().where(s.operators.c.id==actor).values(permissions=permissions))

def test_replay_corrupt_checkpoint_fails_closed_after_boot(environment):
    from app.modules.replays.service import ReplayService
    from app.modules.replays.worker import ReplayWorker
    from app.modules.wordbank.service import WordbankService
    from app.infrastructure.db import schema as s
    from test_local_flow import component_service
    db,actor=environment
    client,jid,sid=prepared_replay(db,'corrupt-replay',[{'source_id':'1','offset_ms':1000,'guess':'香蕉'}])
    with db.engine.begin() as c: c.execute(s.replay_jobs.update().where(s.replay_jobs.c.id==jid).values(checkpoint={}))
    core=component_service(db); replay=ReplayService(core,WordbankService(core)); worker=ReplayWorker(replay); worker.recover_boot(); worker.tick()
    with db.engine.connect() as c:
        row=replay.job(c,actor,jid); assert row['status']=='failed' and row['error_code']=='LOCAL_TIME_RECOVERY_UNSAFE'
        assert c.execute(sa.select(sa.func.count()).select_from(s.guesses).where(s.guesses.c.session_id==sid)).scalar_one()==0

def test_replay_zero_offset_end_keeps_frozen_intermission_deadline(environment):
    from app.infrastructure.db import schema as s
    db,actor=environment
    client,jid,sid=prepared_replay(db,'zero-offset-replay',[{'source_id':'hit','offset_ms':0,'guess':'苹果'},{'source_id':'intermission','offset_ms':1000,'guess':'香蕉'},{'source_id':'next','offset_ms':4000,'guess':'芒果'}])
    worker=client.app.state.replay_worker
    worker.tick(); worker.tick(); worker.tick(); worker.tick()
    with db.engine.connect() as c:
        rounds=list(c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid).order_by(s.rounds.c.created_at)).mappings())
        assert len(rounds)==2
        assert rounds[0]['end_reason']=='SOLVED'
        events=list(c.execute(sa.select(s.replay_events).where(s.replay_events.c.job_id==jid).order_by(s.replay_events.c.ordinal)).mappings())
        assert [event['status'] for event in events]==['applied','rejected','applied']
        checkpoint=c.execute(sa.select(s.replay_jobs.c.checkpoint).where(s.replay_jobs.c.id==jid)).scalar_one()
        assert checkpoint['round_started_ms']==4000 and checkpoint['virtual_ms']==4000

def test_replay_checkpoint_missing_round_started_is_rejected(environment):
    from app.infrastructure.db import schema as s
    db,actor=environment
    client,jid,sid=prepared_replay(db,'missing-start-checkpoint',[{'source_id':'one','offset_ms':0,'guess':'香蕉'}])
    with db.engine.begin() as c:
        job=c.execute(sa.select(s.replay_jobs).where(s.replay_jobs.c.id==jid)).mappings().one()
        checkpoint=dict(job['checkpoint']); checkpoint.pop('round_started_ms')
        c.execute(s.replay_jobs.update().where(s.replay_jobs.c.id==jid).values(checkpoint=checkpoint))
        row=dict(job)|{'checkpoint':checkpoint}
        active=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid)).mappings().one()
        assert client.app.state.replay.valid_checkpoint(row,active) is False
    client.app.state.replay_worker.tick()
    result=client.get('/api/v1/replays/'+jid).json()['data']
    assert result['status']=='failed' and result['error']['code']=='LOCAL_TIME_RECOVERY_UNSAFE'

def test_replay_job_is_the_only_round_clock_controller_and_session_end_cancels_it(environment):
    from app.infrastructure.db import schema as s
    db,actor=environment
    client,jid,sid=prepared_replay(db,'replay-control-authority',[{'source_id':'one','offset_ms':1000,'guess':'香蕉'}])
    with db.engine.connect() as c:roundrow=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid)).mappings().one()
    denied=client.post('/api/v1/rounds/'+roundrow['id']+'/commands',json={'command':'pause','expected_version':roundrow['version'],'args':{}},headers={'Idempotency-Key':'forbidden-direct-replay-pause'})
    assert denied.status_code==403
    current=client.get('/api/v1/live-sessions/'+sid).json()['data']
    ended=post(client,'/api/v1/live-sessions/'+sid+'/commands',{'command':'end','expected_version':current['version'],'args':{'reason':'global stop'}},'end-replay-session')
    assert ended.status_code==200,ended.text
    client.app.state.replay_worker.tick()
    assert client.get('/api/v1/replays/'+jid).json()['data']['status']=='cancelled'
    assert client.get('/api/v1/live-sessions/'+sid).json()['data']['status']=='ENDED'

def test_replay_dependency_loss_is_a_fault_not_a_successful_replay_of_rejections(environment):
    from app.infrastructure.db import schema as s
    db,actor=environment;client,jid,sid=prepared_replay(db,'replay-model-loss',[{'source_id':'one','offset_ms':1000,'guess':'香蕉'}])
    class Lost:
        def readiness(self):return {'ready':False,'manifest_id':None}
    client.app.state.service.scorer=Lost()
    client.app.state.replay_worker.tick()
    result=client.get('/api/v1/replays/'+jid).json()['data']
    assert result['status']=='failed' and result['result']['replay_cursor']=='0'
    with db.engine.connect() as c:assert c.execute(sa.select(s.rounds.c.end_reason).where(s.rounds.c.session_id==sid)).scalar_one()=='INTERRUPTED'

def test_replay_creation_retry_never_regresses_a_cancelled_job_to_queued(environment):
    db,actor=environment;client=client_for(db)
    body={'label':'terminal creation retry','events':[{'source_id':'one','offset_ms':1,'guess':'香蕉'}]}
    created=post(client,'/api/v1/replays',body,'terminal-create-key').json()['data']
    cancelled=post(client,'/api/v1/replays/'+created['job_id']+'/commands',{'command':'cancel','expected_version':created['version'],'args':{}},'terminal-cancel-key')
    assert cancelled.status_code==200
    retry=post(client,'/api/v1/replays',body,'terminal-create-key').json()['data']
    assert retry['job_id']==created['job_id'] and retry['status']=='cancelled'
