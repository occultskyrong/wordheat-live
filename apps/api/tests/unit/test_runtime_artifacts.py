import base64
import hashlib
import json
from pathlib import Path
import pytest
import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from app.errors import DomainError
from app.modules.operations import runtime_artifacts as module

@pytest.fixture
def artifacts(tmp_path):
    key=Ed25519PrivateKey.generate();public=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    data=b'owned synthetic release artifact';(tmp_path/'app.bin').write_bytes(data)
    payload={'installation_id':'wordheat-test','operation':'upgrade_app','app_version':'0.2.0','compatibility':{'pg_major':16,'db_schema_read_versions':[1,2],'db_schema_write_version':2,'upgrade_from_schema_versions':[1]},'files':[{'path':'app.bin','bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}],'bundle':{'version':1,'model_manifest_id':'fixture-model','model_manifest_sha256':'a'*64,'wordbank_version':'sha256:'+'b'*64,'calibration_version':'sha256:'+'c'*64,'puzzles':[]},'required_free_bytes':1024}
    def write(value):
        envelope={'key_id':'fixture','domain':'wordheat-live.maintenance','version':1,'payload':value}
        envelope['signature']=base64.urlsafe_b64encode(key.sign(rfc8785.dumps(envelope))).decode().rstrip('=')
        path=tmp_path/'manifest.json';path.write_text(json.dumps(envelope));return path
    path=write(payload)
    return tmp_path,public,payload,write,path

def registry(data):
    assert hasattr(module,'ArtifactRegistry'),'portable signed maintenance planning missing'
    folder,public,_,_,_=data
    return module.ArtifactRegistry(folder,{'fixture':public},'wordheat-test')

def current():return dict(installation_id='wordheat-test',app_version='0.1.0',schema_version=1,pg_major=16,model_manifest_id='fixture-model',model_manifest_sha256='a'*64,estimated_backup_bytes=64)

def test_signed_plan_binds_bytes_paths_instance_and_rechecks_replacement(artifacts):
    folder,public,payload,write,path=artifacts;reg=registry(artifacts)
    reg.register('release',path)
    plan=reg.prepare('upgrade_app','release','command',current())
    assert plan.fingerprint and plan.payload['installation_id']=='wordheat-test'
    reg.recheck(plan)
    (folder/'app.bin').write_bytes(b'replaced after local confirmation')
    with pytest.raises(DomainError) as error:reg.recheck(plan)
    assert error.value.code=='MAINTENANCE_ARTIFACT_CHANGED'

def test_bad_signature_compatibility_and_space_fail_closed(artifacts,monkeypatch):
    folder,public,payload,write,path=artifacts;reg=registry(artifacts)
    value=json.loads(path.read_text());value['signature']='A'*86;path.write_text(json.dumps(value))
    with pytest.raises(DomainError) as error:reg.register('forged',path)
    assert error.value.code=='MAINTENANCE_SIGNATURE_INVALID'
    write(payload);reg.register('release',path)
    with pytest.raises(DomainError) as error:reg.prepare('upgrade_app','release','command',current()|{'schema_version':99})
    assert error.value.code=='MAINTENANCE_INCOMPATIBLE'
    monkeypatch.setattr(reg,'free_bytes',lambda:0)
    with pytest.raises(DomainError) as error:reg.prepare('upgrade_app','release','command',current())
    assert error.value.code=='MAINTENANCE_SPACE_REQUIRED'

def test_symlink_escape_and_restore_over_current_instance_are_rejected(artifacts,tmp_path):
    folder,public,payload,write,path=artifacts;reg=registry(artifacts)
    (folder/'escape').symlink_to('/etc/hosts')
    payload['files']=[{'path':'escape','bytes':1,'sha256':'a'*64}];write(payload)
    with pytest.raises(DomainError) as error:reg.register('escape',path)
    assert error.value.code=='MAINTENANCE_PATH_REJECTED'
