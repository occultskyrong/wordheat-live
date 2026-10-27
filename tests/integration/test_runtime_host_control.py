"""Actual owned-process stop protocol; synthetic children are not model acceptance."""
import importlib.util
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[2]
def host():
    spec=importlib.util.spec_from_file_location('runtime_host',ROOT/'scripts/backend/20261025-run-local.py')
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value

def test_stop_intent_is_authenticated_to_api_boot_pid_and_challenge():
    value=host()
    assert hasattr(value,'make_stop_intent'),'private API stop intent protocol missing'
    identity={'pid':123,'role':'api','boot_id':'b'*64,'manifest_sha256':'c'*64,'challenge':'n'*64,'endpoint':'127.0.0.1:8765'}
    packet=value.make_stop_intent(b'k'*32,identity,'command-id','nonce')
    assert value.verify_stop_intent(b'k'*32,packet,identity)
    for key in identity:
        assert not value.verify_stop_intent(b'k'*32,packet,identity|{key:'wrong'})
    assert not value.verify_stop_intent(b'x'*32,packet,identity)
    assert not value.verify_stop_intent(b'k'*32,packet|{'intent':'maintenance'},identity)

def test_control_client_needs_inherited_private_channel():
    value=host()
    assert hasattr(value,'ApiControlClient'),'API authenticated stop channel missing'
    with pytest.raises(value.BootstrapError): value.ApiControlClient.from_boot({'identity':{'role':'api'},'secret':b'x'*32})
