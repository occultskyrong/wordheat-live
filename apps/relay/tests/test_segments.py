"""Fresh-scene/RPO and historical cleanup on owned PG and real loopback.

The fixture provider stays synthetic throughout; no official gate is imported.
"""
import base64
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
import socket
import threading
import time
from uuid import uuid4
import httpx
import pytest
import uvicorn
from websockets.sync.client import connect
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from app.core import SourceMessage,SyntheticTestProvider,create_app,RelayError
from app.control import RelayControlPlane
from app.fulfillment import FulfillmentWorker,PlatformOutcome


def test_expired_old_stream_requires_explicit_new_namespace_and_keeps_old_receipts(relay_environment):
    service,old,oldlease,clock=relay_environment
    service.open_window(old.app_id,old.room_id,oldlease['lease_id'],'old-synthetic-round',clock[0]+timedelta(seconds=5))
    service.ingest(old.app_id,old.room_id,[SourceMessage('old-source','comment',{'text':'演练'})],b'old-synthetic',provider_test=True)
    previous=service.connect(old.app_id,old.room_id,oldlease['lease_id']);frames=service.frames(old.app_id,old.room_id,previous,0);service.ack(old.app_id,old.room_id,previous,2)
    old_stream=frames[0]['stream_id'];old_hash=frames[1]['immutable_sha256']
    pair_id=str(uuid4());key=Ed25519PrivateKey.generate();token='synthetic-initial-device-token'
    with service.tx() as c:
        c.execute("INSERT INTO relay.device_pairings(pairing_id,app_id,room_id,room_binding_id,installation_id,actor_id,device_public_key,challenge,status,expires_at,room_verified_until,verification_id,provider_test,device_token_hash,device_token_expires_at) VALUES(%s,%s,%s,'synthetic-binding',%s,'synthetic-admin',%s,'unused','bound',%s,%s,'synthetic-room-proof',true,%s,%s)",(pair_id,old.app_id,old.room_id,old.installation_id,key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw),clock[0]+timedelta(minutes=1),clock[0]+timedelta(days=3),hashlib.sha256(token.encode()).hexdigest(),clock[0]+timedelta(minutes=15)))
    clock[0]+=timedelta(days=2);service.prune_payloads(old.app_id,old.room_id,before=clock[0])
    new=replace(old,installation_id='synthetic-new-installation',session_id='synthetic-new-session',expires_at=clock[0]+timedelta(hours=1))
    newlease=service.acquire(new)
    with pytest.raises(RelayError,match='STREAM_BOOTSTRAP_REQUIRED'):service.connect(new.app_id,new.room_id,newlease['lease_id'])
    plane=RelayControlPlane(service)
    challenge=plane.token_challenge(None,pair_id)
    signature=base64.urlsafe_b64encode(key.sign(base64.b64decode(challenge['challenge_b64']))).rstrip(b'=').decode()
    renewed=plane.rotate_token(None,challenge['challenge_id'],signature)
    old_credential=base64.urlsafe_b64encode(json.dumps({'device_token':renewed['device_token'],'authorization_id':'synthetic-old-scope','session_id':old.session_id}).encode()).rstrip(b'=').decode()
    class Verifier:
        def verify(self,value):return old if value=='old-fixture' else new
    class Platform:
        provider_test=True
        def perform(self,kind,context,payload):
            if kind.startswith('display.'):
                assert context['session_id']==old.session_id and context['installation_id']==old.installation_id
                assert payload['source_reference']['source_msg_id']=='old-source'
            return PlatformOutcome('succeeded',response={'provider_test':True})
    provider=SyntheticTestProvider(b'synthetic-component-key')
    app=create_app(service,provider=provider,grant_verifier=Verifier(),test_mode=True,control_plane=plane,platform_adapter=Platform())
    owned=socket.socket();owned.bind(('127.0.0.1',0));owned.listen(16);port=owned.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,loop='asyncio',ws='websockets-sansio',log_level='critical'))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[owned]},daemon=True);thread.start()
    try:
        until=time.monotonic()+5
        while not server.started:
            assert time.monotonic()<until;time.sleep(.01)
        headers={'Authorization':'Bearer new-fixture','X-BE-Extension-Version':'LW-BE/1.1.0'}
        with httpx.Client(base_url=f'http://127.0.0.1:{port}',headers=headers,trust_env=False,follow_redirects=False,timeout=3) as http:
            result=http.post('/relay/v1/streams/bootstrap',headers={'Idempotency-Key':'fresh-synthetic-session'},json={'lease_id':newlease['lease_id'],'expected_stream_id':old_stream,'reason':'EXPLICIT_FRESH_SESSION'})
            assert result.status_code==200,result.text
            fresh=result.json();assert fresh['stream_id']!=old_stream and fresh['start_seq']=='4'
            assert fresh['rpo']['expired_gap_from']=='1' and fresh['rpo']['expired_gap_through']=='2'
            assert fresh['rpo']['old_history_reconstructed'] is False
            with connect(f'ws://127.0.0.1:{port}/relay/v1/streams',additional_headers=headers,proxy=None,max_size=65536) as ws:
                ws.send(json.dumps({'kind':'resume','lease_id':newlease['lease_id'],'persisted_seq':'3'}))
                ready=json.loads(ws.recv(timeout=3));assert ready['persisted_seq']=='3'
                bootstrap=json.loads(ws.recv(timeout=3));assert bootstrap['stream_seq']=='4' and bootstrap['stream_id']==fresh['stream_id']
                assert json.loads(base64.b64decode(bootstrap['body_b64']))['type']=='stream.bootstrap'
                ws.send(json.dumps({'kind':'transport.persisted','persisted_seq':'4'}));assert json.loads(ws.recv(timeout=3))['persisted_seq']=='4'
            # Old grant expiry is never bypassed for new live operations.
            assert http.post('/relay/v1/windows/open',headers={'Authorization':'Bearer old-fixture'},json={'lease_id':oldlease['lease_id'],'round_id':'forbidden','deadline_at':(clock[0]+timedelta(seconds=5)).isoformat()}).status_code==403
            for stage in ('display.received','display.processed'):
                acknowledged=http.post('/relay/v1/delivery-stages',headers={'Authorization':'Bearer '+old_credential},json={'stream_id':old_stream,'stream_seq':'2','stage':stage,'receipt_id':'old-synthetic-'+stage,'client_time':(clock[0]-timedelta(days=2)).isoformat()})
                assert acknowledged.status_code==200,acknowledged.text
            worker=FulfillmentWorker(service,Platform())
            for _ in range(4):worker.step()
            assert 'platform.fulfilled' in service.stages(old.app_id,old.room_id,2)
            with service.tx() as c:
                original=c.execute('SELECT immutable_sha256,body,stream_id FROM relay.relay_frames WHERE app_id=%s AND room_id=%s AND stream_seq=2',(old.app_id,old.room_id)).fetchone()
                assert original['immutable_sha256']==old_hash and original['body'] is None and original['stream_id']==old_stream
                checkpoints=c.execute('SELECT installation_id,stream_id,persisted_seq FROM relay.relay_checkpoints WHERE app_id=%s AND room_id=%s ORDER BY installation_id',(old.app_id,old.room_id)).fetchall()
                assert next(r for r in checkpoints if r['installation_id']==old.installation_id)['persisted_seq']==2
                assert next(r for r in checkpoints if r['installation_id']==new.installation_id)['stream_id']==fresh['stream_id']
    finally:server.should_exit=True;thread.join(5);owned.close();assert not thread.is_alive()
