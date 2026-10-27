import base64
from datetime import timedelta
from uuid import uuid4
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from app.core import RelayError,SourceMessage
from app.control import RelayControlPlane,ManagementIntent,VerifiedRoom
from app.fulfillment import FulfillmentWorker,PlatformOutcome,RelayRuntime


def test_authenticated_pairing_requires_device_possession_and_one_use(relay_environment):
    service,grant,lease,clock=relay_environment
    intent=ManagementIntent('synthetic-admin',grant.app_id,grant.room_id,'synthetic-binding',grant.installation_id,clock[0]+timedelta(minutes=1))
    class Management:
        def verify(self,value):
            if value!='synthetic-credential':raise RelayError('AUTH_REQUIRED',403)
            return intent
    class Rooms:
        def verify(self,a,r,actor):return VerifiedRoom(a,r,actor,'synthetic-room-proof',clock[0]+timedelta(hours=1),True)
    control=RelayControlPlane(service,management_verifier=Management(),room_verifier=Rooms())
    key=Ed25519PrivateKey.generate();public=base64.urlsafe_b64encode(key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).rstrip(b'=').decode()
    with pytest.raises(RelayError):control.pair('untrusted',{'device_public_key':public})
    issued=control.pair('synthetic-credential',{'device_public_key':public})
    signature=base64.urlsafe_b64encode(key.sign(base64.b64decode(issued['challenge_b64']))).rstrip(b'=').decode()
    complete={'action':'complete','pairing_id':issued['pairing_id'],'signature':signature}
    result=control.pair('synthetic-credential',complete)
    assert result['provider_test'] is True and result['device_token']
    with pytest.raises(RelayError):control.pair('synthetic-credential',complete)


def test_idle_expiry_closes_window_and_persists_stop_then_worker_confirms(relay_environment):
    service,grant,lease,clock=relay_environment
    service.open_window(grant.app_id,grant.room_id,lease['lease_id'],'synthetic-round',clock[0]+timedelta(seconds=60))
    clock[0]+=timedelta(seconds=16)
    class Platform:
        provider_test=True
        def perform(self,kind,context,payload):
            assert kind=='platform.stop'
            return PlatformOutcome('succeeded',response={'provider_test':True,'status':'stopped'})
    worker=FulfillmentWorker(service,Platform());RelayRuntime(service,worker).sweep()
    with service.tx() as c:
        window=c.execute('SELECT state,reason FROM relay.relay_windows WHERE app_id=%s AND room_id=%s',(grant.app_id,grant.room_id)).fetchone()
        assert window=={'state':'CLOSED','reason':'LEASE_EXPIRED'}
        assert c.execute("SELECT status FROM relay.relay_outbox WHERE job_id=%s",('stop:'+lease['lease_id'],)).fetchone()['status']=='pending'
    assert worker.step()['completed']==1
    with service.tx() as c:assert c.execute('SELECT status FROM relay.relay_outbox WHERE job_id=%s',('stop:'+lease['lease_id'],)).fetchone()['status']=='succeeded'


def test_receipt_retry_is_persistent_and_never_reexecutes_game(relay_environment):
    service,grant,lease,clock=relay_environment
    service.open_window(grant.app_id,grant.room_id,lease['lease_id'],'synthetic-round',clock[0]+timedelta(seconds=60))
    service.ingest(grant.app_id,grant.room_id,[SourceMessage('synthetic-source','comment',{'text':'演练'})],b'synthetic',provider_test=True)
    conn=service.connect(grant.app_id,grant.room_id,lease['lease_id']);service.frames(grant.app_id,grant.room_id,conn,0);service.ack(grant.app_id,grant.room_id,conn,2)
    service.record_stage(grant.app_id,grant.room_id,conn,2,'display.received','synthetic-receipt',client_time=clock[0].isoformat(timespec='milliseconds').replace('+00:00','Z'))
    class Platform:
        provider_test=True
        def __init__(self):self.calls=0
        def perform(self,kind,context,payload):
            self.calls+=1
            return PlatformOutcome('retry','TRANSIENT',retryable=True) if self.calls==1 else PlatformOutcome('succeeded',response={'provider_test':True})
    adapter=Platform();worker=FulfillmentWorker(service,adapter)
    assert worker.step()['completed']==0
    clock[0]+=timedelta(seconds=3)
    assert FulfillmentWorker(service,adapter).step()['completed']==1
    assert service.stages(grant.app_id,grant.room_id,2)==['display.received','platform.received','transport.persisted']
    assert adapter.calls==2


def test_short_device_token_rotation_requires_device_signature_and_consumes_challenge(relay_environment):
    import hashlib
    service,grant,lease,clock=relay_environment;key=Ed25519PrivateKey.generate();pairing_id=str(uuid4());token='synthetic-device-token'
    control=RelayControlPlane(service)
    assert hasattr(control,'token_challenge'),'Missing authenticated short device token renewal'
    with service.tx() as c:
        c.execute("INSERT INTO relay.device_pairings(pairing_id,app_id,room_id,room_binding_id,installation_id,actor_id,device_public_key,challenge,status,expires_at,room_verified_until,verification_id,provider_test,device_token_hash,device_token_expires_at) VALUES(%s,%s,%s,'binding',%s,'synthetic-admin',%s,'unused','bound',%s,%s,'synthetic-proof',true,%s,%s)",(pairing_id,grant.app_id,grant.room_id,grant.installation_id,key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw),clock[0]+timedelta(minutes=1),clock[0]+timedelta(hours=1),hashlib.sha256(token.encode()).hexdigest(),clock[0]+timedelta(minutes=1)))
    challenge=control.token_challenge(token,pairing_id)
    signature=base64.urlsafe_b64encode(key.sign(base64.b64decode(challenge['challenge_b64']))).rstrip(b'=').decode()
    renewed=control.rotate_token(token,challenge['challenge_id'],signature)
    assert renewed['device_token']!=token and renewed['ttl_ms']>0
    with pytest.raises(RelayError):control.rotate_token(token,challenge['challenge_id'],signature)


def test_c1_native_stream_rejects_numeric_cursor_instead_of_advancing(relay_environment):
    from fastapi.testclient import TestClient
    from app.core import create_app,SyntheticTestProvider
    service,grant,lease,clock=relay_environment
    class FixtureVerifier:
        def verify(self,value):return grant
    client=TestClient(create_app(service,provider=SyntheticTestProvider(b'synthetic-component-key'),grant_verifier=FixtureVerifier(),test_mode=True))
    with client.websocket_connect('/relay/v1/streams',headers={'Authorization':'Bearer synthetic'}) as ws:
        ws.send_json({'kind':'resume','lease_id':lease['lease_id'],'persisted_seq':0})
        assert ws.receive_json()=={'kind':'relay.error','reason_code':'INPUT_INVALID'}


def test_old_stop_job_never_targets_replacement_owner(relay_environment):
    from app.core import VerifiedGrant
    service,old,lease,clock=relay_environment
    service.revoke(old.app_id,old.room_id,lease['lease_id']);clock[0]+=timedelta(seconds=16)
    replacement=VerifiedGrant(old.app_id,old.room_id,'synthetic-new-install','synthetic-new-session','test_live',old.expires_at)
    newlease=service.acquire(replacement)
    class Platform:
        provider_test=True
        def perform(self,kind,context,payload):
            assert context['lease_id']==lease['lease_id'] and context['session_id']==old.session_id
            return PlatformOutcome('succeeded',response={'provider_test':True})
    assert FulfillmentWorker(service,Platform()).step()['completed']==1
    with service.tx() as c:
        owner=c.execute('SELECT lease_id,expires_at FROM relay.room_owners WHERE app_id=%s AND room_id=%s',(old.app_id,old.room_id)).fetchone()
        assert owner['lease_id']==newlease['lease_id'] and owner['expires_at']>clock[0]


def test_batch_raw_payload_is_charged_once_and_pending_fulfillment_pins_source(relay_environment):
    service,grant,lease,clock=relay_environment
    service.open_window(grant.app_id,grant.room_id,lease['lease_id'],'synthetic-round',clock[0]+timedelta(seconds=60))
    raw=b'synthetic-raw-batch'*500
    service.ingest(grant.app_id,grant.room_id,[SourceMessage('s1','comment',{'text':'演练'}),SourceMessage('s2','comment',{'text':'演练'})],raw,provider_test=True)
    with service.tx() as c:
        row=c.execute('SELECT payload_bytes FROM relay.relay_streams WHERE app_id=%s AND room_id=%s',(grant.app_id,grant.room_id)).fetchone()
        frames=c.execute('SELECT sum(octet_length(body)) AS n FROM relay.relay_frames WHERE app_id=%s AND room_id=%s',(grant.app_id,grant.room_id)).fetchone()['n']
        assert row['payload_bytes']-frames==len(raw)
    conn=service.connect(grant.app_id,grant.room_id,lease['lease_id']);service.frames(grant.app_id,grant.room_id,conn,0);service.ack(grant.app_id,grant.room_id,conn,3)
    service.record_stage(grant.app_id,grant.room_id,conn,2,'display.received','synthetic-receipt',client_time=clock[0].isoformat(timespec='milliseconds').replace('+00:00','Z'))
    clock[0]+=timedelta(days=2);service.prune_payloads(grant.app_id,grant.room_id,before=clock[0])
    with service.tx() as c:assert c.execute('SELECT body IS NOT NULL AS retained FROM relay.relay_frames WHERE app_id=%s AND room_id=%s AND stream_seq=2',(grant.app_id,grant.room_id)).fetchone()['retained'] is True


def test_relay_session_budget_does_not_reset_on_new_lease(relay_environment):
    from dataclasses import replace
    service,grant,lease,clock=relay_environment
    service.revoke(grant.app_id,grant.room_id,lease['lease_id']);clock[0]+=timedelta(seconds=16)
    scoped=replace(grant,trusted=True,authorization_id='synthetic-budget',candidate_digest='0'*64,live_profile='beta',grant_digest='1'*64,room_binding_id='synthetic-binding',max_session_duration_ms=1000)
    current=service.acquire(scoped)
    result=service.open_window(scoped.app_id,scoped.room_id,current['lease_id'],'synthetic-budget-round',clock[0]+timedelta(seconds=30))
    assert result['remaining_ms']==1000
    clock[0]+=timedelta(seconds=2)
    with pytest.raises(RelayError,match='LIVE_AUTHORIZATION_EXPIRED'):service.acquire(scoped)


def test_relay_root_key_chain_rebuild_and_revoked_key_replay(relay_environment):
    import rfc8785
    service,grant,lease,clock=relay_environment;root=Ed25519PrivateKey.generate();child=Ed25519PrivateKey.generate()
    roots={'root':{'public_key':root.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw),'domains':['wordheat-live.trust'],'types':['key_bundle']}}
    plane=RelayControlPlane(service,trusted_keys=roots)
    def package(revision):
        p={'object_id':'synthetic-key-'+str(revision),'issuer_id':'synthetic-root','issuer_revision':revision,'keys':[{'key_id':'child','public_key_b64url':base64.urlsafe_b64encode(child.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).rstrip(b'=').decode(),'domains':['wordheat-live.readiness'],'types':['live_grant']}] if revision==1 else [],'revoked_key_ids':['child'] if revision==2 else []}
        obj={'key_id':'root','domain':'wordheat-live.trust','type':'key_bundle','version':1,'payload':p}
        return obj|{'signature':base64.urlsafe_b64encode(root.sign(rfc8785.dumps(obj))).rstrip(b'=').decode()}
    plane.import_key_bundle(package(1),native_command_id='synthetic-native',confirmation_id='synthetic-confirmation')
    rebooted=RelayControlPlane(service,trusted_keys=roots)
    with service.tx() as c:assert rebooted.effective_keys(c)['child']['revoked'] is False
    rebooted.import_key_bundle(package(2),native_command_id='synthetic-revoke',confirmation_id='synthetic-confirmation-2')
    assert rebooted.import_key_bundle(package(1),native_command_id='synthetic-replay',confirmation_id='synthetic-confirmation-3').imported is False
    with service.tx() as c:assert rebooted.effective_keys(c)['child']['revoked'] is True


def test_idle_expiry_sweep_is_independent_of_blocked_platform_call(relay_environment):
    import asyncio,threading,time
    service,grant,lease,clock=relay_environment
    service.open_window(grant.app_id,grant.room_id,lease['lease_id'],'synthetic-round',clock[0]+timedelta(seconds=60))
    with service.tx() as c:
        owner=c.execute('SELECT * FROM relay.room_owners WHERE app_id=%s AND room_id=%s',(grant.app_id,grant.room_id)).fetchone()
        service.enqueue_job(c,owner,'platform.start',job_id='start:'+lease['lease_id'])
    entered=threading.Event();release=threading.Event()
    class Blocked:
        provider_test=True
        def perform(self,kind,context,payload):
            entered.set();release.wait(2)
            return PlatformOutcome('succeeded',response={'provider_test':True})
    async def run():
        stop=asyncio.Event();task=asyncio.create_task(RelayRuntime(service,FulfillmentWorker(service,Blocked(),call_timeout=2)).run(stop))
        try:
            until=time.monotonic()+1
            while not entered.is_set():
                assert time.monotonic()<until;await asyncio.sleep(.01)
            clock[0]+=timedelta(seconds=16);until=time.monotonic()+.8
            while True:
                with service.tx() as c:state=c.execute('SELECT state FROM relay.relay_windows WHERE app_id=%s AND room_id=%s',(grant.app_id,grant.room_id)).fetchone()['state']
                if state=='CLOSED':break
                assert time.monotonic()<until,'Expiry was blocked by the platform await'
                await asyncio.sleep(.01)
            assert not release.is_set()
            with service.tx() as c:assert c.execute('SELECT status FROM relay.relay_outbox WHERE job_id=%s',('stop:'+lease['lease_id'],)).fetchone()['status']=='pending'
        finally:release.set();stop.set();await asyncio.wait_for(task,3)
    asyncio.run(run())
