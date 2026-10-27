"""All keys/rooms in this module are synthetic; no official gate is established."""
import importlib
import importlib.util
from datetime import datetime,timedelta,timezone
import base64
import pytest
import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat


def control():
    name='app.control'
    assert importlib.util.find_spec(name) is not None,'Missing relay trusted management and task control plane'
    return importlib.import_module(name)


def test_signed_envelope_rejects_duplicate_keys_and_unsigned_grant():
    module=control()
    with pytest.raises(Exception):module.verify_envelope('{"key_id":"a","key_id":"b"}',{})
    with pytest.raises(Exception):module.verify_envelope({'status':'pass'}, {})


def test_verify_ed25519_scope_and_sign_freshness_are_exact_internal_protocol():
    module=control();key=Ed25519PrivateKey.generate()
    keys={'synthetic':{'public_key':key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw),'types':['freshness_proof'],'domains':['wordheat-live.readiness']}}
    signer=module.FreshnessSigner('synthetic',key)
    payload=dict(authorization_id='a',candidate_digest='c',installation_id='i',boot_id='b',connection_nonce='cn',request_nonce='r',revocation_revision=0,server_time=datetime.now(timezone.utc).isoformat(),ttl_ms=1000)
    envelope=signer.sign(payload)
    assert module.verify_envelope(envelope,keys,expected_type='freshness_proof')['payload']==payload
    with pytest.raises(Exception):module.verify_envelope(envelope|{'payload':payload|{'installation_id':'other'}},keys)
