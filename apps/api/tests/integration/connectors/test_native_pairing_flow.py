"""Native bridge to real loopback relay; upstream SDK is MockTransport only."""
import base64,hashlib,json,os,socket,subprocess,time
from pathlib import Path
from datetime import datetime,timedelta,timezone
from uuid import uuid4
import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from app.infrastructure.db import schema as s
from app.modules.auth.service import bootstrap_operator
from app.modules.auth.evidence import canonicalize_jcs
from app.modules.sessions.service import LocalService
from app.modules.operations.trusted_control import TrustedControl
from app.modules.connectors.pairing import NativePairingClient
from app.modules.connectors.native_pairing_bridge import NativePairingBridge
from app.modules.connectors import models as m
from app.errors import DomainError
from test_trusted_control import database


def test_native_entry_rejects_no_management_permission_and_records_verified_binding(database,tmp_path):
    # The relay harness uses its own interpreter/app namespace and owned schema.
    relay_app=Path(os.environ['WORDHEAT_RELAY_TEST_APP']) if os.environ.get('WORDHEAT_RELAY_TEST_APP') else Path(__file__).resolve().parents[4]/'relay'
    harness=relay_app/'tests/native_pairing_server.py'
    assert harness.exists(),'Current relay pairing harness required'
    core=LocalService(database);issuer=Ed25519PrivateKey.generate();key_id='synthetic-pair-manager';candidate='0'*64
    public=issuer.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
    roots={key_id:{'public_key':public,'domains':['wordheat-live.readiness'],'types':['pairing_intent'],'test_only':True}}
    trust=TrustedControl(database,roots,candidate_digest=candidate,boot_id=core.boot_id)
    with database.engine.begin() as c:
        actor=c.execute(sa.select(s.operators.c.id)).scalar_one_or_none()
        if actor is None:actor=bootstrap_operator(c,'native-pair-flow','synthetic-password-123','Synthetic operator')
    env=tmp_path/'.env';env.write_text('UNRELATED_FIELD=preserved\n');env.chmod(0o600)
    bridge=NativePairingBridge(core,env_path=env,control_root=tmp_path)
    context=object();bridge.bind_native_validator(lambda value,aid:value is context and aid==actor)
    review=bridge.prepare_device_key(actor,native_context=context)
    identity=bridge.confirm_device_key(actor,review['command_id'],review['fingerprint'],review['confirmation_nonce'],review['installation_id'],native_context=context)
    room='synthetic-pair-room';binding=uuid4().hex;now=datetime.now(timezone.utc)
    payload=dict(object_id=uuid4().hex,issuer_id=uuid4().hex,issuer_revision=1,actor_id=actor,app_id='synthetic-pair-app',room_id=room,room_binding_id=binding,installation_id=database.settings.instance_id,candidate_digest=candidate,device_key_sha256=identity['device_key_sha256'],anchor_open_id_sha256=hashlib.sha256(b'synthetic-anchor').hexdigest(),not_before=(now-timedelta(seconds=1)).isoformat(),expires_at=(now+timedelta(minutes=10)).isoformat(),allowed_actions=['pair_device'])
    package=dict(key_id=key_id,domain='wordheat-live.readiness',type='pairing_intent',version=1,payload=payload);package['signature']=base64.urlsafe_b64encode(issuer.sign(canonicalize_jcs(package))).rstrip(b'=').decode()
    source=tmp_path/'pair-intent.json';source.write_text(json.dumps(package));source.chmod(0o600)
    root_file=tmp_path/'public-root.json';root_file.write_text(json.dumps({'key_id':key_id,'public_key':base64.urlsafe_b64encode(public).rstrip(b'=').decode(),'candidate':candidate,'room_id':room}));root_file.chmod(0o600)
    interpreter=relay_app/'.venv/bin/python'
    child=subprocess.Popen([str(interpreter),'-B',str(harness),str(root_file)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=dict(os.environ,RELAY_TEST_ENV_FILE='/Users/jiachenclaw/code/wordheat-live/deploy/public-relay/.env',PYTHONPATH=str(harness.parents[1])))
    try:
        ready=json.loads(child.stdout.readline());assert ready.get('port')
        registry={'synthetic':{'url':f"ws://127.0.0.1:{ready['port']}/relay/v1/streams",'test_only':True}}
        paired=[];client=NativePairingClient(core,trust,registry,test_mode=True,on_paired=paired.append);bridge.client=client;client.authorize=bridge._permit
        proposal=bridge.prepare_pair(actor,'pair-intent',native_context=context)
        with pytest.raises(DomainError):bridge.confirm_pair(actor,proposal['command_id'],proposal['fingerprint'],proposal['confirmation_nonce'],proposal['installation_id'],'synthetic-launch',native_context={})
        result=bridge.confirm_pair(actor,proposal['command_id'],proposal['fingerprint'],proposal['confirmation_nonce'],proposal['installation_id'],'synthetic-launch',native_context=context)
        assert result['status']=='paired' and paired and 'token' not in str(result).lower()
        assert 'LOCAL_DEVICE_PAIRING_ID=' in env.read_text() and 'synthetic-launch' not in env.read_text()
        with database.engine.begin() as c:
            actual=c.execute(sa.select(m.room_bindings).where(m.room_bindings.c.id==binding)).mappings().one()
            assert actual['room_id']==room and actual['authorization']['provider_test'] is True
            with pytest.raises(DomainError):trust.authorize_create(c,actor,{'mode':'official','run_purpose':'test_live','live_profile':'beta','room_binding_id':binding})
    finally:
        if child.stdin:child.stdin.write('stop\n');child.stdin.flush()
        child.wait(timeout=10)
        assert child.returncode==0,child.stderr.read()[-1000:]
