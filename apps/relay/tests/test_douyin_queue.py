"""Real owned PG + published wire through MockTransport, never real API calls."""
import json
import threading
from datetime import timedelta
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import httpx
from app.core import SourceMessage
from app.fulfillment import FulfillmentWorker
from app.provider import DouyinClient,DouyinPlatformAdapter


def test_batch_ack_preserves_each_client_time_and_nonzero_err_no_fails_whole_batch(relay_environment):
    service,grant,lease,clock=relay_environment;n=120
    service.open_window(grant.app_id,grant.room_id,lease['lease_id'],'synthetic-round',clock[0]+timedelta(seconds=60))
    messages=[SourceMessage('batch-'+str(i),'comment',{'text':'演练'}) for i in range(n)]
    service.ingest(grant.app_id,grant.room_id,messages,b'synthetic-batch',provider_test=True)
    received=[];reject=[True]
    def transport(request):
        if request.url.path.endswith('/token'):return httpx.Response(200,json={'err_no':0,'data':{'access_token':'synthetic-batch-token','expires_in':7200}})
        assert request.url.path=='/api/live_data/ack';body=json.loads(request.content);received.append(json.loads(body['data']))
        return httpx.Response(200,json={'err_no':1 if reject[0] else 0,'data':{}})
    client=DouyinClient(grant.app_id,'synthetic-batch-secret',profile='data_open_access_token',transport=httpx.MockTransport(transport))
    adapter=DouyinPlatformAdapter(client,scope_guard=service.task_scope_guard)
    with service.tx() as c:
        owner=c.execute('SELECT * FROM relay.room_owners WHERE app_id=%s AND room_id=%s',(grant.app_id,grant.room_id)).fetchone()
        for index in range(n):service.enqueue_job(c,owner,'display.received',stream_seq=index+2,payload={'client_time':clock[0].isoformat(),'receipt_id':'synthetic-'+str(index)})
    worker=FulfillmentWorker(service,adapter)
    assert worker.step()['completed']==0
    with service.tx() as c:
        assert c.execute("SELECT count(*) AS n FROM relay.relay_outbox WHERE app_id=%s AND room_id=%s AND status='failed'",(grant.app_id,grant.room_id)).fetchone()['n']==n
        assert c.execute("SELECT count(*) AS n FROM relay.delivery_stages WHERE app_id=%s AND room_id=%s AND stage='platform.received'",(grant.app_id,grant.room_id)).fetchone()['n']==0
        ids=c.execute('SELECT job_id FROM relay.relay_outbox WHERE app_id=%s AND room_id=%s',(grant.app_id,grant.room_id)).fetchall()
        for row in ids:service.retry_job(c,row['job_id'],'explicit-new-retry',True)
    reject[0]=False
    assert worker.step()['completed']==n and len(received)==2
    assert len(received[1])==n and len({v['msg_id'] for v in received[1]})==n
    assert all(type(v['client_time']) is int and v['msg_type']=='live_comment' for v in received[1])
    with service.tx() as c:
        assert c.execute('SELECT min(attempts) AS total,min(retry_cycle) AS cycle FROM relay.relay_outbox WHERE app_id=%s AND room_id=%s',(grant.app_id,grant.room_id)).fetchone()=={'total':2,'cycle':1}


def test_delayed_old_stop_cannot_overlap_or_stop_new_owner_task(relay_environment):
    service,old,lease,clock=relay_environment;state={'running':False,'starts':0,'stops':0};entered=threading.Event();release=threading.Event();delay=[True]
    def transport(request):
        path=request.url.path
        if path.endswith('/token'):return httpx.Response(200,json={'err_no':0,'data':{'access_token':'synthetic-fence-token','expires_in':7200}})
        if path.endswith('/get'):return httpx.Response(200,json={'err_no':0,'data':{'status':3 if state['running'] else 2}})
        if path.endswith('/start'):
            state['running']=True;state['starts']+=1;return httpx.Response(200,json={'err_no':0,'data':{'task_id':'synthetic-task-'+str(state['starts'])}})
        if path.endswith('/stop'):
            if delay[0]:entered.set();release.wait(3)
            state['running']=False;state['stops']+=1;return httpx.Response(200,json={'err_no':0,'data':{}})
        raise AssertionError('Unregistered endpoint')
    adapter=DouyinPlatformAdapter(DouyinClient(old.app_id,'synthetic-fence-secret',profile='data_open_access_token',transport=httpx.MockTransport(transport)),scope_guard=service.task_scope_guard)
    worker=FulfillmentWorker(service,adapter)
    with service.tx() as c:
        owner=c.execute('SELECT * FROM relay.room_owners WHERE app_id=%s AND room_id=%s',(old.app_id,old.room_id)).fetchone();service.enqueue_job(c,owner,'platform.start',job_id='start:'+lease['lease_id'])
    assert worker.step()['completed']==1
    service.revoke(old.app_id,old.room_id,lease['lease_id'])
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(worker.step);assert entered.wait(1)
        clock[0]+=timedelta(seconds=16)
        new=replace(old,installation_id='synthetic-fence-new-install',session_id='synthetic-fence-new-session',expires_at=clock[0]+timedelta(hours=1));newlease=service.acquire(new)
        with service.tx() as c:
            owner=c.execute('SELECT * FROM relay.room_owners WHERE app_id=%s AND room_id=%s',(new.app_id,new.room_id)).fetchone();service.enqueue_job(c,owner,'platform.start',job_id='start:'+newlease['lease_id'])
        assert worker.step()['completed']==0 and state['starts']==1
        release.set();future.result(3)
    delay[0]=False;clock[0]+=timedelta(seconds=3)
    # Resolve the old uncertain stop before the new start may claim the tuple.
    worker.step();worker.step()
    with service.tx() as c:diagnostic=c.execute('SELECT kind,status,last_error,attempts,cycle_attempts FROM relay.relay_outbox ORDER BY created_at,job_id').fetchall()
    assert state['starts']==2 and state['running'] is True,diagnostic
    previous_stops=state['stops']
    with service.tx() as c:
        c.execute("UPDATE relay.relay_outbox SET status='failed' WHERE job_id=%s",('stop:'+lease['lease_id'],));service.retry_job(c,'stop:'+lease['lease_id'],'explicit-late-old-stop',True)
    worker.step()
    assert state['stops']==previous_stops and state['running'] is True
