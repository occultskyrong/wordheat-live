"""Real PostgreSQL synthetic component tests; never official platform evidence."""
import hashlib
import importlib.util
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = 'wordheat_relay_app'


def module():
    """Load the kernel from the source tree as a real package.

    The kernel resolves sibling modules (fulfillment) with package-relative
    imports, so it must be loaded with its parent package registered in
    sys.modules; a bare top-level file load cannot resolve them.
    """
    import sys
    if PACKAGE not in sys.modules:
        spec = importlib.util.spec_from_file_location(PACKAGE, ROOT / 'app/__init__.py', submodule_search_locations=[str(ROOT / 'app')])
        assert spec and spec.loader and Path(spec.origin).exists(), 'relay application package must exist'
        package = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE] = package
        spec.loader.exec_module(package)
    # Each test gets a fresh kernel, so siblings must resolve against this copy.
    for name in [n for n in list(sys.modules) if n.startswith(PACKAGE + '.')]:
        del sys.modules[name]
    name = PACKAGE + '.core'
    spec = importlib.util.spec_from_file_location(name, ROOT / 'app/core.py')
    assert spec and spec.loader and Path(spec.origin).exists(), 'relay protocol core must exist'
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


class PrivateEnvironment(dict):
    def __repr__(self): return '<private relay test configuration>'


@pytest.fixture(scope='session')
def env():
    p = Path(os.environ['RELAY_TEST_ENV_FILE'])
    return PrivateEnvironment(line.split('=', 1) for line in p.read_text().splitlines() if line and not line.startswith('#'))


@pytest.fixture
def setup(env):
    m = module()
    m.migrate(env['RELAY_MIGRATION_DATABASE_URL'])
    clock = [datetime.now(timezone.utc)]
    service = m.RelayService(env['RELAY_DATABASE_URL'], clock=lambda: clock[0])
    app_id, room = 'test-app', str(uuid4())
    grant = m.VerifiedGrant(app_id, room, 'installation-1', 'session-1', 'test_live', clock[0] + timedelta(hours=1))
    lease = service.acquire(grant)
    yield m, service, grant, lease, clock
    # Exact fixture-owned room only; preserve migrations and other runs.
    with m.safe_connect(env['RELAY_MIGRATION_DATABASE_URL']) as c:
        c.execute('DELETE FROM relay.room_owners WHERE app_id=%s AND room_id=%s', (app_id, room))


def test_empty_core_has_fail_closed_entrypoint():
    m = module()
    with pytest.raises(m.RelayError, match='PROVIDER_ADAPTER_REQUIRED'):
        m.create_app(None)


def open_window(s, g, lease, clock):
    return s.open_window(g.app_id, g.room_id, lease['lease_id'], 'round-1', clock[0] + timedelta(seconds=5))


def message(m, source_id='m1'):
    return m.SourceMessage(source_id, 'comment', {'text': 'synthetic guess'})


def test_commit_dedup_hash_and_restart(setup, env):
    m, s, g, lease, clock = setup
    open_window(s, g, lease, clock)
    result = s.ingest(g.app_id, g.room_id, [message(m)], b'raw', provider_test=True)
    assert result == s.ingest(g.app_id, g.room_id, [message(m)], b'raw', provider_test=True)
    other = m.RelayService(env['RELAY_DATABASE_URL'], clock=lambda: clock[0])
    connection = other.connect(g.app_id, g.room_id, lease['lease_id'])
    frames = other.frames(g.app_id, g.room_id, connection, 0)
    assert [int(f['stream_seq']) for f in frames] == [1, 2]
    import base64
    body = base64.b64decode(frames[-1]['body_b64'])
    assert hashlib.sha256(body).hexdigest() == frames[-1]['immutable_sha256']
    assert json.loads(body)['eligible'] is True
    with pytest.raises(m.RelayError, match='SOURCE_ID_CONFLICT'):
        s.ingest(g.app_id, g.room_id, [m.SourceMessage('m1', 'comment', {'text':'changed'})], b'raw', provider_test=True)


def test_transaction_rollback_has_no_seq_gap(setup):
    m, s, g, lease, clock = setup
    open_window(s, g, lease, clock)
    with pytest.raises(m.RelayError, match='SOURCE_ID_CONFLICT'):
        s.ingest(g.app_id, g.room_id, [message(m), m.SourceMessage('m1', 'comment', {'text':'changed'})], b'raw', provider_test=True)
    assert s.ingest(g.app_id, g.room_id, [message(m)], b'raw', provider_test=True) == ['2']


def test_deadline_barrier_precedes_late_input_and_late_winner_does_not_reopen(setup):
    m, s, g, lease, clock = setup
    window = open_window(s, g, lease, clock)
    s.ingest(g.app_id, g.room_id, [message(m)], b'raw', provider_test=True)
    clock[0] += timedelta(seconds=6)
    assert s.ingest(g.app_id, g.room_id, [message(m, 'm2')], b'raw2', provider_test=True) == ['4']
    close = s.close_window(g.app_id, g.room_id, lease['lease_id'], window['version'], 'SOLVED', expected_epoch=window['epoch'], winner_seq=2)
    assert close['reason'] == 'TIMEOUT' and close['close_seq'] == '2'
    assert close['winner_seq'] == '2'
    with pytest.raises(m.RelayError, match='WINNER_OUTSIDE_WINDOW'):
        s.close_window(g.app_id, g.room_id, lease['lease_id'], window['version'], 'SOLVED', expected_epoch=window['epoch'], winner_seq=4)
    frames = s.frames(g.app_id,g.room_id,s.connect(g.app_id,g.room_id,lease['lease_id']),0)
    import base64
    bodies=[json.loads(base64.b64decode(f['body_b64'])) for f in frames]
    assert [b['type'] for b in bodies] == ['window.open','comment','window.close','comment']
    assert bodies[-1]['eligible'] is False


def test_owner_unique_nonce_fencing_contiguous_ack_and_no_fake_processed(setup):
    m,s,g,lease,clock=setup
    with pytest.raises(m.RelayError, match='OWNER_CONFLICT'):
        s.acquire(m.VerifiedGrant(g.app_id,g.room_id,'other','session-2','test_live',g.expires_at))
    open_window(s,g,lease,clock)
    c1=s.connect(g.app_id,g.room_id,lease['lease_id'])
    s.frames(g.app_id,g.room_id,c1,0)
    c2=s.connect(g.app_id,g.room_id,lease['lease_id'])
    with pytest.raises(m.RelayError, match='CONNECTION_STALE'):
        s.frames(g.app_id,g.room_id,c1,0)
    with pytest.raises(m.RelayError, match='ACK_OUT_OF_RANGE'):
        s.ack(g.app_id,g.room_id,c2,2)
    s.frames(g.app_id,g.room_id,c2,0)
    assert s.ack(g.app_id,g.room_id,c2,1) == 1
    assert s.stages(g.app_id,g.room_id,1) == ['transport.persisted']


def test_concurrent_callbacks_are_deduplicated_and_contiguous(setup):
    m,s,g,lease,clock=setup
    open_window(s,g,lease,clock)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(lambda _:s.ingest(g.app_id,g.room_id,[message(m)],b'raw',provider_test=True),range(12)))
    assert results == [['2']]*12


def test_http_signature_and_ws_persisted_ack(setup):
    m,s,g,lease,clock=setup
    open_window(s,g,lease,clock)
    provider=m.SyntheticTestProvider(b'component-only-key')
    class Trusted:
        def verify(self, token):
            if token != 'trusted-token': raise m.RelayError('AUTH_REQUIRED',401)
            return g
    client=TestClient(m.create_app(s,provider=provider,grant_verifier=Trusted(),test_mode=True))
    body=json.dumps({'app_id':g.app_id,'room_id':g.room_id,'messages':[{'source_msg_id':'m1','type':'comment','payload':{'text':'guess'}}]}).encode()
    assert client.post('/relay/v1/douyin/callback',content=body).status_code==403
    signature=provider.sign(body)
    assert client.post('/relay/v1/douyin/callback',content=body,headers={'X-Synthetic-Signature':signature}).status_code==200
    with client.websocket_connect('/relay/v1/streams',headers={'Authorization':'Bearer trusted-token'}) as ws:
        ws.send_json({'kind':'resume','lease_id':lease['lease_id'],'persisted_seq':'0'})
        assert ws.receive_json()['kind']=='resume_ready'
        f1=ws.receive_json();f2=ws.receive_json()
        assert [f1['stream_seq'],f2['stream_seq']]==['1','2']
        ws.send_json({'kind':'transport.persisted','persisted_seq':'2'})
        assert ws.receive_json()['kind']=='transport.persisted.accepted'
    assert s.stages(g.app_id,g.room_id,2)==['transport.persisted']


def test_pause_resume_revoke_and_expiry_are_durable_barriers(setup):
    m,s,g,lease,clock=setup
    window=open_window(s,g,lease,clock)
    paused=s.pause_window(g.app_id,g.room_id,lease['lease_id'],window['version'],expected_epoch=window['epoch'])
    assert paused['state']=='PAUSED'
    s.ingest(g.app_id,g.room_id,[message(m)],b'paused',provider_test=True)
    resumed=s.resume_window(g.app_id,g.room_id,lease['lease_id'],paused['version'],expected_epoch=window['epoch'])
    assert resumed['state']=='OPEN'
    s.revoke(g.app_id,g.room_id,lease['lease_id'])
    with pytest.raises(m.RelayError,match='LEASE_REVOKED'):
        s.connect(g.app_id,g.room_id,lease['lease_id'])
    with pytest.raises(m.RelayError,match='OWNER_CONFLICT'):
        s.acquire(m.VerifiedGrant(g.app_id,g.room_id,'other','other','test_live',g.expires_at))


def test_clock_rollback_closes_window_even_though_operation_rejected(setup,env):
    m,s,g,lease,clock=setup
    open_window(s,g,lease,clock)
    clock[0]-=timedelta(seconds=2)
    with pytest.raises(m.RelayError,match='CLOCK_UNCERTAIN'):
        s.ingest(g.app_id,g.room_id,[message(m)],b'raw',provider_test=True)
    with m.safe_connect(env['RELAY_DATABASE_URL']) as c:
        assert c.execute('SELECT state,reason FROM relay.relay_windows WHERE app_id=%s AND room_id=%s',(g.app_id,g.room_id)).fetchone()==('CLOSED','CLOCK_UNCERTAIN')


def test_retention_keeps_dedup_metadata_and_reports_gap(setup):
    m,s,g,lease,clock=setup
    open_window(s,g,lease,clock)
    s.ingest(g.app_id,g.room_id,[message(m)],b'raw',provider_test=True)
    assert s.prune_payloads(g.app_id,g.room_id,before=clock[0]+timedelta(seconds=1))==0  # Minimum 24h retention.
    clock[0]+=timedelta(days=2)
    assert s.prune_payloads(g.app_id,g.room_id,before=clock[0])==2
    assert s.ingest(g.app_id,g.room_id,[message(m)],b'raw',provider_test=True)==['2']
    # No sequence reset and no source ID reuse after payload expiry.
    with pytest.raises(m.RelayError,match='SOURCE_ID_CONFLICT'):
        s.ingest(g.app_id,g.room_id,[m.SourceMessage('m1','comment',{'text':'changed'})],b'x',provider_test=True)


def test_delivery_stages_and_unsupported_fulfillment_are_independent(setup):
    m,s,g,lease,clock=setup
    open_window(s,g,lease,clock)
    s.ingest(g.app_id,g.room_id,[message(m)],b'raw',provider_test=True)
    conn=s.connect(g.app_id,g.room_id,lease['lease_id']);s.frames(g.app_id,g.room_id,conn,0);s.ack(g.app_id,g.room_id,conn,2)
    assert s.record_stage(g.app_id,g.room_id,conn,2,'display.received','receipt-1',client_time=clock[0].isoformat(timespec='milliseconds').replace('+00:00','Z'))['status']=='pending'
    assert s.stages(g.app_id,g.room_id,2)==['display.received','transport.persisted']
    assert s.record_stage(g.app_id,g.room_id,conn,2,'display.processed','receipt-2',client_time=clock[0].isoformat(timespec='milliseconds').replace('+00:00','Z'))['status']=='pending'
    result=s.fulfill_pending()
    assert result['unsupported']==2 and result['completed']==0
    assert 'platform.fulfilled' not in s.stages(g.app_id,g.room_id,2)


def test_runtime_cannot_ddl_or_change_immutable_bytes(setup,env):
    m,s,g,lease,clock=setup
    open_window(s,g,lease,clock)
    with m.safe_connect(env['RELAY_DATABASE_URL']) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute('CREATE TABLE relay.forbidden(x int)')
    with m.safe_connect(env['RELAY_DATABASE_URL']) as c:
        with pytest.raises(psycopg.errors.RaiseException,match='IMMUTABLE_FRAME'):
            c.execute("UPDATE relay.relay_frames SET body='changed'::bytea WHERE app_id=%s AND room_id=%s",(g.app_id,g.room_id))


def test_ack_loss_reconnect_uses_relay_durable_cursor(setup):
    m,s,g,lease,clock=setup
    open_window(s,g,lease,clock)
    provider=m.SyntheticTestProvider(b'component-only-key')
    class Trusted:
        def verify(self,token): return g
    client=TestClient(m.create_app(s,provider=provider,grant_verifier=Trusted(),test_mode=True))
    c=s.connect(g.app_id,g.room_id,lease['lease_id']);s.frames(g.app_id,g.room_id,c,0)
    # The first frame was locally persisted but its ACK did not reach relay.
    with client.websocket_connect('/relay/v1/streams',headers={'Authorization':'Bearer trusted'}) as ws:
        ws.send_json({'kind':'resume','lease_id':lease['lease_id'],'persisted_seq':'1'})
        ready=ws.receive_json()
        assert ready['kind']=='resume_ready' and ready['persisted_seq']=='0'
        assert ws.receive_json()['stream_seq']=='1'


def test_quota_rollback_and_room_registration_rejection(setup):
    m,s,g,lease,clock=setup
    open_window(s,g,lease,clock)
    s.max_bytes=1
    with pytest.raises(m.RelayError,match='RELAY_QUOTA_EXCEEDED'):
        s.ingest(g.app_id,g.room_id,[message(m)],b'raw',provider_test=True)
    s.max_bytes=1_073_741_824
    assert s.ingest(g.app_id,g.room_id,[message(m)],b'raw',provider_test=True)==['2']
    with pytest.raises(m.RelayError,match='ROOM_UNREGISTERED'):
        s.ingest(g.app_id,'unregistered',[message(m)],b'raw',provider_test=True)


def test_expired_lease_closes_before_rejected_connection(setup,env):
    m,s,g,lease,clock=setup
    open_window(s,g,lease,clock)
    clock[0]+=timedelta(seconds=16)
    with pytest.raises(m.RelayError,match='LEASE_EXPIRED'):
        s.connect(g.app_id,g.room_id,lease['lease_id'])
    with m.safe_connect(env['RELAY_DATABASE_URL']) as c:
        assert c.execute('SELECT state,reason FROM relay.relay_windows WHERE app_id=%s AND room_id=%s',(g.app_id,g.room_id)).fetchone()==('CLOSED','LEASE_EXPIRED')


def test_control_plane_is_required_and_device_scope_is_checked(setup):
    m,s,g,lease,clock=setup
    provider=m.SyntheticTestProvider(b'component-only-key')
    with pytest.raises(m.RelayError,match='TRUSTED_CONTROL_PLANE_REQUIRED'):
        m.create_app(s,provider=provider,test_mode=True)
    class OtherDevice:
        def verify(self,token): return m.VerifiedGrant(g.app_id,g.room_id,'different',g.session_id,'test_live',g.expires_at)
    client=TestClient(m.create_app(s,provider=provider,grant_verifier=OtherDevice(),test_mode=True))
    assert client.post('/relay/v1/windows/open',headers={'Authorization':'Bearer arbitrary'},json={'lease_id':lease['lease_id'],'round_id':'round-1','deadline_at':(clock[0]+timedelta(seconds=5)).isoformat()}).status_code==403


def test_full_quota_still_allows_safety_close(setup):
    m,s,g,lease,clock=setup
    open_window(s,g,lease,clock)
    s.max_bytes=1
    result=s.revoke(g.app_id,g.room_id,lease['lease_id'])
    assert result['status']=='revoked'


def test_old_epoch_cannot_close_a_new_window_with_same_version(setup):
    m,s,g,lease,clock=setup
    w1=open_window(s,g,lease,clock)
    s.close_window(g.app_id,g.room_id,lease['lease_id'],w1['version'],'VOID',expected_epoch=w1['epoch'])
    w2=s.open_window(g.app_id,g.room_id,lease['lease_id'],'round-2',clock[0]+timedelta(seconds=4))
    assert w2['version']==w1['version']
    with pytest.raises(m.RelayError,match='WINDOW_EPOCH_CONFLICT'):
        s.close_window(g.app_id,g.room_id,lease['lease_id'],w1['version'],'VOID',expected_epoch=w1['epoch'])


def test_rv1_game_deadline_is_independent_of_short_lease(setup,env):
    m,s,g,lease,clock=setup
    w=s.open_window(g.app_id,g.room_id,lease['lease_id'],'round-long',clock[0]+timedelta(seconds=600))
    assert w['deadline_at']==(clock[0]+timedelta(seconds=600)).isoformat()
    clock[0]+=timedelta(seconds=10)
    paused=s.pause_window(g.app_id,g.room_id,lease['lease_id'],w['version'],expected_epoch=w['epoch'])
    s.resume_window(g.app_id,g.room_id,lease['lease_id'],paused['version'],expected_epoch=w['epoch'])
    clock[0]+=timedelta(seconds=6)
    with pytest.raises(m.RelayError,match='LEASE_EXPIRED'):
        s.connect(g.app_id,g.room_id,lease['lease_id'])
    with m.safe_connect(env['RELAY_DATABASE_URL']) as c:
        assert c.execute('SELECT state,reason FROM relay.relay_windows WHERE app_id=%s AND room_id=%s',(g.app_id,g.room_id)).fetchone()==('CLOSED','LEASE_EXPIRED')


def test_rv2_revoked_grant_cannot_write_processed_or_ack(setup):
    m,s,g,lease,clock=setup
    open_window(s,g,lease,clock);s.ingest(g.app_id,g.room_id,[message(m)],b'raw',provider_test=True)
    class Revocable:
        revoked=False
        def verify(self,token):
            if self.revoked: raise m.RelayError('LIVE_AUTHORIZATION_REVOKED',403)
            return g
    verifier=Revocable()
    client=TestClient(m.create_app(s,provider=m.SyntheticTestProvider(b'component-only-key'),grant_verifier=verifier,test_mode=True))
    with client.websocket_connect('/relay/v1/streams',headers={'Authorization':'Bearer trusted'}) as ws:
        ws.send_json({'kind':'resume','lease_id':lease['lease_id'],'persisted_seq':'0'})
        assert ws.receive_json()['kind']=='resume_ready';ws.receive_json();ws.receive_json()
        ws.send_json({'kind':'transport.persisted','persisted_seq':'2'});assert ws.receive_json()['kind']=='transport.persisted.accepted'
        ws.send_json({'kind':'display.received','stream_seq':'2','receipt_id':'received'});assert ws.receive_json()['kind']=='delivery.stage.accepted'
        verifier.revoked=True
        ws.send_json({'kind':'display.processed','stream_seq':'2','receipt_id':'processed'})
        assert ws.receive_json()['reason_code']=='LIVE_AUTHORIZATION_REVOKED'
    assert s.stages(g.app_id,g.room_id,2)==['display.received','transport.persisted']


@pytest.mark.parametrize('binary',[False,True])
def test_rv3_oversized_ws_message_cannot_write_receipt(setup,binary):
    m,s,g,lease,clock=setup
    open_window(s,g,lease,clock);s.ingest(g.app_id,g.room_id,[message(m)],b'raw',provider_test=True)
    class Trusted:
        def verify(self,token): return g
    client=TestClient(m.create_app(s,provider=m.SyntheticTestProvider(b'component-only-key'),grant_verifier=Trusted(),test_mode=True))
    with client.websocket_connect('/relay/v1/streams',headers={'Authorization':'Bearer trusted'}) as ws:
        ws.send_json({'kind':'resume','lease_id':lease['lease_id'],'persisted_seq':'0'})
        ws.receive_json();ws.receive_json();ws.receive_json()
        ws.send_json({'kind':'transport.persisted','persisted_seq':'2'});ws.receive_json()
        raw=json.dumps({'kind':'display.received','stream_seq':'2','receipt_id':'字'*24000},ensure_ascii=False)
        if binary: ws.send_bytes(raw.encode())
        else: ws.send_text(raw)
        assert ws.receive_json()['reason_code']=='PAYLOAD_TOO_LARGE'
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect) as closed: ws.receive_json()
        assert closed.value.code==1009
    assert s.stages(g.app_id,g.room_id,2)==['transport.persisted']


def test_rv3_receipt_id_and_json_depth_are_bounded(setup):
    m,s,g,lease,clock=setup
    open_window(s,g,lease,clock);s.ingest(g.app_id,g.room_id,[message(m)],b'raw',provider_test=True)
    conn=s.connect(g.app_id,g.room_id,lease['lease_id']);s.frames(g.app_id,g.room_id,conn,0);s.ack(g.app_id,g.room_id,conn,2)
    with pytest.raises(m.RelayError,match='INPUT_INVALID'):
        s.record_stage(g.app_id,g.room_id,conn,2,'display.received','字'*100,client_time=clock[0].isoformat(timespec='milliseconds').replace('+00:00','Z'))
    assert s.stages(g.app_id,g.room_id,2)==['transport.persisted']


@pytest.mark.parametrize('terminal',['VOID','INTERRUPTED'])
def test_rv4_void_or_interrupted_cannot_gain_winner(setup,terminal):
    m,s,g,lease,clock=setup
    w=open_window(s,g,lease,clock);s.ingest(g.app_id,g.room_id,[message(m)],b'raw',provider_test=True)
    s.close_window(g.app_id,g.room_id,lease['lease_id'],w['version'],terminal,expected_epoch=w['epoch'])
    with pytest.raises(m.RelayError,match='WINDOW_TERMINAL_CONFLICT'):
        s.close_window(g.app_id,g.room_id,lease['lease_id'],w['version'],'SOLVED',expected_epoch=w['epoch'],winner_seq=2)


def test_rv4_solved_result_returns_same_winner_and_rejects_different(setup):
    m,s,g,lease,clock=setup
    w=open_window(s,g,lease,clock);s.ingest(g.app_id,g.room_id,[message(m),message(m,'m2')],b'raw',provider_test=True)
    first=s.close_window(g.app_id,g.room_id,lease['lease_id'],w['version'],'SOLVED',expected_epoch=w['epoch'],winner_seq=2)
    assert first==s.close_window(g.app_id,g.room_id,lease['lease_id'],w['version'],'SOLVED',expected_epoch=w['epoch'],winner_seq=2)
    with pytest.raises(m.RelayError,match='WINNER_CONFLICT'):
        s.close_window(g.app_id,g.room_id,lease['lease_id'],w['version'],'SOLVED',expected_epoch=w['epoch'],winner_seq=3)
    assert s.close_window(g.app_id,g.room_id,lease['lease_id'],w['version'],'TIMEOUT',expected_epoch=w['epoch'])['winner_seq']=='2'


def test_rv5_control_barrier_has_no_display_fulfillment(setup):
    m,s,g,lease,clock=setup
    open_window(s,g,lease,clock)
    conn=s.connect(g.app_id,g.room_id,lease['lease_id']);s.frames(g.app_id,g.room_id,conn,0);s.ack(g.app_id,g.room_id,conn,1)
    with pytest.raises(m.RelayError,match='DISPLAY_SOURCE_DELIVERY_REQUIRED'):
        s.record_stage(g.app_id,g.room_id,conn,1,'display.received','barrier-receipt',client_time=clock[0].isoformat(timespec='milliseconds').replace('+00:00','Z'))
    assert s.stages(g.app_id,g.room_id,1)==['transport.persisted']
    assert s.fulfill_pending()=={'unsupported':0,'completed':0}


def test_rv2_revoked_grant_cannot_advance_durable_ack(setup):
    m,s,g,lease,clock=setup
    open_window(s,g,lease,clock)
    class Revocable:
        revoked=False
        def verify(self,token):
            if self.revoked: raise m.RelayError('LIVE_AUTHORIZATION_REVOKED',403)
            return g
    verifier=Revocable()
    client=TestClient(m.create_app(s,provider=m.SyntheticTestProvider(b'component-only-key'),grant_verifier=verifier,test_mode=True))
    with client.websocket_connect('/relay/v1/streams',headers={'Authorization':'Bearer trusted'}) as ws:
        ws.send_json({'kind':'resume','lease_id':lease['lease_id'],'persisted_seq':'0'})
        ws.receive_json();ws.receive_json();verifier.revoked=True
        ws.send_json({'kind':'transport.persisted','persisted_seq':'1'})
        assert ws.receive_json()['reason_code']=='LIVE_AUTHORIZATION_REVOKED'
    assert s.stages(g.app_id,g.room_id,1)==[]
    new=s.connect(g.app_id,g.room_id,lease['lease_id'])
    assert new['persisted_seq']=='0'


def test_rv5_wrong_original_round_mapping_cannot_create_display_stage(setup,env):
    m,s,g,lease,clock=setup
    w=open_window(s,g,lease,clock);s.ingest(g.app_id,g.room_id,[message(m)],b'raw',provider_test=True)
    s.close_window(g.app_id,g.room_id,lease['lease_id'],w['version'],'VOID',expected_epoch=w['epoch'])
    s.open_window(g.app_id,g.room_id,lease['lease_id'],'round-2',clock[0]+timedelta(seconds=5))
    conn=s.connect(g.app_id,g.room_id,lease['lease_id']);s.frames(g.app_id,g.room_id,conn,0);s.ack(g.app_id,g.room_id,conn,4)
    with m.safe_connect(env['RELAY_MIGRATION_DATABASE_URL']) as c:
        c.execute('UPDATE relay.relay_windows SET round_id=%s WHERE app_id=%s AND room_id=%s AND epoch=%s',('wrong-original-round',g.app_id,g.room_id,int(w['epoch'])))
    with pytest.raises(m.RelayError,match='DISPLAY_SOURCE_DELIVERY_REQUIRED'):
        s.record_stage(g.app_id,g.room_id,conn,2,'display.received','wrong-round-receipt',client_time=clock[0].isoformat(timespec='milliseconds').replace('+00:00','Z'))


@pytest.mark.parametrize('terminal',['SOLVED','TIMEOUT'])
@pytest.mark.parametrize('received_before_close',[False,True])
def test_rv6_pending_source_receipts_continue_across_rounds(setup,terminal,received_before_close):
    m,s,g,lease,clock=setup
    w=open_window(s,g,lease,clock);s.ingest(g.app_id,g.room_id,[message(m)],b'raw',provider_test=True)
    conn=s.connect(g.app_id,g.room_id,lease['lease_id']);s.frames(g.app_id,g.room_id,conn,0);s.ack(g.app_id,g.room_id,conn,2)
    if received_before_close:
        s.record_stage(g.app_id,g.room_id,conn,2,'display.received','before-close',client_time=clock[0].isoformat(timespec='milliseconds').replace('+00:00','Z'))
    closed=s.close_window(g.app_id,g.room_id,lease['lease_id'],w['version'],terminal,expected_epoch=w['epoch'],winner_seq=2 if terminal=='SOLVED' else None)
    next_window=s.open_window(g.app_id,g.room_id,lease['lease_id'],'round-2',clock[0]+timedelta(seconds=5))
    assert next_window['epoch']!=w['epoch']
    if not received_before_close:
        s.record_stage(g.app_id,g.room_id,conn,2,'display.received','after-next-open',client_time=clock[0].isoformat(timespec='milliseconds').replace('+00:00','Z'))
    assert s.record_stage(g.app_id,g.room_id,conn,2,'display.processed','after-next-open-processed',client_time=clock[0].isoformat(timespec='milliseconds').replace('+00:00','Z'))['status']=='pending'
    assert s.stages(g.app_id,g.room_id,2)==['display.processed','display.received','transport.persisted']
    # A historical receipt only advances delivery evidence, never old round terminal/winner or current admission.
    assert s.close_window(g.app_id,g.room_id,lease['lease_id'],next_window['version'],'VOID',expected_epoch=next_window['epoch'])['winner_seq'] is None
    with s.tx() as c:
        old=c.execute('SELECT reason,winner_seq FROM relay.relay_windows WHERE app_id=%s AND room_id=%s AND epoch=%s',(g.app_id,g.room_id,int(w['epoch']))).fetchone()
    assert old=={'reason':closed['reason'],'winner_seq':2 if terminal=='SOLVED' else None}


@pytest.mark.parametrize('terminal',['VOID','INTERRUPTED'])
def test_rv6_received_effect_can_finish_after_historical_cancellation(setup,terminal):
    m,s,g,lease,clock=setup
    w=open_window(s,g,lease,clock);s.ingest(g.app_id,g.room_id,[message(m)],b'raw',provider_test=True)
    conn=s.connect(g.app_id,g.room_id,lease['lease_id']);s.frames(g.app_id,g.room_id,conn,0);s.ack(g.app_id,g.room_id,conn,2)
    s.record_stage(g.app_id,g.room_id,conn,2,'display.received','actually-received',client_time=clock[0].isoformat(timespec='milliseconds').replace('+00:00','Z'))
    s.close_window(g.app_id,g.room_id,lease['lease_id'],w['version'],terminal,expected_epoch=w['epoch'])
    s.open_window(g.app_id,g.room_id,lease['lease_id'],'round-2',clock[0]+timedelta(seconds=5))
    assert s.record_stage(g.app_id,g.room_id,conn,2,'display.processed','history-processed',client_time=clock[0].isoformat(timespec='milliseconds').replace('+00:00','Z'))['status']=='pending'


def test_rv6_new_owner_cannot_fulfill_old_owner_source(setup):
    m,s,g,lease,clock=setup
    w=open_window(s,g,lease,clock);s.ingest(g.app_id,g.room_id,[message(m)],b'raw',provider_test=True)
    s.close_window(g.app_id,g.room_id,lease['lease_id'],w['version'],'SOLVED',expected_epoch=w['epoch'],winner_seq=2)
    # The old owner persists transport evidence before the room changes hands.
    old=s.connect(g.app_id,g.room_id,lease['lease_id']);s.frames(g.app_id,g.room_id,old,0);s.ack(g.app_id,g.room_id,old,3)
    clock[0]+=timedelta(seconds=16)
    other=m.VerifiedGrant(g.app_id,g.room_id,'installation-2','session-2','test_live',g.expires_at)
    new=s.acquire(other)
    # A new installation/session must start a new scene explicitly; the previous
    # stream stays with its own session and cannot be replayed into this one.
    with pytest.raises(m.RelayError,match='STREAM_BOOTSTRAP_REQUIRED'):
        s.connect(g.app_id,g.room_id,new['lease_id'])
    s.bootstrap_stream(g.app_id,g.room_id,new['lease_id'],expected_stream_id=new['stream_id'],command_id='fresh-owner-scene',reason='EXPLICIT_FRESH_SESSION')
    conn=s.connect(g.app_id,g.room_id,new['lease_id'])
    s.frames(g.app_id,g.room_id,conn,3);s.ack(g.app_id,g.room_id,conn,4)
    with pytest.raises(m.RelayError,match='DISPLAY_SOURCE_DELIVERY_REQUIRED'):
        s.record_stage(g.app_id,g.room_id,conn,2,'display.received','wrong-owner',client_time=clock[0].isoformat(timespec='milliseconds').replace('+00:00','Z'))
    assert s.stages(g.app_id,g.room_id,2)==['transport.persisted']
