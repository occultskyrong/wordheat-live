"""BE-A20: real dedicated-PG commit, retention and opaque round trace acceptance."""
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sqlalchemy as sa
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from app.errors import DomainError
from app.infrastructure.db import schema as s
from app.modules.analytics import models as m
from app.modules.analytics.service import moment
from app.modules.auth.service import AuthService

spec=importlib.util.spec_from_file_location('analytics_fixture',Path(__file__).parents[1]/'test_analytics.py')
helpers=importlib.util.module_from_spec(spec); spec.loader.exec_module(helpers)
@pytest.fixture
def env():
    if not helpers.ENV: pytest.skip('WORDHEAT_TEST_CONFIG dedicated PG configuration required')
    from app.config import read_fields
    from app.infrastructure.db.migrate import migrate
    from app.infrastructure.db.database import engine_for
    from sqlalchemy.engine import make_url
    values=read_fields(Path(helpers.ENV),{'WORDHEAT_TEST_DATABASE_URL','WORDHEAT_TEST_MIGRATION_DATABASE_URL'})
    assert make_url(values['WORDHEAT_TEST_DATABASE_URL']).database=='wordheat_test'
    migration=values['WORDHEAT_TEST_MIGRATION_DATABASE_URL']
    assert make_url(migration).database=='wordheat_test'
    engine=engine_for(migration)
    with engine.connect() as c:
        assert c.execute(sa.text("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()")).scalar_one()=='wordheat-live:test:local'
        assert c.execute(sa.select(s.instance.c.instance_id)).scalar_one()=='wordheat-local-development'
    engine.dispose()
    migrate(migration,'wordheat-local-development',make_url(values['WORDHEAT_TEST_DATABASE_URL']).username)
    fixture=helpers.env.__wrapped__(); result=next(fixture)
    try: yield result
    finally:
        with result.db.engine.begin() as c:
            c.execute(s.trace_steps.delete().where(s.trace_steps.c.message_id.like(result.prefix+'%')))
        try: next(fixture)
        except StopIteration: pass

def finish(e,rollback=False):
    from app.modules.sessions.service import LocalService
    core=LocalService.__new__(LocalService); core.analytics=e.svc; core.analytics_observer=None
    with e.db.engine.connect() as c:
        transaction=c.begin()
        c.execute(sa.select(s.sessions).where(s.sessions.c.id==e.sid).with_for_update())
        c.execute(s.sessions.update().where(s.sessions.c.id==e.sid).values(status='ENDED'))
        e.svc.observe(c,'session_ended',e.sid,{'ended_at':moment().isoformat()})
        core.touch(c,e.sid)
        if rollback: transaction.rollback()
        else: transaction.commit()

def persisted(e):
    with e.db.engine.connect() as c:
        return c.execute(sa.select(m.finals).where(m.finals.c.session_id==e.sid)).mappings().first()

def client(e):
    from app.modules.analytics.router import create_router as analytics_router
    from app.modules.operations.messages import create_router as messages_router
    app=FastAPI()
    core=SimpleNamespace(db=e.db,analytics=e.svc,connector_trace_metadata=getattr(e,'connector_trace_metadata',None),authorize_session=lambda c,sid,actor:e.svc.authorize(c,sid,actor))
    auth=AuthService(e.db)
    app.include_router(analytics_router(e.db,auth,e.svc,b'x'*32)); app.include_router(messages_router(auth,core))
    @app.middleware('http')
    async def request_id(request,call_next):
        request.state.request_id='retention-test'; return await call_next(request)
    @app.exception_handler(DomainError)
    async def error(request,exc):
        return JSONResponse({'error':{'code':exc.code,'details':exc.details}},status_code=exc.status_code)
    result=TestClient(app,headers={'X-BE-Extension-Version':'LW-BE/1.1.0'}); result.cookies.set('lw_operator',e.token)
    return result

def test_seal_atomically_persists_aggregate_before_any_get(env):
    e=env; helpers.add_guess(e); finish(e)
    row=persisted(e)
    assert row is not None, 'seal committed without durable anonymous final aggregate'
    with e.db.engine.connect() as c:
        session=c.execute(sa.select(s.sessions).where(s.sessions.c.id==e.sid)).mappings().one()
        assert row['payload']['as_of']['local_commit_watermark']==str(session['analytics_commit_counter'])
        assert c.execute(sa.select(sa.func.count()).select_from(m.snapshots).where(m.snapshots.c.session_id==e.sid)).scalar_one()==0
    sealed=row['payload']['closure']['sealed_at']
    from app.modules.analytics.service import stamp
    assert row['expires_at']==stamp(sealed)+timedelta(days=90)
    text=json.dumps(row['payload'])
    assert not any(secret in text for secret in ['private-answer','private-explanation','identity_key','normalized_word'])

def test_rollback_preserves_counter_and_has_no_final(env):
    e=env; helpers.add_guess(e)
    with e.db.engine.connect() as c: before=c.execute(sa.select(s.sessions.c.analytics_commit_counter).where(s.sessions.c.id==e.sid)).scalar_one()
    finish(e,rollback=True)
    assert persisted(e) is None
    with e.db.engine.connect() as c:
        assert c.execute(sa.select(s.sessions.c.analytics_commit_counter).where(s.sessions.c.id==e.sid)).scalar_one()==before
        assert not c.execute(sa.select(m.session_facts.c.payload).where(m.session_facts.c.session_id==e.sid)).scalar_one().get('sealed_at')

def age_owned_final(e,days):
    """Advance only this fixture's retained business timeline; no real clock mutation."""
    from app.modules.analytics.service import stamp,iso
    delta=timedelta(days=days)
    keys={'captured_at','sealed_at','started_at','ended_at','through_at','bucket_start','bucket_end'}
    def shift(value):
        if isinstance(value,dict): return {k:iso(stamp(v)-delta) if k in keys and v else shift(v) for k,v in value.items()}
        if isinstance(value,list): return [shift(v) for v in value]
        return value
    with e.db.engine.begin() as c:
        row=c.execute(sa.select(m.finals).where(m.finals.c.session_id==e.sid)).mappings().one()
        c.execute(m.finals.update().where(m.finals.c.session_id==e.sid).values(payload=shift(row['payload']),expires_at=row['expires_at']-delta))
        facts=c.execute(sa.select(m.session_facts.c.payload).where(m.session_facts.c.session_id==e.sid)).scalar_one()
        c.execute(m.session_facts.update().where(m.session_facts.c.session_id==e.sid).values(payload=shift(facts)))
        c.execute(s.guesses.update().where(s.guesses.c.session_id==e.sid).values(created_at=s.guesses.c.created_at-delta))
        c.execute(s.participants.update().where(s.participants.c.session_id==e.sid).values(created_at=s.participants.c.created_at-delta))
        c.execute(m.traces.update().where(m.traces.c.session_id==e.sid).values(expires_at=m.traces.c.expires_at-delta))
    return persisted(e)

def test_no_preget_day8_raw_cleanup_keeps_pages_csv_anomaly_and_basis(env):
    e=env; gid=helpers.add_guess(e)
    with e.db.engine.begin() as c:
        c.execute(s.trace_steps.insert().values(id=e.prefix+'retained-trace',message_id=gid,stage='feedback',state='committed',time=moment(),reason_code='CLOCK_DISCONTINUITY'))
        e.svc.observe(c,'round_settled',e.sid,{'round_id':e.rid,'anomaly_codes':['CLOCK_DISCONTINUITY']})
    finish(e)
    assert persisted(e) is not None, 'raw cleanup requires durable sealed aggregate'
    original=age_owned_final(e,8)
    # Only this fixture's raw rows are removed; no ledger, session, puzzle or metadata cleanup.
    with e.db.engine.begin() as c:
        c.execute(s.guesses.delete().where(s.guesses.c.session_id==e.sid))
        c.execute(m.attempts.delete().where(m.attempts.c.session_id==e.sid))
        c.execute(s.participants.delete().where(s.participants.c.session_id==e.sid))
    http=client(e); data=http.get(f'/api/v1/live-sessions/{e.sid}/analytics?limit=1').json()['data']; snap=data['snapshot_id']
    assert data['valid_distinct_guesses']==1 and data['round_details']['items'][0]['anomaly_codes']==['CLOCK_DISCONTINUITY']
    assert data['as_of']['local_commit_watermark']==original['payload']['as_of']['local_commit_watermark']
    assert data['coverage']==original['payload']['coverage']
    assert sum(x['valid_distinct_guesses'] for x in e.svc.snapshot(e.sid,e.actor)['_minutes'])==1
    rounds=http.get(f'/api/v1/live-sessions/{e.sid}/analytics/rounds',params={'snapshot_id':snap,'limit':1})
    assert rounds.status_code==200 and rounds.json()['data']['items'][0]['valid_distinct_guesses']==1
    cursor=data['minute_participation']['next_cursor']; minutes=http.get(f'/api/v1/live-sessions/{e.sid}/analytics',params={'snapshot_id':snap,'section':'minutes','cursor':cursor,'limit':1})
    assert minutes.status_code==200
    exported=http.get(f'/api/v1/live-sessions/{e.sid}/analytics',params={'snapshot_id':snap,'format':'csv'})
    assert exported.status_code==200 and 'CLOCK_DISCONTINUITY' in exported.text and 'private-answer' not in exported.text
    trace=http.get('/api/v1/messages/'+data['round_details']['items'][0]['trace_ref']+'/trace')
    assert trace.status_code==200 and trace.json()['data']['steps'][0]['reason_code']=='CLOCK_DISCONTINUITY'
    assert persisted(e)['expires_at']==original['expires_at']


def test_opaque_round_trace_resolves_with_current_authorization(env):
    e=env; gid=helpers.add_guess(e)
    with e.db.engine.begin() as c:
        c.execute(s.trace_steps.insert().values(id=e.prefix+'trace',message_id=gid,stage='feedback',state='committed',time=moment(),reason_code='CLOCK_DISCONTINUITY'))
        ref=c.execute(sa.select(m.round_facts.c.payload).where(m.round_facts.c.round_id==e.rid)).scalar_one()['trace_ref']
    http=client(e); response=http.get(f'/api/v1/messages/{ref}/trace')
    assert response.status_code==200, 'opaque analytics trace_ref has no messages/trace resolver'
    data=response.json()['data']; assert data['round_id']==e.rid and data['steps'][0]['reason_code']=='CLOCK_DISCONTINUITY'
    assert 'identity_key' not in response.text and 'private-answer' not in response.text
    with e.db.engine.begin() as c: c.execute(s.operators.update().where(s.operators.c.id==e.actor).values(permissions=[]))
    assert http.get(f'/api/v1/messages/{ref}/trace').status_code==403
    with e.db.engine.begin() as c: c.execute(s.operators.update().where(s.operators.c.id==e.actor).values(permissions=['diagnostics.read']))
    with e.db.engine.begin() as c:
        other=e.prefix+'other'
        c.execute(s.operators.insert().values(id=other,username=other,password_hash='fixture',display_name='Other',permissions=['diagnostics.read']))
        c.execute(s.sessions.update().where(s.sessions.c.id==e.sid).values(actor_id=other))
    assert http.get(f'/api/v1/messages/{ref}/trace').status_code==403
    with e.db.engine.begin() as c:
        c.execute(s.sessions.update().where(s.sessions.c.id==e.sid).values(actor_id=e.actor)); c.execute(s.operators.delete().where(s.operators.c.id==other))

def test_day31_trace_expired_reason_while_day90_anomaly_survives(env,monkeypatch):
    e=env; helpers.add_guess(e)
    with e.db.engine.begin() as c:
        e.svc.observe(c,'round_settled',e.sid,{'round_id':e.rid,'anomaly_codes':['INTERRUPTED']})
        ref=c.execute(sa.select(m.round_facts.c.payload).where(m.round_facts.c.round_id==e.rid)).scalar_one()['trace_ref']
    finish(e)
    import app.modules.operations.messages as messages
    monkeypatch.setattr(messages,'now',lambda: moment()+timedelta(days=31))
    response=client(e).get(f'/api/v1/messages/{ref}/trace')
    assert response.status_code==410 and response.json()['error']['code']=='TRACE_EXPIRED'
    assert response.json()['error']['details']['reason']=='EXPIRED'
    assert persisted(e)['payload']['_rounds'][0]['anomaly_codes']==['INTERRUPTED']

@pytest.mark.parametrize('kind',['crash','cross_boot','negative'])
def test_real_commit_missing_latency_never_becomes_zero(env,kind):
    e=env; gid=helpers.add_guess(e)
    if kind!='crash':
        e.svc.after_commit(e.sid,gid,'admission','boot-a',10000000)
        e.svc.after_commit(e.sid,gid,'feedback','boot-b' if kind=='cross_boot' else 'boot-a',1000000)
    finish(e); row=persisted(e)
    assert row is not None
    data=row['payload']; assert data['latency_missing_samples']==1 and data['denominators']['latency_samples']==0
    assert all(data[f'pipeline_latency_p{p}_ms'] is None for p in [50,95,99])
    e.svc.after_commit(e.sid,gid,'feedback','boot-a',20000000)
    assert persisted(e)['payload']==data


def test_final_retention_ends_at_seal_plus90_without_get_extension(env):
    e=env; helpers.add_guess(e); finish(e); aged=age_owned_final(e,91)
    response=client(e).get(f'/api/v1/live-sessions/{e.sid}/analytics')
    assert response.status_code==410
    assert response.json()['error']['code']=='ANALYTICS_SNAPSHOT_UNAVAILABLE'
    assert response.json()['error']['details']['reason']=='EXPIRED'
    assert persisted(e)['expires_at']==aged['expires_at']  # Error rollback cannot renew the expired basis.
    with e.db.engine.connect() as c:
        assert c.execute(sa.select(m.session_facts.c.payload).where(m.session_facts.c.session_id==e.sid)).scalar_one()['sealed_at']
        assert c.execute(sa.select(sa.func.count()).select_from(s.guesses).where(s.guesses.c.session_id==e.sid)).scalar_one()==1


def test_existing_message_id_trace_stays_compatible_and_has_expiry_reason(env,monkeypatch):
    e=env; gid=helpers.add_guess(e)
    response=client(e).get(f'/api/v1/messages/{gid}/trace')
    assert response.status_code==200 and response.json()['data']['message_id']==gid
    import app.modules.operations.messages as messages
    monkeypatch.setattr(messages,'now',lambda:moment()+timedelta(days=31))
    expired=client(e).get(f'/api/v1/messages/{gid}/trace')
    assert expired.status_code==410 and expired.json()['error']['details']['reason']=='EXPIRED'


def test_recovery_after_commit_persists_final_with_same_watermark(env):
    e=env; gid=helpers.add_guess(e,'queued')
    with e.db.engine.begin() as c:
        c.execute(sa.select(s.sessions).where(s.sessions.c.id==e.sid).with_for_update())
        c.execute(s.sessions.update().where(s.sessions.c.id==e.sid).values(status='ENDED'))
        e.svc.observe(c,'session_ended',e.sid,{'ended_at':moment().isoformat()})
        c.execute(s.sessions.update().where(s.sessions.c.id==e.sid).values(analytics_commit_counter=s.sessions.c.analytics_commit_counter+1))
    assert persisted(e) is None
    with e.db.engine.begin() as c: c.execute(s.guesses.update().where(s.guesses.c.id==gid).values(status='scored'))
    e.svc.after_commit(e.sid,gid,'feedback','restored-boot',30000)
    row=persisted(e); assert row is not None
    with e.db.engine.connect() as c:
        assert row['payload']['as_of']['local_commit_watermark']==str(c.execute(sa.select(s.sessions.c.analytics_commit_counter).where(s.sessions.c.id==e.sid)).scalar_one())
    assert row['payload']['latency_missing_samples']==1


def test_get_never_repairs_missing_final_or_extends_seal(env):
    e=env
    with e.db.engine.begin() as c:
        c.execute(s.sessions.update().where(s.sessions.c.id==e.sid).values(status='ENDED'))
        e.svc.observe(c,'session_ended',e.sid,{'ended_at':moment().isoformat()})
    assert persisted(e) is None
    response=client(e).get(f'/api/v1/live-sessions/{e.sid}/analytics')
    assert response.status_code==410 and response.json()['error']['details']['reason']=='FINAL_AGGREGATE_MISSING'
    assert persisted(e) is None


def test_day31_trace_payload_pruned_but_opaque_tombstone_is_still410(env,monkeypatch):
    e=env; gid=helpers.add_guess(e)
    with e.db.engine.begin() as c:
        c.execute(s.trace_steps.insert().values(id=e.prefix+'prune-trace',message_id=gid,stage='feedback',state='committed',time=moment(),reason_code='CLOCK_DISCONTINUITY'))
    finish(e); original=persisted(e); ref=original['payload']['_rounds'][0]['trace_ref']
    with e.db.engine.begin() as c:
        e.svc.prune_expired_traces(c,e.sid,moment()+timedelta(days=31))
        assert c.execute(sa.select(sa.func.count()).select_from(m.traces).where(m.traces.c.session_id==e.sid)).scalar_one()==0
        assert c.execute(sa.select(m.round_facts.c.payload).where(m.round_facts.c.round_id==e.rid)).scalar_one()['trace_ref']==ref
    import app.modules.operations.messages as messages
    monkeypatch.setattr(messages,'now',lambda:moment()+timedelta(days=31))
    response=client(e).get(f'/api/v1/messages/{ref}/trace')
    assert response.status_code==410 and response.json()['error']['details']['reason']=='EXPIRED'
    assert persisted(e)['payload']==original['payload']


def test_real_end_and_receipt_refresh_query_watermark_keep_durable_business_basis(env):
    e=env
    helpers.test_new_sealed_snapshot_has_current_pending_but_old_snapshot_stays_immutable(e)
    with e.db.engine.connect() as c:
        final=c.execute(sa.select(m.finals).where(m.finals.c.session_id==e.sid)).mappings().one()
        snapshots=list(c.execute(sa.select(m.snapshots).where(m.snapshots.c.session_id==e.sid).order_by(m.snapshots.c.captured_at)).mappings())
    assert len(snapshots)==2
    first,last=(row['payload'] for row in snapshots)
    assert final['payload']['as_of']['local_commit_watermark']==first['as_of']['local_commit_watermark']
    assert int(last['as_of']['local_commit_watermark'])>int(final['payload']['as_of']['local_commit_watermark'])
    for key in ['metric_definition_version','coverage','_minutes','_rounds','points_net']:
        assert first[key]==last[key]==final['payload'][key]


@pytest.mark.parametrize('budget',['rows','bytes'])
def test_snapshot_budget_does_not_rollback_end_or_fake_a_final(env,monkeypatch,budget):
    e=env; gid=helpers.add_guess(e)
    with e.db.engine.begin() as c:
        pid=c.execute(sa.select(s.guesses.c.participant_id).where(s.guesses.c.id==gid)).scalar_one()
        c.execute(s.ledger.insert().values(id=e.prefix+'retained-ledger',round_id=e.rid,participant_id=pid,reason='participation',reference_id=gid,delta=1))
    import app.modules.analytics.service as analytics
    monkeypatch.setattr(analytics,'MAX_ROWS' if budget=='rows' else 'MAX_BYTES',1)
    try: finish(e)
    except DomainError as error:
        pytest.fail('query budget rolled back business end: '+error.code)
    assert persisted(e) is None
    with e.db.engine.connect() as c:
        row=c.execute(sa.select(s.sessions).where(s.sessions.c.id==e.sid)).mappings().one()
        facts=c.execute(sa.select(m.session_facts.c.payload).where(m.session_facts.c.session_id==e.sid)).scalar_one()
        assert row['status']=='ENDED' and not facts.get('sealed_at')
        assert facts['materialization_pending_reason']=='FINAL_AGGREGATE_TOO_LARGE'
        assert c.execute(sa.select(sa.func.count()).select_from(s.guesses).where(s.guesses.c.session_id==e.sid)).scalar_one()==1
        assert c.execute(sa.select(sa.func.sum(s.ledger.c.delta)).where(s.ledger.c.round_id==e.rid)).scalar_one()==1
        counter=row['analytics_commit_counter']
    http=client(e)
    for _ in range(2):
        response=http.get(f'/api/v1/live-sessions/{e.sid}/analytics')
        assert response.status_code==413 and response.json()['error']['code']=='ANALYTICS_SNAPSHOT_TOO_LARGE'
    assert persisted(e) is None
    with e.db.engine.connect() as c:
        assert c.execute(sa.select(s.sessions.c.analytics_commit_counter).where(s.sessions.c.id==e.sid)).scalar_one()==counter
        assert c.execute(sa.select(sa.func.count()).select_from(m.snapshots).where(m.snapshots.c.session_id==e.sid)).scalar_one()==0


@pytest.mark.parametrize('failure',['dependency','database'])
def test_nonbudget_persistence_error_propagates_and_rolls_back_whole_transaction(env,monkeypatch,failure):
    e=env; helpers.add_guess(e)
    with e.db.engine.connect() as c:
        before=c.execute(sa.select(s.sessions.c.analytics_commit_counter).where(s.sessions.c.id==e.sid)).scalar_one()
    def fail(c,session,captured):
        if failure=='dependency': raise DomainError('DEPENDENCY_UNAVAILABLE','Synthetic unavailable dependency',503)
        c.execute(sa.text('SELECT 1/0'))
    monkeypatch.setattr(e.svc,'business_payload',fail)
    with pytest.raises(DomainError if failure=='dependency' else sa.exc.DataError): finish(e)
    assert persisted(e) is None
    with e.db.engine.connect() as c:
        session=c.execute(sa.select(s.sessions).where(s.sessions.c.id==e.sid)).mappings().one()
        facts=c.execute(sa.select(m.session_facts.c.payload).where(m.session_facts.c.session_id==e.sid)).scalar_one()
        assert session['status']=='ACTIVE' and session['analytics_commit_counter']==before
        assert not facts.get('sealed_at') and not facts.get('materialization_pending_reason')


def test_raw_comment_cleanup_preserves_ledger_and_anonymous_final(env):
    e=env; gid=helpers.add_guess(e)
    with e.db.engine.begin() as c:
        pid=c.execute(sa.select(s.guesses.c.participant_id).where(s.guesses.c.id==gid)).scalar_one()
        c.execute(s.ledger.insert().values(id=e.prefix+'preserved-ledger',round_id=e.rid,participant_id=pid,reason='participation',reference_id=gid,delta=1))
    finish(e); original=age_owned_final(e,8)
    assert original is not None
    with e.db.engine.begin() as c:
        c.execute(s.guesses.delete().where(s.guesses.c.session_id==e.sid))
        c.execute(m.attempts.delete().where(m.attempts.c.session_id==e.sid))
        assert c.execute(sa.select(sa.func.sum(s.ledger.c.delta)).where(s.ledger.c.round_id==e.rid)).scalar_one()==1
    data=client(e).get(f'/api/v1/live-sessions/{e.sid}/analytics').json()['data']
    assert data['points_net']==1 and data['valid_distinct_guesses']==1
    serialized=json.dumps(original['payload'])
    assert all(value not in serialized for value in [gid,pid,'private-answer','normalized_word','identity_key'])
