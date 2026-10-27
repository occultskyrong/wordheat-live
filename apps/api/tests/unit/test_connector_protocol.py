"""Synthetic protocol examples; these cannot establish official readiness."""
import base64
import hashlib
import importlib
import importlib.util
import json
import pytest
from app.errors import DomainError


def protocol():
    name='app.modules.connectors.protocol'
    assert importlib.util.find_spec(name) is not None, 'Missing durable connector protocol implementation'
    return importlib.import_module(name)


def frame(body):
    raw=json.dumps(body,separators=(',',':')).encode()
    return dict(kind='relay.frame',stream_id='stream',stream_seq=body['stream_seq'],body_b64=base64.b64encode(raw).decode(),immutable_sha256=hashlib.sha256(raw).hexdigest(),lease_id='lease',installation_id='install',delivery_generation='3',connection_nonce='nonce',persisted_seq='0')


def body():
    return dict(schema_version=1,contract_version='LW-C1/1.0.0',stream_id='stream',stream_seq='2',original_generation='1',round_id='round',window_epoch='1',relay_received_at='2026-10-26T00:00:00Z',type='comment',eligible=True,provider_test=True,run_purpose='test_live',source_mode='synthetic',payload={'source_msg_id':'msg','content':{'text':'演练'}})


def test_registered_target_rejects_arbitrary_url_and_accepts_explicit_loopback_test():
    p=protocol()
    assert p.registered_target('test',{'test':{'url':'ws://127.0.0.1:9876/relay/v1/streams','test_only':True}},test_mode=True)['test_only'] is True
    for name,targets in [('ws://attacker.invalid',{}),('x',{'x':{'url':'ws://attacker.invalid/relay/v1/streams','test_only':True}})]:
        with pytest.raises(DomainError): p.registered_target(name,targets,test_mode=True)


def test_decode_preserves_original_owner_and_rejects_wrong_connection_hash_or_sequence():
    p=protocol(); f=frame(body())
    expected={k:f[k] for k in ('lease_id','installation_id','delivery_generation','connection_nonce')}
    assert p.decode_delivery(f,expected)['original_generation']=='1'
    for changed in [dict(f,connection_nonce='old'),dict(f,immutable_sha256='0'*64),dict(f,stream_seq='3')]:
        with pytest.raises(DomainError): p.decode_delivery(changed,expected)


def test_decode_rejects_duplicate_keys_and_non_decimal_cursor():
    p=protocol();f=frame(body());raw=b'{"stream_seq":"2","stream_seq":"3"}'
    f.update(body_b64=base64.b64encode(raw).decode(),immutable_sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(DomainError): p.decode_delivery(f,{k:f[k] for k in ('lease_id','installation_id','delivery_generation','connection_nonce')})
    for value in (1,'01','-1',True):
        with pytest.raises(DomainError):p.cursor(value)
