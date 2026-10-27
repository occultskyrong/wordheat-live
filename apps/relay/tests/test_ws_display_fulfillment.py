"""Owned PG and real loopback WS -> durable jobs -> mocked published SDK wire."""
from contextlib import contextmanager
from datetime import timedelta
import json
import base64
import hashlib
from uuid import uuid4
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from websockets.sync.client import connect

from app.core import RelayError,SourceMessage,SyntheticTestProvider,create_app
from app.provider import DouyinClient,DouyinPlatformAdapter
from app.control import RelayControlPlane


@contextmanager
def live_relay(environment):
    service,grant,lease,clock=environment
    service.open_window(grant.app_id,grant.room_id,lease['lease_id'],'ws-actual-time-round',clock[0]+timedelta(seconds=60))
    service.ingest(grant.app_id,grant.room_id,[SourceMessage('ws-time-a','comment',{'text':'演练'}),SourceMessage('ws-time-b','comment',{'text':'测试'})],b'synthetic-ws-times',provider_test=True)
    reported=[]
    def wire(request):
        if request.url.path.endswith('/token'):
            return httpx.Response(200,json={'err_no':0,'data':{'access_token':'synthetic-ws-time-token','expires_in':7200}})
        assert request.url.path=='/api/live_data/ack'
        body=json.loads(request.content)
        reported.append((body['ack_type'],json.loads(body['data'])))
        return httpx.Response(200,json={'err_no':0,'data':{}})
    adapter=DouyinPlatformAdapter(DouyinClient(grant.app_id,'synthetic-ws-time-secret',profile='data_open_access_token',transport=httpx.MockTransport(wire)),scope_guard=service.task_scope_guard)
    class Verifier:
        def verify(self,value):return grant
    app=create_app(service,provider=SyntheticTestProvider(b'synthetic-ws-time-key'),grant_verifier=Verifier(),test_mode=True,control_plane=RelayControlPlane(service),platform_adapter=adapter,run_workers=True)
    owned=socket.socket();owned.bind(('127.0.0.1',0));owned.listen(16)
    server=uvicorn.Server(uvicorn.Config(app,loop='asyncio',ws='websockets-sansio',log_level='critical'))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[owned]},daemon=True);thread.start()
    try:
        deadline=time.monotonic()+5
        while not server.started:
            assert time.monotonic()<deadline,'Owned WS server failed to start'
            time.sleep(.01)
        yield service,grant,lease,f'ws://127.0.0.1:{owned.getsockname()[1]}/relay/v1/streams',reported
    finally:
        server.should_exit=True;thread.join(5);owned.close()
        assert not thread.is_alive(),'Owned WS server failed to stop'


@contextmanager
def resumed(url,lease,extension='LW-BE/1.1.0'):
    headers={'Authorization':'Bearer synthetic-ws-time'}
    if extension is not None:headers['X-BE-Extension-Version']=extension
    with connect(url,additional_headers=headers,proxy=None,max_size=65536,open_timeout=3) as ws:
        ws.send(json.dumps({'kind':'resume','lease_id':lease['lease_id'],'persisted_seq':'0'}))
        assert json.loads(ws.recv(timeout=3))['kind']=='resume_ready'
        for sequence in ('1','2','3'):
            assert json.loads(ws.recv(timeout=3))['stream_seq']==sequence
        ws.send(json.dumps({'kind':'transport.persisted','persisted_seq':'3'}))
        assert json.loads(ws.recv(timeout=3))=={'kind':'transport.persisted.accepted','persisted_seq':'3'}
        yield ws


def send_stage(ws,stage,seq,client_time):
    ws.send(json.dumps({'kind':stage,'stream_seq':str(seq),'receipt_id':f'ws-{stage}-{seq}','client_time':client_time}))
    response=json.loads(ws.recv(timeout=3))
    assert response['kind']=='delivery.stage.accepted',response


def test_ws_display_stages_reach_sdk_with_each_original_client_time_and_idempotency(relay_environment):
    times={2:'2026-10-27T01:02:03.123Z',3:'2026-10-27T01:02:04.456Z'}
    with live_relay(relay_environment) as (service,grant,lease,url,reported):
        with resumed(url,lease) as ws:
            for stage in ('display.received','display.processed'):
                for seq,stamp in times.items():
                    send_stage(ws,stage,seq,stamp)
                    send_stage(ws,stage,seq,stamp)
            deadline=time.monotonic()+5
            while not all('platform.fulfilled' in service.stages(grant.app_id,grant.room_id,seq) for seq in times):
                assert time.monotonic()<deadline,'WS display reports failed to reach the SDK'
                time.sleep(.02)
        with service.tx() as c:
            jobs=c.execute("SELECT stream_seq,kind,payload,status FROM relay.relay_outbox WHERE kind IN ('display.received','display.processed') ORDER BY stream_seq,kind").fetchall()
            assert len(jobs)==4
            assert all(row['status']=='succeeded' and row['payload']['client_time']==times[row['stream_seq']] for row in jobs)
        expected={(ack_type,'ws-time-'+suffix,millis) for ack_type in (1,2) for suffix,millis in (('a',1793062923123),('b',1793062924456))}
        actual=[(ack_type,item['msg_id'],item['client_time']) for ack_type,items in reported for item in items]
        assert set(actual)==expected and len(actual)==4


@pytest.mark.parametrize('extension,field,error',[
    (None,{'client_time':'2026-10-27T01:02:03.123Z'},'BE_EXTENSION_VERSION_UNSUPPORTED'),
    ('LW-BE/0.0.0',{'client_time':'2026-10-27T01:02:03.123Z'},'BE_EXTENSION_VERSION_UNSUPPORTED'),
    ('LW-BE/1.1.0',{},'INPUT_INVALID'),
    ('LW-BE/1.1.0',{'client_time':None},'INPUT_INVALID'),
    ('LW-BE/1.1.0',{'client_time':1793062923123},'INPUT_INVALID'),
    ('LW-BE/1.1.0',{'client_time':'2026-10-27T01:02:03Z'},'INPUT_INVALID'),
    ('LW-BE/1.1.0',{'client_time':'2026-10-27T01:02:03.123+08:00'},'INPUT_INVALID'),
    ('LW-BE/1.1.0',{'client_time':'2026-02-30T01:02:03.123Z'},'INPUT_INVALID'),
    ('LW-BE/1.1.0',{'client_time':'1969-12-31T23:59:59.999Z'},'INPUT_INVALID'),
])
def test_invalid_ws_display_time_or_negotiation_rejected_before_stage_and_job(relay_environment,extension,field,error):
    with live_relay(relay_environment) as (service,grant,lease,url,reported):
        with resumed(url,lease,extension) as ws:
            ws.send(json.dumps({'kind':'display.received','stream_seq':'2','receipt_id':'invalid-time',**field}))
            assert json.loads(ws.recv(timeout=3))=={'kind':'relay.error','reason_code':error}
            with pytest.raises(__import__('websockets').exceptions.ConnectionClosed) as closed:ws.recv(timeout=3)
            assert closed.value.rcvd.code==1008
        with service.tx() as c:
            assert c.execute("SELECT count(*) AS n FROM relay.delivery_stages WHERE stage IN ('display.received','display.processed')").fetchone()['n']==0
            assert c.execute('SELECT count(*) AS n FROM relay.relay_outbox').fetchone()['n']==0
        assert reported==[]


def test_direct_record_stage_without_actual_time_does_not_acknowledge_pending(relay_environment):
    service,grant,lease,clock=relay_environment
    service.open_window(grant.app_id,grant.room_id,lease['lease_id'],'time-required-round',clock[0]+timedelta(seconds=60))
    service.ingest(grant.app_id,grant.room_id,[SourceMessage('time-required','comment',{'text':'演练'})],b'synthetic-time-required',provider_test=True)
    connection=service.connect(grant.app_id,grant.room_id,lease['lease_id'])
    service.frames(grant.app_id,grant.room_id,connection,0);service.ack(grant.app_id,grant.room_id,connection,2)
    with pytest.raises(RelayError) as rejected:
        service.record_stage(grant.app_id,grant.room_id,connection,2,'display.received','missing-time')
    assert rejected.value.code=='INPUT_INVALID'
    with service.tx() as c:
        assert c.execute("SELECT count(*) AS n FROM relay.delivery_stages WHERE stage='display.received'").fetchone()['n']==0
        assert c.execute('SELECT count(*) AS n FROM relay.relay_outbox').fetchone()['n']==0


@pytest.mark.parametrize('stage,seq,reason',[('display.processed',2,'DISPLAY_RECEIVED_REQUIRED'),('display.received',1,'DISPLAY_SOURCE_DELIVERY_REQUIRED')])
def test_valid_time_does_not_bypass_source_or_stage_scope(relay_environment,stage,seq,reason):
    with live_relay(relay_environment) as (service,grant,lease,url,reported):
        with resumed(url,lease) as ws:
            ws.send(json.dumps({'kind':stage,'stream_seq':str(seq),'receipt_id':'wrong-scope','client_time':'2026-10-27T01:02:03.123Z'}))
            assert json.loads(ws.recv(timeout=3))=={'kind':'relay.error','reason_code':reason}
        with service.tx() as c:
            assert c.execute('SELECT count(*) AS n FROM relay.relay_outbox').fetchone()['n']==0
        assert reported==[]


@pytest.mark.parametrize('stage',['display.received','display.processed'])
def test_ws_same_receipt_different_actual_time_conflicts_without_mutating_history(relay_environment,stage):
    original='2026-10-27T01:02:03.123Z'
    with live_relay(relay_environment) as (service,grant,lease,url,reported):
        with resumed(url,lease) as ws:
            send_stage(ws,'display.received',2,original)
            if stage=='display.processed':send_stage(ws,stage,2,original)
            ws.send(json.dumps({'kind':stage,'stream_seq':'2','receipt_id':f'ws-{stage}-2','client_time':'2026-10-27T01:02:03.124Z'}))
            assert json.loads(ws.recv(timeout=3))=={'kind':'relay.error','reason_code':'RECEIPT_CONFLICT'}
        with service.tx() as c:
            payload=c.execute('SELECT payload FROM relay.relay_outbox WHERE stream_seq=2 AND kind=%s',(stage,)).fetchone()['payload']
            assert payload['client_time']==original
            assert c.execute('SELECT count(*) AS n FROM relay.relay_outbox WHERE stream_seq=2 AND kind=%s',(stage,)).fetchone()['n']==1


@pytest.mark.parametrize('stage',['display.received','display.processed'])
def test_http_then_ws_same_sdk_millisecond_keeps_original_time_and_different_time_conflicts(relay_environment,stage):
    original='2026-10-27T09:02:03.123456+08:00'
    with live_relay(relay_environment) as (service,grant,lease,url,reported):
        token='synthetic-http-ws-time-token'
        with service.tx() as c:
            c.execute("INSERT INTO relay.device_pairings(pairing_id,app_id,room_id,room_binding_id,installation_id,actor_id,device_public_key,challenge,status,expires_at,room_verified_until,verification_id,provider_test,device_token_hash,device_token_expires_at) VALUES(%s,%s,%s,'synthetic-binding',%s,'synthetic-admin',%s,'unused','bound',%s,%s,'synthetic-room-proof',true,%s,%s)",(str(uuid4()),grant.app_id,grant.room_id,grant.installation_id,b'0'*32,grant.expires_at,grant.expires_at,hashlib.sha256(token.encode()).hexdigest(),grant.expires_at))
            stream=c.execute('SELECT stream_id FROM relay.relay_streams WHERE app_id=%s AND room_id=%s',(grant.app_id,grant.room_id)).fetchone()['stream_id']
        credential=base64.urlsafe_b64encode(json.dumps({'device_token':token,'authorization_id':'synthetic-no-live-grant','session_id':grant.session_id}).encode()).rstrip(b'=').decode()
        with resumed(url,lease) as ws:
            endpoint=url.replace('ws://','http://').replace('/streams','/delivery-stages')
            with httpx.Client(trust_env=False,follow_redirects=False,timeout=3) as client:
                for accepted_stage in (('display.received',) if stage=='display.received' else ('display.received','display.processed')):
                    response=client.post(endpoint,headers={'Authorization':'Bearer '+credential,'X-BE-Extension-Version':'LW-BE/1.1.0'},json={'stage':accepted_stage,'stream_id':stream,'stream_seq':'2','receipt_id':f'ws-{accepted_stage}-2','client_time':original})
                    assert response.status_code==200,response.text
            send_stage(ws,stage,2,'2026-10-27T01:02:03.123Z')
            ws.send(json.dumps({'kind':stage,'stream_seq':'2','receipt_id':f'ws-{stage}-2','client_time':'2026-10-27T01:02:03.124Z'}))
            assert json.loads(ws.recv(timeout=3))=={'kind':'relay.error','reason_code':'RECEIPT_CONFLICT'}
        with service.tx() as c:
            payload=c.execute('SELECT payload FROM relay.relay_outbox WHERE stream_seq=2 AND kind=%s',(stage,)).fetchone()['payload']
            assert payload['client_time']==original


def test_ws_cannot_invent_time_for_a_legacy_job_without_actual_timestamp(relay_environment):
    with live_relay(relay_environment) as (service,grant,lease,url,reported):
        with resumed(url,lease) as ws:
            with service.tx() as c:
                owner=c.execute('SELECT * FROM relay.room_owners WHERE app_id=%s AND room_id=%s',(grant.app_id,grant.room_id)).fetchone()
                c.execute("INSERT INTO relay.delivery_stages VALUES(%s,%s,2,'display.received','legacy-no-time',%s)",(grant.app_id,grant.room_id,relay_environment[3][0]))
                job=service.enqueue_job(c,owner,'display.received',stream_seq=2,payload={'receipt_id':'legacy-no-time'})
                c.execute("UPDATE relay.relay_outbox SET status='failed',last_error='DISPLAY_CLIENT_TIME_REQUIRED' WHERE job_id=%s",(job,))
            ws.send(json.dumps({'kind':'display.received','stream_seq':'2','receipt_id':'legacy-no-time','client_time':'2026-10-27T01:02:03.123Z'}))
            assert json.loads(ws.recv(timeout=3))=={'kind':'relay.error','reason_code':'RECEIPT_CONFLICT'}
        with service.tx() as c:
            row=c.execute('SELECT status,payload FROM relay.relay_outbox WHERE job_id=%s',(job,)).fetchone()
            assert row['status']=='failed' and 'client_time' not in row['payload']
        assert reported==[]


def test_valid_client_time_cannot_reuse_a_superseded_ws_connection(relay_environment):
    with live_relay(relay_environment) as (service,grant,lease,url,reported):
        with resumed(url,lease) as ws:
            service.connect(grant.app_id,grant.room_id,lease['lease_id'])
            ws.send(json.dumps({'kind':'display.received','stream_seq':'2','receipt_id':'stale-connection','client_time':'2026-10-27T01:02:03.123Z'}))
            assert json.loads(ws.recv(timeout=3))=={'kind':'relay.error','reason_code':'CONNECTION_STALE'}
        with service.tx() as c:
            assert c.execute("SELECT count(*) AS n FROM relay.delivery_stages WHERE stage IN ('display.received','display.processed')").fetchone()['n']==0
            assert c.execute('SELECT count(*) AS n FROM relay.relay_outbox').fetchone()['n']==0
        assert reported==[]
