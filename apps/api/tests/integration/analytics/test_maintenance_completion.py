"""Mac executable analytics retention, trusted observations and bounded recovery."""
from datetime import timedelta
import hashlib
import hmac
import importlib.util
from pathlib import Path
import pytest
import sqlalchemy as sa
from app.modules.analytics import models as m
from app.modules.analytics.service import moment
from app.infrastructure.db import schema as s

spec=importlib.util.spec_from_file_location('analytics_retention_helpers',Path(__file__).with_name('test_commit_retention_acceptance.py'))
h=importlib.util.module_from_spec(spec); spec.loader.exec_module(h)
env=h.env

def test_official_local_snapshot_has_observed_ack_and_unknown_remote(env):
    e=env
    with e.db.engine.begin() as c:
        c.execute(s.sessions.update().where(s.sessions.c.id==e.sid).values(mode='official',run_purpose='test_live'))
        assert hasattr(e.svc,'observe_delivery'), 'trusted local ACK observation port missing'
        e.svc.observe_delivery(c,e.sid,'connector',1,ack_outbox=3,remote_fulfillment_pending=None,relay_coverage='partial',relay_confirmed_stream_seq='12')
        c.execute(s.sessions.update().where(s.sessions.c.id==e.sid).values(analytics_commit_counter=s.sessions.c.analytics_commit_counter+1))
    snap=e.svc.snapshot(e.sid,e.actor)
    with e.db.engine.connect() as c:
        queue_exists=c.execute(sa.text("SELECT to_regclass('connector_ack_outbox')")).scalar_one_or_none() is not None
        assert c.execute(sa.select(m.delivery.c.payload).where(m.delivery.c.session_id==e.sid)).scalar_one()['ack_outbox']==3
    assert snap['closure']['local_delivery_pending']['ack_outbox']==(0 if queue_exists else 3)
    assert snap['closure']['remote_fulfillment_pending'] is None
    assert snap['coverage']['relay_coverage']=='partial'

def test_legacy_unobserved_round_trace_ref_resolves_before_seal(env):
    e=env; h.helpers.add_guess(e)
    with e.db.engine.begin() as c: c.execute(m.round_facts.delete().where(m.round_facts.c.session_id==e.sid))
    data=h.client(e).get(f'/api/v1/live-sessions/{e.sid}/analytics').json()['data']
    ref=hmac.new(b'x'*32,('trace:'+e.sid+':'+e.rid).encode(),'sha256').hexdigest()
    assert data['round_details']['items'][0]['trace_ref']==ref
    response=h.client(e).get('/api/v1/messages/'+ref+'/trace')
    assert response.status_code==200, 'legacy HMAC ref cannot resolve without observer facts'
    with e.db.engine.begin() as c: c.execute(s.rounds.update().where(s.rounds.c.id==e.rid).values(created_at=moment()-timedelta(days=31)))
    assert h.client(e).get('/api/v1/messages/'+ref+'/trace').status_code==410

def test_automatic_retention_scrubs_raw_only_after_final_and_keeps_ledger(env):
    e=env; gid=h.helpers.add_guess(e)
    with e.db.engine.begin() as c:
        pid=c.execute(sa.select(s.guesses.c.participant_id).where(s.guesses.c.id==gid)).scalar_one()
        c.execute(s.ledger.insert().values(id=e.prefix+'ledger',round_id=e.rid,participant_id=pid,reason='participation',reference_id=gid,delta=1))
    h.finish(e); h.age_owned_final(e,8)
    assert hasattr(e.svc,'maintenance_tick'), 'automatic bounded retention use case missing'
    e.svc.maintenance_tick()
    with e.db.engine.connect() as c:
        guess=c.execute(sa.select(s.guesses).where(s.guesses.c.id==gid)).mappings().one()
        participant=c.execute(sa.select(s.participants).where(s.participants.c.id==pid)).mappings().one()
        assert guess['normalized_word']!='same' and guess['status']=='scored'
        assert participant['identity_key']!=gid and participant['display_name']==''
        assert c.execute(sa.select(s.ledger.c.reference_id).where(s.ledger.c.id==e.prefix+'ledger')).scalar_one()==gid
    assert e.svc.snapshot(e.sid,e.actor)['points_net']==1

@pytest.mark.parametrize('budget',['MAX_ROWS','MAX_BYTES'])
def test_query_budget_pending_final_has_executable_bounded_recovery(env,monkeypatch,budget):
    e=env; h.helpers.add_guess(e)
    import app.modules.analytics.service as module
    original=getattr(module,budget)
    monkeypatch.setattr(module,budget,1)
    h.finish(e)
    assert h.persisted(e) is None
    assert hasattr(e.svc,'maintenance_tick'), 'final materialization has no recovery execution entry'
    e.svc.maintenance_tick()
    with e.db.engine.connect() as c:
        facts=c.execute(sa.select(m.session_facts.c.payload).where(m.session_facts.c.session_id==e.sid)).scalar_one()
        assert facts.get('sealed_at') and not facts.get('materialization_pending_reason')
        assert c.execute(sa.select(m.finals.c.session_id).where(m.finals.c.session_id==e.sid)).scalar_one()==e.sid
    monkeypatch.setattr(module,budget,original)
    assert e.svc.snapshot(e.sid,e.actor)['valid_distinct_guesses']==1

def test_pending_effect_protects_raw_and_finished_frame_deletes_with_watermark(env):
    e=env; gid=h.helpers.add_guess(e)
    with e.db.engine.begin() as c:
        c.execute(s.effects.insert().values(id=e.prefix+'effect',session_id=e.sid,round_id=e.rid,feedback_id=gid,delivery_id=e.prefix+'delivery',source_seq=1,payload={'word':'private-comment','display_name':'private-name'},created_at=moment()-timedelta(days=8)))
        c.execute(s.outbox.insert().values(id=e.prefix+'event',session_id=e.sid,audience='DISPLAY',seq=1,type='guess.feedback',payload={'word':'private-comment'},created_at=moment()-timedelta(days=8)))
        c.execute(s.sessions.update().where(s.sessions.c.id==e.sid).values(display_seq=1))
    h.finish(e); h.age_owned_final(e,8)
    e.svc.maintenance_tick()
    with e.db.engine.connect() as c:
        assert c.execute(sa.select(s.guesses.c.normalized_word).where(s.guesses.c.id==gid)).scalar_one()=='same'
        assert c.execute(sa.select(s.outbox.c.payload).where(s.outbox.c.id==e.prefix+'event')).scalar_one()=={'word':'private-comment'}
    with e.db.engine.begin() as c:
        c.execute(s.effects.update().where(s.effects.c.id==e.prefix+'effect').values(processed=True))
        c.execute(m.maintenance.delete().where(m.maintenance.c.session_id==e.sid))
    e.svc.maintenance_tick()
    with e.db.engine.connect() as c:
        assert c.execute(sa.select(s.outbox.c.id).where(s.outbox.c.id==e.prefix+'event')).first() is None
        row=c.execute(sa.text('SELECT through_seq,payload_hash FROM analytics_event_retention WHERE session_id=:sid AND audience=\'DISPLAY\''),{'sid':e.sid}).mappings().one()
        assert row['through_seq']==1 and row['payload_hash']
        assert c.execute(sa.select(s.effects.c.processed).where(s.effects.c.id==e.prefix+'effect')).scalar_one() is True
        assert 'private-comment' not in str(c.execute(sa.select(s.effects.c.payload).where(s.effects.c.id==e.prefix+'effect')).scalar_one())

def test_retention_30_and_90_days_do_not_delete_ledger_or_recreate_aggregates(env):
    e=env; gid=h.helpers.add_guess(e)
    with e.db.engine.begin() as c:
        c.execute(s.trace_steps.insert().values(id=e.prefix+'phase',message_id=gid,stage='feedback',state='committed',time=moment()-timedelta(days=31)))
    h.finish(e); h.age_owned_final(e,31)
    e.svc.maintenance_tick()
    with e.db.engine.connect() as c:
        assert c.execute(sa.select(m.traces.c.round_id).where(m.traces.c.session_id==e.sid)).first() is None
        assert c.execute(sa.select(s.trace_steps.c.id).where(s.trace_steps.c.id==e.prefix+'phase')).first() is None
        assert c.execute(sa.select(m.finals.c.session_id).where(m.finals.c.session_id==e.sid)).scalar_one()==e.sid
        ref=c.execute(sa.select(m.round_facts.c.payload).where(m.round_facts.c.round_id==e.rid)).scalar_one()['trace_ref']
    assert h.client(e).get('/api/v1/messages/'+ref+'/trace').status_code==410
    with e.db.engine.begin() as c:
        c.execute(m.finals.update().where(m.finals.c.session_id==e.sid).values(expires_at=moment()-timedelta(seconds=1)))
        c.execute(m.maintenance.delete().where(m.maintenance.c.session_id==e.sid))
    e.svc.maintenance_tick()
    with e.db.engine.connect() as c:
        assert c.execute(sa.select(m.finals.c.session_id).where(m.finals.c.session_id==e.sid)).first() is None
        assert c.execute(sa.select(s.guesses.c.status).where(s.guesses.c.id==gid)).scalar_one()=='scored'
    assert h.client(e).get(f'/api/v1/live-sessions/{e.sid}/analytics').status_code==410

def test_installation_snapshot_257th_evicts_and_busy_pin_is_atomic(env):
    e=env; snap=e.svc.snapshot(e.sid,e.actor)
    with e.db.engine.begin() as c:
        row=dict(c.execute(sa.select(m.snapshots).where(m.snapshots.c.id==snap['snapshot_id'])).mappings().one())
        # Separate actor/session scopes isolate the installation quota from 16/scope.
        actors=[]; sessions=[]; copies=[]
        for i in range(255):
            actor=e.prefix+'quotaactor'+str(i); sid=e.prefix+'quotasession'+str(i)
            actors.append(dict(id=actor,username=actor,password_hash='fixture',display_name='',permissions=[]))
            sessions.append(dict(id=sid,actor_id=actor,label='Quota fixture',mode='rehearsal',run_purpose='local',status='CREATED'))
            copies.append(row|dict(id=e.prefix+'quota'+str(i),actor_id=actor,session_id=sid))
        c.execute(s.operators.insert(),actors); c.execute(s.sessions.insert(),sessions); c.execute(m.snapshots.insert(),copies)
        with c.begin_nested(): e.svc.reserve(c,e.actor,e.sid,1,moment())
        assert c.execute(sa.select(sa.func.count()).select_from(m.snapshots).where(m.snapshots.c.payload.is_not(None))).scalar_one()==255
        # All surviving 255 have pins and the remaining 4MiB reserve exceeds the exact byte cap.
        import app.modules.analytics.service as module
        for_quota=255*4*1024*1024
        c.execute(m.snapshots.update().where(m.snapshots.c.payload.is_not(None)).values(size_bytes=4*1024*1024,pin_count=1,pin_until=moment()+timedelta(minutes=4)))
        assert for_quota<module.MAX_TOTAL_BYTES
        with pytest.raises(Exception) as error: e.svc.reserve(c,e.actor,e.sid,4*1024*1024+1,moment())
        assert error.value.code=='ANALYTICS_CAPACITY_BUSY'
        # Clean fixture-only synthetic identities in FK order.
        c.execute(s.sessions.delete().where(s.sessions.c.id.in_([x['id'] for x in sessions])))
        c.execute(s.operators.delete().where(s.operators.c.id.in_([x['id'] for x in actors])))

def test_csv_actor_one_installation_two_and_context_release(env):
    e=env
    from app.errors import DomainError
    with e.svc.export_slot(e.actor):
        with pytest.raises(DomainError) as same: 
            with e.svc.export_slot(e.actor): pass
        assert same.value.code=='ANALYTICS_EXPORT_LIMITED'
        other=e.prefix+'other-exporter'
        with e.db.engine.begin() as c: c.execute(s.operators.insert().values(id=other,username=other,password_hash='fixture',display_name='',permissions=[]))
        try:
            with e.svc.export_slot(other):
                with pytest.raises(DomainError) as third:
                    with e.svc.export_slot(e.actor+'not-needed'): pass
                assert third.value.code=='ANALYTICS_EXPORT_LIMITED'
        finally:
            with e.db.engine.begin() as c: c.execute(s.operators.delete().where(s.operators.c.id==other))
    with e.db.engine.connect() as c: assert c.execute(sa.select(sa.func.count()).select_from(m.exports)).scalar_one()==0

def test_pin_never_extends_beyond_first_five_minutes(env):
    e=env; payload=e.svc.snapshot(e.sid,e.actor); ident=payload['snapshot_id']
    with e.svc.pinned(e.sid,e.actor,ident):
        with e.db.engine.connect() as c:
            first=c.execute(sa.select(m.snapshots).where(m.snapshots.c.id==ident)).mappings().one()
            assert first['pin_until']-first['pin_first']==timedelta(minutes=5)
        with e.svc.pinned(e.sid,e.actor,ident):
            with e.db.engine.connect() as c:
                second=c.execute(sa.select(m.snapshots).where(m.snapshots.c.id==ident)).mappings().one()
                assert second['pin_first']==first['pin_first'] and second['pin_until']==first['pin_until'] and second['pin_count']==2
    with e.db.engine.connect() as c: assert c.execute(sa.select(m.snapshots.c.pin_count).where(m.snapshots.c.id==ident)).scalar_one()==0

@pytest.mark.parametrize('budget',['rows','bytes'])
def test_real_over_budget_anonymous_final_is_recovered_in_bounded_rows(env,budget):
    e=env; h.helpers.add_guess(e)
    amount=20000 if budget=='rows' else 2500
    with e.db.engine.begin() as c:
        c.execute(sa.text("""INSERT INTO rounds(id,session_id,puzzle_version_id,status,short_code,boot_id,end_reason,frozen_puzzle)
          SELECT :prefix||'bulkround'||n,:sid,:pid,'REVEALED',:prefix||'code'||n,'fixture','TIMEOUT','{}'::jsonb
          FROM generate_series(1,:amount) n"""),{'prefix':e.prefix,'sid':e.sid,'pid':e.pid,'amount':amount})
        if budget=='bytes':
            c.execute(sa.text("""INSERT INTO analytics_round_facts(round_id,session_id,payload)
              SELECT id,session_id,jsonb_build_object('anomaly_codes',jsonb_build_array(repeat('X',1500)))
              FROM rounds WHERE session_id=:sid ON CONFLICT(round_id) DO NOTHING"""),{'sid':e.sid})
        facts=c.execute(sa.select(m.session_facts.c.payload).where(m.session_facts.c.session_id==e.sid)).scalar_one()
        facts.update(ended_at=moment().isoformat(),materialization_basis_at=moment().isoformat(),materialization_watermark=1,materialization_pending_reason='FINAL_AGGREGATE_TOO_LARGE')
        c.execute(m.session_facts.update().where(m.session_facts.c.session_id==e.sid).values(payload=facts))
        c.execute(s.sessions.update().where(s.sessions.c.id==e.sid).values(status='ENDED'))
    e.svc.maintenance_tick()
    with e.db.engine.connect() as c:
        final=c.execute(sa.select(m.finals.c.payload).where(m.finals.c.session_id==e.sid)).scalar_one()
        assert final['final'] and final['round_counts']['total']==amount+1
        assert c.execute(sa.select(sa.func.count()).select_from(m.final_rows).where(m.final_rows.c.session_id==e.sid,m.final_rows.c.section=='rounds')).scalar_one()==amount+1
        assert final['_row_count']>20000 if budget=='rows' else final['_row_bytes']>4*1024*1024
    from app.errors import DomainError
    with pytest.raises(DomainError) as error: e.svc.snapshot(e.sid,e.actor)
    assert error.value.code=='ANALYTICS_SNAPSHOT_TOO_LARGE'

def test_legacy_trace_ref_remains_stable_across_seal(env):
    e=env; h.helpers.add_guess(e)
    with e.db.engine.begin() as c: c.execute(m.round_facts.delete().where(m.round_facts.c.session_id==e.sid))
    before=e.svc.snapshot(e.sid,e.actor)['_rounds'][0]['trace_ref']
    h.finish(e)
    with e.db.engine.connect() as c:
        after=c.execute(sa.select(m.finals.c.payload).where(m.finals.c.session_id==e.sid)).scalar_one()['_rounds'][0]['trace_ref']
    assert before==after, 'sealing replaced an already issued legacy opaque ref'

def test_unfinished_raw_without_anonymous_final_is_not_scrubbed(env):
    e=env; gid=h.helpers.add_guess(e,'queued')
    with e.db.engine.begin() as c: c.execute(s.guesses.update().where(s.guesses.c.id==gid).values(created_at=moment()-timedelta(days=8)))
    h.finish(e); e.svc.maintenance_tick()
    with e.db.engine.connect() as c:
        assert c.execute(sa.select(s.guesses.c.normalized_word).where(s.guesses.c.id==gid)).scalar_one()=='same'
        assert c.execute(sa.select(m.finals.c.session_id).where(m.finals.c.session_id==e.sid)).first() is None

def test_connector_retention_hook_runs_only_with_final_in_existing_transaction(env):
    e=env; gid=h.helpers.add_guess(e); seen=[]
    def hook(c,sid,cutoff):
        seen.append((sid,c.in_transaction(),cutoff))
    e.svc.connector_retention=hook
    e.svc.maintenance_tick(); assert seen==[]
    h.finish(e); h.age_owned_final(e,8)
    with e.db.engine.begin() as c: c.execute(m.maintenance.delete().where(m.maintenance.c.session_id==e.sid))
    e.svc.maintenance_tick()
    assert len(seen)==1 and seen[0][0]==e.sid and seen[0][1] is True
    assert abs((moment()-seen[0][2]).total_seconds()-7*86400)<2

def test_late_delivery_touch_cannot_recreate_expired_business_aggregate(env):
    e=env; h.helpers.add_guess(e); h.finish(e); h.age_owned_final(e,91)
    e.svc.maintenance_tick()
    with e.db.engine.begin() as c:
        c.execute(sa.select(s.sessions.c.id).where(s.sessions.c.id==e.sid).with_for_update())
        c.execute(s.sessions.update().where(s.sessions.c.id==e.sid).values(analytics_commit_counter=s.sessions.c.analytics_commit_counter+1))
        e.svc.persist_sealed(c,e.sid)
        assert c.execute(sa.select(m.finals.c.session_id).where(m.finals.c.session_id==e.sid)).first() is None

def test_csv_preflight_rechecks_revoked_permission_before_attachment(env,monkeypatch):
    e=env; snap=e.svc.snapshot(e.sid,e.actor); created=[]
    original=e.svc.csv_file
    def preflight(payload):
        path=original(payload); created.append(path)
        with e.db.engine.begin() as c: c.execute(s.operators.update().where(s.operators.c.id==e.actor).values(permissions=[]))
        return path
    monkeypatch.setattr(e.svc,'csv_file',preflight)
    response=h.client(e).get(f'/api/v1/live-sessions/{e.sid}/analytics',params={'snapshot_id':snap['snapshot_id'],'format':'csv'})
    assert response.status_code==403
    assert created and all(not path.exists() for path in created)

def test_csv_preflight_expiry_is_410_and_releases_file_pin_slot(env,monkeypatch):
    import app.modules.analytics.router as router
    e=env; snap=e.svc.snapshot(e.sid,e.actor); created=[]; expired=[False]
    original=e.svc.csv_file; actual=router.moment
    monkeypatch.setattr(router,'moment',lambda:actual()+timedelta(hours=25) if expired[0] else actual())
    def preflight(payload):
        path=original(payload); created.append(path); expired[0]=True; return path
    monkeypatch.setattr(e.svc,'csv_file',preflight)
    response=h.client(e).get(f'/api/v1/live-sessions/{e.sid}/analytics',params={'snapshot_id':snap['snapshot_id'],'format':'csv'})
    assert response.status_code==410 and response.json()['error']['details']['reason']=='EXPIRED'
    assert created and all(not path.exists() for path in created)
    with e.db.engine.connect() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(m.exports)).scalar_one()==0
        assert c.execute(sa.select(m.snapshots.c.pin_count).where(m.snapshots.c.id==snap['snapshot_id'])).scalar_one()==0

def test_quota_reservation_does_not_load_materialized_payloads_into_memory(env):
    import tracemalloc
    from app.modules.analytics.service import json_bytes
    e=env; snap=e.svc.snapshot(e.sid,e.actor)
    payload=dict(snap,_rounds=[dict(snap['_rounds'][0],anomaly_codes=['X'*(1024*1024)])])
    with e.db.engine.begin() as c:
        row=dict(c.execute(sa.select(m.snapshots).where(m.snapshots.c.id==snap['snapshot_id'])).mappings().one())
        c.execute(m.snapshots.insert(),[row|dict(id=e.prefix+'materialized'+str(i),payload=payload,size_bytes=len(json_bytes(payload))) for i in range(15)])
        tracemalloc.start()
        try:
            e.svc.reserve(c,e.actor,e.sid,1024,moment())
            _,peak=tracemalloc.get_traced_memory()
        finally: tracemalloc.stop()
    assert peak<5*1024*1024, 'quota reservation loaded 15 MiB of payloads instead of bounded metadata'

def test_compressible_round_facts_are_preflighted_by_uncompressed_bytes(env):
    import tracemalloc
    from app.errors import DomainError
    e=env
    with e.db.engine.begin() as c:
        rf=c.execute(sa.select(m.round_facts.c.payload).where(m.round_facts.c.round_id==e.rid)).scalar_one()
        c.execute(m.round_facts.update().where(m.round_facts.c.round_id==e.rid).values(payload=rf|{'anomaly_codes':['X'*(8*1024*1024)]}))
    tracemalloc.start()
    try:
        with pytest.raises(DomainError) as error: e.svc.snapshot(e.sid,e.actor)
        _,peak=tracemalloc.get_traced_memory()
    finally: tracemalloc.stop()
    assert error.value.code=='ANALYTICS_SNAPSHOT_TOO_LARGE'
    assert peak<5*1024*1024, 'TOAST compression bypassed the raw JSON preflight budget'

def test_resource_failures_publish_explicit_reason(env):
    from app.errors import DomainError
    e=env
    with e.svc.export_slot(e.actor):
        with pytest.raises(DomainError) as error:
            with e.svc.export_slot(e.actor): pass
        assert error.value.details.get('reason')=='CONCURRENCY_LIMIT'

def test_trace_reads_trusted_local_stream_metadata_after_authorization(env):
    e=env; gid=h.helpers.add_guess(e)
    e.connector_trace_metadata=lambda c,message_id: {'stream_id':'trusted-stream','stream_seq':'12'} if message_id==gid else None
    response=h.client(e).get('/api/v1/messages/'+gid+'/trace')
    assert response.status_code==200
    assert response.json()['data']['stream_id']=='trusted-stream' and response.json()['data']['stream_seq']=='12'

def test_snapshot_reuse_requires_same_metric_definition_version(env):
    e=env; first=e.svc.snapshot(e.sid,e.actor)
    with e.db.engine.begin() as c:
        c.execute(m.snapshots.update().where(m.snapshots.c.id==first['snapshot_id']).values(payload=first|{'metric_definition_version':'older-version'},captured_at=moment()-timedelta(seconds=6),expires_at=moment()+timedelta(hours=23)))
    current=e.svc.snapshot(e.sid,e.actor)
    assert current['snapshot_id']!=first['snapshot_id'] and current['metric_definition_version']=='LW-BE-ANALYTICS/1.1.0'

def test_query_copy_does_not_extend_ninety_day_final_retention(env):
    from app.modules.analytics.service import stamp
    e=env; h.helpers.add_guess(e); h.finish(e); final=h.age_owned_final(e,89.5)
    snap=e.svc.snapshot(e.sid,e.actor)
    assert stamp(snap['expires_at'])<=final['expires_at'], '24h query copy extended the 90d business retention'

def test_participant_retention_uses_own_latest_input_in_long_session(env):
    """One participant spans day2/day9; another only spoke on day2."""
    e=env;old_gid=h.helpers.add_guess(e);expired_gid=h.helpers.add_guess(e)
    clock=moment();old=clock-timedelta(days=8);recent=clock-timedelta(days=1)
    with e.db.engine.begin() as c:
        old_person=c.execute(sa.select(s.guesses.c.participant_id).where(s.guesses.c.id==old_gid)).scalar_one()
        expired_person=c.execute(sa.select(s.guesses.c.participant_id).where(s.guesses.c.id==expired_gid)).scalar_one()
        before=dict(c.execute(sa.select(s.participants).where(s.participants.c.id==old_person)).mappings().one())
        c.execute(s.participants.update().where(s.participants.c.session_id==e.sid).values(created_at=clock-timedelta(days=9)))
        c.execute(s.guesses.update().where(s.guesses.c.session_id==e.sid).values(created_at=old))
        c.execute(s.guesses.update().where(s.guesses.c.id==old_gid).values(normalized_word='苹果'))
        c.execute(s.guesses.update().where(s.guesses.c.id==expired_gid).values(normalized_word='橘子'))
        c.execute(s.rounds.update().where(s.rounds.c.id==e.rid).values(created_at=old))
        e.svc.observe(c,'session_started',e.sid,{'started_at':(clock-timedelta(days=10)).isoformat()})
        e.svc.observe(c,'round_settled',e.sid,{'round_id':e.rid,'started_at':old.isoformat(),'ended_at':(old+timedelta(seconds=100)).isoformat()})
        rid=e.prefix+'recent-round';gid=e.prefix+'recent-message'
        c.execute(s.rounds.insert().values(id=rid,session_id=e.sid,puzzle_version_id=e.pid,status='REVEALED',end_reason='TIMEOUT',short_code=e.prefix+'recent',boot_id='test',created_at=recent,frozen_puzzle={'category_id':'fixture','difficulty':2}))
        e.svc.observe(c,'round_started',e.sid,{'round_id':rid,'started_at':recent.isoformat(),'mono_ns':1,'boot_id':'test'})
        e.svc.observe(c,'round_settled',e.sid,{'round_id':rid,'ended_at':(recent+timedelta(seconds=100)).isoformat(),'mono_ns':100000000001,'boot_id':'test'})
        c.execute(s.guesses.insert().values(id=gid,session_id=e.sid,round_id=rid,participant_id=old_person,source_mode='manual',source_id=gid,normalized_word='西瓜',status='scored',solved=False,boot_id='test',created_at=recent))
        c.execute(s.sessions.update().where(s.sessions.c.id==e.sid).values(status='ENDED'))
        e.svc.observe(c,'session_ended',e.sid,{'ended_at':(recent+timedelta(seconds=101)).isoformat()})
        c.execute(s.sessions.update().where(s.sessions.c.id==e.sid).values(analytics_commit_counter=s.sessions.c.analytics_commit_counter+1))
        e.svc.persist_sealed(c,e.sid)
    assert h.persisted(e) is not None
    e.svc.maintenance_tick()
    with e.db.engine.connect() as c:
        kept=c.execute(sa.select(s.participants).where(s.participants.c.id==old_person)).mappings().one()
        expired=c.execute(sa.select(s.participants).where(s.participants.c.id==expired_person)).mappings().one()
        assert kept['identity_key']==before['identity_key'] and kept['display_name']==before['display_name']
        assert expired['identity_key'].startswith('retained:') and expired['display_name']==''
        assert c.execute(sa.select(s.guesses.c.normalized_word).where(s.guesses.c.id==old_gid)).scalar_one().startswith('retained:')
        assert c.execute(sa.select(s.guesses.c.normalized_word).where(s.guesses.c.id==gid)).scalar_one()=='西瓜'
    page=h.client(e).get('/api/v1/messages',params={'session_id':e.sid}).json()['data']
    assert page['items'][0]['display_name']==before['display_name'] and page['items'][0]['normalized_word']=='西瓜'

def test_real_rejection_feedback_cache_expires_without_touching_wordbank_cache(env):
    e=env
    from app.modules.sessions.service import LocalService
    with e.db.engine.begin() as c:c.execute(s.operators.update().where(s.operators.c.id==e.actor).values(permissions=['session.control','diagnostics.read']))
    core=LocalService(e.db);core.analytics=e.svc
    response=core.submit_guess(e.actor,e.prefix+'reject-key',{'session_id':e.sid,'round_id':e.rid,'short_code':'AB','word':'苹果','display_name':'old rejected nickname'})
    assert response['status']=='round_closed' and response['word'] is None and response['display_name']=='old rejected nickname'
    rid=response['feedback_id'];h.finish(e);h.age_owned_final(e,8)
    with e.db.engine.begin() as c:
        c.execute(s.rejections.update().where(s.rejections.c.id==rid).values(created_at=moment()-timedelta(days=8)))
        c.execute(s.idempotency.update().where(s.idempotency.c.response['feedback_id'].astext==rid).values(created_at=moment()-timedelta(days=8)))
        c.execute(s.idempotency.insert().values(id=e.prefix+'wordbank-cache',actor_id=e.actor,method='POST',path='/api/v1/puzzles/fixture/commands',key=e.prefix+'wordbank-key',request_hash='unchanged',created_at=moment()-timedelta(days=8),response={'command_id':'wordbank-result','feedback_id':rid,'word':'draft word','display_name':'draft label'}))
    e.svc.maintenance_tick()
    with e.db.engine.connect() as c:
        cached=c.execute(sa.select(s.idempotency).where(s.idempotency.c.key==e.prefix+'reject-key')).mappings().one()
        assert cached['response']['word'] is None and cached['response']['display_name']==''
        assert cached['response']['feedback_id']==rid and cached['response']['status']=='round_closed'
        other=c.execute(sa.select(s.idempotency.c.response).where(s.idempotency.c.id==e.prefix+'wordbank-cache')).scalar_one()
        assert other['word']=='draft word' and other['display_name']=='draft label'


def test_internal_rehearsal_bridge_feedback_retention_and_recorded_source(env):
    e=env
    from app.modules.sessions.service import LocalService
    with e.db.engine.begin() as c:c.execute(s.operators.update().where(s.operators.c.id==e.actor).values(permissions=['session.control','diagnostics.read']))
    core=LocalService(e.db);core.analytics=e.svc
    permit=core.rehearsal_input_issuer()('private-synthetic-viewer','Synthetic source nickname')
    response=core.admit_rehearsal_input(e.actor,e.prefix+'internal-cache',{'session_id':e.sid,'round_id':e.rid,'short_code':'AB','word':'苹果'},permit)
    assert response['status']=='round_closed'
    items=h.client(e).get('/api/v1/messages',params={'session_id':e.sid}).json()['data']['items']
    assert next(x for x in items if x['message_id']==response['feedback_id'])['source_mode']=='bridge'
    h.finish(e);h.age_owned_final(e,8)
    with e.db.engine.begin() as c:
        c.execute(s.rejections.update().where(s.rejections.c.id==response['feedback_id']).values(created_at=moment()-timedelta(days=8)))
        c.execute(s.idempotency.update().where(s.idempotency.c.response['feedback_id'].astext==response['feedback_id']).values(created_at=moment()-timedelta(days=8)))
    e.svc.maintenance_tick()
    with e.db.engine.connect() as c:
        cached=c.execute(sa.select(s.idempotency).where(s.idempotency.c.key==e.prefix+'internal-cache')).mappings().one()
        assert cached['path']=='/internal/rehearsal-input' and cached['method']=='POST'
        assert cached['response']['display_name']=='' and cached['response']['word'] is None
        assert cached['response']['feedback_id']==response['feedback_id'] and cached['request_hash']
