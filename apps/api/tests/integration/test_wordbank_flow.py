import math
import sqlalchemy as sa
from fastapi.testclient import TestClient
from test_local_flow import environment,component_service,prepared_round

class Embeddings:
    manifest_id='sample-model'; manifest_sha256='a'*64; dimension=2
    def readiness(self): return {'ready':True,'manifest_id':self.manifest_id}
    async def embeddings(self,texts):
        values={'苹果':1.,'冰块':.1,'石头':.2,'果园':.6,'水果':.85,'苹果树':.95,'沙子':.1,'雨滴':.2,'树木':.6,'果肉':.85,'鲜果':.95}
        return [[values[text],math.sqrt(1-values[text]**2)] for text in texts]

def login(db):
    from app.main import create_app
    client=TestClient(create_app(db.settings,scorer=Embeddings(),display_probe=lambda **kw:True),headers={'Origin':'http://testserver'})
    data=client.post('/api/v1/auth/sessions',json={'username':'testoperator','password':'private-test-password-123'}).json()['data']
    client.headers['X-CSRF-Token']=data['csrf_token']
    return client

def post(client,path,data,key): return client.post(path,json=data,headers={'Idempotency-Key':key})

def draft(): return dict(word='苹果',aliases=[],category_id='fruit',difficulty=1,hints=[],explanation='水果',source_ref='owned-fixture')

def test_real_jobs_calibration_blind_review_publish_immutable_patch(environment):
    db,actor=environment; client=login(db)
    created=post(client,'/api/v1/puzzles',draft(),'new-draft'); assert created.status_code==200,created.text
    puzzle=created.json()['data']; pid=puzzle['puzzle_id']
    pairs=[{'guess':guess,'human_rating':rating} for guess,rating in [('冰块','cold'),('石头','cold'),('果园','warm'),('水果','hot'),('苹果树','hot')]]
    from app.modules.wordbank.worker import WordbankWorker
    worker=client.app.state.wordbank_worker
    for split in ('calibration','blind'):
        if split=='blind': pairs=[{'guess':guess,'human_rating':rating} for guess,rating in [('沙子','cold'),('雨滴','cold'),('树木','warm'),('果肉','hot'),('鲜果','hot')]]
        result=post(client,'/api/v1/scoring-evaluations',dict(puzzle_version_id=puzzle['id'],manifest_id='sample-model',split=split,pairs=pairs),split)
        assert result.status_code==202,result.text
        jid=result.json()['data']['job_id']; worker.tick()
        job=client.get('/api/v1/scoring-evaluations/'+jid).json()['data']
        assert job['status']=='succeeded',job
        assert job['result']['global_G1_Q'] is False
        if split=='blind': assert job['result']['annotation_locked']
    approved=post(client,f'/api/v1/puzzles/{pid}/commands',dict(command='approve',expected_version=1,args={'evaluation_id':jid,'decision':'approve','notes':'reviewed fixture'}),'approve')
    assert approved.status_code==200,approved.text
    published=post(client,f'/api/v1/puzzles/{pid}/commands',dict(command='publish',expected_version=1,args={'approved_version':1}),'publish')
    assert published.status_code==200,published.text
    assert post(client,f'/api/v1/puzzles/{pid}/commands',dict(command='publish',expected_version=1,args={'approved_version':1}),'publish').json()['data']==published.json()['data']
    edited=client.patch('/api/v1/puzzles/'+pid,json=draft()|{'word':'梨子','expected_version':1},headers={'Idempotency-Key':'edit'})
    assert edited.status_code==200,edited.text
    assert edited.json()['data']['version']==2 and edited.json()['data']['status']=='draft'
    from app.infrastructure.db import schema as s
    with db.engine.connect() as c:
        original=c.execute(sa.select(s.puzzles).where(s.puzzles.c.id==puzzle['id'])).mappings().one()
        assert original['word']=='苹果' and original['status']=='published' and original['scoring_artifact']['calibration']['b']==.2

def test_csv_requires_explicit_valid_rows_and_preserves_security(environment):
    db,actor=environment; client=login(db)
    raw='word,aliases,category_id,difficulty,hints,explanation,source_ref\n苹果,[],csv-test,1,[],水果,owned\nbad,[],csv-test,1,[],bad,owned\n'.encode()
    assert client.post('/api/v1/puzzle-imports',files={'file':('a.csv',raw)},headers={'Idempotency-Key':'csv-bad-origin','Origin':'http://evil.invalid'}).status_code==403
    result=client.post('/api/v1/puzzle-imports',files={'file':('a.csv',raw)},headers={'Idempotency-Key':'csv'})
    assert result.status_code==202,result.text
    jid=result.json()['data']['job_id']; client.app.state.wordbank_worker.tick()
    report=client.get('/api/v1/puzzle-imports/'+jid).json()['data']; assert report['result']['rows'][1]['row_number']==3 and report['result']['invalid_count']==1
    bad=post(client,f'/api/v1/puzzle-imports/{jid}/commands',dict(command='confirm',expected_version=report['version'],args={'rows':[3]}),'invalid-row')
    assert bad.status_code==422
    good=post(client,f'/api/v1/puzzle-imports/{jid}/commands',dict(command='confirm',expected_version=report['version'],args={'rows':[2]}),'valid-row')
    assert good.status_code==200,good.text
    listing=client.get('/api/v1/puzzles',params={'category_id':'csv-test','page_size':1}).json()['data']
    assert listing['total']==1 and listing['items'][0]['status']=='draft'

def test_word_ranking_contains_scored_current_round_words(environment):
    from app.infrastructure.db import schema as s
    from app.modules.auth.service import uid
    db,actor=environment; service,body=prepared_round(db,actor,'Word ranks')
    with db.engine.begin() as c:
        for index in range(10):
            pid=uid(); c.execute(s.participants.insert().values(id=pid,session_id=body['session_id'],identity_key='rank:'+str(index),display_name='P'+str(index)))
            c.execute(s.guesses.insert().values(id=uid(),session_id=body['session_id'],round_id=body['round_id'],participant_id=pid,source_mode='manual',source_id=uid(),normalized_word='词'+chr(0x4e00+index),admission_seq=index+1,status='scored',display_score=90-index,solved=False,boot_id=service.boot_id))
        ranking=service.ranking(c,body['session_id'])
        assert len(ranking['words'])==8 and ranking['words'][0]['display_score']==90

def test_runtime_maintenance_reports_unimplemented_truthfully(environment):
    db,actor=environment; client=login(db)
    data=client.get('/api/v1/runtime/status').json()['data']['maintenance_status']
    assert set(data)=={'command_id','operation','stage','requires_native_confirmation','last_error'}
    assert data['stage']=='unavailable' and data['last_error']['code']=='NATIVE_MAINTENANCE_UNAVAILABLE'

def test_auto_next_waits_frozen_intermission_and_readiness(environment):
    import time
    from app.infrastructure.db import schema as s
    from app.modules.rounds.worker import LocalRoundWorker
    from app.modules.auth.service import uid
    db,actor=environment; service,body=prepared_round(db,actor,'Auto-next')
    with db.engine.begin() as c:
        c.execute(s.puzzles.insert().values(id=uid(),puzzle_id=uid(),word='梨子',aliases=[],category_id='fruit',difficulty=1,explanation='fixture',status='published',manifest_id='reviewed-manifest',calibration_id='reviewed-calibration',evaluation_id='reviewed-evaluation',approved_by=actor))
        row=c.execute(sa.select(s.rounds).where(s.rounds.c.id==body['round_id'])).mappings().one()
        session=service.session(c,body['session_id'],actor)
        service.settle(c,session,row,'TIMEOUT',None,None)
    worker=LocalRoundWorker(service); worker.tick()
    with db.engine.connect() as c: assert c.execute(sa.select(sa.func.count()).select_from(s.rounds).where(s.rounds.c.session_id==body['session_id'])).scalar_one()==1
    with db.engine.begin() as c: c.execute(s.rounds.update().where(s.rounds.c.id==body['round_id']).values(next_due_ns=time.monotonic_ns()-1))
    worker.next_schedule_check=0; worker.tick()
    with db.engine.connect() as c:
        rounds=list(c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==body['session_id'])).mappings())
        assert len(rounds)==2 and any(r['status']=='RUNNING' for r in rounds)

def test_selection_rejects_published_version_from_different_model(environment):
    import pytest
    from app.errors import DomainError
    from app.infrastructure.db import schema as s
    from app.modules.auth.service import uid
    db,actor=environment; service=component_service(db); pid=uid()
    with db.engine.begin() as c:
        c.execute(s.puzzles.insert().values(id=uid(),puzzle_id=pid,word='桃子',aliases=[],category_id='fruit',difficulty=1,explanation='fixture',status='published',manifest_id='different-model',calibration_id='different-calibration',evaluation_id='different-evaluation',approved_by=actor))
        sid=service.create_session(c,actor,{'label':'Model mismatch','mode':'rehearsal','run_purpose':'local','rules':{}})['session_id']
    with db.engine.begin() as c:
        # Spec 17.4: a specific selection that exists but is not eligible is an
        # explicit 409, never a silent EMPTY that hides the mismatch.
        with pytest.raises(DomainError) as error:
            service.session_command(c,actor,sid,{'command':'select_next','expected_version':1,'args':{'strategy':'specific','puzzle_id':pid,'expected_selection_id':None,'expected_selection_version':0}})
        assert error.value.code=='NO_ELIGIBLE_PUZZLE'

def test_puzzle_editor_needs_no_session_permission_and_revocation_beats_cache(environment):
    from app.infrastructure.db import schema as s
    db,actor=environment; client=login(db)
    with db.engine.begin() as c:
        permissions=c.execute(sa.select(s.operators.c.permissions).where(s.operators.c.id==actor)).scalar_one()
        c.execute(s.operators.update().where(s.operators.c.id==actor).values(permissions=['puzzle.edit']))
    try:
        result=post(client,'/api/v1/puzzles',draft(),'editor-only'); assert result.status_code==200,result.text
        assert client.get('/api/v1/puzzles').status_code==200
        with db.engine.begin() as c: c.execute(s.operators.update().where(s.operators.c.id==actor).values(permissions=['diagnostics.read']))
        assert post(client,'/api/v1/puzzles',draft(),'editor-only').status_code==403
    finally:
        with db.engine.begin() as c: c.execute(s.operators.update().where(s.operators.c.id==actor).values(permissions=permissions))

def test_missing_model_is_rejected_before_freeze_and_wrong_job_route_denied(environment):
    db,actor=environment; client=login(db)
    puzzle=post(client,'/api/v1/puzzles',draft(),'missing-model-draft').json()['data']
    pairs=[{'guess':'冰块','human_rating':'cold'},{'guess':'果园','human_rating':'warm'},{'guess':'水果','human_rating':'hot'}]
    missing=post(client,'/api/v1/scoring-evaluations',{'puzzle_version_id':puzzle['id'],'manifest_id':'missing-model','split':'calibration','pairs':pairs},'missing-model-eval')
    assert missing.status_code==503 and missing.json()['error']['code']=='SCORER_NOT_READY'
    job=post(client,'/api/v1/scoring-evaluations',{'puzzle_version_id':puzzle['id'],'manifest_id':'sample-model','split':'calibration','pairs':pairs},'valid-frozen-model-eval').json()['data']
    assert job['status']=='queued' and job['result'] is None
    client.app.state.wordbank_worker.tick()
    assert client.get('/api/v1/puzzle-imports/'+job['job_id']).status_code==404
    assert post(client,f"/api/v1/puzzles/{puzzle['puzzle_id']}/commands",{'command':'evaluate','expected_version':1,'args':{'manifest_id':'sample-model','split':'calibration','pairs':[{'guess':'冰块','human_rating':'bad'}]}},'malformed-evaluate').status_code==422

def test_wordbank_difficulty_filter_is_applied_and_invalid_range_rejected(environment):
    db,actor=environment; client=login(db)
    for level in (1,5):
        created=post(client,'/api/v1/puzzles',draft()|{'category_id':'difficulty-p2','difficulty':level},'difficulty-p2-'+str(level)); assert created.status_code==200
    response=client.get('/api/v1/puzzles',params={'category_id':'difficulty-p2','difficulty':5})
    assert response.status_code==200,response.text
    assert [item['difficulty'] for item in response.json()['data']['items']]==[5]
    assert client.get('/api/v1/puzzles',params={'difficulty_min':5,'difficulty_max':1}).status_code==422
    assert client.get('/api/v1/puzzles',params={'difficulty':6}).status_code==422

def test_wordbank_cursor_binds_filters_and_freezes_concurrent_versions(environment):
    from app.infrastructure.db import schema as s
    from app.modules.auth.service import uid
    db,actor=environment; client=login(db)
    puzzles=[]
    for index in range(4): puzzles.append(post(client,'/api/v1/puzzles',draft()|{'category_id':'cursor-p2','difficulty':5},'cursor-p2-'+str(index)).json()['data'])
    expected=sorted(puzzles,key=lambda item:item['puzzle_id'])
    # Begin a new version before page 1 but commit after it: wall timestamps
    # alone cannot freeze this version set.
    with db.engine.connect() as pending:
        transaction=pending.begin()
        pending.execute(s.puzzles.insert().values(id=uid(),puzzle_id=expected[-1]['puzzle_id'],version=2,word='梨子',aliases=[],category_id='cursor-p2',difficulty=5,hints=[],explanation='new version fixture',source_ref='owned',status='draft'))
        params={'category_id':'cursor-p2','difficulty_min':5,'difficulty_max':5,'page_size':1}
        first=client.get('/api/v1/puzzles',params=params); assert first.status_code==200,first.text
        data=first.json()['data']; cursor=data.get('next_cursor'); assert cursor
        transaction.commit()
    cross=client.get('/api/v1/puzzles',params=params|{'cursor':cursor,'difficulty_min':1}); assert cross.status_code==422,cross.text
    position=len(cursor)//2; replacement='A' if cursor[position]!='A' else 'B'
    assert client.get('/api/v1/puzzles',params=params|{'cursor':cursor[:position]+replacement+cursor[position+1:]}).status_code==422
    observed=list(data['items'])
    while cursor:
        response=client.get('/api/v1/puzzles',params=params|{'cursor':cursor}); assert response.status_code==200,response.text
        page=response.json()['data']; observed.extend(page['items']); cursor=page['next_cursor']
    assert [item['id'] for item in observed]==[item['id'] for item in expected]
    assert len({item['puzzle_id'] for item in observed})==4

def test_evaluation_freezes_input_and_rejects_changed_target_version(environment):
    from app.infrastructure.db import schema as s
    db,actor=environment;client=login(db)
    puzzle=post(client,'/api/v1/puzzles',draft(),'freeze-target-draft').json()['data']
    pairs=[{'guess':guess,'human_rating':rating} for guess,rating in [('冰块','cold'),('果园','warm'),('水果','hot')]]
    created=post(client,'/api/v1/scoring-evaluations',{'puzzle_version_id':puzzle['id'],'manifest_id':'sample-model','split':'calibration','pairs':pairs},'freeze-target-job').json()['data']
    with db.engine.begin() as c:
        job=c.execute(sa.select(s.wordbank_jobs).where(s.wordbank_jobs.c.id==created['job_id'])).mappings().one()
        assert job['input']['frozen_puzzle']['word']=='苹果' and job['input']['puzzle_input_sha256']
        # Simulate forbidden same-version mutation; normal PATCH creates a new immutable version.
        c.execute(s.puzzles.update().where(s.puzzles.c.id==puzzle['id']).values(word='梨子'))
    client.app.state.wordbank_worker.tick()
    result=client.get('/api/v1/scoring-evaluations/'+created['job_id']).json()['data']
    assert result['status']=='failed' and result['error']['code']=='RESULT_STALE'
