import time
import sqlalchemy as sa
from test_local_flow import environment, ComponentScorer
from app.infrastructure.db import schema as s
from app.modules.sessions.service import LocalService

class Score(ComponentScorer):
    async def score(self,**kwargs): return 87.5

def started(db,actor,auto_next=False):
    core=LocalService(db,Score(),display_probe=lambda **k:True)
    with db.engine.begin() as c:
        session=core.create_session(c,actor,{'label':'Mac spec flow','mode':'rehearsal','run_purpose':'local','rules':{'auto_next':auto_next}})
        ticket=core.ticket(c,actor,{'session_id':session['session_id'],'purpose':'primary'})
    core.exchange(ticket['ticket'])
    with db.engine.begin() as c:
        result=core.session_command(c,actor,session['session_id'],{'command':'start','expected_version':1,'args':{'run_purpose':'local'}})
    return core,session['session_id'],result

def test_start_creates_first_round_and_end_closes_it(environment):
    db,actor=environment; core,sid,result=started(db,actor)
    with db.engine.begin() as c:
        row=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid)).mappings().one_or_none()
        assert row is not None, 'start must create its first round'
        assert row['status']=='RUNNING'
        session=core.session(c,sid,actor)
        ended=core.session_command(c,actor,sid,{'command':'end','expected_version':session['version'],'args':{'reason':'done'}})
        assert ended['status']=='applied'
        assert core.session(c,sid,actor)['status']=='ENDED'
        assert c.execute(sa.select(sa.func.count()).select_from(s.settlements).where(s.settlements.c.round_id==row['id'])).scalar_one()==1

def test_scoring_is_persistent_and_driven_after_request(environment):
    from app.modules.scoring.worker import ScoreWorker
    db,actor=environment; core,sid,_=started(db,actor)
    with db.engine.connect() as c: row=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid)).mappings().one()
    body={'session_id':sid,'round_id':row['id'],'short_code':row['short_code'],'word':'香蕉','display_name':'演练玩家'}
    accepted=core.submit_guess(actor,'async-score',body)
    assert accepted['status']=='queued'
    with db.engine.connect() as c: assert c.execute(sa.select(sa.func.count()).select_from(s.ledger).where(s.ledger.c.round_id==row['id'])).scalar_one()==0
    ScoreWorker(core).tick()
    with db.engine.connect() as c:
        guess=c.execute(sa.select(s.guesses).where(s.guesses.c.id==accepted['feedback_id'])).mappings().one()
        assert guess['status']=='scored' and guess['display_score']==87.5
        assert c.execute(sa.select(sa.func.sum(s.ledger.c.delta)).where(s.ledger.c.round_id==row['id'])).scalar_one()==7
    assert core.submit_guess(actor,'async-score',body)['status']=='scored'

def test_official_ports_create_freeze_and_admit_are_used(environment):
    from app.modules.auth.service import now
    from datetime import timedelta
    db,actor=environment; calls=[]
    class Coordinator:
        def authorize_create(self,c,actor,body): return dict(app_id='app',room_id='room',room_binding_id='binding',installation_id='install',live_profile='beta',authorization_id='grant',candidate_digest='candidate',grant_digest='grant-hash')
        def prepare_start(self,c,session,*,fresh_start=False):
            assert fresh_start is True
            calls.append('prepare'); return {'command_id':'prepare','status':'pending','resource_version':session['version'],'state':{'intent_id':'prepare','operation':'prepare'},'error':None}
        def mark_started(self,c,session): calls.append('mark_started')
        def terminal_feedback(self,c,feedback_id): return None
        def require_current(self,*a,**k): return None
        def create_round(self,c,session,round): calls.append('open'); return {'command_id':'open','status':'pending','resource_version':round['version'],'state':{'intent_id':'open','operation':'open'},'error':None}
        def command(self,c,session,round,operation,args): calls.append(operation); return {'command_id':operation,'status':'pending','resource_version':1,'state':{'intent_id':operation,'operation':operation},'error':None}
    core=LocalService(db,Score(),display_probe=lambda **k:True,official=Coordinator())
    with db.engine.begin() as c:
        detail=core.create_session(c,actor,{'label':'Official port','mode':'official','run_purpose':'test_live','room_binding_id':'binding','live_profile':'beta','rules':{}})
        sid=detail['session_id']; session=core.session(c,sid,actor)
        assert session['room_id']=='room'
        pending=core.session_command(c,actor,sid,{'command':'start','expected_version':1,'args':{'run_purpose':'test_live'}})
        assert pending['status']=='pending'
        core.apply_window_result(c,session,None,{'operation':'prepare','status':'succeeded','intent_id':'prepare'})
        row=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid)).mappings().one()
        assert row['status']=='PREPARING'
        core.apply_window_result(c,core.session(c,sid),row,{'operation':'open','status':'succeeded','intent_id':'open','window_epoch':'3','deadline_at':(now()+timedelta(seconds=240)).isoformat(),'remaining_ms':240000})
        admitted=core.admit_delivery(c,core.session(c,sid),{'schema_version':1,'contract_version':'LW-C1/1.0.0','stream_id':'stream','stream_seq':'10','original_generation':'1','round_id':row['id'],'window_epoch':'3','relay_received_at':now().isoformat(),'type':'comment','eligible':True,'provider_test':True,'run_purpose':'test_live','source_mode':'synthetic','payload':{'source_msg_id':'message','participant_key':'opaque-person','display_name':'User','content':{'text':'猜'+row['short_code']+'香蕉'}}})
        assert admitted['status']=='queued'
    with db.engine.begin() as c:
        session=core.session(c,sid,actor)
        recovery=core.session_command(c,actor,sid,{'command':'recover','expected_version':session['version'],'args':{'strategy':'new_round'}})
        assert recovery['status']=='pending'
        row=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid)).mappings().one()
        core.apply_window_result(c,session,row,{'operation':'interrupt','status':'failed','error_code':'CONTROLLED_TEST_INTERRUPTION'})
        core.apply_window_result(c,core.session(c,sid),None,{'operation':'recover','status':'succeeded','strategy':'new_round'})
        assert c.execute(sa.select(sa.func.count()).select_from(s.rounds).where(s.rounds.c.session_id==sid)).scalar_one()==2
    assert calls==['prepare','mark_started','open','recover','open']

def test_active_dependency_loss_interrupts_without_new_winner(environment):
    from app.modules.rounds.worker import LocalRoundWorker
    db,actor=environment; core,sid,_=started(db,actor)
    class Lost:
        def readiness(self): return {'ready':False,'manifest_id':None}
    core.scorer=Lost(); core.readiness.scorer=core.scorer
    LocalRoundWorker(core).tick()
    with db.engine.connect() as c:
        assert c.execute(sa.select(s.sessions.c.status).where(s.sessions.c.id==sid)).scalar_one()=='RECOVERY_REQUIRED'
        assert c.execute(sa.select(s.rounds.c.end_reason).where(s.rounds.c.session_id==sid)).scalar_one()=='INTERRUPTED'

def test_realtime_catchup_is_bounded_without_skipping_cursor(environment):
    from app.modules.realtime.service import RealtimeService
    db,actor=environment; core,sid,_=started(db,actor); realtime=RealtimeService(core)
    with db.engine.begin() as c:
        after=core.session(c,sid)['display_seq']
        for number in range(300): core.event(c,sid,'ranking.updated',{'ranking':{'words':[],'players':[]}})
    first,cursor=realtime.replay(sid,'DISPLAY',after)
    assert len(first)==256 and cursor==after+256
    second,last=realtime.replay(sid,'DISPLAY',cursor)
    assert len(second)==44 and last==after+300

def test_official_score_waits_through_short_reconnect_without_reinference(environment):
    from app.modules.scoring.worker import ScoreWorker
    from app.errors import DomainError
    db,actor=environment; core,sid,_=started(db,actor)
    with db.engine.connect() as c: row=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid)).mappings().one()
    body={'session_id':sid,'round_id':row['id'],'short_code':row['short_code'],'word':'香蕉','display_name':'演练重连提交夹具'}
    result=core.submit_guess(actor,'wait-through-reconnect',body)
    # Controlled commit-state fixture, not official provider acceptance evidence.
    with db.engine.begin() as c: c.execute(s.sessions.update().where(s.sessions.c.id==sid).values(mode='official',run_purpose='test_live'))
    class Connection:
        reconnecting=True
        def terminal_feedback(self,c,feedback_id): return None
        def require_current(self,*a,**k):
            if self.reconnecting: raise DomainError('CONNECTOR_RECONNECTING','short reconnect',409,True)
    core.official=Connection(); worker=ScoreWorker(core)
    deadline=time.monotonic()+1
    while time.monotonic()<deadline:
        worker.tick()
        with db.engine.connect() as c: state=c.execute(sa.select(s.score_jobs.c.status).where(s.score_jobs.c.guess_id==result['feedback_id'])).scalar_one()
        if state!='queued': break
        time.sleep(.005)
    with db.engine.connect() as c:
        assert c.execute(sa.select(s.score_jobs.c.status).where(s.score_jobs.c.guess_id==result['feedback_id'])).scalar_one()=='waiting'
        assert c.execute(sa.select(sa.func.count()).select_from(s.ledger).where(s.ledger.c.round_id==row['id'])).scalar_one()==0
    core.official.reconnecting=False
    with db.engine.begin() as c: c.execute(s.score_jobs.update().where(s.score_jobs.c.guess_id==result['feedback_id']).values(lease_until=None))
    worker.tick()
    with db.engine.connect() as c: assert c.execute(sa.select(s.guesses.c.status).where(s.guesses.c.id==result['feedback_id'])).scalar_one()=='scored'

def test_model_change_invalidates_saved_selection_and_notifies_privately(environment):
    from app.modules.rounds.worker import LocalRoundWorker
    db,actor=environment; core=LocalService(db,Score(),display_probe=lambda **k:True)
    with db.engine.begin() as c:
        sid=core.create_session(c,actor,{'label':'Version change','mode':'rehearsal','run_purpose':'local','rules':{}})['session_id']
        selected=core.session_command(c,actor,sid,{'command':'select_next','expected_version':1,'args':{'strategy':'specific','puzzle_id':'p-test','expected_selection_id':None,'expected_selection_version':0}})['state']
    class Changed:
        def readiness(self): return {'ready':True,'manifest_id':'different-frozen-model'}
    core.scorer=Changed();core.readiness.scorer=core.scorer
    LocalRoundWorker(core).tick()
    with db.engine.connect() as c:
        current=core.next_selection(c,sid)
        assert current['selection_id']!=selected['selection_id'] and current['status']=='EMPTY'
        old=c.execute(sa.select(s.next_selections).where(s.next_selections.c.id==selected['selection_id'])).mappings().one()
        assert old['status']=='INVALIDATED' and old['reason_code']=='MANIFEST_CHANGED'
        assert c.execute(sa.select(s.commands.c.id).where(s.commands.c.session_id==sid,s.commands.c.actor_id=='system:selection')).first()

def test_empty_start_persists_empty_selection_and_failed_command(environment):
    from fastapi.testclient import TestClient
    from app.main import create_app
    db,actor=environment
    client=TestClient(create_app(db.settings,scorer=Score(),display_probe=lambda **k:True),headers={'Origin':'http://testserver','X-BE-Extension-Version':'LW-BE/1.1.0'})
    csrf=client.post('/api/v1/auth/sessions',json={'username':'testoperator','password':'private-test-password-123'}).json()['data']['csrf_token'];client.headers['X-CSRF-Token']=csrf
    def post(path,body,key): return client.post(path,json=body,headers={'Idempotency-Key':key})
    sid=post('/api/v1/live-sessions',{'label':'Empty pool','mode':'rehearsal','run_purpose':'local','rules':{'category_id':'no-eligible-puzzle'}},'empty-create').json()['data']['session_id']
    ticket=post('/api/v1/display-tickets',{'session_id':sid,'purpose':'primary'},'empty-ticket').json()['data']
    client.post('/api/v1/display-sessions',json={'ticket':ticket['ticket']})
    failed=post('/api/v1/live-sessions/'+sid+'/commands',{'command':'start','expected_version':1,'args':{'run_purpose':'local'}},'empty-start')
    assert failed.status_code==409
    detail=client.get('/api/v1/live-sessions/'+sid).json()['data']
    assert detail['status']=='CREATED' and detail['next_selection']['status']=='EMPTY'
    command_id=failed.json()['error']['details']['command_id']
    assert client.get('/api/v1/commands/'+command_id).json()['data']['status']=='failed'

def test_bundle_activation_refuses_browser_json_instead_of_native_capability(environment):
    import pytest
    from app.errors import DomainError
    db,actor=environment;core=LocalService(db,Score(),display_probe=lambda **k:True)
    with pytest.raises(DomainError) as error:
        with db.engine.begin() as c: core.activate_verified_bundle(c,{'verified':True,'actor_id':actor})
    assert error.value.code=='FORBIDDEN'

def test_score_worker_processes_bounded_parallel_claims(environment):
    import threading
    from app.modules.scoring.worker import ScoreWorker
    from app.modules.auth.service import uid,now
    from datetime import timedelta
    db,actor=environment;core,sid,_=started(db,actor)
    entered=threading.Event();release=threading.Event();lock=threading.Lock();active=[0];peak=[0]
    class Blocking(Score):
        def score(self,**kwargs):
            with lock:
                active[0]+=1;peak[0]=max(peak[0],active[0])
                if active[0]==3:entered.set()
            assert release.wait(1)
            with lock:active[0]-=1
            return 80.
    core.scorer=Blocking()
    with db.engine.begin() as c:
        row=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid)).mappings().one()
        versions=core.score_input_versions(row)
        for index in range(5):
            pid=uid();gid=uid();c.execute(s.participants.insert().values(id=pid,session_id=sid,identity_key='bounded-'+str(index),display_name='执行并发夹具'))
            c.execute(s.guesses.insert().values(id=gid,session_id=sid,round_id=row['id'],participant_id=pid,source_mode='manual',source_id=uid(),normalized_word='词'+chr(0x4e00+index),admission_seq=index+1,status='queued',solved=False,admitted_mono_ns=time.monotonic_ns(),boot_id=core.boot_id))
            c.execute(s.score_jobs.insert().values(id=uid(),guess_id=gid,status='queued',deadline_at=now()+timedelta(seconds=2),input_versions=versions))
    worker=ScoreWorker(core,concurrency=3)
    try:
        worker.start();assert entered.wait(1);assert peak[0]==3
        release.set()
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            with db.engine.connect() as c: pending=c.execute(sa.select(sa.func.count()).select_from(s.score_jobs).join(s.guesses,s.guesses.c.id==s.score_jobs.c.guess_id).where(s.guesses.c.session_id==sid,s.score_jobs.c.status.in_(['queued','running','waiting']))).scalar_one()
            if not pending:break
            time.sleep(.02)
        assert pending==0 and peak[0]<=3
    finally:release.set();worker.stop()

def test_bundle_activation_command_reconciles_without_reselection(environment,monkeypatch):
    from app.modules.wordbank import bundle
    db,actor=environment;core=LocalService(db,Score(),display_probe=lambda **k:True)
    identity={'version':1,'model_manifest_id':'reviewed-manifest','model_manifest_sha256':'a'*64,'wordbank_version':'execution-test','calibration_version':'execution-test','puzzles':[]}
    # Isolate activation transaction idempotency; full authority runs through real-model workflow.
    monkeypatch.setattr(bundle,'validate',lambda core,c,identity:identity)
    with db.engine.begin() as c:
        c.execute(s.rounds.update().where(s.rounds.c.session_id.in_(sa.select(s.sessions.c.id).where(s.sessions.c.actor_id==actor)),s.rounds.c.status.in_(['PREPARING','RUNNING','PAUSED','DRAINING','FAULTED'])).values(status='INTERMISSION'))
    permit=core.bundle_permit_issuer()(identity,actor,'stable-native-command')
    with db.engine.connect() as c: previous=dict(c.execute(sa.select(s.catalog)).mappings().one())
    try:
        with db.engine.begin() as c:first=core.activate_verified_bundle(c,permit)
        with db.engine.begin() as c:second=core.activate_verified_bundle(c,permit)
        assert second==first and first['changed'] is True
    finally:
        with db.engine.begin() as c:c.execute(s.catalog.update().where(s.catalog.c.id=='global').values(joint_identity=previous['joint_identity'],bundle_digest=previous['bundle_digest'],model_manifest_id=previous['model_manifest_id'],model_manifest_sha256=previous['model_manifest_sha256'],revision=s.catalog.c.revision+1))

def test_trusted_rehearsal_port_preserves_participant_rate_and_mode(environment):
    import pytest
    from app.errors import DomainError
    db,actor=environment;core,sid,_=started(db,actor)
    with db.engine.connect() as c:row=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid)).mappings().one()
    body={'session_id':sid,'round_id':row['id'],'short_code':row['short_code'],'word':'香蕉'}
    issuer=core.rehearsal_input_issuer();a=issuer('synthetic-a','甲');b=issuer('synthetic-b','乙')
    first=core.admit_rehearsal_input(actor,'synthetic-a-one',body,a)
    second=core.admit_rehearsal_input(actor,'synthetic-b-one',body,b)
    limited=core.admit_rehearsal_input(actor,'synthetic-a-two',body|{'word':'芒果'},a)
    assert first['status']=='queued' and second['status']=='queued' and limited['status']=='rate_limited'
    with db.engine.connect() as c:
        rows=list(c.execute(sa.select(s.guesses.c.source_mode,s.guesses.c.provider_test).where(s.guesses.c.session_id==sid)))
        assert len(rows)==2 and all(tuple(row)==('bridge',True) for row in rows)
        assert c.execute(sa.select(sa.func.count()).select_from(s.participants).where(s.participants.c.session_id==sid)).scalar_one()==2
    with pytest.raises(DomainError):core.admit_rehearsal_input(actor,'json-forgery',body,{'participant_ref':'synthetic-c'})

def test_cross_boot_paused_round_keeps_checkpoint_but_cancels_old_score(environment):
    from app.modules.rounds.worker import LocalRoundWorker
    db,actor=environment;core,sid,_=started(db,actor)
    with db.engine.connect() as c:row=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid)).mappings().one()
    accepted=core.submit_guess(actor,'paused-old-boot',{'session_id':sid,'round_id':row['id'],'short_code':row['short_code'],'word':'香蕉','display_name':'演练恢复'})
    with db.engine.begin() as c:core.round_command(c,actor,row['id'],{'command':'pause','expected_version':row['version'],'args':{}})
    reboot=LocalService(db,Score(),display_probe=lambda **k:True);LocalRoundWorker(reboot).recover_boot()
    with db.engine.connect() as c:
        assert c.execute(sa.select(s.rounds.c.status).where(s.rounds.c.id==row['id'])).scalar_one()=='PAUSED'
        assert c.execute(sa.select(s.sessions.c.status).where(s.sessions.c.id==sid)).scalar_one()=='RECOVERY_REQUIRED'
        assert c.execute(sa.select(s.score_jobs.c.status).where(s.score_jobs.c.guess_id==accepted['feedback_id'])).scalar_one()=='failed'

def test_rejection_source_namespace_does_not_collide_between_manual_and_bridge(environment):
    db,actor=environment;core,sid,_=started(db,actor)
    with db.engine.connect() as c:row=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid)).mappings().one()
    body={'session_id':sid,'round_id':row['id'],'short_code':row['short_code'],'word':'x'}
    manual=core.submit_guess(actor,'bridge:collision',body|{'display_name':'演练人工'})
    permit=core.rehearsal_input_issuer()('local-source','合成')
    bridge=core.admit_rehearsal_input(actor,'collision',body,permit)
    assert manual['feedback_id']!=bridge['feedback_id'] and manual['status']==bridge['status']=='format_invalid'

def test_known_model_health_loss_cannot_award_an_exact_hit_before_monitor_tick(environment):
    import pytest
    from app.errors import DomainError
    db,actor=environment;core,sid,_=started(db,actor)
    with db.engine.connect() as c:row=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid)).mappings().one()
    class Lost:
        def readiness(self):return {'ready':False,'manifest_id':None}
    core.scorer=Lost()
    with pytest.raises(DomainError) as error:core.submit_guess(actor,'no-model-exact',{'session_id':sid,'round_id':row['id'],'short_code':row['short_code'],'word':'苹果','display_name':'演练依赖守卫'})
    assert error.value.code=='DEPENDENCY_UNAVAILABLE'
    with db.engine.connect() as c:assert c.execute(sa.select(sa.func.count()).select_from(s.ledger).where(s.ledger.c.round_id==row['id'])).scalar_one()==0
