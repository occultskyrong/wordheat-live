import importlib,importlib.util
import json,os
from pathlib import Path
import pytest


def config_module():
    assert importlib.util.find_spec('app.server_config') is not None,'Missing fixed private relay configuration loader'
    return importlib.import_module('app.server_config')


def root(tmp_path,text):
    p=tmp_path/'private';p.mkdir();(p/'.env').write_text(text);(p/'.env').chmod(0o600)
    return p


def test_fixed_root_ignores_local_env_and_separates_migration_identity(tmp_path):
    m=config_module();p=root(tmp_path,'RELAY_DATABASE_URL=postgresql://runtime:synthetic@127.0.0.1/wordheat_relay_test\nRELAY_DATABASE_NAME=wordheat_relay_test\nRELAY_DATABASE_MARKER=wordheat-live:test:relay\nWORDHEAT_ENV=test\nRELAY_MIGRATION_DATABASE_URL=postgresql://migration:synthetic@127.0.0.1/wordheat_relay_test\nRELAY_RUNTIME_ROLE=runtime\nLOCAL_DATABASE_URL=should-not-load\n')
    (tmp_path/'.env').write_text('trap local configuration');(p/'local.env').write_text('trap local env')
    runtime=m.load_runtime(p);migration=m.load_migration(p)
    assert 'migration' not in repr(runtime) and 'should-not-load' not in repr(runtime)
    assert runtime.settings.database_name=='wordheat_relay_test' and migration.runtime_role=='runtime'
    assert runtime.roots=={} and runtime.candidates.allowed==frozenset()


def test_owned_private_files_no_symlink_and_no_short_token_configuration(tmp_path):
    m=config_module();p=root(tmp_path,'ACCESS_TOKEN=synthetic-secret-must-not-escape\n')
    with pytest.raises(Exception) as error:m.load_runtime(p)
    assert 'synthetic-secret' not in str(error.value)
    (p/'.env').chmod(0o644)
    with pytest.raises(Exception):m.load_runtime(p)
    (p/'.env').chmod(0o600);link=tmp_path/'link';link.symlink_to(p,target_is_directory=True)
    with pytest.raises(Exception):m.load_runtime(link)


def test_cli_has_safe_errors_and_no_secret_argument_surface(capsys):
    assert importlib.util.find_spec('app.cli') is not None,'Missing standard relay CLI'
    cli=importlib.import_module('app.cli')
    assert cli.main(['serve','--access-token','synthetic-cli-secret'])==2
    captured=capsys.readouterr();assert 'synthetic-cli-secret' not in captured.out+captured.err


def runtime_text():
    return 'RELAY_DATABASE_URL=postgresql://runtime:synthetic-runtime@127.0.0.1/wordheat_relay_test\nRELAY_DATABASE_NAME=wordheat_relay_test\nRELAY_DATABASE_MARKER=wordheat-live:test:relay\nWORDHEAT_ENV=test\n'


def encoded(raw):
    import base64
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def test_cached_allowlist_and_purpose_scoped_freshness_signer(tmp_path):
    m=config_module()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
    seed=bytes(range(32));pub=Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
    p=root(tmp_path,runtime_text()+'RELAY_TRUST_ROOTS_FILE=roots.json\nRELAY_ALLOWED_CANDIDATES_FILE=candidates.json\nRELAY_FRESHNESS_KEY_ID=fresh\nRELAY_FRESHNESS_PRIVATE_KEY_B64URL='+encoded(seed)+'\n')
    roots={'version':1,'keys':[{'key_id':'fresh','public_key_b64url':encoded(pub),'domains':['wordheat-live.readiness'],'types':['freshness_proof'],'test_only':False}]}
    (p/'roots.json').write_text(json.dumps(roots));(p/'roots.json').chmod(0o600)
    (p/'candidates.json').write_text(json.dumps({'version':1,'allowed_candidate_digests':['a'*64]}));(p/'candidates.json').chmod(0o600)
    cfg=m.load_runtime(p)
    assert cfg.candidates.permits('a'*64) and encoded(seed) not in repr(cfg)
    from app.composition import ConfiguredControlPlane
    from app.control import verify_envelope
    plane=ConfiguredControlPlane(object(),cfg)
    signed=plane.signer.sign({'server_time':'2026-10-26T00:00:00Z','ttl_ms':10000})
    assert verify_envelope(signed,cfg.roots,expected_type='freshness_proof')['key_id']=='fresh'
    (p/'candidates.json').write_text('invalid-after-start')
    assert cfg.candidates.permits('a'*64) and not cfg.candidates.permits('b'*64)
    # A server freshness private key must never gain grant or management power.
    (p/'candidates.json').write_text(json.dumps({'version':1,'allowed_candidate_digests':['a'*64]}))
    roots['keys'][0]['types'].append('live_grant');(p/'roots.json').write_text(json.dumps(roots))
    with pytest.raises(ValueError,match='CONFIG_SIGNER_PURPOSE_INVALID'):m.load_runtime(p)


def test_public_roots_unknown_duplicate_and_symlink_are_rejected(tmp_path):
    m=config_module();p=root(tmp_path,runtime_text()+'RELAY_TRUST_ROOTS_FILE=roots.json\n')
    row={'key_id':'authority','public_key_b64url':encoded(bytes(32)),'domains':['wordheat-live.readiness'],'types':['unknown_authority'],'test_only':False}
    f=p/'roots.json';f.write_text(json.dumps({'version':1,'keys':[row]}));f.chmod(0o600)
    with pytest.raises(ValueError,match='CONFIG_KEY_PURPOSE_INVALID'):m.load_runtime(p)
    row['types']=['live_grant'];f.write_text(json.dumps({'version':1,'keys':[row,row]}))
    with pytest.raises(ValueError,match='CONFIG_ROOTS_INVALID'):m.load_runtime(p)
    f.unlink();f.symlink_to(p/'.env')
    with pytest.raises(ValueError):m.load_runtime(p)


def test_allowlist_cannot_create_pairing_or_grant_authority(tmp_path):
    from app.composition import ConfiguredControlPlane
    from app.core import RelayError
    from app.server_config import RuntimeConfig,AllowedCandidates
    from app.config import RelaySettings
    from contextlib import contextmanager
    class Connection:
        def execute(self,*args):self.query=args[0];return self
        def fetchall(self):return [] if 'key_bundles' in self.query else [{'digest':'a'*64}]
    class Service:
        @contextmanager
        def tx(self):yield Connection()
    config=RuntimeConfig(RelaySettings.from_mapping(dict(RELAY_DATABASE_URL='postgresql://r:p@localhost/wordheat_relay_test',RELAY_DATABASE_NAME='wordheat_relay_test',RELAY_DATABASE_MARKER='wordheat-live:test:relay',WORDHEAT_ENV='test')),{},AllowedCandidates(frozenset({'a'*64})))
    plane=ConfiguredControlPlane(Service(),config)
    # Cached allowlist does not let an unsigned, self-declared management intent
    # pass the inherited trusted-key signature verifier.
    request=encoded(json.dumps({'domain':'wordheat-live.readiness','type':'pairing_intent','version':1,'key_id':'untrusted','payload':{'candidate_digest':'a'*64},'signature':'invalid'}).encode())
    with pytest.raises(RelayError):plane.management.verify(request)
    assert plane._candidate.get() is None and plane.signer is None and plane.keys=={}
