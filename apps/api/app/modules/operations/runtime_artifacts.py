"""Fixed-root, signed maintenance plans. No browser supplies a path or trust key."""
import base64
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path,PurePosixPath
import shutil
import stat
import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from app.errors import DomainError

OPERATIONS={'backup_local','verify_local_backup','restore_new_instance','upgrade_app','switch_model'}

def fail(code):raise DomainError(code,'Maintenance artifact or plan rejected',409)
def sha_file(path):
    value=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):value.update(chunk)
    return value.hexdigest()
def fingerprint(value):return hashlib.sha256(rfc8785.dumps(value)).hexdigest()
def unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('Duplicate key')
        result[key]=value
    return result

@dataclass(frozen=True)
class VerifiedPlan:
    payload:dict
    fingerprint:str
    _registry_token:object

class ArtifactRegistry:
    def __init__(self,root,public_keys,installation_id,*,control_root=None):
        self.root=Path(root)
        if not self.root.is_absolute() or not self.root.is_dir() or self.root.is_symlink():fail('MAINTENANCE_PATH_REJECTED')
        self.root=self.root.resolve(strict=True);self.public_keys=dict(public_keys);self.installation_id=installation_id
        self.control_root=Path(control_root) if control_root is not None else self.root/'control'
        if not self.control_root.is_absolute() or any(p.is_symlink() for p in [self.control_root,*self.control_root.parents]):fail('MAINTENANCE_PATH_REJECTED')
        self.control_root.mkdir(parents=True,mode=0o700,exist_ok=True)
        if not self.control_root.is_dir() or self.control_root.stat().st_uid!=os.getuid() or self.control_root.stat().st_mode&0o077:fail('MAINTENANCE_PATH_REJECTED')
        self.control_root=self.control_root.resolve(strict=True)
        self.artifacts={};self.targets={};self.controls={};self.model_roots={};self._token=object()
        index=self.root/'.registry.json'
        if index.exists():
            if index.is_symlink() or index.stat().st_mode&0o077 or index.stat().st_size>4*1024*1024:fail('MAINTENANCE_REGISTRY_INVALID')
            try:saved=json.loads(index.read_bytes(),object_pairs_hook=unique)
            except (ValueError,TypeError):fail('MAINTENANCE_REGISTRY_INVALID')
            if saved.get('version')!=1 or saved.get('installation_id')!=installation_id:fail('MAINTENANCE_INSTANCE_MISMATCH')
            if saved.get('control_root') not in {None,str(self.control_root)} and saved.get('controls'):fail('MAINTENANCE_REGISTRY_INVALID')
            self.artifacts=saved.get('artifacts',{});self.targets=saved.get('targets',{});self.controls=saved.get('controls',{});self.model_roots=saved.get('model_roots',{})
            if not all(isinstance(x,dict) for x in [self.artifacts,self.targets,self.controls,self.model_roots]):fail('MAINTENANCE_REGISTRY_INVALID')

    def save_registry(self):
        target=self.root/'.registry.json'
        if target.is_symlink():fail('MAINTENANCE_PATH_REJECTED')
        temporary=self.root/('.registry-'+secrets.token_hex(12))
        payload={'version':1,'installation_id':self.installation_id,'artifacts':self.artifacts,'targets':self.targets,'controls':self.controls,'model_roots':self.model_roots,'control_root':str(self.control_root)}
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(rfc8785.dumps(payload));stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,target)
        finally:temporary.unlink(missing_ok=True)

    def register_model_root(self,logical_id,path):
        root=Path(path)
        if not isinstance(logical_id,str) or not logical_id or not root.is_absolute() or not root.is_dir() or any(p.is_symlink() for p in [root,*root.parents]):fail('MAINTENANCE_PATH_REJECTED')
        self.model_roots[logical_id]=str(root.resolve(strict=True));self.save_registry()

    def control_path(self,value):
        path=Path(value)
        if not path.is_absolute():path=self.control_root/path
        if any(p.is_symlink() for p in [path,*path.parents]) or not path.resolve(strict=True).is_relative_to(self.control_root) or not path.is_file():fail('MAINTENANCE_PATH_REJECTED')
        return path.resolve(strict=True)

    def register_control(self,source_id,path):
        path=self.control_path(path)
        if path.stat().st_size>2*1024*1024:fail('MAINTENANCE_ARTIFACT_INVALID')
        self.controls[source_id]={'path':str(path),'sha256':sha_file(path)}
        self.save_registry()

    def read_control(self,source_id,expected_hash=None):
        record=self.controls.get(source_id)
        if not record:fail('MAINTENANCE_ARTIFACT_UNKNOWN')
        path=self.control_path(record['path'])
        if path.stat().st_size>2*1024*1024 or sha_file(path)!=record['sha256'] or expected_hash is not None and record['sha256']!=expected_hash:fail('MAINTENANCE_ARTIFACT_CHANGED')
        return path,path.read_bytes(),record['sha256']

    def path(self,value,*,must_exist=True):
        path=Path(value)
        if not path.is_absolute():path=self.root/path
        try:relative=path.relative_to(self.root)
        except ValueError:fail('MAINTENANCE_PATH_REJECTED')
        if any(part in {'..','.'} for part in relative.parts):fail('MAINTENANCE_PATH_REJECTED')
        current=self.root
        for part in relative.parts:
            current=current/part
            if current.is_symlink():fail('MAINTENANCE_PATH_REJECTED')
        if must_exist:
            try:resolved=path.resolve(strict=True)
            except (OSError,RuntimeError):fail('MAINTENANCE_PATH_REJECTED')
            if not resolved.is_relative_to(self.root) or not resolved.is_file():fail('MAINTENANCE_PATH_REJECTED')
            return resolved
        if not path.parent.resolve(strict=True).is_relative_to(self.root):fail('MAINTENANCE_PATH_REJECTED')
        return path

    def envelope(self,path):
        try:
            if path.stat().st_size>2*1024*1024:raise ValueError()
            body=json.loads(path.read_bytes(),object_pairs_hook=unique)
            if set(body)!={'key_id','domain','version','payload','signature'} or body['domain']!='wordheat-live.maintenance' or body['version']!=1:raise ValueError()
            raw=self.public_keys[body['key_id']]
            if not isinstance(raw,bytes) or len(raw)!=32:raise ValueError()
            signature=base64.urlsafe_b64decode(body['signature']+'='*(-len(body['signature'])%4))
            unsigned={k:v for k,v in body.items() if k!='signature'}
            Ed25519PublicKey.from_public_bytes(raw).verify(signature,rfc8785.dumps(unsigned))
            payload=body['payload']
            if not isinstance(payload,dict) or payload.get('installation_id')!=self.installation_id or payload.get('operation') not in OPERATIONS:raise ValueError()
            return payload
        except Exception as error:
            if isinstance(error,DomainError):raise
            raise DomainError('MAINTENANCE_SIGNATURE_INVALID','Trusted signed maintenance manifest required',403) from None

    def files(self,payload):
        files=payload.get('files')
        if not isinstance(files,list) or len(files)>10000:fail('MAINTENANCE_ARTIFACT_INVALID')
        paths=[];seen=set()
        for item in files:
            if not isinstance(item,dict) or set(item)!={'path','bytes','sha256'}:fail('MAINTENANCE_ARTIFACT_INVALID')
            name=item['path']
            if not isinstance(name,str) or '\\' in name or PurePosixPath(name).is_absolute() or any(x in {'','..','.'} for x in name.split('/')) or name in seen:fail('MAINTENANCE_PATH_REJECTED')
            seen.add(name);path=self.path(name)
            if type(item['bytes']) is not int or item['bytes']<0 or path.stat().st_size!=item['bytes'] or not isinstance(item['sha256'],str) or sha_file(path)!=item['sha256']:fail('MAINTENANCE_ARTIFACT_CHANGED')
            paths.append(str(path))
        return paths

    def register(self,artifact_id,manifest_path):
        if not isinstance(artifact_id,str) or not 1<=len(artifact_id)<=128:fail('MAINTENANCE_ARTIFACT_INVALID')
        path=self.path(manifest_path);payload=self.envelope(path);self.files(payload)
        self.artifacts[artifact_id]={'manifest':str(path),'manifest_sha256':sha_file(path),'payload':payload}
        self.save_registry()

    def register_backup(self,artifact_id,manifest_path,expected_sha256):
        path=self.path(manifest_path)
        if sha_file(path)!=expected_sha256:fail('MAINTENANCE_ARTIFACT_CHANGED')
        try:payload=json.loads(path.read_bytes(),object_pairs_hook=unique)
        except (ValueError,TypeError):fail('BACKUP_MANIFEST_INVALID')
        if payload.get('format')!='wordheat-local-backup/1' or payload.get('installation_id')!=self.installation_id or payload.get('operation')!='backup_local' or not isinstance(payload.get('database_backup'),dict):fail('BACKUP_MANIFEST_INVALID')
        self.files(payload)
        self.artifacts[artifact_id]={'manifest':str(path),'manifest_sha256':expected_sha256,'payload':payload,'kind':'local_backup'}
        self.save_registry()

    def registered_payload(self,stored):
        path=self.path(stored['manifest'])
        if sha_file(path)!=stored['manifest_sha256']:fail('MAINTENANCE_ARTIFACT_CHANGED')
        if stored.get('kind')=='local_backup':
            try:value=json.loads(path.read_bytes(),object_pairs_hook=unique)
            except (ValueError,TypeError):fail('BACKUP_MANIFEST_INVALID')
            if value.get('format')!='wordheat-local-backup/1' or fingerprint(value)!=fingerprint(stored['payload']):fail('BACKUP_MANIFEST_INVALID')
            return value
        return self.envelope(path)

    def register_target(self,target_id,path,installation_id):
        if not target_id or installation_id==self.installation_id:fail('MAINTENANCE_TARGET_REJECTED')
        path=self.path(path,must_exist=False)
        if path.exists():fail('MAINTENANCE_TARGET_REJECTED')
        self.targets[target_id]={'installation_id':installation_id,'path':str(path)}
        self.save_registry()

    def free_bytes(self):return shutil.disk_usage(self.root).free

    def prepare(self,operation,artifact_id,command_id,current,*,target_id=None,allow_data_loss=False):
        if operation not in OPERATIONS or current.get('installation_id')!=self.installation_id:fail('MAINTENANCE_INSTANCE_MISMATCH')
        artifact=None
        if artifact_id:
            if artifact_id not in self.artifacts:fail('MAINTENANCE_ARTIFACT_UNKNOWN')
            stored=self.artifacts[artifact_id];path=self.path(stored['manifest'])
            if sha_file(path)!=stored['manifest_sha256']:fail('MAINTENANCE_ARTIFACT_CHANGED')
            artifact=self.registered_payload(stored);paths=self.files(artifact)
            eligible={operation} if operation not in {'verify_local_backup','restore_new_instance'} else {'backup_local',operation}
            if artifact['operation'] not in eligible:fail('MAINTENANCE_ARTIFACT_INVALID')
        elif operation!='backup_local':fail('MAINTENANCE_ARTIFACT_REQUIRED')
        if operation in {'upgrade_app','switch_model'}:
            compatibility=artifact.get('compatibility',{})
            if compatibility.get('pg_major')!=16 or current.get('pg_major')!=16 or current.get('schema_version') not in compatibility.get('db_schema_read_versions',[]):fail('MAINTENANCE_INCOMPATIBLE')
            if operation=='upgrade_app' and current['schema_version'] not in compatibility.get('upgrade_from_schema_versions',[]):fail('MAINTENANCE_INCOMPATIBLE')
            bundle=artifact.get('bundle')
            if not isinstance(bundle,dict) or set(bundle)!={'version','model_manifest_id','model_manifest_sha256','wordbank_version','calibration_version','puzzles'} or bundle['version']!=1 or not isinstance(bundle['puzzles'],list):fail('MAINTENANCE_BUNDLE_INVALID')
        estimate=current.get('estimated_backup_bytes',0)
        required=max((artifact or {}).get('required_free_bytes',0),sum(x['bytes'] for x in (artifact or {}).get('files',[]))+2*estimate)
        if type(required) is not int or required<0 or self.free_bytes()<required:fail('MAINTENANCE_SPACE_REQUIRED')
        target=None
        if operation=='restore_new_instance':
            target=self.targets.get(target_id)
            if not target or target['installation_id']==self.installation_id or Path(target['path']).exists() or allow_data_loss is not True:fail('MAINTENANCE_TARGET_REJECTED')
        plan={'version':1,'command_id':command_id,'installation_id':self.installation_id,'operation':operation,'artifact_id':artifact_id,'artifact':artifact,'artifact_paths':paths if artifact else [],'manifest_sha256':self.artifacts[artifact_id]['manifest_sha256'] if artifact_id else None,'target_id':target_id,'target':target,'allow_data_loss':bool(allow_data_loss),'before':dict(current),'required_free_bytes':required,'backup_root':str(self.root)}
        return VerifiedPlan(plan,fingerprint(plan),self._token)

    def recheck(self,plan):
        if not isinstance(plan,VerifiedPlan) or plan._registry_token is not self._token or fingerprint(plan.payload)!=plan.fingerprint:fail('MAINTENANCE_PLAN_INVALID')
        p=plan.payload;artifact_id=p['artifact_id']
        if artifact_id:
            registered=self.artifacts.get(artifact_id)
            if not registered or registered['manifest_sha256']!=p['manifest_sha256'] or sha_file(self.path(registered['manifest']))!=p['manifest_sha256']:fail('MAINTENANCE_ARTIFACT_CHANGED')
            artifact=self.registered_payload(registered)
            if fingerprint(artifact)!=fingerprint(p['artifact']):fail('MAINTENANCE_ARTIFACT_CHANGED')
            self.files(artifact)
        if self.free_bytes()<p['required_free_bytes']:fail('MAINTENANCE_SPACE_REQUIRED')
        if p['target']:
            if self.targets.get(p['target_id'])!=p['target']:fail('MAINTENANCE_TARGET_REJECTED')
            self.path(p['target']['path'],must_exist=False)
        return plan

    def restore_plan(self,payload,expected_fingerprint):
        plan=VerifiedPlan(payload,expected_fingerprint,self._token)
        return self.recheck(plan)


def verify_model_data(manifest_path,model_root):
    """Verify bytes only; never import model code or load tensors in the API/host."""
    path=Path(manifest_path);root=Path(model_root)
    if path.is_symlink() or root.is_symlink() or not root.is_dir() or path.stat().st_size>2*1024*1024:fail('MODEL_MANIFEST_INVALID')
    root=root.resolve(strict=True)
    try:manifest=json.loads(path.read_bytes(),object_pairs_hook=unique)
    except (ValueError,TypeError):fail('MODEL_MANIFEST_INVALID')
    if not isinstance(manifest.get('manifest_id'),str) or not manifest['manifest_id'] or not isinstance(manifest.get('revision'),str) or not __import__('re').fullmatch('[0-9a-f]{40}',manifest['revision']) or type(manifest.get('dimension')) is not int or not 1<=manifest['dimension']<=4096 or type(manifest.get('normalize')) is not bool or manifest.get('precision')!='float32' or manifest.get('pooling') not in {'cls','mean'} or not isinstance(manifest.get('files'),list) or not manifest['files']:fail('MODEL_MANIFEST_INVALID')
    seen=set()
    for item in manifest['files']:
        name=item.get('path')
        if not isinstance(name,str) or name in seen or '\\' in name or ':' in name or PurePosixPath(name).is_absolute() or any(x in {'','..','.'} for x in name.split('/')):fail('MAINTENANCE_PATH_REJECTED')
        target=root.joinpath(*PurePosixPath(name).parts)
        if any(p.is_symlink() for p in [target,*target.parents] if p.is_relative_to(root)) or not target.resolve(strict=True).is_relative_to(root) or not target.is_file() or type(item.get('size')) is not int or target.stat().st_size!=item['size'] or sha_file(target)!=item.get('sha256'):fail('MODEL_FILE_MISMATCH')
        seen.add(name)
    for member in root.rglob('*'):
        if member.is_symlink() or member.is_file() and member.relative_to(root).as_posix() not in seen:fail('MODEL_FILE_NOT_LISTED')
    return {'manifest':manifest,'manifest_sha256':fingerprint(manifest),'file_sha256':sha_file(path),'model_root':str(root)}
