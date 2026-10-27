"""Actual offline encoder + IPC dev bootstrap + Runtime + isolated PG + HTTP.

Owned lexical fixtures exercise execution, not human G1-Q qualification.
"""
import asyncio,importlib.util,os,secrets,subprocess,time
from pathlib import Path
import sqlalchemy as sa
import tempfile
import pytest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_local_flow import environment




def process_usage(pid):
    """Resident KiB and lifetime-average CPU percent for one real process.

    `ps` is the portable path. Hosts that deny process enumeration to `ps`/`top`
    still expose the same kernel counters through libproc; the fallback reads
    those counters directly so the resource evidence is not silently skipped.
    """
    try:
        run=subprocess.run(['ps','-o','pid=,rss=,pcpu=','-p',str(pid)],capture_output=True,text=True,check=True)
        _,rss,cpu=run.stdout.split()
        return int(rss),float(cpu)
    except (OSError,ValueError,subprocess.CalledProcessError):
        import ctypes,ctypes.util
        class TaskInfo(ctypes.Structure):
            _fields_=[('virtual_size',ctypes.c_uint64),('resident_size',ctypes.c_uint64),('total_user',ctypes.c_uint64),('total_system',ctypes.c_uint64),('threads_user',ctypes.c_uint64),('threads_system',ctypes.c_uint64),*[(name,ctypes.c_int32) for name in ('policy','faults','pageins','cow_faults','messages_sent','messages_received','syscalls_mach','syscalls_unix','csw','threadnum','numrunning','priority')]]
        class BsdInfo(ctypes.Structure):
            _fields_=[('flags',ctypes.c_uint32),('status',ctypes.c_uint32),('xstatus',ctypes.c_uint32),('pid',ctypes.c_uint32),('ppid',ctypes.c_uint32),('uid',ctypes.c_uint32),('gid',ctypes.c_uint32),('ruid',ctypes.c_uint32),('rgid',ctypes.c_uint32),('svuid',ctypes.c_uint32),('svgid',ctypes.c_uint32),('comm',ctypes.c_char*16),('name',ctypes.c_char*32),('nfiles',ctypes.c_uint32),('pgid',ctypes.c_uint32),('pjobc',ctypes.c_uint32),('tdev',ctypes.c_uint32),('tpgid',ctypes.c_uint32),('nice',ctypes.c_int32),('rfu_1',ctypes.c_uint32),('start_sec',ctypes.c_uint64),('start_usec',ctypes.c_uint64)]
        library=ctypes.CDLL(ctypes.util.find_library('proc') or '/usr/lib/libproc.dylib',use_errno=True)
        class Timebase(ctypes.Structure):_fields_=[('numer',ctypes.c_uint32),('denom',ctypes.c_uint32)]
        base=Timebase()
        if ctypes.CDLL(ctypes.util.find_library('c')).mach_timebase_info(ctypes.byref(base)):raise RuntimeError('MACH_TIMEBASE_UNAVAILABLE')
        task=TaskInfo();info=BsdInfo()
        if library.proc_pidinfo(pid,4,0,ctypes.byref(task),ctypes.sizeof(task))!=ctypes.sizeof(task):raise RuntimeError('PROC_TASKINFO_UNAVAILABLE')
        if library.proc_pidinfo(pid,3,0,ctypes.byref(info),ctypes.sizeof(info))!=ctypes.sizeof(info):raise RuntimeError('PROC_BSDINFO_UNAVAILABLE')
        lifetime=max(1e-3,time.time()-(info.start_sec+info.start_usec/1e6))
        # pti_total_* are mach absolute ticks, not nanoseconds; same average-CPU meaning as ps pcpu.
        cpu_seconds=(task.total_user+task.total_system)*base.numer/base.denom/1e9
        return task.resident_size//1024,round(100.*cpu_seconds/lifetime,1)


def exercise_load(app,db,actor,operator,display,ws,sid,ds,ready,scorer_pid):
    from concurrent.futures import ThreadPoolExecutor
    from collections import Counter
    import traceback
    from datetime import datetime,timezone
    import json,threading,subprocess,math
    from app.infrastructure.db import schema as s
    from app.errors import DomainError
    core=app.state.service
    permits=[core.rehearsal_input_issuer()(f'capacity-{i:03d}',f'容量参与人{i}') for i in range(250)]
    guard=threading.Lock(); running=threading.Event();running.set()
    submitted={};admitted={};processed={}; errors=[];results=Counter();samples=[];futures=[]
    current={};latest=[0.0];inflight=[0];peak=[0];receiver_error=[];last_admitted=[0.0]
    started=time.monotonic();round_ids=set()
    import queue
    deliveries_queue=queue.Queue(maxsize=256)
    seen_deliveries=set();batch_sizes=[];protocol_frames=[0]
    def receive():
        try:
            for _ in range(1000000):
                frame=ws.receive_json();protocol_frames[0]+=1
                # A real primary display follows the authoritative round from the
                # stream instead of re-fetching the full snapshot twice a second.
                round_event=(frame.get('payload') or {}).get('round')
                if frame.get('kind')=='event' and isinstance(round_event,dict) and round_event.get('round_id') and 'short_code' in round_event:
                    with guard:
                        current.clear();current.update(round_event);round_ids.add(round_event['round_id'])
                for effect in frame.get('pending_effects',[])+frame.get('payload',{}).get('effects',[]):
                    for did in effect['delivery_ids']:
                        if did in seen_deliveries:continue
                        seen_deliveries.add(did)
                        deliveries_queue.put({'delivery_id':did,'feedback_id':effect['feedback_id'],'client_time':datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')},timeout=5)
                if not running.is_set():return
        except Exception as error:
            if running.is_set():receiver_error.append(type(error).__name__)
    def acknowledge():
        try:
            while running.is_set() or not deliveries_queue.empty():
                try:batch=[deliveries_queue.get(timeout=.05)]
                except queue.Empty:continue
                deadline=time.monotonic()+.02
                while len(batch)<128:
                    try:batch.append(deliveries_queue.get(timeout=max(.0001,deadline-time.monotonic())))
                    except queue.Empty:break
                    if time.monotonic()>=deadline:break
                batch_sizes.append(len(batch))
                for stage in ('received','processed'):
                    receipt={'display_instance_id':ds['display_instance_id'],'render_generation':ready['render_generation'],'batch_id':'capacity-'+str(time.monotonic_ns())+'-'+stage,'stage':stage,'deliveries':batch}
                    reply=display.post('/api/v1/display/receipts',json=receipt)
                    if reply.status_code!=200:raise RuntimeError('RECEIPT_HTTP_'+str(reply.status_code))
                with guard:
                    for delivery in batch:processed.setdefault(delivery['feedback_id'],time.monotonic())
        except Exception as error:
            receiver_error.append(type(error).__name__)
    thread=threading.Thread(target=receive,name='owned-capacity-protocol-reader',daemon=True);thread.start()
    ack_thread=threading.Thread(target=acknowledge,name='owned-capacity-batched-receipts',daemon=True);ack_thread.start()
    def snapshot():
        reply=display.get('/api/v1/display/sessions/'+sid+'/snapshot')
        if reply.status_code!=200:raise RuntimeError('SNAPSHOT_HTTP_'+str(reply.status_code))
        value=reply.json()['data']['round']
        with guard:
            current.clear();current.update(value or {})
            if value:round_ids.add(value['round_id'])
        latest[0]=time.monotonic()
    def submit(number,body,phase,due):
        began=time.monotonic()
        try:
            result=core.admit_rehearsal_input(actor,'capacity-'+str(number),body,permits[number%250])
            status=result['status'];gid=result.get('feedback_id')
            with guard:
                results[phase+':'+status]+=1
                if status in {'queued','scored','hit'} and gid:
                    admitted[gid]={'submitted':began,'phase':phase,'admission_lag_ms':max(0,(began-due)*1000)};last_admitted[0]=time.monotonic()
        except DomainError as error:
            with guard:results[phase+':exception:'+error.code]+=1
        except Exception as error:
            with guard:errors.append({'type':type(error).__name__,'frames':[{'file':Path(f.filename).name,'function':f.name,'line':f.lineno} for f in traceback.extract_tb(error.__traceback__)]})
        finally:
            with guard:inflight[0]-=1
    def telemetry(phase):
        with db.engine.connect() as c:
            queue=c.execute(sa.select(sa.func.count()).select_from(s.score_jobs).join(s.guesses,s.guesses.c.id==s.score_jobs.c.guess_id).where(s.guesses.c.session_id==sid,s.score_jobs.c.status.in_(['queued','running','waiting']))).scalar_one()
            water=c.execute(sa.select(s.sessions.c.admission_seq).where(s.sessions.c.id==sid)).scalar_one()
        usage=[]
        for pid,role in ((os.getpid(),'api_and_driver'),(scorer_pid,'scorer')):
            rss,cpu=process_usage(pid);usage.append({'role':role,'rss_kib':rss,'cpu_percent':cpu})
        samples.append({'elapsed_seconds':round(time.monotonic()-started,3),'phase':phase,'score_jobs_pending':queue,'admission_watermark':str(water),'processes':usage})
        progress=Path(os.environ['WORDHEAT_LOAD_REPORT']).with_suffix('.progress.json')
        progress.write_text(json.dumps({'latest':samples[-1],'outcomes':dict(results),'processed':len(processed),'unexpected_errors':errors,'consumer_errors':receiver_error},ensure_ascii=False,indent=2))
    duration=float(os.environ.get('WORDHEAT_LOAD_SECONDS','1800'));sustained_rate=int(os.environ.get('WORDHEAT_LOAD_RATE','4'))
    phases=[('sustained',duration,sustained_rate),('burst',float(os.environ.get('WORDHEAT_LOAD_BURST_SECONDS','10')),100)]
    sequence=0;sent=Counter();next_sample=0;timings=[]
    snapshot()
    with ThreadPoolExecutor(max_workers=16,thread_name_prefix='owned-capacity-input') as pool:
        for phase,seconds,rate in phases:
            origin=time.monotonic();phase_real_start=origin;total=int(seconds*rate)
            for offset in range(total):
                due=origin+offset/rate;remaining=due-time.monotonic()
                if remaining>0:time.sleep(remaining)
                elif remaining<-.1:
                    # Never compress delayed input into a catch-up flood.
                    due=time.monotonic()
                    origin=due-offset/rate
                with guard:
                    track=dict(current)
                if not track:
                    # Only when the stream has not delivered a round yet.
                    snapshot()
                    with guard:track=dict(current)
                if time.monotonic()>=next_sample:
                    telemetry(phase);next_sample=time.monotonic()+5
                word='测'+chr(0x4e00+sequence//256)+chr(0x4e00+sequence%256)
                if sequence>=65536:raise AssertionError('Synthetic distinct-word range exhausted')
                body={'session_id':sid,'round_id':track['round_id'],'short_code':track['short_code'],'word':word}
                with guard:
                    if inflight[0]>=32:results[phase+':driver_capacity_not_submitted']+=1;continue
                    inflight[0]+=1;peak[0]=max(peak[0],inflight[0])
                futures.append(pool.submit(submit,sequence,body,phase,due));sent[phase]+=1;sequence+=1
            timings.append({'phase':phase,'scheduled_seconds':seconds,'actual_seconds':round(time.monotonic()-phase_real_start,3),'target_events_per_second':rate,'submitted':sent[phase]})
        for future in futures:future.result(timeout=10)
    # Bound wait; preserve real terminal failures rather than turning them into successful scores.
    cutoff=time.monotonic()+15
    while time.monotonic()<cutoff:
        with db.engine.connect() as c:
            remaining=c.execute(sa.select(sa.func.count()).select_from(s.score_jobs).join(s.guesses,s.guesses.c.id==s.score_jobs.c.guess_id).where(s.guesses.c.session_id==sid,s.score_jobs.c.status.in_(['queued','running','waiting']))).scalar_one()
        if not remaining:break
        time.sleep(.2)
    receipt_cutoff=time.monotonic()+15
    while time.monotonic()<receipt_cutoff:
        with guard:missing=len(set(admitted)-set(processed))
        if not missing:break
        time.sleep(.1)
    telemetry('drained')
    running.clear();ack_thread.join(timeout=5);thread.join(timeout=3)
    if ack_thread.is_alive():receiver_error.append('ACK_DRAIN_TIMEOUT')
    with db.engine.connect() as c:
        terminal_rows=list(c.execute(sa.select(s.guesses.c.id,s.guesses.c.status).where(s.guesses.c.session_id==sid)).all())
        counts=dict(Counter(status for gid,status in terminal_rows))
        jobs=dict(c.execute(sa.select(s.score_jobs.c.status,sa.func.count()).join(s.guesses,s.guesses.c.id==s.score_jobs.c.guess_id).where(s.guesses.c.session_id==sid).group_by(s.score_jobs.c.status)).all())
        reasons=dict(c.execute(sa.select(s.score_jobs.c.error_code,sa.func.count()).join(s.guesses,s.guesses.c.id==s.score_jobs.c.guess_id).where(s.guesses.c.session_id==sid,s.score_jobs.c.error_code.is_not(None)).group_by(s.score_jobs.c.error_code)).all())
        round_history=[dict(row) for row in c.execute(sa.select(s.rounds.c.id,s.rounds.c.status,s.rounds.c.end_reason,s.rounds.c.created_at,s.rounds.c.deadline_at).where(s.rounds.c.session_id==sid).order_by(s.rounds.c.created_at)).mappings()]
        boundary_history=[dict(row) for row in c.execute(sa.select(s.rejections.c.id,s.rejections.c.round_id,s.rejections.c.created_at).where(s.rejections.c.session_id==sid,s.rejections.c.reason_code=='ROUND_CLOSED')).mappings()]
        source_modes=[dict(row) for row in c.execute(sa.select(s.guesses.c.source_mode,s.guesses.c.provider_test,sa.func.count().label('count')).where(s.guesses.c.session_id==sid).group_by(s.guesses.c.source_mode,s.guesses.c.provider_test)).mappings()]
    def percentile(values,p):
        values=sorted(values)
        return round(values[max(0,math.ceil(len(values)*p)-1)],3) if values else None
    latencies={}
    for phase,_,_ in phases:
        values=[(processed[gid]-entry['submitted'])*1000 for gid,entry in admitted.items() if entry['phase']==phase and gid in processed]
        latencies[phase]={'processed_count':len(values),'p50_ms':percentile(values,.5),'p95_ms':percentile(values,.95),'p99_ms':percentile(values,.99),'accepted_count':sum(x['phase']==phase for x in admitted.values())}
    terminal_by_phase={}
    for phase,_,_ in phases:
        terminal_by_phase[phase]=dict(Counter(status for gid,status in terminal_rows if admitted.get(gid,{}).get('phase')==phase))
    report={'round_history':[{k:v.isoformat() if hasattr(v,'isoformat') else v for k,v in row.items()} for row in round_history],'closed_rejections':[{k:v.isoformat() if hasattr(v,'isoformat') else v for k,v in row.items()} for row in boundary_history],'scope':'Mac backend real offline BGE, real PostgreSQL, actual time and protocol consumer; all inputs synthetic Han strings/source_mode bridge/provider_test true; no frontend/OBS/official gate claim','phases':timings,'admission_outcomes':dict(results),'terminal_guess_states':counts,'terminal_states_by_phase':terminal_by_phase,'score_job_states':jobs,'score_failure_reasons':reasons,'source_partitions':source_modes,'feedback_latency':latencies,'resource_samples':samples,'peak_driver_inflight':peak[0],'rounds_observed':len(round_ids),'unexpected_errors':errors,'protocol_consumer_errors':receiver_error,'accepted_without_processed':len(set(admitted)-set(processed)),'scorer_cache':{'pair_limit':4096,'byte_limit':1048576},'pending_at_end':remaining,'session_id':sid,'protocol_frames_received':protocol_frames[0],'receipt_batch_sizes':batch_sizes,'consumer_queue_capacity':256,'max_receipt_batch':128,'batch_delay_ms':20}
    Path(os.environ['WORDHEAT_LOAD_REPORT']).write_text(json.dumps(report,ensure_ascii=False,indent=2))
    assert not errors and not receiver_error,report
    assert remaining==0,report
    assert not set(admitted)-set(processed),report
    assert sent['sustained']==int(duration*sustained_rate),report
    boundary=results['sustained:round_closed']
    sustained_admitted=sum(entry['phase']=='sustained' for entry in admitted.values())
    assert sustained_admitted>0 and sustained_admitted+boundary==sent['sustained'],report
    # A lost DISPLAY round event would turn the measured window into almost pure
    # round_closed traffic that the accounting above still satisfies; require the
    # stream to have delivered rounds and the run to have admitted its input.
    assert sustained_admitted>=0.9*sent['sustained'],report
    # Input must still have been admitted at the end of the timed phase; a run that
    # silently stopped accepting halfway through cannot qualify as sustained.
    assert last_admitted[0]>=started+timings[0]['actual_seconds']-60,report
    assert terminal_by_phase['sustained'].get('scored',0)==sustained_admitted,report
    assert latencies['sustained']['p95_ms'] is not None and latencies['sustained']['p95_ms']<=1500,report
    assert timings[0]['actual_seconds']<=duration+1,report
    assert not any(':exception:' in key and key!='burst:exception:RATE_LIMITED' and count for key,count in results.items()),report
    if duration>=1800:assert len(round_ids)>=3,report
    assert sum(counts.values())==len(admitted),report
    assert all(item['source_mode']=='bridge' and item['provider_test'] is True for item in source_modes),report
    return report


@pytest.mark.skipif(os.environ.get('WORDHEAT_RUN_CAPACITY')!='1',reason='Explicit real-time capacity run requires exclusive project test lock, dedicated DB and offline model')
def test_real_time_backend_capacity(environment):
    from fastapi.testclient import TestClient
    from app.application import create_app
    from app.modules.scoring.runtime import ScorerRuntime
    from app.infrastructure.db import schema as s
    db,actor=environment
    root=Path(__file__).resolve().parents[5]
    manifest=root/'resources/model-manifest.json'
    model_value=os.environ.get('WORDHEAT_TEST_MODEL_ROOT');python_value=os.environ.get('WORDHEAT_TEST_SCORER_PYTHON')
    if not model_value or not python_value:pytest.skip('Explicit owned real-model root and scorer interpreter required')
    model=Path(model_value)
    private=tempfile.TemporaryDirectory(prefix='wh-standard-real-',dir='/private/tmp');private_root=Path(private.name);private_root.chmod(0o700)
    helper=root/'scripts/backend/20261025-run-local.py'
    spec=importlib.util.spec_from_file_location('workflow_dev_bootstrap',helper);host=importlib.util.module_from_spec(spec);spec.loader.exec_module(host)
    identity,sha=host.manifest_identity(manifest); secret=secrets.token_bytes(32); owned=[]
    command=[str(root/'apps/scorer/app/runtime_entry.py'),'--source-root',str(root),'--config-root',str(root),'--manifest',str(manifest),'--model-root',str(model),'--fds']
    try:
        child,fd,boot=host.spawn_child('scorer',Path(python_value),root/'apps/scorer',command,secret,secrets.token_hex(32),sha,'127.0.0.1:8124',owned)
        host.await_ack(child,fd,boot,secret,120)
        runtime=ScorerRuntime(secret,identity['manifest_id'],sha,identity['dimension'],manifest_metadata=identity)
        from app.config import Settings
        settings=Settings(db.settings.database_url,allowed_hosts=('testserver',),allowed_origins=('http://testserver',),analytics_cursor_key=b'a'*32,server_root=private_root,source_root=root)
        app=runtime.wrap_lifespan(create_app(settings,scorer=runtime,source_root=root,program_entry=root/'apps/api/app/runtime_entry.py'))
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
            session=post('/api/v1/live-sessions',{'label':'Real Runtime workflow','mode':'rehearsal','run_purpose':'local','rules':{'category_id':'real-workflow','duration_seconds':240,'intermission_seconds':3,'auto_next':True}});sid=session['session_id']
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
                    report=exercise_load(app,db,actor,operator,display,ws,sid,ds,ready,child.pid)
                    current=operator.get('/api/v1/live-sessions/'+sid).json()['data']
                    post('/api/v1/live-sessions/'+sid+'/commands',{'command':'end','expected_version':current['version'],'args':{'reason':'real-time backend capacity execution finished'}})
            output=Path(os.environ['WORDHEAT_LOAD_REPORT'])
            output.write_text(__import__('json').dumps(report,ensure_ascii=False,indent=2))
            print('REAL_TIME_CAPACITY_REPORT_WRITTEN')
    finally:
        host.stop_children(owned)
        private.cleanup()

