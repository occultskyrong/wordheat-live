import importlib.util
from pathlib import Path
import sys
import pytest

ROOT=Path(__file__).resolve().parents[1]


def load(name):
    spec=importlib.util.spec_from_file_location('relay_unit_'+name,ROOT/'app'/f'{name}.py')
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    return module


def test_config_repr_hides_database_url():
    m=load('config')
    settings=m.RelaySettings.from_mapping({'RELAY_DATABASE_URL':'postgresql://user:synthetic-secret@127.0.0.1/wordheat_relay_test','RELAY_DATABASE_NAME':'wordheat_relay_test','RELAY_DATABASE_MARKER':'wordheat-live:test:relay','WORDHEAT_ENV':'test'})
    assert 'synthetic-secret' not in repr(settings)
    assert 'postgresql' not in repr(settings)


def test_config_missing_fields_and_production_synthetic_fail_closed():
    m=load('config')
    with pytest.raises(ValueError,match='CONFIG_REQUIRED:RELAY_DATABASE_URL'):
        m.RelaySettings.from_mapping({})
    with pytest.raises(ValueError,match='SYNTHETIC_PROVIDER_REQUIRES_TEST_ENV'):
        m.RelaySettings.from_mapping({'RELAY_DATABASE_URL':'postgresql://user:synthetic@localhost/relay','RELAY_DATABASE_NAME':'relay','RELAY_DATABASE_MARKER':'wordheat:relay','WORDHEAT_ENV':'production','RELAY_SYNTHETIC_TEST_ENABLED':'true'})


def test_synthetic_provider_is_not_client_selectable_for_production():
    m=load('core')
    with pytest.raises(m.RelayError,match='TEST_PROVIDER_FORBIDDEN'):
        m.create_app(None,provider=m.SyntheticTestProvider(b'component-only-key'))
    p=m.SyntheticTestProvider(b'component-only-key')
    raw=b'{"provider_test":false,"run_purpose":"production"}'
    with pytest.raises(m.RelayError,match='INPUT_INVALID'):
        p.verify_and_decode(raw,{'x-synthetic-signature':p.sign(raw)})


def test_connection_query_cannot_override_validated_database_identity():
    m=load('config')
    for suffix in ('?dbname=other','?host=elsewhere','?options=-csearch_path=public','?','#fragment'):
        with pytest.raises(ValueError,match='CONFIG_INVALID:RELAY_DATABASE_URL'):
            m.RelaySettings.from_mapping({'RELAY_DATABASE_URL':'postgresql://user:synthetic@localhost/relay'+suffix,'RELAY_DATABASE_NAME':'relay','RELAY_DATABASE_MARKER':'wordheat:relay','WORDHEAT_ENV':'test'})
    core=load('core')
    with pytest.raises(core.RelayError,match='DATABASE_URL_INVALID'):
        core.dsn('postgresql://user:synthetic@localhost/relay?dbname=other')


def test_wrong_database_name_rejected_before_any_connection(monkeypatch):
    m=load('core')
    def forbidden(*args,**kwargs):
        raise AssertionError('must not connect to a different database')
    monkeypatch.setattr(m.psycopg,'connect',forbidden)
    with pytest.raises(m.RelayError,match='DATABASE_IDENTITY_MISMATCH'):
        m.RelayService('postgresql://user:synthetic@localhost/other_database')
    with pytest.raises(m.RelayError,match='DATABASE_IDENTITY_MISMATCH'):
        m.migrate('postgresql://user:synthetic@localhost/other_database')


def test_connect_errors_never_expose_driver_payload(monkeypatch):
    m=load('core')
    def failed(*args,**kwargs): raise m.psycopg.ProgrammingError('synthetic-secret-do-not-print')
    monkeypatch.setattr(m.psycopg,'connect',failed)
    with pytest.raises(m.RelayError) as caught:
        with m.safe_connect('postgresql+psycopg://user:synthetic@localhost/wordheat_relay_test'): pass
    assert str(caught.value)=='DATABASE_UNAVAILABLE'
    assert caught.value.__suppress_context__


def test_low_level_factory_also_requires_trusted_verifier():
    m=load('core')
    class ProductionProvider:
        provider_test=False
    with pytest.raises(m.RelayError,match='TRUSTED_CONTROL_PLANE_REQUIRED'):
        m.create_app(None,provider=ProductionProvider())


def test_bounded_json_rejects_depth_and_collection_expansion():
    m=load('core')
    with pytest.raises(m.RelayError,match='INPUT_INVALID'):
        m.bounded_json('{"nested":'+('['*20)+'0'+(']'*20)+'}')
    with pytest.raises(m.RelayError,match='INPUT_INVALID'):
        m.bounded_json('{"batch":['+','.join('0' for _ in range(1001))+']}')
    with pytest.raises(m.RelayError,match='PAYLOAD_TOO_LARGE'):
        m.bounded_json('{"text":"'+'字'*22000+'"}')


def test_migration_connect_errors_are_sanitized(monkeypatch):
    m=load('core')
    def failed(*args,**kwargs): raise m.psycopg.ProgrammingError('synthetic-secret-do-not-print')
    monkeypatch.setattr(m.psycopg,'connect',failed)
    with pytest.raises(m.RelayError) as caught:
        m.migrate('postgresql+psycopg://user:synthetic@localhost/wordheat_relay_test')
    assert str(caught.value)=='DATABASE_UNAVAILABLE' and caught.value.__suppress_context__


def test_direct_settings_cannot_enable_synthetic_production():
    m=load('config')
    with pytest.raises(ValueError,match='SYNTHETIC_PROVIDER_REQUIRES_TEST_ENV'):
        m.RelaySettings('postgresql://user:synthetic@localhost/relay','relay','wordheat:relay','production',True)
