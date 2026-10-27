"""Owned PostgreSQL evidence lifecycle; generated keys are synthetic test material."""
import base64
import importlib
import importlib.util
import os
from pathlib import Path
from uuid import uuid4
from datetime import datetime,timedelta,timezone
import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from app.errors import DomainError
from app.modules.auth.evidence import canonicalize_jcs


def control_module():
    name='app.modules.operations.trusted_control'
    assert importlib.util.find_spec(name) is not None,'Missing persistent trusted control plane'
    return importlib.import_module(name)


def signed(key,payload,kind='live_grant'):
    obj=dict(key_id='synthetic-key',domain='wordheat-live.readiness',type=kind,version=1,payload=payload)
    return obj|{'signature':base64.urlsafe_b64encode(key.sign(canonicalize_jcs(obj))).rstrip(b'=').decode()}


@pytest.fixture
def database(postgres_isolation):
    from app.config import read_fields,Settings
    from app.infrastructure.db.database import Database
    values=read_fields(Path(os.environ['WORDHEAT_TEST_CONFIG']),{'WORDHEAT_TEST_DATABASE_URL'})
    db=Database(Settings(values['WORDHEAT_TEST_DATABASE_URL']))
    yield db
    db.engine.dispose()


def test_import_revision_and_revocation_survive_control_restart(database):
    module=control_module(); key=Ed25519PrivateKey.generate(); tag=uuid4().hex
    keys={'synthetic-key':dict(public_key=key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw),domains=['wordheat-live.readiness'],types=['live_grant','revocation'],test_only=True)}
    now=datetime.now(timezone.utc)
    payload=dict(object_id=tag,issuer_id=tag,issuer_revision=1,authorization_id=tag,kind='controlled_beta',allowed_profiles=['beta'],app_id='synthetic-app',room_binding_id='synthetic-room',installation_id='wordheat-local-development',candidate_digest='a'*64,not_before=(now-timedelta(seconds=1)).isoformat(),expires_at=(now+timedelta(hours=1)).isoformat(),max_session_duration_ms=10000)
    first=module.TrustedControl(database,keys)
    with database.engine.begin() as c:
        assert first.import_envelope(c,signed(key,payload))['imported'] is True
        assert first.import_envelope(c,signed(key,payload))['imported'] is False
    second=module.TrustedControl(database,keys)
    with database.engine.begin() as c:
        assert second.get_grant(c,tag).payload['authorization_id']==tag
        changed=payload|{'candidate_digest':'b'*64}
        with pytest.raises(DomainError): second.import_envelope(c,signed(key,changed))
    with database.engine.begin() as c:
        rev=dict(object_id='rev-'+tag,issuer_id=tag,issuer_revision=2,revocation_revision=1,revoked_object_ids=[tag])
        second.import_envelope(c,signed(key,rev,'revocation'))
    with database.engine.begin() as c:
        with pytest.raises(DomainError,match='revoked'): module.TrustedControl(database,keys).get_grant(c,tag)


def test_unsigned_import_and_synthetic_gate_cannot_authorize_live(database):
    module=control_module();key=Ed25519PrivateKey.generate()
    keys={'synthetic-key':dict(public_key=key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw),domains=['wordheat-live.readiness'],types=['gate_evidence'],test_only=True)}
    control=module.TrustedControl(database,keys)
    with database.engine.begin() as c:
        with pytest.raises(DomainError):control.import_envelope(c,{'status':'pass','gate':'G0'})
        payload=dict(object_id=uuid4().hex,issuer_id=uuid4().hex,issuer_revision=1,gate='G0',result='pass',provider_test=True)
        with pytest.raises(DomainError):control.import_envelope(c,signed(key,payload,'gate_evidence'))


def test_first_start_anchor_is_persistent_once_and_caps_immutable_runtime(database):
    from types import SimpleNamespace
    from app.modules.auth.evidence import RuntimeAuthorization,NonceRegistry
    from app.modules.connectors import models as m
    module=control_module();sid=uuid4().hex;clock=[1000000000]
    runtime=RuntimeAuthorization('g','a','c','i','boot','nonce',0,100000000000,lambda:clock[0],NonceRegistry('boot','nonce'))
    class IsolatedBudgetControl(module.TrustedControl):
        def require_current(self,c,session):return self._runtime.get(sid,runtime)
        def get_grant(self,c,authorization_id):return SimpleNamespace(payload={'max_session_duration_ms':10000})
    control=IsolatedBudgetControl(database,boot_id='boot',now_ns=lambda:clock[0]);session={'id':sid,'authorization_id':'a'}
    with database.engine.begin() as c:
        c.execute(m.bindings.insert().values(id=sid,session_id=sid,connector_id='synthetic',state='CONNECTED',boot_id='boot'))
        assert control.mark_started(c,session)==1000000000
    clock[0]+=1000000000
    with database.engine.begin() as c:
        assert control.mark_started(c,session)==1000000000
        assert c.execute(sa.select(m.bindings.c.authorization_deadline_ns).where(m.bindings.c.session_id==sid)).scalar_one()==11000000000


def test_root_signed_key_rotation_rebuilds_across_boot_and_replay_cannot_unrevoke(database):
    module=control_module();root=Ed25519PrivateKey.generate();child=Ed25519PrivateKey.generate();tag=uuid4().hex
    keys={'root':{'public_key':root.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw),'domains':['wordheat-live.trust'],'types':['key_bundle']}}
    control=module.TrustedControl(database,keys)
    assert hasattr(control,'import_key_bundle'),'Missing persistent root-signed key rotation'
    def package(revision,revoked=()):
        payload=dict(object_id=tag+str(revision),issuer_id=tag,issuer_revision=revision,keys=[dict(key_id='child-'+tag,public_key_b64url=base64.urlsafe_b64encode(child.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).rstrip(b'=').decode(),domains=['wordheat-live.readiness'],types=['live_grant'])] if revision==1 else [],revoked_key_ids=list(revoked))
        value=dict(key_id='root',domain='wordheat-live.trust',type='key_bundle',version=1,payload=payload)
        return value|{'signature':base64.urlsafe_b64encode(root.sign(canonicalize_jcs(value))).rstrip(b'=').decode()}
    first=package(1)
    with database.engine.begin() as c:
        result=control.import_key_bundle(c,first,native_command_id='native-synthetic',confirmation_id='confirmed-synthetic')
        assert result.key_ids==('child-'+tag,)
    rebooted=module.TrustedControl(database,keys)
    with database.engine.begin() as c:
        assert rebooted.effective_keys(c)['child-'+tag]['revoked'] is False
        rebooted.import_key_bundle(c,package(2,['child-'+tag]),native_command_id='native-revoke',confirmation_id='confirm-revoke')
    with database.engine.begin() as c:
        rebooted.import_key_bundle(c,first,native_command_id='replay',confirmation_id='replay-confirm')
        assert rebooted.effective_keys(c)['child-'+tag]['revoked'] is True


def test_room_binding_id_cannot_change_immutable_tuple_and_same_scope_can_renew(database):
    from dataclasses import replace
    module=control_module();control=module.TrustedControl(database);tag=uuid4().hex;now=datetime.now(timezone.utc)
    fact=module.VerifiedRoomBinding(tag,'synthetic-room-app','synthetic-room',database.settings.instance_id,'synthetic-registered',('synthetic-actor',),now+timedelta(minutes=1),'synthetic-contract-proof',True)
    with database.engine.begin() as c:control.register_room_binding(c,fact)
    with database.engine.begin() as c:
        for changed in ({'room_id':'different'},{'app_id':'other'},{'installation_id':'wordheat-other'},{'connector_id':'unregistered'}):
            with pytest.raises(DomainError,match='new binding'):control.register_room_binding(c,replace(fact,**changed))
        control.register_room_binding(c,replace(fact,expires_at=now+timedelta(minutes=2),verification_id='synthetic-renewed-proof'))
    from app.modules.connectors import models as m
    with database.engine.begin() as c:
        actual=c.execute(sa.select(m.room_bindings).where(m.room_bindings.c.id==tag)).mappings().one()
        assert actual['room_id']=='synthetic-room' and actual['authorization']['verification_id']=='synthetic-renewed-proof'
        with pytest.raises(sa.exc.DBAPIError):
            with c.begin_nested():c.execute(m.room_bindings.update().where(m.room_bindings.c.id==tag).values(room_id='raw-sql-rebind'))
