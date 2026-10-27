import pytest
from app.modules.scoring.runtime import ScorerRuntime
from app.modules.wordbank.models import PuzzleCommand
from pydantic import TypeAdapter


def test_runtime_exposes_frozen_identity():
    runtime=ScorerRuntime(b'x'*32,'model-1','a'*64,768)
    assert runtime.manifest_id=='model-1'
    assert runtime.manifest_sha256=='a'*64
    assert runtime.dimension==768


def test_original_review_and_publish_command_shapes():
    adapter=TypeAdapter(PuzzleCommand)
    review=adapter.validate_python({'command':'approve','expected_version':2,'args':{'evaluation_id':'e','decision':'reject','notes':'review'}})
    assert review.args.decision=='reject'
    published=adapter.validate_python({'command':'publish','expected_version':2,'args':{'approved_version':2}})
    assert published.args.approved_version==2

def test_replay_preserves_opaque_participant_identity():
    from app.modules.replays.models import ReplayCreate
    row=ReplayCreate.model_validate({'label':'replay','events':[{'source_id':'a','offset_ms':1,'guess':'香蕉','participant_key':'p1','display_name':'甲'}]})
    assert row.events[0].participant_key=='p1'


def test_score_rejects_changed_manifest_before_model_call():
    import asyncio
    from app.modules.scoring.client import ScorerClient,ScorerFailure
    client=ScorerClient(b'x'*32,'m','a'*64,2)
    try:
        with pytest.raises(ScorerFailure) as error:
            asyncio.run(client.score('香蕉',{'manifest_id':'m','manifest_sha256':'b'*64,'dimension':2,'normalization':'unit-l2/1','calibration':{'b':0,'u':1,'gamma':1},'target_vector':[1,0]}))
        assert error.value.code=='SCORER_FROZEN_PUZZLE_INVALID'
    finally: asyncio.run(client.aclose())

def test_global_admission_token_bucket_covers_burst_and_recovery():
    from app.modules.inbox.limiter import GlobalAdmissionLimiter
    clock=[0.]
    limiter=GlobalAdmissionLimiter(rate=100,burst=200,clock=lambda:clock[0])
    assert all(limiter.allow() for _ in range(200))
    assert not limiter.allow()
    clock[0]=.01
    assert limiter.allow()
    assert not limiter.allow()

def test_runtime_pair_cache_coalesces_across_threads_and_versions():
    import asyncio,threading
    from concurrent.futures import ThreadPoolExecutor
    calls=[]; lock=threading.Lock()
    class Client:
        def __init__(self,*a,**k): pass
        async def score(self,*a):
            with lock: calls.append(a)
            await asyncio.sleep(.05)
            return 80.
        async def aclose(self): pass
    runtime=ScorerRuntime(b'x'*32,'m','a'*64,2,client_factory=Client)
    frozen={'manifest_id':'m','calibration_id':'c','puzzle_version_id':'p','target_vector':[1.,0.]}
    with ThreadPoolExecutor(2) as pool:
        values=list(pool.map(lambda _:asyncio.run(runtime.score('香蕉',frozen)),range(2)))
    assert values==[80.,80.] and len(calls)==1
    assert asyncio.run(runtime.score('香蕉',frozen))==80. and len(calls)==1
    asyncio.run(runtime.score('香蕉',frozen|{'calibration_id':'next'}))
    assert len(calls)==2

def test_quality_tool_freezes_two_reviewers_and_hides_blind_scores(tmp_path):
    import subprocess,sys,json
    from pathlib import Path
    tool=Path(__file__).resolve().parents[4]/'packages/evaluation/20261026-quality-evaluation.py'
    source={'schema_version':1,'stage':'direction','manifest':{'manifest_id':'m','manifest_sha256':'a'*64,'wordbank_manifest_id':'w','calibration_id':'c'},'source_ref':'synthetic test only','pairs':[{'puzzle_version_id':'p','guess':'香蕉','split':'blind','annotations':[{'reviewer_id':'one','rating':'warm'},{'reviewer_id':'two','rating':'warm'}]}]}
    incoming=tmp_path/'input.json'; frozen=tmp_path/'frozen.json'; incoming.write_text(json.dumps(source))
    result=subprocess.run([sys.executable,str(tool),'freeze',str(incoming),str(frozen)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    document=json.loads(frozen.read_text())
    assert document['annotation_locked'] is True and document['complete'] is False
    assert 'raw_similarity' not in str(document)
    assert subprocess.run([sys.executable,str(tool),'freeze',str(incoming),str(frozen)],capture_output=True).returncode!=0

def test_quality_report_computes_blind_metrics_and_cannot_rewrite_annotations():
    import runpy
    from pathlib import Path
    tool=runpy.run_path(str(Path(__file__).resolve().parents[4]/'packages/evaluation/20261026-quality-evaluation.py'))
    pairs=[];scores=[]
    for split in ('calibration','blind'):
        for index,rating in enumerate(('cold','warm','hot')):
            guess=split+str(index)
            pairs.append({'puzzle_version_id':'p','guess':guess,'split':split,'annotations':[{'reviewer_id':'a','rating':rating},{'reviewer_id':'b','rating':rating}]})
            scores.append({'puzzle_version_id':'p','guess':guess,'raw_similarity':.1+index*.4,'display_score':index*40.})
    source={'schema_version':1,'stage':'direction','manifest':{'manifest_id':'m','manifest_sha256':'a'*64,'wordbank_manifest_id':'w','calibration_id':'c'},'source_ref':'synthetic metric regression','pairs':pairs}
    frozen=tool['freeze'](source);export={'manifest':frozen['manifest'],'annotation_hash':frozen['annotation_hash'],'producer':'synthetic unit fixture','pairs':scores}
    report=tool['report'](frozen,export)
    assert report['splits']['blind']['pairwise_accuracy']==1
    assert report['complete'] is False and report['gate_authority'] is False
    export['pairs'][0]['human_rating']='hot'
    with pytest.raises(ValueError):tool['report'](frozen,export)

def test_wordbank_and_inbox_share_unicode_han_normalization():
    from app.modules.wordbank.models import Draft
    from app.modules.inbox.normalization import normalize_word
    word='𠀀𠀁'
    row=Draft.model_validate({'word':word,'aliases':['𠀂𠀃'],'category_id':'unicode','difficulty':1,'hints':[],'explanation':'执行校验','source_ref':'synthetic unicode fixture'})
    assert row.word==normalize_word(word) and row.aliases==['𠀂𠀃']

def test_blind_evaluation_can_have_cold_and_hot_without_warm_group():
    import asyncio
    from app.modules.wordbank.evaluation import evaluate
    class Embeddings:
        manifest_id='m';manifest_sha256='a'*64;dimension=2
        async def embeddings(self,texts):return [[1.,0.],[0.,1.],[.8,.6]]
    result=asyncio.run(evaluate(Embeddings(),{'word':'苹果','calibration':{'b':0.,'u':1.,'gamma':1.}},{'manifest_id':'m','split':'blind','pairs':[{'guess':'石头','human_rating':'cold'},{'guess':'苹果汁','human_rating':'hot'}]}))
    assert result['metrics']['pairwise_accuracy']==1 and 'warm' not in result['metrics']['group_means']

def test_delivery_retry_connector_argument_is_strict_and_has_no_arbitrary_target():
    from app.main import ConnectorArgs
    from pydantic import ValidationError
    assert ConnectorArgs.model_validate({'session_id':'s','retry_failed_deliveries':True}).retry_failed_deliveries is True
    assert ConnectorArgs.model_validate({'session_id':'s'}).retry_failed_deliveries is False
    for extra in ({'retry_failed_deliveries':1},{'relay_url':'https://untrusted.invalid'},{'rootkey':'not-accepted'}):
        with pytest.raises(ValidationError):ConnectorArgs.model_validate({'session_id':'s'}|extra)
