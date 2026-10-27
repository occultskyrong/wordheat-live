"""Real loopback HTTP/WS + owned PG; the provider is explicitly synthetic."""
import json
import socket
import threading
import time
from datetime import timedelta
import httpx
import uvicorn
from websockets.sync.client import connect
from app.core import create_app,SyntheticTestProvider
from app.control import RelayControlPlane,FreshnessSigner,verify_envelope
from app.fulfillment import PlatformOutcome
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat


def test_synthetic_loopback_five_stages_and_confirmed_stop(relay_environment):
    service,grant,lease,clock=relay_environment;provider=SyntheticTestProvider(b'synthetic-component-key')
    calls=[]
    class FixtureVerifier:
        def verify(self,value):return grant
    class FixturePlatform:
        provider_test=True
        def perform(self,kind,context,payload):
            calls.append(kind)
            return PlatformOutcome('succeeded',response={'provider_test':True,'status':'done'})
    plane=RelayControlPlane(service)
    app=create_app(service,provider=provider,grant_verifier=FixtureVerifier(),test_mode=True,control_plane=plane,platform_adapter=FixturePlatform(),run_workers=True)
    owned=socket.socket();owned.bind(('127.0.0.1',0));owned.listen(16);port=owned.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,loop='asyncio',ws='websockets-sansio',log_level='critical'))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[owned]},daemon=True);thread.start()
    try:
        until=time.monotonic()+5
        while not server.started:
            assert time.monotonic()<until,'Owned relay did not start'
            time.sleep(.01)
        headers={'Authorization':'Bearer synthetic-fixture','X-BE-Extension-Version':'LW-BE/1.1.0'}
        with httpx.Client(base_url=f'http://127.0.0.1:{port}',headers=headers,trust_env=False,follow_redirects=False,timeout=3) as http:
            prepared=http.post('/relay/v1/leases',json={}).json()
            assert prepared['be_extension_version']=='LW-BE/1.1.0'
            assert prepared['official_ingress_ready'] is False and prepared['platform_ready'] is False
            task=http.post('/relay/v1/tasks/start',json={'lease_id':lease['lease_id']})
            assert task.status_code==200
            opened=http.post('/relay/v1/windows/open',json={'lease_id':lease['lease_id'],'round_id':'synthetic-round','deadline_at':(clock[0]+timedelta(seconds=60)).isoformat()},headers={'Idempotency-Key':'synthetic-open'})
            assert opened.status_code==200
            raw=json.dumps({'app_id':grant.app_id,'room_id':grant.room_id,'messages':[{'source_msg_id':'synthetic-message','type':'comment','payload':{'text':'演练'}}]},ensure_ascii=False).encode()
            accepted=http.post('/relay/v1/douyin/callback',content=raw,headers={'X-Synthetic-Signature':provider.sign(raw)})
            assert accepted.json()['provider_test'] is True
            with connect(f'ws://127.0.0.1:{port}/relay/v1/streams',additional_headers=headers,proxy=None,max_size=65536,open_timeout=3) as ws:
                ws.send(json.dumps({'kind':'resume','lease_id':lease['lease_id'],'persisted_seq':'0'}))
                assert json.loads(ws.recv(timeout=3))['kind']=='resume_ready'
                first=json.loads(ws.recv(timeout=3));second=json.loads(ws.recv(timeout=3))
                assert (first['stream_seq'],second['stream_seq'])==('1','2')
                ws.send(json.dumps({'kind':'transport.persisted','persisted_seq':'2'}))
                assert json.loads(ws.recv(timeout=3))['kind']=='transport.persisted.accepted'
                for stage in ('display.received','display.processed'):
                    ws.send(json.dumps({'kind':stage,'stream_seq':'2','receipt_id':'synthetic-'+stage,'client_time':clock[0].isoformat(timespec='milliseconds').replace('+00:00','Z')}))
                    assert json.loads(ws.recv(timeout=3))['kind']=='delivery.stage.accepted'
                until=time.monotonic()+5
                while 'platform.fulfilled' not in service.stages(grant.app_id,grant.room_id,2):
                    assert time.monotonic()<until,'Durable synthetic fulfillment remained pending'
                    time.sleep(.02)
                stopped=http.post('/relay/v1/tasks/stop',json={'lease_id':lease['lease_id']}).json()
                until=time.monotonic()+5
                while stopped['status']!='succeeded':
                    assert time.monotonic()<until,'Synthetic stop was not confirmed'
                    time.sleep(.02)
                    stopped=http.post('/relay/v1/tasks/stop',json={'lease_id':lease['lease_id']}).json()
                assert stopped['platform_fulfilled'] is True
            assert {'platform.start','display.received','display.processed','platform.stop'}<=set(calls)
    finally:
        server.should_exit=True;thread.join(5);owned.close()
        assert not thread.is_alive(),'Owned loopback relay did not stop'
