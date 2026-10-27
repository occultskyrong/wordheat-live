import importlib.util
from pathlib import Path
from datetime import datetime, timezone, timedelta

def service():
    path=Path(__file__).parents[2]/'app/modules/analytics/service.py'
    assert path.exists(), 'Analytics calculation implementation missing'
    spec=importlib.util.spec_from_file_location('analytics_service',path)
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod

def test_units_void_pending_interrupted_and_real_denominators():
    mod=service(); now=datetime(2026,10,25,tzinfo=timezone.utc)
    rounds=[dict(id='a',status='REVEALED',end_reason='SOLVED',puzzle_version_id='p',frozen_puzzle={}),dict(id='b',status='REVEALED',end_reason='INTERRUPTED',puzzle_version_id='p',frozen_puzzle={}),dict(id='v',status='REVEALED',end_reason='VOID',puzzle_version_id='p',frozen_puzzle={})]
    guesses=[dict(id=str(i),round_id=r,participant_id=p,status=status,source_mode='manual',created_at=now+timedelta(seconds=i*40),normalized_word='same') for i,(r,p,status) in enumerate([('a','one','scored'),('a','two','scored'),('b','one','queued'),('v','one','scored')])]
    result=mod.calculate(dict(id='s',mode='rehearsal',run_purpose='local',status='ACTIVE',analytics_commit_counter=7,studio_seq=2),rounds,guesses,[],[],[],dict(started_at=now),{},now+timedelta(seconds=180))
    assert result['participants_count']==2
    assert result['valid_distinct_guesses']==2
    assert result['round_completion_rate']==0.5
    assert result['attempt_outcomes']['nonterminal']==1
    assert result['attempt_outcomes']['void_excluded']==1
    assert result['median_round_duration_ms'] is None
    assert result['pipeline_latency_p50_ms'] is None
    assert result['coverage']['relay_coverage']=='unknown'
    assert sum(x['valid_distinct_guesses'] for x in result['_minutes'])==2
    assert result['_rounds'][2]['exclusion_reason']=='VOID'

def test_cursor_context_and_tampering():
    mod=service(); signer=mod.CursorSigner(b'x'*32)
    token=signer.encode('snap','session','rounds',50,[2,'b'])
    assert signer.decode(token,'snap','session','rounds',50)==[2,'b']
    for args in [('snap','other','rounds',50),('snap','session','minutes',50),('snap','session','rounds',51)]:
        try: signer.decode(token,*args)
        except mod.DomainError as e: assert e.code=='ANALYTICS_CURSOR_INVALID'
        else: assert False
    try: signer.decode(token+'x','snap','session','rounds',50)
    except mod.DomainError as e: assert e.status_code==422
    else: assert False

def test_no_samples_have_null_rates():
    mod=service(); now=datetime.now(timezone.utc)
    result=mod.calculate(dict(id='s',mode='rehearsal',run_purpose='local',status='CREATED',analytics_commit_counter=0,studio_seq=0),[],[],[],[],[],{}, {},now)
    assert result['participants_count']==0
    assert result['_minutes']==[]
    assert all(result[x] is None for x in ['guesses_per_participant','round_completion_rate','hint_rate','invalid_ratio','median_round_duration_ms','pipeline_latency_p50_ms'])

def test_router_is_implemented():
    path=Path(__file__).parents[2]/'app/modules/analytics/router.py'
    assert path.exists(), 'Analytics HTTP adapter missing'

def test_csv_budget_formula_and_private_file():
    mod=service(); now=datetime.now(timezone.utc)
    payload=mod.calculate(dict(id='s',mode='rehearsal',run_purpose='local',status='CREATED',analytics_commit_counter=0,studio_seq=0),[],[],[],[],[],{}, {},now)
    payload.update(snapshot_id='snapshot',expires_at='2026-10-26T00:00:00Z',source_partitions=[dict(source_mode='=danger()')])
    path=mod.AnalyticsService.csv_file(payload)
    try:
        assert path.stat().st_mode&0o077==0
        assert "'=danger()" in path.read_text()
    finally: path.unlink()
    mod.MAX_EXPORT_BYTES=10
    try: mod.AnalyticsService.csv_file(payload)
    except mod.DomainError as e: assert e.status_code==413
    else: assert False

def test_production_does_not_count_manual_unknown_or_provider_tests():
    mod=service(); now=datetime.now(timezone.utc)
    rnd=dict(id='r',status='REVEALED',end_reason='TIMEOUT',puzzle_version_id='p',frozen_puzzle={})
    guesses=[dict(id=str(i),round_id='r',participant_id=str(i),status='scored',source_mode=source,provider_test=test,created_at=now,normalized_word='same') for i,(source,test) in enumerate([('manual',False),('official',True),('official',None),('official',False)])]
    result=mod.calculate(dict(id='s',mode='official',run_purpose='production',status='ACTIVE',analytics_commit_counter=7,studio_seq=2),[rnd],guesses,[],[],[],{}, {},now)
    assert result['valid_distinct_guesses']==1
    assert result['participants_count']==1
    assert sum(p['eligible_for_production'] for p in result['source_partitions'])==1
    assert result['source_mode_counts']=={'manual':1,'official':3}

def test_csv_columns_are_fixed_across_different_sources():
    mod=service(); now=datetime.now(timezone.utc)
    payload=mod.calculate(dict(id='s',mode='rehearsal',run_purpose='local',status='CREATED',analytics_commit_counter=0,studio_seq=0),[],[],[],[],[],{}, {},now)
    payload.update(snapshot_id='snapshot',expires_at='2026-10-26T00:00:00Z')
    a=mod.AnalyticsService.csv_file(payload)
    b=mod.AnalyticsService.csv_file(payload|{'source_partitions':[dict(source_mode='manual',provider_test=False,messages=1)]})
    try: assert a.read_text().splitlines()[0]==b.read_text().splitlines()[0]
    finally: a.unlink(); b.unlink()

def test_new_sealed_snapshot_refreshes_diagnostics_without_mutating_baseline():
    mod=service(); captured=datetime(2026,10,25,12,tzinfo=timezone.utc)
    baseline=dict(participants_count=1,points_net=20,final=True,metric_definition_version=mod.VERSION,
        coverage={'through_at':'2026-10-25T11:00:00Z'},
        as_of={'captured_at':'2026-10-25T11:00:00Z','local_commit_watermark':'9','studio_as_of_seq':'7'},
        closure={'state':'sealed','sealed_at':'2026-10-25T11:00:00Z','local_delivery_pending':{'outbox':0,'display_effects':1,'ack_outbox':0}},
        _minutes=[{'valid_distinct_guesses':1}],_rounds=[{'points_net':20}])
    session={'analytics_commit_counter':10,'studio_seq':8}
    result=mod.AnalyticsService.refresh_final(baseline,session,captured,{'outbox':0,'display_effects':0,'ack_outbox':0})
    assert result['as_of']=={'captured_at':mod.iso(captured),'local_commit_watermark':'10','studio_as_of_seq':'8'}
    assert result['closure']['local_delivery_pending']['display_effects']==0
    assert baseline['closure']['local_delivery_pending']['display_effects']==1
    assert result['coverage']==baseline['coverage']
    assert result['_minutes']==baseline['_minutes'] and result['_rounds']==baseline['_rounds']
    assert result['closure']['sealed_at']==baseline['closure']['sealed_at']
