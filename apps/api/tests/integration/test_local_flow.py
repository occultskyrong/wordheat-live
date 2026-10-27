class ComponentScorer:
    def readiness(self): return {'ready':True,'manifest_id':'reviewed-manifest'}
    def score(self,**kwargs):
        from app.errors import DomainError
        raise DomainError('SCORER_NOT_READY','Component scorer only observes readiness',503)

def component_service(db):
    from app.modules.sessions.service import LocalService
    return LocalService(db,ComponentScorer(),display_probe=lambda **scope:True)

"""Requires the explicitly provisioned dedicated PostgreSQL roles, never SQLite."""
import os
from pathlib import Path
import pytest
import sqlalchemy as sa

@pytest.fixture(scope='module')
def environment():
    from app.config import read_fields, Settings
    from app.infrastructure.db.database import Database
    from app.infrastructure.db.migrate import migrate
    from app.modules.auth.service import bootstrap_operator
    from app.infrastructure.db import schema as s
    path = os.environ.get('WORDHEAT_TEST_CONFIG')
    if not path: pytest.skip('WORDHEAT_TEST_CONFIG dedicated PG configuration required')
    fields = read_fields(Path(path), {'WORDHEAT_TEST_DATABASE_URL','WORDHEAT_TEST_MIGRATION_DATABASE_URL'})
    run, migration = fields['WORDHEAT_TEST_DATABASE_URL'], fields['WORDHEAT_TEST_MIGRATION_DATABASE_URL']
    migrate(migration, 'wordheat-local-development', 'wordheat_test_app')
    db = Database(Settings(run, allowed_hosts=('testserver',), allowed_origins=('http://testserver',)))
    db.verify_runtime()
    with db.engine.begin() as c:
        for table in reversed(s.metadata.sorted_tables):
            if table.name not in {'service_instance','wordbank_catalog'}: c.execute(table.delete())
        c.execute(s.catalog.update().where(s.catalog.c.id=='global').values(joint_identity={},bundle_digest=None,model_manifest_id=None,model_manifest_sha256=None))
        actor = bootstrap_operator(c, 'testoperator', 'private-test-password-123', 'Operator')
        c.execute(s.puzzles.insert().values(id='pv-test', puzzle_id='p-test', word='苹果', aliases=['林檎'], category_id='fruit', difficulty=1, explanation='测试题', status='published', manifest_id='reviewed-manifest', calibration_id='reviewed-calibration', evaluation_id='reviewed-evaluation', approved_by=actor))
    yield db, actor
    db.engine.dispose()


def test_transactional_exact_hit_and_role_isolation(environment):
    from fastapi.testclient import TestClient
    from app.main import create_app
    db, _ = environment
    client = TestClient(create_app(db.settings,scorer=ComponentScorer(),display_probe=lambda **scope:True), headers={'Origin':'http://testserver','X-Contract-Version':'LW-C1/1.0.0','X-BE-Extension-Version':'LW-BE/1.1.0'})
    login = client.post('/api/v1/auth/sessions', json={'username':'testoperator','password':'private-test-password-123'})
    assert login.status_code == 200, login.text
    csrf = login.json()['data']['csrf_token']
    client.headers['X-CSRF-Token'] = csrf
    def post(path, body, key): return client.post(path, json=body, headers={'Idempotency-Key':key})
    created = post('/api/v1/live-sessions', {'label':'Offline','mode':'rehearsal','run_purpose':'local','room_binding_id':None,'rules':{}}, 'create')
    assert created.status_code == 200, created.text
    sid = created.json()['data']['session_id']
    retry = post('/api/v1/live-sessions', {'label':'Offline','mode':'rehearsal','run_purpose':'local','room_binding_id':None,'rules':{}}, 'create')
    assert retry.json()['data'] == created.json()['data']
    assert post('/api/v1/live-sessions', {'label':'Changed','mode':'rehearsal','run_purpose':'local','rules':{}}, 'create').status_code == 409
    ticket = post('/api/v1/display-tickets', {'session_id':sid,'purpose':'primary'}, 'ticket').json()['data']
    display = TestClient(create_app(db.settings,scorer=ComponentScorer(),display_probe=lambda **scope:True), headers={'Origin':'http://testserver'})
    exchanged = display.post('/api/v1/display-sessions', json={'ticket':ticket['ticket']})
    assert exchanged.status_code == 200, exchanged.text
    assert display.post('/api/v1/display-sessions', json={'ticket':ticket['ticket']}).status_code == 403
    assert display.get(f'/api/v1/live-sessions/{sid}').status_code == 401
    started = post(f'/api/v1/live-sessions/{sid}/commands', {'command':'start','expected_version':1,'args':{'run_purpose':'local'}}, 'start')
    assert started.status_code == 200, started.text
    from app.infrastructure.db import schema as s
    with db.engine.connect() as c: current=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid)).mappings().one()
    r={'round_id':current['id'],'short_code':current['short_code']}; rid=r['round_id']
    snap = display.get(f'/api/v1/display/sessions/{sid}/snapshot').json()['data']
    assert snap['reveal'] is None and '苹果' not in str(snap)
    assert display.get(f'/api/v1/rounds/{rid}/private-state').status_code == 401
    gbody={'session_id':sid,'round_id':rid,'short_code':r['short_code'],'word':' 苹果 ','display_name':'Player'}
    hit=post('/api/v1/rehearsal/guesses',gbody,'guess')
    assert hit.status_code == 200, hit.text
    assert hit.json()['data']['accepted'] is True
    assert set(hit.json()['data'])=={'accepted','reason','guess_id'}
    assert post('/api/v1/rehearsal/guesses',gbody,'guess').json()['data']==hit.json()['data']
    assert post(f'/api/v1/rounds/{rid}/commands',{'command':'void','expected_version':2,'args':{'reason':'too late'}},'void').status_code==409
    from app.infrastructure.db import schema as s
    with db.engine.connect() as c:
        assert c.execute(sa.select(sa.func.sum(s.ledger.c.delta)).where(s.ledger.c.round_id==rid)).scalar_one()==20
        assert c.execute(sa.select(sa.func.count()).select_from(s.settlements).where(s.settlements.c.round_id==rid)).scalar_one()==1
        assert c.execute(sa.select(s.sessions.c.admission_seq).where(s.sessions.c.id==sid)).scalar_one()==1
    final=display.get(f'/api/v1/display/sessions/{sid}/snapshot').json()['data']
    assert final['reveal']['answer']=='苹果'
    assert final['ranking']['players'][0]['points']==20
    assert client.post('/api/v1/live-sessions',json={'label':'bad'},headers={'X-CSRF-Token':'bad'}).status_code==403
    assert client.get('/api/v1/health/live',headers={'Host':'evil.example'}).status_code==403


def test_next_selection_is_persistent_cas_and_public_private_separated(environment):
    from app.modules.sessions.service import LocalService
    from app.errors import DomainError
    from app.infrastructure.db import schema as s
    db,actor=environment; service=component_service(db)
    with db.engine.begin() as c:
        session=service.create_session(c,actor,{'label':'Selection','mode':'rehearsal','run_purpose':'local','rules':{}})
    sid=session['session_id']
    with db.engine.begin() as c:
        selected=service.session_command(c,actor,sid,{'command':'select_next','expected_version':1,'args':{'strategy':'specific','puzzle_id':'p-test','expected_selection_id':None,'expected_selection_version':0}})
    assert selected['state']['status']=='READY'
    selection=selected['state']
    with db.engine.begin() as c:
        detail=service.next_selection(c,sid)
        assert detail['answer']=='苹果' and detail['version']==1
        public=service.snapshot(c,sid)
        assert 'next_selection' not in public and '苹果' not in str(public)
    with pytest.raises(DomainError) as error:
        with db.engine.begin() as c:
            service.session_command(c,actor,sid,{'command':'select_next','expected_version':2,'args':{'strategy':'random','expected_selection_id':None,'expected_selection_version':0}})
    assert error.value.code=='NEXT_SELECTION_VERSION_CONFLICT'


def test_runtime_role_cannot_ddl_and_rollback_preserves_counter(environment):
    from app.infrastructure.db import schema as s
    from sqlalchemy.exc import DBAPIError
    from app.modules.sessions.service import LocalService
    db,actor=environment; service=component_service(db)
    with pytest.raises(DBAPIError):
        with db.engine.begin() as c: c.execute(sa.text('CREATE TABLE runtime_ddl_forbidden (id int)'))
    with db.engine.begin() as c:
        sid=service.create_session(c,actor,{'label':'Rollback','mode':'rehearsal','run_purpose':'local','rules':{}})['session_id']
    with db.engine.connect() as c: before=c.execute(sa.select(s.sessions.c.analytics_commit_counter).where(s.sessions.c.id==sid)).scalar_one()
    with pytest.raises(RuntimeError):
        with db.engine.begin() as c:
            service.touch(c,sid)
            raise RuntimeError('rollback')
    with db.engine.connect() as c: assert c.execute(sa.select(s.sessions.c.analytics_commit_counter).where(s.sessions.c.id==sid)).scalar_one()==before


def prepared_round(db,actor,label,observer=None):
    from app.modules.sessions.service import LocalService
    from app.modules.auth.service import now,uid
    from datetime import timedelta
    from app.infrastructure.db import schema as s
    service=component_service(db)
    service.analytics=observer
    with db.engine.begin() as c:
        session=service.create_session(c,actor,{'label':label,'mode':'rehearsal','run_purpose':'local','rules':{}})
        sid=session['session_id']
        token=service.ticket(c,actor,{'session_id':sid,'purpose':'primary'})['ticket']
    service.exchange(token)
    with db.engine.begin() as c:
        choice=service.session_command(c,actor,sid,{'command':'select_next','expected_version':1,'args':{'strategy':'specific','puzzle_id':'p-test','expected_selection_id':None,'expected_selection_version':0}})
        service.session_command(c,actor,sid,{'command':'start','expected_version':choice['resource_version'],'args':{'run_purpose':'local'}})
        row=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid)).mappings().one()
        r={'round_id':row['id'],'short_code':row['short_code']}
    body={'session_id':sid,'round_id':r['round_id'],'short_code':r['short_code'],'word':'苹果','display_name':'Player'}
    return service,body


def test_concurrent_exact_hit_commits_one_winner(environment):
    from concurrent.futures import ThreadPoolExecutor
    from app.errors import DomainError
    from app.infrastructure.db import schema as s
    db,actor=environment; service,body=prepared_round(db,actor,'Concurrent')
    def submit(key):
        try:
            return service.command(actor,'POST','/guesses',key,body,lambda c:service.guess(c,actor,body,key))
        except DomainError as error: return error.code
    with ThreadPoolExecutor(max_workers=2) as pool: outcomes=list(pool.map(submit,['race-a','race-b']))
    assert sum(isinstance(x,dict) and x['solved'] for x in outcomes)==1
    assert outcomes.count('ROUND_CLOSED')==1
    with db.engine.connect() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(s.settlements).where(s.settlements.c.round_id==body['round_id'])).scalar_one()==1
        assert c.execute(sa.select(sa.func.sum(s.ledger.c.delta)).where(s.ledger.c.round_id==body['round_id'])).scalar_one()==20


def test_failed_outbox_rolls_back_winner_and_admission(environment,monkeypatch):
    from app.infrastructure.db import schema as s
    db,actor=environment; service,body=prepared_round(db,actor,'Failure')
    original=service.event
    def fail(*args,**kwargs): raise RuntimeError('injected outbox failure')
    monkeypatch.setattr(service,'event',fail)
    with pytest.raises(RuntimeError):
        service.command(actor,'POST','/guesses','failed-event',body,lambda c:service.guess(c,actor,body,'failed-event'))
    with db.engine.connect() as c:
        assert c.execute(sa.select(s.sessions.c.admission_seq).where(s.sessions.c.id==body['session_id'])).scalar_one()==0
        assert c.execute(sa.select(sa.func.count()).select_from(s.settlements).where(s.settlements.c.round_id==body['round_id'])).scalar_one()==0
        assert c.execute(sa.select(sa.func.count()).select_from(s.guesses).where(s.guesses.c.round_id==body['round_id'])).scalar_one()==0
    monkeypatch.setattr(service,'event',original)
    assert service.command(actor,'POST','/guesses','failed-event',body,lambda c:service.guess(c,actor,body,'failed-event'))['solved']


def score_submit(service,actor,key,body):
    from app.modules.scoring.worker import ScoreWorker
    result=service.submit_guess(actor,key,body)
    if result['status']=='queued':
        ScoreWorker(service).tick()
        result=service.submit_guess(actor,key,body)
    return result


def test_non_exact_calls_scorer_outside_transaction_and_never_wins(environment):
    db,actor=environment; service,body=prepared_round(db,actor,'Scorer')
    body['word']='香蕉'
    class RelatedScorer(ComponentScorer):
        calls=0
        def score(self,**kwargs):
            self.calls+=1
            # A separate transaction can take the session lock while inference runs.
            from app.infrastructure.db import schema as s
            with db.engine.begin() as c:
                c.execute(sa.text("SET LOCAL lock_timeout='250ms'"))
                c.execute(sa.select(s.sessions).where(s.sessions.c.id==body['session_id']).with_for_update()).first()
            return 99.9
    scorer=RelatedScorer(); service.scorer=scorer
    result=score_submit(service,actor,'scoring-test',body)
    assert scorer.calls==1
    assert result['status']=='scored' and result['display_score']==99.9 and result['solved'] is False
    assert score_submit(service,actor,'scoring-test',body)==result
    assert scorer.calls==1


def test_missing_scorer_is_persisted_failure_without_points(environment):
    from app.infrastructure.db import schema as s
    db,actor=environment; service,body=prepared_round(db,actor,'Missing model'); body['word']='香蕉'
    result=score_submit(service,actor,'no-model',body)
    assert result['status']=='system_failed' and result['message']=='SCORER_NOT_READY'
    assert result['display_score'] is None and result['solved'] is False
    with db.engine.connect() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(s.ledger).where(s.ledger.c.round_id==body['round_id'])).scalar_one()==0


def test_void_reverses_each_entry_and_cannot_repeat(environment):
    from app.infrastructure.db import schema as s
    from app.modules.auth.service import uid
    from app.errors import DomainError
    db,actor=environment; service,body=prepared_round(db,actor,'Void')
    class Scorer(ComponentScorer):
        def score(self,**kwargs): return 80.0
    service.scorer=Scorer(); body['word']='香蕉'
    assert score_submit(service,actor,'void-score',body)['status']=='scored'
    with db.engine.begin() as c:
        result=service.round_command(c,actor,body['round_id'],{'command':'void','expected_version':1,'args':{'reason':'operator correction'}})
    assert result['state']=='INTERMISSION'
    with db.engine.connect() as c:
        assert c.execute(sa.select(sa.func.sum(s.ledger.c.delta)).where(s.ledger.c.round_id==body['round_id'])).scalar_one()==0
        assert c.execute(sa.select(sa.func.count()).select_from(s.ledger).where(s.ledger.c.round_id==body['round_id'],s.ledger.c.reverses_id.is_not(None))).scalar_one()==3
    with pytest.raises(DomainError) as error:
        with db.engine.begin() as c: service.round_command(c,actor,body['round_id'],{'command':'void','expected_version':2,'args':{'reason':'again'}})
    assert error.value.code=='ROUND_CLOSED'


def test_cross_boot_pause_cannot_resume_implicitly(environment):
    from app.modules.sessions.service import LocalService
    from app.errors import DomainError
    db,actor=environment; service,body=prepared_round(db,actor,'Boot')
    with db.engine.begin() as c: service.round_command(c,actor,body['round_id'],{'command':'pause','expected_version':1,'args':{}})
    restarted=LocalService(db)
    with pytest.raises(DomainError) as error:
        with db.engine.begin() as c: restarted.round_command(c,actor,body['round_id'],{'command':'resume','expected_version':2,'args':{}})
    assert error.value.code=='LOCAL_TIME_RECOVERY_UNSAFE'


def logged_in_client(db):
    from fastapi.testclient import TestClient
    from app.main import create_app
    client=TestClient(create_app(db.settings,scorer=ComponentScorer(),display_probe=lambda **scope:True),headers={'Origin':'http://testserver'})
    response=client.post('/api/v1/auth/sessions',json={'username':'testoperator','password':'private-test-password-123'})
    client.headers['X-CSRF-Token']=response.json()['data']['csrf_token']
    return client


def test_be_negotiation_required_for_every_affected_write_and_retries_survive(environment):
    db,actor=environment; client=logged_in_client(db)
    create={'label':'BE guard','mode':'rehearsal','run_purpose':'local','rules':{}}
    missing=client.post('/api/v1/live-sessions',json=create,headers={'Idempotency-Key':'be-create'})
    assert missing.status_code==409 and missing.json()['error']['code']=='BE_EXTENSION_VERSION_UNSUPPORTED'
    client.headers['X-BE-Extension-Version']='LW-BE/1.1.0'
    created=client.post('/api/v1/live-sessions',json=create,headers={'Idempotency-Key':'be-create'})
    assert created.status_code==200
    sid=created.json()['data']['session_id']
    client.headers.pop('X-BE-Extension-Version')
    retry=client.post('/api/v1/live-sessions',json=create,headers={'Idempotency-Key':'be-create'})
    assert retry.json()['data']==created.json()['data']
    for name in ['start','configure','select_next']:
        response=client.post(f'/api/v1/live-sessions/{sid}/commands',json={'command':name,'expected_version':99,'args':{}},headers={'Idempotency-Key':'be-'+name})
        assert response.status_code==409 and response.json()['error']['code']=='BE_EXTENSION_VERSION_UNSUPPORTED'
    response=client.post('/api/v1/rounds',json={'session_id':sid,'expected_session_version':99,'selection':{'strategy':'specific','puzzle_id':'p-test'}},headers={'Idempotency-Key':'be-round'})
    assert response.status_code==409 and response.json()['error']['code']=='BE_EXTENSION_VERSION_UNSUPPORTED'
    # Successful start retry is returned before now-stale CAS and missing BE checks.
    ticket=client.post('/api/v1/display-tickets',json={'session_id':sid,'purpose':'primary'},headers={'Idempotency-Key':'be-ticket'}).json()['data']['ticket']
    assert client.post('/api/v1/display-sessions',json={'ticket':ticket}).status_code==200
    start={'command':'start','expected_version':1,'args':{'run_purpose':'local'}}
    client.headers['X-BE-Extension-Version']='LW-BE/1.1.0'
    first=client.post(f'/api/v1/live-sessions/{sid}/commands',json=start,headers={'Idempotency-Key':'be-start-ok'})
    assert first.status_code==200, first.text
    client.headers.pop('X-BE-Extension-Version')
    assert client.post(f'/api/v1/live-sessions/{sid}/commands',json=start,headers={'Idempotency-Key':'be-start-ok'}).json()['data']==first.json()['data']


def test_cached_ticket_requires_current_ownership_and_permission(environment):
    from app.infrastructure.db import schema as s
    from app.modules.auth.service import uid,hash_password,PERMISSIONS
    from app.modules.sessions.service import LocalService
    db,actor=environment; service=component_service(db); client=logged_in_client(db)
    with db.engine.begin() as c:
        sid=service.create_session(c,actor,{'label':'Cached ownership','mode':'rehearsal','run_purpose':'local','rules':{}})['session_id']
        other=uid()
        c.execute(s.operators.insert().values(id=other,username='other-'+other,password_hash=hash_password('independent-password-123'),display_name='Other',permissions=PERMISSIONS))
    path='/api/v1/display-tickets'; body={'session_id':sid,'purpose':'preview'}
    assert client.post(path,json=body,headers={'Idempotency-Key':'owner-ticket'}).status_code==200
    with db.engine.begin() as c: c.execute(s.sessions.update().where(s.sessions.c.id==sid).values(actor_id=other))
    rejected=client.post(path,json=body,headers={'Idempotency-Key':'owner-ticket'})
    assert rejected.status_code==403
    assert client.post(path,json=body,headers={'Idempotency-Key':'owner-ticket-new'}).status_code==403
    # Ownership denial precedes missing BE negotiation and stale CAS.
    denied_start=client.post(f'/api/v1/live-sessions/{sid}/commands',json={'command':'start','expected_version':99,'args':{}},headers={'Idempotency-Key':'owner-start'})
    assert denied_start.status_code==403
    with db.engine.begin() as c: c.execute(s.sessions.update().where(s.sessions.c.id==sid).values(actor_id=actor))
    with db.engine.begin() as c: c.execute(s.operators.update().where(s.operators.c.id==actor).values(permissions=['session.control']))
    try:
        assert client.post(path,json=body,headers={'Idempotency-Key':'owner-ticket'}).status_code==403
        from app.errors import DomainError
        with pytest.raises(DomainError) as error:
            service.command(actor,'POST',path,'owner-ticket',body,lambda c:pytest.fail('revoked command executed'))
        assert error.value.code=='FORBIDDEN'
    finally:
        with db.engine.begin() as c: c.execute(s.operators.update().where(s.operators.c.id==actor).values(permissions=PERMISSIONS))


def test_registered_private_dtos_match_spec_fields(environment):
    db,actor=environment; client=logged_in_client(db)
    current=client.get('/api/v1/auth/sessions/current').json()['data']
    assert set(current)=={'actor','csrf_token','expires_at'}
    assert set(current['actor'])=={'actor_id','display_name','permissions'}
    service,body=prepared_round(db,actor,'DTO')
    detail=client.get('/api/v1/live-sessions/'+body['session_id']).json()['data']
    assert set(detail)=={'session_id','version','status','mode','run_purpose','room_binding_id','session_rules','next_rules','active_round_id'}
    private=client.get('/api/v1/rounds/'+body['round_id']+'/private-state').json()['data']
    assert set(private)=={'round_id','version','status','window_epoch','puzzle_version_id','answer','aliases','manifest_id','calibration_id','rules_version','clock','pending_command_ids'}


def test_default_readiness_and_runtime_fail_closed_without_model_and_display(environment):
    db,actor=environment
    from fastapi.testclient import TestClient
    from app.main import create_app
    client=TestClient(create_app(db.settings),headers={'Origin':'http://testserver'})
    login=client.post('/api/v1/auth/sessions',json={'username':'testoperator','password':'private-test-password-123'})
    client.headers['X-CSRF-Token']=login.json()['data']['csrf_token']
    ready=client.get('/api/v1/health/ready').json()['data']
    assert set(ready['checks'])=={'db','model','wordbank','relay','lease','display'}
    assert not any(ready[k] for k in ['can_rehearse','can_test_live','can_go_live'])
    assert ready['checks']['model']['status']=='fail'
    assert client.get('/api/v1/health/ready',headers={'X-BE-Extension-Version':'unknown'}).status_code==409
    runtime=client.get('/api/v1/runtime/status').json()['data']
    assert set(runtime)=={'version','launcher_state','active_session_id','installation_id','app_version','model_manifest_id','schema_version','readiness','backup_status','maintenance_status'}
    assert runtime['launcher_state']=='WARMING'
    from app.modules.sessions.service import LocalService
    service=LocalService(db)
    with db.engine.begin() as c:
        sid=service.create_session(c,actor,{'label':'Blocked missing model','mode':'rehearsal','run_purpose':'local','rules':{}})['session_id']
        token=service.ticket(c,actor,{'session_id':sid,'purpose':'primary'})['ticket']
    service.exchange(token)
    from app.errors import DomainError
    with pytest.raises(DomainError) as failure:
        with db.engine.begin() as c: service.session_command(c,actor,sid,{'command':'start','expected_version':1,'args':{'run_purpose':'local'}})
    assert failure.value.code=='READINESS_NOT_MET'
    assert 'model' in str(failure.value.details)


def test_worker_expires_local_deadline_and_reboot_archives_old_running(environment):
    from app.infrastructure.db import schema as s
    from app.modules.rounds.worker import LocalRoundWorker
    db,actor=environment; service,body=prepared_round(db,actor,'Deadline worker')
    with db.engine.begin() as c: c.execute(s.rounds.update().where(s.rounds.c.id==body['round_id']).values(mono_started_ns=0))
    worker=LocalRoundWorker(service)
    worker.tick()
    with db.engine.connect() as c:
        row=c.execute(sa.select(s.rounds).where(s.rounds.c.id==body['round_id'])).mappings().one()
        assert row['status']=='INTERMISSION' and row['end_reason']=='TIMEOUT'
        assert c.execute(sa.select(sa.func.count()).select_from(s.settlements).where(s.settlements.c.round_id==row['id'])).scalar_one()==1
    old,second=prepared_round(db,actor,'Reboot worker')
    reboot=component_service(db); LocalRoundWorker(reboot).recover_boot()
    with db.engine.connect() as c:
        assert c.execute(sa.select(s.rounds.c.end_reason).where(s.rounds.c.id==second['round_id'])).scalar_one()=='INTERRUPTED'
        assert c.execute(sa.select(s.sessions.c.status).where(s.sessions.c.id==second['session_id'])).scalar_one()=='RECOVERY_REQUIRED'


def test_pause_preserves_admitted_score_and_drain_waits_for_completion(environment):
    import threading,time
    from concurrent.futures import ThreadPoolExecutor
    from app.infrastructure.db import schema as s
    from app.modules.rounds.worker import LocalRoundWorker
    db,actor=environment; service,body=prepared_round(db,actor,'Drain ordering')
    started=threading.Event(); release=threading.Event()
    class BlockingScorer(ComponentScorer):
        def score(self,**kwargs):
            started.set(); assert release.wait(2); return 80.0
    service.scorer=BlockingScorer(); nonexact=body|{'word':'香蕉'}
    with ThreadPoolExecutor(max_workers=1) as pool:
        scoring=pool.submit(score_submit,service,actor,'pending-score',nonexact)
        assert started.wait(1)
        with db.engine.begin() as c:
            paused=service.round_command(c,actor,body['round_id'],{'command':'pause','expected_version':1,'args':{}})
        with db.engine.connect() as c: assert c.execute(sa.select(s.score_jobs.c.status).join(s.guesses,s.guesses.c.id==s.score_jobs.c.guess_id).where(s.guesses.c.round_id==body['round_id'])).scalar_one()=='running'
        with db.engine.begin() as c:
            service.round_command(c,actor,body['round_id'],{'command':'resume','expected_version':paused['resource_version'],'args':{}})
            # Controlled temporal fixture clears the same-actor 1s rate gate,
            # keeping the already-admitted score inside its 2s budget.
            c.execute(s.guesses.update().where(s.guesses.c.round_id==body['round_id']).values(admitted_mono_ns=time.monotonic_ns()-1100000000))
        exact=service.command(actor,'POST','/api/v1/rehearsal/guesses','drain-winner',body,lambda c:service.guess(c,actor,body,'drain-winner'))
        assert exact['solved']
        worker=LocalRoundWorker(service); worker.tick()
        with db.engine.connect() as c: assert c.execute(sa.select(s.rounds.c.status).where(s.rounds.c.id==body['round_id'])).scalar_one()=='DRAINING'
        release.set(); assert scoring.result(timeout=2)['status']=='scored'
    worker.tick()
    with db.engine.connect() as c:
        assert c.execute(sa.select(s.rounds.c.end_reason).where(s.rounds.c.id==body['round_id'])).scalar_one()=='SOLVED'
        assert c.execute(sa.select(sa.func.sum(s.ledger.c.delta)).where(s.ledger.c.round_id==body['round_id'])).scalar_one()==25


def test_startup_preparation_does_not_require_session_or_display(environment):
    db,actor=environment; client=logged_in_client(db)
    response=client.get('/api/v1/runtime/status',headers={'X-BE-Extension-Version':'LW-BE/1.1.0'})
    data=response.json()['data']
    assert data['launcher_state']=='READY_REHEARSAL'
    assert data['readiness']['context']['session_id'] is None
    assert data['readiness']['checks']['display']['status']=='fail'
    assert all(not profile['allowed'] for profile in data['readiness']['profiles'].values())


def test_async_scorer_port_is_awaited_outside_transaction(environment):
    db,actor=environment; service,body=prepared_round(db,actor,'Async scorer'); body['word']='香蕉'
    class AsyncScorer(ComponentScorer):
        async def score(self,**kwargs): return 99.9
    service.scorer=AsyncScorer()
    result=score_submit(service,actor,'async-score',body)
    assert result['display_score']==99.9 and result['solved'] is False


def test_real_ws_readiness_live_receipts_and_reconnect_generation(environment):
    from fastapi.testclient import TestClient
    from app.main import create_app
    db,actor=environment
    app=create_app(db.settings,scorer=ComponentScorer())
    operator=TestClient(app,headers={'Origin':'http://testserver','X-BE-Extension-Version':'LW-BE/1.1.0'})
    login=operator.post('/api/v1/auth/sessions',json={'username':'testoperator','password':'private-test-password-123'}).json()['data']
    operator.headers['X-CSRF-Token']=login['csrf_token']
    def post(path,body,key): return operator.post(path,json=body,headers={'Idempotency-Key':key})
    sid=post('/api/v1/live-sessions',{'label':'Real WS','mode':'rehearsal','run_purpose':'local','rules':{}},'ws-create').json()['data']['session_id']
    ticket=post('/api/v1/display-tickets',{'session_id':sid,'purpose':'primary'},'ws-ticket').json()['data']
    display=TestClient(app,headers={'Origin':'http://testserver'})
    ds=display.post('/api/v1/display-sessions',json={'ticket':ticket['ticket']}).json()['data']
    display.headers['X-CSRF-Token']=ds['receipt_csrf_token']
    start={'command':'start','expected_version':1,'args':{'run_purpose':'local'}}
    assert post('/api/v1/live-sessions/'+sid+'/commands',start,'ws-start').status_code==409
    resume={'kind':'resume','schema_version':1,'contract_version':'LW-C1/1.0.0','session_id':sid,'after_seq':'0'}
    with display.websocket_connect('/api/v1/display/ws') as ws:
        ws.send_json(resume); ready=ws.receive_json()
        assert ready['kind']=='resume_ready' and ready['pending_effects']==[]
        generation=ready['render_generation']
        assert post('/api/v1/live-sessions/'+sid+'/commands',start,'ws-start').status_code==200
        with db.engine.connect() as c:
            from app.infrastructure.db import schema as s
            row=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid)).mappings().one()
            rr={'round_id':row['id'],'short_code':row['short_code']}
        hit=post('/api/v1/rehearsal/guesses',{'session_id':sid,'round_id':rr['round_id'],'short_code':rr['short_code'],'word':'苹果','display_name':'WS Player'},'ws-hit')
        assert hit.status_code==200,hit.text
        effect=None
        for _ in range(12):
            event=ws.receive_json()
            if event.get('type')=='guess.feedback' and event['payload']['effects']:
                effect=event['payload']['effects'][0]; break
        assert effect is not None
        batch={'display_instance_id':ds['display_instance_id'],'render_generation':generation,'batch_id':'receipt-test','stage':'processed','deliveries':[{'delivery_id':effect['delivery_ids'][0],'feedback_id':effect['feedback_id'],'client_time':'2026-10-25T01:00:00.000Z'}]}
        assert display.post('/api/v1/display/receipts',json=batch).status_code==422
        batch['stage']='received'; assert display.post('/api/v1/display/receipts',json=batch).status_code==200
        batch['stage']='processed'; processed=display.post('/api/v1/display/receipts',json=batch)
        assert processed.status_code==200,processed.text
        assert display.post('/api/v1/display/receipts',json=batch).json()['data']==processed.json()['data']
    assert operator.get('/api/v1/health/ready',params={'session_id':sid}).json()['data']['checks']['display']['status']=='fail'
    asof=display.get('/api/v1/display/sessions/'+sid+'/snapshot').json()['data']['as_of_seq']
    with display.websocket_connect('/api/v1/display/ws') as ws:
        ws.send_json(resume|{'after_seq':asof}); new=ws.receive_json()
        assert int(new['render_generation'])>int(generation)
        assert display.post('/api/v1/display/receipts',json=batch).status_code==403
    studio=operator.get('/api/v1/studio/sessions/'+sid+'/snapshot')
    assert studio.status_code==200,studio.text
    assert 'next_selection' in studio.json()['data']


def test_effect_credit_received_does_not_release_processed_pumps_history(environment):
    from app.modules.realtime.service import RealtimeService
    from app.infrastructure.db import schema as s
    from app.modules.auth.service import uid
    db,actor=environment; service,body=prepared_round(db,actor,'Effect credit'); rt=RealtimeService(service); service.realtime=rt
    with db.engine.begin() as c:
        display=c.execute(sa.select(s.displays).where(s.displays.c.session_id==body['session_id'],s.displays.c.purpose=='primary')).mappings().one()
        for i in range(130):
            feedback=uid(); delivery=uid(); payload={'feedback_id':feedback,'origin_round_id':body['round_id'],'window_epoch':'1','delivery_ids':[delivery],'outcome':'scored','status_message':'','word':'香蕉','display_name':'Credit fixture','display_score':80.0,'solved':False}
            c.execute(s.effects.insert().values(id=uid(),session_id=body['session_id'],round_id=body['round_id'],feedback_id=feedback,delivery_id=delivery,source_seq=i+1,payload=payload))
    nonce,generation=rt.connect(display['id'],body['session_id']); events,upto=rt.replay(body['session_id'],'DISPLAY',0)
    ready=rt.ready(display['id'],nonce,upto); assert len(ready['pending_effects'])==128
    e=ready['pending_effects'][0]
    receipt={'display_instance_id':display['id'],'render_generation':str(generation),'batch_id':'credit','stage':'received','deliveries':[{'delivery_id':e['delivery_ids'][0],'feedback_id':e['feedback_id'],'client_time':'2026-10-25T01:00:00.000Z'}]}
    rt.receipt(display['id'],receipt)
    with db.engine.connect() as c: assert c.execute(sa.select(sa.func.count()).select_from(s.grants).where(s.grants.c.display_id==display['id'])).scalar_one()==128
    with db.engine.connect() as c: before_processed=c.execute(sa.select(s.sessions.c.analytics_commit_counter).where(s.sessions.c.id==body['session_id'])).scalar_one()
    rt.receipt(display['id'],receipt|{'stage':'processed'})
    with db.engine.connect() as c: after_processed=c.execute(sa.select(s.sessions.c.analytics_commit_counter).where(s.sessions.c.id==body['session_id'])).scalar_one()
    assert after_processed>before_processed
    rt.receipt(display['id'],receipt|{'stage':'processed'})
    with db.engine.connect() as c: assert c.execute(sa.select(s.sessions.c.analytics_commit_counter).where(s.sessions.c.id==body['session_id'])).scalar_one()==after_processed
    with db.engine.connect() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(s.grants).where(s.grants.c.display_id==display['id'],s.grants.c.processed.is_(False))).scalar_one()==128
        pump=c.execute(sa.select(s.outbox).where(s.outbox.c.session_id==body['session_id'],s.outbox.c.audience=='DISPLAY',s.outbox.c.type=='guess.feedback').order_by(s.outbox.c.seq.desc())).mappings().first()
        assert pump['payload']['feedbacks']==[] and len(pump['payload']['effects'])==1
    assert rt.envelope(pump,'replay',True)['payload']['effects']==[]


def test_catalog_configure_disable_empty_and_hint_events_are_persistent(environment):
    from app.infrastructure.db import schema as s
    from app.modules.wordbank.catalog import disable
    db,actor=environment; service=component_service(db)
    with db.engine.begin() as c:
        sid=service.create_session(c,actor,{'label':'Catalog','mode':'rehearsal','run_purpose':'local','rules':{}})['session_id']
        first=service.session_command(c,actor,sid,{'command':'select_next','expected_version':1,'args':{'strategy':'specific','puzzle_id':'p-test','expected_selection_id':None,'expected_selection_version':0}})
    with db.engine.begin() as c:
        before=c.execute(sa.select(s.sessions.c.version).where(s.sessions.c.id==sid)).scalar_one()
        rules=dict(service.session(c,sid)['rules']); rules['category_id']='missing-category'
        configured=service.session_command(c,actor,sid,{'command':'configure','expected_version':before,'args':{'apply':'next_round','next_rules':rules}})
        assert configured['state']['status']=='EMPTY'
        assert service.next_selection(c,sid)['answer'] is None
        assert c.execute(sa.select(sa.func.count()).select_from(s.commands).where(s.commands.c.session_id==sid,s.commands.c.actor_id=='system:selection')).scalar_one()>=2
    # Published pre-reviewed hints freeze into the round; no arbitrary text accepted.
    with db.engine.begin() as c: c.execute(s.puzzles.update().where(s.puzzles.c.id=='pv-test').values(hints=[{'stage':1,'text':'常见水果'}]))
    live,body=prepared_round(db,actor,'Hint')
    with db.engine.begin() as c:
        hint=live.round_command(c,actor,body['round_id'],{'command':'reveal_hint','expected_version':1,'args':{'stage':1}})
        roundrow=c.execute(sa.select(s.rounds).where(s.rounds.c.id==body['round_id'])).mappings().one()
        assert live.public_round(roundrow)['published_hints'][0]['text']=='常见水果'
        assert hint['resource_version']==2
    with db.engine.begin() as c:
        disabled=disable(live,c,actor,'p-test',{'command':'disable','expected_version':1,'args':{'reason':'review correction'}})
        assert disabled['status']=='applied'
        # Frozen active round is unchanged after disabling future selection.
        assert c.execute(sa.select(s.rounds.c.frozen_puzzle).where(s.rounds.c.id==body['round_id'])).scalar_one()['word']=='苹果'
        c.execute(s.puzzles.update().where(s.puzzles.c.id=='pv-test').values(status='published',hints=[]))


def test_rejected_guess_is_persistent_and_observers_run_at_real_transaction_boundaries(environment):
    from app.infrastructure.db import schema as s
    db,actor=environment; service,body=prepared_round(db,actor,'Observer')
    class Observer:
        observations=[]; commits=[]
        def observe(self,c,kind,sid,facts):
            assert c.in_transaction()
            assert not ({'word','answer','platform_uid'} & set(facts))
            self.observations.append((kind,facts))
        def after_commit(self,sid,gid,phase,boot_id,mono_ns):
            with db.engine.begin() as c:
                c.execute(sa.text("SET LOCAL lock_timeout='250ms'")); c.execute(sa.select(s.sessions).where(s.sessions.c.id==sid).with_for_update()).one()
            self.commits.append((gid,phase))
    observer=Observer(); service.analytics=observer
    rejected=service.submit_guess(actor,'reject-persist',body|{'word':'x'})
    assert rejected['status']=='format_invalid'
    with db.engine.connect() as c: assert c.execute(sa.select(sa.func.count()).select_from(s.rejections).where(s.rejections.c.id==rejected['feedback_id'])).scalar_one()==1
    assert any(kind=='guess_rejected' for kind,facts in observer.observations)
    count=len(observer.commits)
    assert service.submit_guess(actor,'reject-persist',body|{'word':'x'})==rejected
    assert len(observer.commits)==count
    assert service.submit_guess(actor,'observer-exact',body)['solved']
    assert len(observer.commits)==count+2


def test_new_published_version_does_not_replace_frozen_ready_selection(environment):
    from app.modules.wordbank import selection
    from app.infrastructure.db import schema as s
    db,actor=environment; service=component_service(db)
    with db.engine.begin() as c:
        sid=service.create_session(c,actor,{'label':'Frozen preview','mode':'rehearsal','run_purpose':'local','rules':{}})['session_id']
        chosen=service.session_command(c,actor,sid,{'command':'select_next','expected_version':1,'args':{'strategy':'specific','puzzle_id':'p-test','expected_selection_id':None,'expected_selection_version':0}})['state']
        c.execute(s.puzzles.insert().values(id='pv-test-new',puzzle_id='p-test',version=2,word='梨子',aliases=[],category_id='fruit',difficulty=1,explanation='Version fixture',status='published',manifest_id='reviewed-manifest',calibration_id='reviewed-calibration',evaluation_id='reviewed-evaluation',approved_by=actor))
        session=service.session(c,sid,actor)
        puzzle,identity,revision=selection.consume(c,session,{'selection_id':chosen['selection_id'],'expected_selection_version':chosen['version'],'selection':{'strategy':'specific','puzzle_id':'p-test'}})
        assert puzzle['id']=='pv-test' and puzzle['word']=='苹果'
        c.execute(s.puzzles.delete().where(s.puzzles.c.id=='pv-test-new'))


def test_start_rejects_empty_next_rules_with_real_primary_server_state(environment):
    from app.modules.sessions.service import LocalService
    from app.modules.realtime.service import RealtimeService
    from app.infrastructure.db import schema as s
    from app.errors import DomainError
    db,actor=environment; service=LocalService(db,ComponentScorer()); rt=RealtimeService(service); service.realtime=rt; service.readiness.display_probe=rt.is_ready
    with db.engine.begin() as c:
        sid=service.create_session(c,actor,{'label':'Empty next start','mode':'rehearsal','run_purpose':'local','rules':{}})['session_id']
        rules=dict(service.session(c,sid)['rules']); rules['category_id']='no-published-category'
        configured=service.session_command(c,actor,sid,{'command':'configure','expected_version':1,'args':{'apply':'next_round','next_rules':rules}})
        token=service.ticket(c,actor,{'session_id':sid,'purpose':'primary'})['ticket']
    _,display=service.exchange(token); nonce,generation=rt.connect(display['id'],sid); _,upto=rt.replay(sid,'DISPLAY',0); rt.ready(display['id'],nonce,upto)
    assert rt.is_ready(session_id=sid,display_instance_id=display['id'])
    with pytest.raises(DomainError) as failure:
        with db.engine.begin() as c: service.session_command(c,actor,sid,{'command':'start','expected_version':configured['resource_version'],'args':{'run_purpose':'local'}})
    assert failure.value.code in {'NO_ELIGIBLE_PUZZLE','READINESS_NOT_MET'}
    with db.engine.connect() as c: assert c.execute(sa.select(s.sessions.c.status).where(s.sessions.c.id==sid)).scalar_one()=='CREATED'


def test_recover_end_session_observes_ended_inside_final_transaction(environment):
    from app.infrastructure.db import schema as s
    db,actor=environment; service,body=prepared_round(db,actor,'Recovered end')
    class Observer:
        facts=[]
        def observe(self,c,kind,sid,facts):
            if kind=='session_ended':
                assert c.execute(sa.select(s.sessions.c.status).where(s.sessions.c.id==sid)).scalar_one()=='ENDED'
                assert c.in_transaction() and facts.get('ended_at')
                self.facts.append(facts)
    observer=Observer(); service.analytics=observer
    with db.engine.begin() as c:
        version=c.execute(sa.select(s.sessions.c.version).where(s.sessions.c.id==body['session_id'])).scalar_one()
        result=service.session_command(c,actor,body['session_id'],{'command':'recover','expected_version':version,'args':{'strategy':'end_session'}})
    assert result['state']=='ENDED' and len(observer.facts)==1


def test_round_freezes_real_difficulty_and_observes_category(environment):
    from app.infrastructure.db import schema as s
    db,actor=environment
    class Observer:
        started=[]
        def observe(self,c,kind,sid,facts):
            if kind=='round_started': self.started.append(facts)
    observer=Observer()
    with db.engine.begin() as c: c.execute(s.puzzles.update().where(s.puzzles.c.id=='pv-test').values(difficulty=2))
    try:
        service,body=prepared_round(db,actor,'Difficulty provenance',observer=observer)
        with db.engine.connect() as c:
            frozen=c.execute(sa.select(s.rounds.c.frozen_puzzle).where(s.rounds.c.id==body['round_id'])).scalar_one()
        assert frozen.get('difficulty')==2 and frozen['category_id']=='fruit'
        assert observer.started[0]['difficulty']==2 and observer.started[0]['category_id']=='fruit'
    finally:
        with db.engine.begin() as c: c.execute(s.puzzles.update().where(s.puzzles.c.id=='pv-test').values(difficulty=1))
