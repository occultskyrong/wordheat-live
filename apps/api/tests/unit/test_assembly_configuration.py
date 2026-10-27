from pathlib import Path
import pytest
from app.config import read_fields,load_settings


def private_env(tmp_path,text):
    path=tmp_path/'.env';path.write_text(text);path.chmod(0o600);return path

@pytest.mark.parametrize('text',[
    'LOCAL_DATABASE_URL=a\nLOCAL_DATABASE_URL=b\n',
    'UNKNOWN_CONFIG=must-not-appear-in-errors\n',
])
def test_root_configuration_rejects_duplicate_or_unknown_names(tmp_path,text):
    path=private_env(tmp_path,text)
    with pytest.raises(ValueError) as error:read_fields(path,{'LOCAL_DATABASE_URL'})
    assert 'must-not-appear' not in str(error.value)

def test_loader_keeps_official_configuration_optional_and_roots_explicit(tmp_path):
    private_env(tmp_path,'WORDHEAT_ENV=development\nWORDHEAT_INSTANCE_ID=wordheat-component-fixture\nLOCAL_DATABASE_URL=postgresql://user:sentinel-secret@127.0.0.1/wordheat_test\nLOCAL_ANALYTICS_CURSOR_KEY='+('a'*64)+'\n')
    source=Path(__file__).resolve().parents[4]
    settings=load_settings(tmp_path,source_root=source)
    assert settings.server_root==tmp_path.resolve()
    assert settings.native_root==tmp_path/'.local/runtime'
    assert settings.maintenance_root==tmp_path/'.local/maintenance'
    assert settings.environment=='development'
    assert settings.gate_trust_roots_file is None and settings.connector_registry_file is None
    assert not settings.native_root.exists(), 'loading configuration must not create runtime directories'
    assert 'sentinel-secret' not in repr(settings)

def test_candidate_file_hash_cannot_be_published_with_a_changed_file_identity(tmp_path,monkeypatch):
    import hashlib
    from app.infrastructure import runtime_configuration as cfg
    from app.errors import DomainError
    path=tmp_path/'artifact.bin';path.write_bytes(b'good')
    original=cfg.sha_file
    def changed_after_hash(value):
        result=original(value);value.write_bytes(b'evil');return result
    monkeypatch.setattr(cfg,'sha_file',changed_after_hash)
    with pytest.raises(DomainError):cfg.safe_file(tmp_path,{'path':'artifact.bin','bytes':4,'sha256':hashlib.sha256(b'good').hexdigest()})

def test_gate_leaf_key_never_acquires_root_rotation_purpose(tmp_path):
    import json,base64
    from app.infrastructure.runtime_configuration import gate_roots
    path=tmp_path/'roots.json'
    path.write_text(json.dumps({'version':1,'keys':[{'key_id':'leaf','public_key_b64url':base64.urlsafe_b64encode(b'a'*32).rstrip(b'=').decode(),'domains':['wordheat-live.readiness'],'types':['gate_evidence'],'test_only':False}]}));path.chmod(0o600)
    keys=gate_roots(path)
    assert keys['leaf']['domains']==('wordheat-live.readiness',) and 'key_bundle' not in keys['leaf']['types']

def test_bad_signed_candidate_disables_digest_without_falling_back_to_source_hash(tmp_path):
    import json,base64
    from types import SimpleNamespace
    import rfc8785
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    from app.infrastructure.runtime_configuration import SignedCandidateResolver
    trusted=Ed25519PrivateKey.generate();other=Ed25519PrivateKey.generate()
    encode=lambda raw:base64.urlsafe_b64encode(raw).rstrip(b'=').decode()
    keys=tmp_path/'release-roots.json';keys.write_text(json.dumps({'version':1,'keys':[{'key_id':'publisher','public_key_b64url':encode(trusted.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)),'purposes':['candidate_manifest']}]}));keys.chmod(0o600)
    document={'key_id':'publisher','domain':'wordheat-live.candidate','type':'candidate_manifest','version':1,'payload':{'candidate_id':'bad'}}
    document['signature']=encode(other.sign(rfc8785.dumps(document)))
    manifest=tmp_path/'candidate.json';manifest.write_text(json.dumps(document));manifest.chmod(0o600)
    settings=SimpleNamespace(candidate_manifest_file=manifest,candidate_root=tmp_path,candidate_public_keys_file=keys,release_public_keys_file=None,instance_id='wordheat-fixture')
    resolver=SignedCandidateResolver(settings,object(),tmp_path);resolver.refresh()
    assert resolver.current_digest() is None and resolver.last_error=='CANDIDATE_SIGNATURE_INVALID'

def test_mac_device_seed_is_only_loaded_from_private_root_environment(tmp_path):
    import base64
    seed=base64.urlsafe_b64encode(b'k'*32).rstrip(b'=').decode()
    private_env(tmp_path,'WORDHEAT_ENV=test\nWORDHEAT_INSTANCE_ID=wordheat-component-fixture\nLOCAL_DATABASE_URL=postgresql://user:sentinel-secret@127.0.0.1/wordheat_test\nLOCAL_ANALYTICS_CURSOR_KEY='+('a'*64)+'\nLOCAL_DEVICE_PRIVATE_KEY_B64URL='+seed+'\nLOCAL_DEVICE_PAIRING_ID=paired-device\n')
    settings=load_settings(tmp_path,source_root=Path(__file__).resolve().parents[4])
    assert settings.device_private_key==b'k'*32 and settings.device_pairing_id=='paired-device'
    assert settings.environment=='test' and seed not in repr(settings)
    from app.infrastructure.runtime_configuration import DeviceCredentials
    credentials=DeviceCredentials(settings.device_private_key,settings.device_pairing_id)
    credentials.initialize()
    assert credentials.value._token is None, 'short token must be obtained by PoP, never persisted'

def test_complete_signed_candidate_has_real_inventory_and_invalidates_changed_artifact(tmp_path):
    """Cryptographic/inventory component fixture; establishes no live gates."""
    import json,base64,hashlib,tomllib,zipfile,sys
    from types import SimpleNamespace
    import rfc8785
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    from app.config import Settings
    from app.infrastructure.runtime_configuration import SignedCandidateResolver,C1,BE
    from app.modules.wordbank.resources import describe
    source=Path(__file__).resolve().parents[4]
    encode=lambda raw:base64.urlsafe_b64encode(raw).rstrip(b'=').decode()
    signing=Ed25519PrivateKey.generate();public=encode(signing.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw))
    def save(name,value,private=False):
        path=tmp_path/name;path.write_text(json.dumps(value,ensure_ascii=False));path.chmod(0o600 if private else 0o644);return path
    def entry(path,root):return {'path':path.relative_to(root).as_posix(),'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    def signed(domain,payload,kind=None):
        value={'key_id':'component-publisher','domain':domain,'version':1,'payload':payload}
        if kind:value['type']=kind
        return value|{'signature':encode(signing.sign(rfc8785.dumps(value)))}
    release_keys=save('release-keys.json',{'version':1,'keys':[{'key_id':'component-publisher','public_key_b64url':public,'purposes':['release_artifact']}]},True)
    candidate_keys=save('candidate-keys.json',{'version':1,'keys':[{'key_id':'component-publisher','public_key_b64url':public,'purposes':['candidate_manifest']}]},True)
    model={'manifest_id':'component-model-identity','dimension':2,'files':[]}
    definition={'word':'苹果','aliases':[],'category_id':'fixture','difficulty':1,'hints':[],'explanation':'component fixture only','source_ref':'synthetic, not G1-Q'}
    content={'version':1,'model_manifest':model,'puzzles':[{'puzzle_version_id':'p1v1','puzzle_id':'p1','version':1,'definition':definition,'calibration_id':'cal1','evaluation_id':'eval1','scoring_artifact':{},'evaluation_input':{},'evaluation_result':{},'annotations_locked_at':'2026-10-26T00:00:00.000Z'}]}
    joint=describe(content)
    save('model.json',model);save('wordbank.json',content);save('calibration.json',{'version':1,'model_manifest_sha256':joint['model_manifest_sha256'],'calibration_version':joint['calibration_version'],'entries':joint['puzzles']})
    for name in ('relay','platform'):(tmp_path/(name+'.bin')).write_bytes((name+' component fixture').encode())
    webfiles={'web/index.html':b'prebuilt fixture','web/assets/app.js':b'compiled fixture bytes'}
    with zipfile.ZipFile(tmp_path/'windows.zip','w') as archive:
        for name,data in webfiles.items():archive.writestr(name,data)
    settings=Settings('postgresql://fixture:sentinel-secret@127.0.0.1/wordheat_test',instance_id='wordheat-component-fixture',candidate_root=tmp_path,candidate_manifest_file=tmp_path/'candidate.json',candidate_public_keys_file=candidate_keys,release_public_keys_file=release_keys)
    scorer=SimpleNamespace(manifest_metadata=model,manifest_sha256=joint['model_manifest_sha256'],readiness=lambda:{'ready':True,'manifest_id':model['manifest_id']})
    core=SimpleNamespace(scorer=scorer,describe_bundle_resources=describe)
    resolver=SignedCandidateResolver(settings,core,source);resolver.actual_joint=lambda:joint
    components={}
    for name in ('api','scorer','contract'):
        version=C1 if name=='contract' else tomllib.loads((source/f'apps/{name}/pyproject.toml').read_text())['project']['version']
        components[name]={'version':version,'files':[entry(source/path,source) for path in sorted(resolver.required(name))]}
    for name,file,version in [('relay','relay.bin','fixture'),('platform','platform.bin','fixture'),('windows','windows.zip','fixture'),('model','model.json',joint['model_manifest_id']),('wordbank','wordbank.json',joint['wordbank_version']),('calibration','calibration.json',joint['calibration_version'])]:components[name]={'version':version,'files':[entry(tmp_path/file,tmp_path)]}
    components['windows']['prebuilt_web']={'archive':'windows.zip','root':'web','files':[{'path':name.removeprefix('web/'),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()} for name,data in webfiles.items()]}
    compat={'contract_version':C1,'be_extension_version':BE,'schema_version':1,'runtime_platforms':[sys.platform]}
    release={'installation_id':settings.instance_id,'operation':'upgrade_app','app_version':components['api']['version'],'schema_version':1,'bundle':joint,'compat':compat,'components':components,'files':[item for name,component in components.items() if name not in {'api','scorer','contract'} for item in component['files']]}
    release_path=save('release.json',signed('wordheat-live.maintenance',release))
    payload={'candidate_id':'inventory-component-fixture','installation_id':settings.instance_id,'compat':compat,'bundle':joint,'components':components,'release_manifest':entry(release_path,tmp_path)}
    save('candidate.json',signed('wordheat-live.candidate',payload,'candidate_manifest'))
    resolver.refresh();assert resolver.current_digest() is not None,resolver.last_error
    assert len(resolver.inventory())==9 and not resolver.changed()
    (tmp_path/'relay.bin').write_bytes(b'changed artifact')
    assert resolver.changed();resolver.invalidate('CANDIDATE_REVALIDATING');resolver.refresh()
    assert resolver.current_digest() is None and resolver.last_error=='CANDIDATE_ARTIFACT_CHANGED'

def test_native_signed_dispatcher_never_imports_freshness_from_a_file():
    import json
    from app.infrastructure.runtime_configuration import SignedControlImporter,SignedControlReceipt
    from app.errors import DomainError
    class Repository:
        calls=[]
        def import_envelope(self,c,raw):self.calls.append('readiness');return {'object_id':'signed-object','digest':'a'*64,'imported':True}
    repository=Repository();dispatch=SignedControlImporter(repository)
    def document(kind):return json.dumps({'domain':'wordheat-live.readiness','type':kind,'payload':{'issuer_revision':2}})
    result=dispatch(None,document('live_grant'),native_command_id='native-plan',confirmation_id='confirmed')
    assert isinstance(result,SignedControlReceipt) and result.type=='live_grant' and result.issuer_revision==2
    with pytest.raises(DomainError) as error:dispatch(None,document('freshness_proof'),native_command_id='native-plan',confirmation_id='confirmed')
    assert error.value.code=='FRESHNESS_PROOF_CHANNEL_REQUIRED' and repository.calls==['readiness']


def test_pure_model_maintenance_does_not_require_postgres_backup_tools():
    from types import SimpleNamespace
    from app.infrastructure.runtime_configuration import RegisteredBackupExecutor
    class MissingTools:
        def load(self):raise AssertionError('Postgres tools must only load for a database operation')
    core=SimpleNamespace(db=SimpleNamespace(test_namespace=None),scorer=object(),export_verified_bundle=lambda c:None)
    factory=RegisteredBackupExecutor(object(),tools=MissingTools(),registry=object(),core=core)
    for stage in ('STAGE_MODEL','WARM_MODEL','SELF_CHECK','ACTIVATE_POINTER','INSTALL_VERSION'):
        executor=factory.executor(stage)
        assert executor.source_key is None and executor.source_schema is None
    with pytest.raises(AssertionError):factory.executor('BACKUP')

def test_pairing_intent_signer_remains_readiness_scoped(tmp_path):
    import json,base64
    from app.infrastructure.runtime_configuration import gate_roots
    file=tmp_path/'pairing-root.json'
    file.write_text(json.dumps({'version':1,'keys':[{'key_id':'pairing','public_key_b64url':base64.urlsafe_b64encode(b'p'*32).rstrip(b'=').decode(),'domains':['wordheat-live.readiness'],'types':['pairing_intent'],'test_only':False}]}));file.chmod(0o600)
    value=gate_roots(file)['pairing']
    assert value['types']==('pairing_intent',) and value['domains']==('wordheat-live.readiness',)
