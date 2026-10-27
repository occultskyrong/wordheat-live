"""Real dedicated-PG analytics integration; env values are never reported."""
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import json
import os
import secrets
import sqlalchemy as sa
from sqlalchemy.engine import make_url
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from app.infrastructure.db.database import Database, engine_for
from app.infrastructure.db import schema as s
from app.modules.auth.service import AuthService, token_hash
from app.errors import DomainError
from app.modules.analytics.service import AnalyticsService, moment
from app.modules.analytics import models as m

ROOT=Path(__file__).resolve().parents[5]
ENV=os.environ.get('WORDHEAT_TEST_CONFIG')

def _owned_env_impl():
    if not ENV: pytest.skip('WORDHEAT_TEST_CONFIG dedicated PG configuration required')
    from app.config import read_fields
    values=read_fields(Path(ENV),{'WORDHEAT_TEST_DATABASE_URL','WORDHEAT_TEST_MIGRATION_DATABASE_URL'})
    runtime=values['WORDHEAT_TEST_DATABASE_URL']; migration=values['WORDHEAT_TEST_MIGRATION_DATABASE_URL']
    assert make_url(runtime).database=='wordheat_test'
    eng=engine_for(migration)
    with eng.begin() as c:
        assert c.execute(sa.text('SELECT current_database()')).scalar_one()=='wordheat_test'
        assert c.execute(sa.text("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()")).scalar_one()=='wordheat-live:test:local'
        assert c.execute(sa.select(s.instance.c.instance_id)).scalar_one()=='wordheat-local-development'
        sql=Path(__file__).parents[2]/'migrations/versions/20261025-snapshot-analytics.sql'
        c.exec_driver_sql(sql.read_text())
        role=c.dialect.identifier_preparer.quote(make_url(runtime).username)
        c.exec_driver_sql('GRANT SELECT,INSERT,UPDATE,DELETE ON analytics_snapshots,analytics_final_aggregates,analytics_session_facts,analytics_round_facts,analytics_attempt_observations,analytics_latency_observations,analytics_exports TO '+role)
    eng.dispose()
    db=Database(SimpleNamespace(database_url=runtime)); svc=AnalyticsService(db,b'x'*32)
    prefix='analytics_'+secrets.token_hex(7); sid=prefix+'s'; actor=prefix+'a'; rid=prefix+'r'; pid=prefix+'p'; token=secrets.token_urlsafe(24)
    now=moment()-timedelta(minutes=3)
    with db.engine.begin() as c:
        c.execute(s.operators.insert().values(id=actor,username=actor,password_hash='not-a-login-hash',display_name='Analyst',permissions=['diagnostics.read']))
        c.execute(s.auth_sessions.insert().values(id=prefix+'auth',actor_id=actor,token_hash=token_hash(token),csrf='csrf',expires_at=moment()+timedelta(hours=1)))
        c.execute(s.sessions.insert().values(id=sid,actor_id=actor,label='Analytics test',mode='rehearsal',run_purpose='local',status='ACTIVE',analytics_commit_counter=1))
        c.execute(s.puzzles.insert().values(id=pid,puzzle_id=pid,word='private-answer',category_id='fixture',difficulty=2,explanation='private-explanation',status='published'))
        c.execute(s.rounds.insert().values(id=rid,session_id=sid,puzzle_version_id=pid,status='REVEALED',short_code=prefix,boot_id='test',end_reason='SOLVED',frozen_puzzle={'category_id':'fixture','difficulty':2}))
        svc.observe(c,'session_started',sid,{'started_at':now.isoformat()})
        svc.observe(c,'round_started',sid,{'round_id':rid,'started_at':now.isoformat(),'mono_ns':1,'boot_id':'test'})
        svc.observe(c,'round_settled',sid,{'round_id':rid,'ended_at':(now+timedelta(seconds=100)).isoformat(),'mono_ns':100000000001,'boot_id':'test'})
    result=SimpleNamespace(db=db,svc=svc,sid=sid,actor=actor,rid=rid,pid=pid,token=token,prefix=prefix,start=now,lifecycle_contexts=[])
    yield result
    for context in reversed(result.lifecycle_contexts):context.close()
    with db.engine.begin() as c:
        # All deletes are confined to this fixture's owned identifiers.
        c.execute(m.latencies.delete().where(m.latencies.c.session_id==sid))
        c.execute(s.score_jobs.delete().where(s.score_jobs.c.guess_id.in_(sa.select(s.guesses.c.id).where(s.guesses.c.session_id==sid))))
        c.execute(s.ledger.delete().where(s.ledger.c.round_id.in_(sa.select(s.rounds.c.id).where(s.rounds.c.session_id==sid))))
        for table in [s.grants,s.connections,s.receipts]:
            c.execute(table.delete().where(table.c.display_id.in_(sa.select(s.displays.c.id).where(s.displays.c.session_id==sid))))
        for table in [s.effects,s.commands,s.configuration,s.rejections]: c.execute(table.delete().where(table.c.session_id==sid))
        c.execute(s.idempotency.delete().where(s.idempotency.c.actor_id==actor))
        for table in [s.guesses,s.participants,s.outbox,s.displays,s.tickets,s.selection_state,s.next_selections]: c.execute(table.delete().where(table.c.session_id==sid))
        c.execute(s.settlements.delete().where(s.settlements.c.round_id.in_(sa.select(s.rounds.c.id).where(s.rounds.c.session_id==sid))))
        c.execute(s.rounds.delete().where(s.rounds.c.session_id==sid)); c.execute(s.sessions.delete().where(s.sessions.c.id==sid))
        c.execute(s.puzzles.delete().where(s.puzzles.c.id==pid)); c.execute(s.auth_sessions.delete().where(s.auth_sessions.c.actor_id==actor)); c.execute(s.operators.delete().where(s.operators.c.id==actor))
    db.engine.dispose()

@pytest.fixture
def env():
    # Component cases share a pytest process with core producers. Give this
    # fixture its own registered child namespace, not a global runtime reset or
    # an end command against another fixture's unfinished session.
    import conftest
    isolation=getattr(conftest,'_isolation',None)
    if isolation is None:
        implementation=_owned_env_impl()
        yield next(implementation)
        try:next(implementation)
        except StopIteration:pass
        return
    with isolation.temporary_schema() as namespace:
        previous=isolation.schema;isolation.schema=namespace
        implementation=_owned_env_impl()
        try:
            value=next(implementation)
            yield value
        finally:
            try:
                try:next(implementation)
                except StopIteration:pass
            finally:isolation.schema=previous

def add_guess(e,status='scored',offset=10):
    ident=e.prefix+secrets.token_hex(4)
    with e.db.engine.begin() as c:
        c.execute(s.participants.insert().values(id=ident+'p',session_id=e.sid,identity_key=ident,display_name='=private formula'))
        c.execute(s.guesses.insert().values(id=ident,session_id=e.sid,round_id=e.rid,participant_id=ident+'p',source_mode='manual',source_id=ident,normalized_word='same',status=status,solved=False,boot_id='test',created_at=e.start+timedelta(seconds=offset)))
        c.execute(s.sessions.update().where(s.sessions.c.id==e.sid).values(analytics_commit_counter=s.sessions.c.analytics_commit_counter+1))
    return ident

def app_client(e):
    from app.modules.analytics.router import create_router
    app=FastAPI(); app.include_router(create_router(e.db,AuthService(e.db),e.svc,b'x'*32))
    @app.exception_handler(DomainError)
    async def failure(request,error): return JSONResponse({'error':{'code':error.code,'details':error.details}},status_code=error.status_code)
    client=TestClient(app,headers={'X-BE-Extension-Version':'LW-BE/1.1.0'}); client.cookies.set('lw_operator',e.token)
    return client

def age_snapshot(e,identity):
    with e.db.engine.begin() as c:
        c.execute(m.snapshots.update().where(m.snapshots.c.id==identity).values(captured_at=moment()-timedelta(seconds=6),expires_at=moment()+timedelta(hours=23)))

def test_frozen_pages_csv_pending_and_void(env):
    e=env; add_guess(e); pending=add_guess(e,'queued',70)
    client=app_client(e)
    first=client.get(f'/api/v1/live-sessions/{e.sid}/analytics?limit=1')
    assert first.status_code==200
    snap=first.json()['data']; identity=snap['snapshot_id']
    assert snap['valid_distinct_guesses']==1 and snap['latency_missing_samples']==1
    assert snap['denominators']['duration_samples']==1
    old_csv=client.get(f'/api/v1/live-sessions/{e.sid}/analytics?format=csv&snapshot_id={identity}')
    assert old_csv.status_code==200 and 'private-answer' not in old_csv.text
    with e.db.engine.begin() as c:
        c.execute(s.guesses.update().where(s.guesses.c.id==pending).values(status='scored'))
        c.execute(s.sessions.update().where(s.sessions.c.id==e.sid).values(analytics_commit_counter=s.sessions.c.analytics_commit_counter+1))
    age_snapshot(e,identity)
    newer=client.get(f'/api/v1/live-sessions/{e.sid}/analytics').json()['data']
    assert newer['valid_distinct_guesses']==2
    assert newer['minute_participation']['items'][1]['valid_distinct_guesses']==1
    assert client.get(f'/api/v1/live-sessions/{e.sid}/analytics?format=csv&snapshot_id={identity}').content==old_csv.content
    with e.db.engine.begin() as c:
        c.execute(s.rounds.update().where(s.rounds.c.id==e.rid).values(end_reason='VOID'))
        c.execute(s.sessions.update().where(s.sessions.c.id==e.sid).values(analytics_commit_counter=s.sessions.c.analytics_commit_counter+1))
    age_snapshot(e,newer['snapshot_id'])
    void=client.get(f'/api/v1/live-sessions/{e.sid}/analytics').json()['data']
    assert void['valid_distinct_guesses']==0 and void['round_details']['items'][0]['exclusion_reason']=='VOID'
    assert void['attempt_outcomes']['void_excluded']==2

def test_paging_cursor_permission_expiry_and_rate(env):
    e=env; client=app_client(e); url=f'/api/v1/live-sessions/{e.sid}/analytics'
    first=client.get(url+'?limit=1').json()['data']; identity=first['snapshot_id']; cursor=first['minute_participation']['next_cursor']
    page=client.get(url,params=dict(snapshot_id=identity,section='minutes',cursor=cursor,limit=1))
    assert page.status_code==200 and page.json()['data']['items'][0]['bucket_start']!=first['minute_participation']['items'][0]['bucket_start']
    assert client.get(url,params=dict(snapshot_id=identity,section='minutes',cursor=cursor+'x',limit=1)).status_code==422
    assert client.get(url,params=dict(snapshot_id=identity,section='minutes',cursor=cursor,limit=2)).status_code==422
    assert client.get(url+'?format=csv').status_code==422
    assert client.get(url).json()['data']['snapshot_id']==identity
    add_guess(e)
    assert client.get(url+'?fresh=true').status_code==429
    with e.db.engine.begin() as c: c.execute(s.operators.update().where(s.operators.c.id==e.actor).values(permissions=[]))
    assert client.get(url+'?snapshot_id='+identity).status_code==403
    with e.db.engine.begin() as c:
        c.execute(s.operators.update().where(s.operators.c.id==e.actor).values(permissions=['diagnostics.read']))
        c.execute(m.snapshots.update().where(m.snapshots.c.id==identity).values(expires_at=moment()-timedelta(seconds=1)))
    expired=client.get(url+'?snapshot_id='+identity)
    assert expired.status_code==410 and expired.json()['error']['details']['reason']=='EXPIRED'

def test_pin_capacity_and_csv_formula_cleanup(env):
    e=env; snap=e.svc.snapshot(e.sid,e.actor); identity=snap['snapshot_id']
    with e.svc.pinned(e.sid,e.actor,identity):
        with e.db.engine.begin() as c:
            # Scope filled with pinned copies so the next reservation must fail atomically.
            row=dict(c.execute(sa.select(m.snapshots).where(m.snapshots.c.id==identity)).mappings().one())
            for i in range(15): c.execute(m.snapshots.insert().values(**(row|{'id':identity+str(i)})))
        with e.svc.locked() as c:
            with c.begin():
                with pytest.raises(DomainError,match='capacity'): e.svc.reserve(c,e.actor,e.sid,1,moment())
    payload=snap|{'source_partitions':[{'source_mode':'=CMD()'}]}
    path=e.svc.csv_file(payload)
    assert path.stat().st_mode&0o077==0
    assert "'=CMD()" in path.read_text()
    path.unlink()

def test_missing_observation_is_unknown_and_commit_latency(env):
    e=env; gid=add_guess(e)
    e.svc.after_commit(e.sid,gid,'admission','boot-a',1000000)
    e.svc.after_commit(e.sid,gid,'feedback','boot-b',8000000)
    snap=e.svc.snapshot(e.sid,e.actor)
    assert snap['latency_missing_samples']==1 and snap['pipeline_latency_p50_ms'] is None
    assert snap['source_partitions'][0]['eligible_for_production'] is False

def test_expired_payload_releases_sql_quota_and_eviction_is_not_json_null(env):
    e=env; snap=e.svc.snapshot(e.sid,e.actor)
    with e.db.engine.begin() as c:
        c.execute(m.snapshots.update().where(m.snapshots.c.id==snap['snapshot_id']).values(expires_at=moment()-timedelta(seconds=1)))
        e.svc.expire(c,moment())
        assert c.execute(sa.select(m.snapshots.c.payload.is_(None)).where(m.snapshots.c.id==snap['snapshot_id'])).scalar_one() is True
        assert c.execute(sa.select(m.snapshots.c.size_bytes).where(m.snapshots.c.id==snap['snapshot_id'])).scalar_one()==0

def test_final_aggregate_survives_query_expiry_and_late_commit_samples(env):
    e=env; gid=add_guess(e)
    with e.db.engine.begin() as c:
        e.svc.observe(c,'session_ended',e.sid,{'ended_at':moment().isoformat(),'sealed_at':moment().isoformat()})
        c.execute(s.sessions.update().where(s.sessions.c.id==e.sid).values(status='ENDED',analytics_commit_counter=9))
        e.svc.persist_sealed(c,e.sid)
    snap=e.svc.snapshot(e.sid,e.actor)
    assert snap['final'] is True
    e.svc.after_commit(e.sid,gid,'feedback','test',9000)
    with e.db.engine.begin() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(m.latencies).where(m.latencies.c.guess_id==gid)).scalar_one()==0
        assert c.execute(sa.select(m.finals.c.expires_at).where(m.finals.c.session_id==e.sid)).scalar_one()>moment()+timedelta(days=89)
        c.execute(m.snapshots.update().where(m.snapshots.c.id==snap['snapshot_id']).values(expires_at=moment()-timedelta(hours=2),captured_at=moment()-timedelta(hours=25)))
    rebuilt=e.svc.snapshot(e.sid,e.actor)
    assert rebuilt['snapshot_id']!=snap['snapshot_id']
    assert rebuilt['as_of']['captured_at']!=snap['as_of']['captured_at']
    assert rebuilt['closure']['sealed_at']==snap['closure']['sealed_at']
    assert rebuilt['coverage']==snap['coverage'] and rebuilt['points_net']==snap['points_net']

def test_large_raw_input_is_database_aggregated_into_one_round(env):
    e=env; gid=add_guess(e); count=25001
    with e.db.engine.begin() as c:
        participant=c.execute(sa.select(s.guesses.c.participant_id).where(s.guesses.c.id==gid)).scalar_one()
        c.execute(sa.text("""INSERT INTO guesses(id,session_id,round_id,participant_id,source_mode,source_id,normalized_word,status,solved,boot_id,created_at)
          SELECT :prefix||n::text,:sid,:rid,:participant,'manual',:prefix||n::text,n::text,'scored',false,'test',CAST(:at AS timestamptz)
          FROM generate_series(1,:amount) n"""),{'prefix':e.prefix+'bulk','sid':e.sid,'rid':e.rid,'participant':participant,'at':e.start+timedelta(seconds=20),'amount':count})
        c.execute(s.sessions.update().where(s.sessions.c.id==e.sid).values(analytics_commit_counter=s.sessions.c.analytics_commit_counter+1))
    snap=e.svc.snapshot(e.sid,e.actor)
    assert snap['valid_distinct_guesses']==count+1 and snap['participants_count']==1
    assert len(snap['_rounds'])==1 and len(snap['_minutes'])<=4
    assert snap['_rounds'][0]['valid_distinct_guesses']==count+1

def test_domain_end_seals_only_after_real_pending_work_is_terminal(env):
    e=env; gid=add_guess(e,'queued')
    with e.db.engine.begin() as c:
        c.execute(s.sessions.update().where(s.sessions.c.id==e.sid).values(status='ENDED'))
        e.svc.observe(c,'session_ended',e.sid,{'ended_at':moment().isoformat()})
        assert not c.execute(sa.select(m.session_facts.c.payload).where(m.session_facts.c.session_id==e.sid)).scalar_one().get('sealed_at')
        c.execute(s.guesses.update().where(s.guesses.c.id==gid).values(status='scored'))
        assert e.svc.try_seal_session(c,e.sid) is True
        c.execute(s.sessions.update().where(s.sessions.c.id==e.sid).values(analytics_commit_counter=s.sessions.c.analytics_commit_counter+1))
        e.svc.persist_sealed(c,e.sid)
    assert e.svc.snapshot(e.sid,e.actor)['final'] is True

def test_official_missing_ack_schema_requires_trusted_observation(env):
    e=env
    with e.db.engine.begin() as c: c.execute(s.sessions.update().where(s.sessions.c.id==e.sid).values(mode='official',run_purpose='production'))
    with e.db.engine.connect() as c: queue_exists=c.execute(sa.text("SELECT to_regclass('connector_ack_outbox')")).scalar_one_or_none() is not None
    if not queue_exists:
        with pytest.raises(DomainError) as error: e.svc.snapshot(e.sid,e.actor)
        assert error.value.code=='DEPENDENCY_UNAVAILABLE' and error.value.details['reason']=='ACK_SCHEMA_MISSING'
    else:
        assert e.svc.snapshot(e.sid,e.actor)['closure']['local_delivery_pending']['ack_outbox']==0
    with e.db.engine.begin() as c:
        e.svc.observe_delivery(c,e.sid,'connector',1,ack_outbox=0)
    result=e.svc.snapshot(e.sid,e.actor)
    assert result['closure']['local_delivery_pending']['ack_outbox']==0
    assert result['closure']['remote_fulfillment_pending'] is None

def test_missing_effect_metadata_is_explicitly_unavailable(env,monkeypatch):
    e=env; monkeypatch.delattr(s,'effects')
    with pytest.raises(DomainError) as error: e.svc.snapshot(e.sid,e.actor)
    assert error.value.status_code==503 and error.value.code=='DEPENDENCY_UNAVAILABLE'

def core3_hit_client(e,check_analytics=True):
    from app.config import Settings
    from app.application import create_app
    from app.modules.auth.service import PERMISSIONS
    from app.modules.sessions.service import RULES
    class ComponentScorer:
        def readiness(self): return {'ready':True,'manifest_id':'reviewed-manifest'}
        def score(self,**kw): raise AssertionError('Exact hit must not call model')
    with e.db.engine.begin() as c:
        c.execute(s.rounds.delete().where(s.rounds.c.id==e.rid))
        c.execute(m.session_facts.delete().where(m.session_facts.c.session_id==e.sid))
        c.execute(s.sessions.update().where(s.sessions.c.id==e.sid).values(status='CREATED',version=1,rules=RULES))
        c.execute(s.operators.update().where(s.operators.c.id==e.actor).values(permissions=PERMISSIONS))
        c.execute(s.selection_state.insert().values(id=e.prefix+'selection',session_id=e.sid,revision=0))
        c.execute(s.configuration.insert().values(id=e.prefix+'config',session_id=e.sid))
        c.execute(s.puzzles.update().where(s.puzzles.c.id==e.pid).values(word='苹果',manifest_id='reviewed-manifest',calibration_id='calibration-test',evaluation_id='evaluation-test',approved_by=e.actor))
    from contextlib import ExitStack
    from tempfile import TemporaryDirectory
    lifecycle=ExitStack();e.lifecycle_contexts.append(lifecycle)
    configuration=Path(lifecycle.enter_context(TemporaryDirectory(prefix='wh-analytics-',dir='/private/tmp')))
    settings=Settings(e.db.settings.database_url,allowed_hosts=('testserver',),allowed_origins=('http://testserver',),analytics_cursor_key=secrets.token_bytes(32),server_root=configuration,source_root=Path(__file__).resolve().parents[4],environment='test')
    app=create_app(settings,scorer=ComponentScorer(),display_probe=lambda **scope:True)
    client=lifecycle.enter_context(TestClient(app,headers={'Origin':'http://testserver','X-BE-Extension-Version':'LW-BE/1.1.0','X-CSRF-Token':'csrf'}))
    client.cookies.set('lw_operator',e.token)
    def post(path,body,key): return client.post(path,json=body,headers={'Idempotency-Key':e.prefix+key})
    ticket=post('/api/v1/display-tickets',dict(session_id=e.sid,purpose='primary'),'ticket')
    assert ticket.status_code==200, ticket.text
    display=TestClient(app,headers={'Origin':'http://testserver'})
    exchange=display.post('/api/v1/display-sessions',json={'ticket':ticket.json()['data']['ticket']})
    assert exchange.status_code==200
    ds=exchange.json()['data']; display.headers['X-CSRF-Token']=ds['receipt_csrf_token']
    detail=client.get(f'/api/v1/live-sessions/{e.sid}').json()['data']
    selected=post(f'/api/v1/live-sessions/{e.sid}/commands',{'command':'select_next','expected_version':detail['version'],'args':{'strategy':'specific','puzzle_id':e.pid,'expected_selection_id':detail['next_selection']['selection_id'],'expected_selection_version':detail['next_selection']['version']}},'select-fixture')
    assert selected.status_code==200,selected.text
    started=post(f'/api/v1/live-sessions/{e.sid}/commands',{'command':'start','expected_version':selected.json()['data']['resource_version'],'args':{'run_purpose':'local'}},'start')
    assert started.status_code==200, started.text
    with e.db.engine.connect() as c:
        current_round=c.execute(sa.select(s.rounds.c.id,s.rounds.c.short_code).where(s.rounds.c.session_id==e.sid).order_by(s.rounds.c.created_at.desc()).limit(1)).mappings().one()
    rnd={'round_id':current_round['id'],'short_code':current_round['short_code']}
    hit=post('/api/v1/rehearsal/guesses',{'session_id':e.sid,'round_id':rnd['round_id'],'short_code':rnd['short_code'],'word':'苹果','display_name':'Real observer'},'hit')
    assert hit.status_code==200, hit.text
    if not check_analytics: return app,client,display,ds
    stats=client.get(f'/api/v1/live-sessions/{e.sid}/analytics')
    assert stats.status_code==200, stats.text
    data=stats.json()['data']
    assert data['participants_count']==1 and data['valid_distinct_guesses']==1 and data['points_net']==20
    assert data['coverage']['started_at'] is not None
    assert data['round_details']['items'][0]['started_at'] is not None
    assert data['denominators']['duration_samples']==1
    assert data['closure']['local_delivery_pending']=={'outbox':0,'display_effects':1,'ack_outbox':0}
    assert data['denominators']['latency_samples']==1
    lifecycle.close()

def test_core3_http_mutations_generate_real_observer_facts_and_analytics(env):
    core3_hit_client(env)


def test_new_sealed_snapshot_has_current_pending_but_old_snapshot_stays_immutable(env):
    import time
    e=env; app,operator,display,ds=core3_hit_client(e,check_analytics=False)
    current=operator.get(f'/api/v1/live-sessions/{e.sid}').json()['data']
    ended=operator.post(f'/api/v1/live-sessions/{e.sid}/commands',json={'command':'end','expected_version':current['version'],'args':{}},headers={'Idempotency-Key':e.prefix+'end'})
    assert ended.status_code==200,ended.text
    old=operator.get(f'/api/v1/live-sessions/{e.sid}/analytics').json()['data']
    assert old['final'] is True and old['closure']['local_delivery_pending']['display_effects']==1
    resume={'kind':'resume','schema_version':1,'contract_version':'LW-C1/1.0.0','session_id':e.sid,'after_seq':'0'}
    with display.websocket_connect('/api/v1/display/ws') as ws:
        ws.send_json(resume)
        ready=None
        for _ in range(20):
            value=ws.receive_json()
            if value.get('kind')=='resume_ready': ready=value; break
        assert ready and len(ready['pending_effects'])==1
        effect=ready['pending_effects'][0]
        batch={'display_instance_id':ds['display_instance_id'],'render_generation':ready['render_generation'],'batch_id':e.prefix+'processed','stage':'received','deliveries':[{'delivery_id':effect['delivery_ids'][0],'feedback_id':effect['feedback_id'],'client_time':'2026-10-25T01:00:00.000Z'}]}
        assert display.post('/api/v1/display/receipts',json=batch).status_code==200
        batch['stage']='processed'; processed=display.post('/api/v1/display/receipts',json=batch)
        assert processed.status_code==200,processed.text
        # Counter changes cannot bypass the creation interval.
        assert operator.get(f'/api/v1/live-sessions/{e.sid}/analytics?fresh=true').status_code==429
        time.sleep(5.05)
        fresh=operator.get(f'/api/v1/live-sessions/{e.sid}/analytics?fresh=true')
        assert fresh.status_code==200,fresh.text
        new=fresh.json()['data']
        assert new['snapshot_id']!=old['snapshot_id'] and new['final'] is True
        assert new['closure']['local_delivery_pending']['display_effects']==0
        assert int(new['as_of']['local_commit_watermark'])>int(old['as_of']['local_commit_watermark'])
        assert new['as_of']['captured_at']>old['as_of']['captured_at']
        assert new['coverage']==old['coverage'] and new['closure']['sealed_at']==old['closure']['sealed_at']
        assert new['points_net']==old['points_net']==20
        assert new['round_details']['items']==old['round_details']['items']
        frozen=operator.get(f"/api/v1/live-sessions/{e.sid}/analytics?snapshot_id={old['snapshot_id']}").json()['data']
        assert frozen==old
    app.state.database.engine.dispose()
