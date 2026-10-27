"""Synthetic native contexts; actual Unix peer validation is runtime-owned."""
import importlib,importlib.util
from pathlib import Path
import os
import pytest
import sqlalchemy as sa
from app.infrastructure.db import schema as s
from app.modules.sessions.service import LocalService
from app.modules.auth.service import bootstrap_operator
from test_trusted_control import database


def test_confirmed_device_key_is_atomic_private_and_never_overwrites(database,tmp_path):
    name='app.modules.connectors.native_pairing_bridge'
    assert importlib.util.find_spec(name) is not None,'Missing actual native device/pairing bridge'
    module=importlib.import_module(name);core=LocalService(database)
    with database.engine.begin() as c:
        actor=c.execute(sa.select(s.operators.c.id)).scalar_one_or_none()
        if actor is None:actor=bootstrap_operator(c,'native-synthetic','synthetic-password-123','Native test')
    env=tmp_path/'.env';env.write_text('UNRELATED_FIELD=preserve-exactly\n');env.chmod(0o600)
    bridge=module.NativePairingBridge(core,env_path=env,control_root=tmp_path,pairing_client=None)
    trusted=object();bridge.bind_native_validator(lambda ctx,aid:ctx is trusted and aid==actor)
    with pytest.raises(Exception):bridge.prepare_device_key(actor,native_context={})
    review=bridge.prepare_device_key(actor,native_context=trusted)
    done=bridge.confirm_device_key(actor,review['command_id'],review['fingerprint'],review['confirmation_nonce'],review['installation_id'],native_context=trusted)
    content=env.read_text();assert content.startswith('UNRELATED_FIELD=preserve-exactly\n')
    assert content.count('LOCAL_DEVICE_PRIVATE_KEY_B64URL=')==1 and os.stat(env).st_mode&0o777==0o600
    repeated=bridge.confirm_device_key(actor,review['command_id'],review['fingerprint'],review['confirmation_nonce'],review['installation_id'],native_context=trusted)
    assert repeated==done and env.read_text()==content and 'private_key' not in str(done).lower()
