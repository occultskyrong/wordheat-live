"""Restricted server configuration and cached signed-candidate verification.

No browser supplies paths, signing keys, device material or execution evidence.
Candidate file/model IO runs in the monitor thread, never in a business SQL txn.
"""
import asyncio,base64,hashlib,json,os,stat,threading,tomllib
from dataclasses import dataclass
from pathlib import Path,PurePosixPath
import rfc8785
import sqlalchemy as sa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey,Ed25519PrivateKey
from app.config import explicit_path,read_private_bytes
from app.errors import DomainError
from app.infrastructure.db import schema as s

C1='LW-C1/1.0.0';BE='LW-BE/1.1.0'
COMPONENTS=frozenset({'api','scorer','relay','contract','platform','windows','model','wordbank','calibration'})

def fail(code='SERVER_CONFIGURATION_INVALID'):
    raise DomainError(code,'Restricted server configuration rejected',503)

def unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:fail()
        result[key]=value
    return result

def parse_private_json(path,limit=4*1024*1024):
    try:
        value=json.loads(read_private_bytes(path,limit).decode('utf-8'),object_pairs_hook=unique,parse_constant=lambda _:fail())
        rfc8785.dumps(value)
        if not isinstance(value,dict):fail()
        return value
    except DomainError:raise
    except Exception:fail()

def raw_key(value,size=32):
    from app.modules.auth.trust_keys import raw32
    try:return raw32(value,size)
    except Exception:fail()

def private_directory(path):
    path=explicit_path(path)
    if path is None or any(p.is_symlink() for p in [path,*path.parents]):fail('PRIVATE_DIRECTORY_REJECTED')
    path.mkdir(mode=0o700,parents=True,exist_ok=True)
    info=path.stat()
    if info.st_uid!=os.getuid() or not stat.S_ISDIR(info.st_mode) or info.st_mode&0o077:fail('PRIVATE_DIRECTORY_REJECTED')
    return path

def roots(settings,source_root=None,program_entry=None):
    root=explicit_path(getattr(settings,'server_root',None))
    if root is None:fail('CONFIG_ROOT_REQUIRED')
    if not root.is_dir() or root.is_symlink():fail('CONFIG_ROOT_REQUIRED')
    source=explicit_path(source_root or getattr(settings,'source_root',None))
    if source is None or source.is_symlink() or not source.is_dir():fail('SOURCE_ROOT_REQUIRED')
    source=source.resolve(strict=True)
    if (source/'apps/api/app/application.py').resolve(strict=True)!=Path(__file__).resolve().parents[1]/'application.py':fail('SOURCE_ROOT_MISMATCH')
    return {'server_root':root.resolve(strict=True),'source_root':source,
            'native_root':explicit_path(getattr(settings,'native_root',None)) or root/'.local/runtime',
            'maintenance_root':explicit_path(getattr(settings,'maintenance_root',None)) or root/'.local/maintenance',
            'control_root':explicit_path(getattr(settings,'control_root',None)) or root/'.local/control',
            'program_entry':explicit_path(program_entry) or source/'apps/api/app/runtime_entry.py'}

def gate_roots(path):
    if path is None:return {}
    value=parse_private_json(path);keys={}
    if set(value)!={'version','keys'} or type(value['version']) is not int or value['version']!=1 or not isinstance(value['keys'],list) or len(value['keys'])>128:fail('GATE_ROOT_CONFIGURATION_INVALID')
    for item in value['keys']:
        if not isinstance(item,dict) or set(item)!={'key_id','public_key_b64url','domains','types','test_only'}:fail('GATE_ROOT_CONFIGURATION_INVALID')
        kid=item['key_id'];domains=item['domains'];types=item['types']
        if not isinstance(kid,str) or not 1<=len(kid)<=128 or kid in keys or type(item['test_only']) is not bool or not isinstance(domains,list) or not isinstance(types,list) or not domains or not types or any(not isinstance(v,str) for v in domains+types) or len(set(domains))!=len(domains) or len(set(types))!=len(types):fail('GATE_ROOT_CONFIGURATION_INVALID')
        if not set(domains)<={'wordheat-live.trust','wordheat-live.readiness'} or not set(types)<={'key_bundle','gate_evidence','live_grant','revocation','freshness_proof','pairing_intent'} or ('key_bundle' in types)!=('wordheat-live.trust' in domains) or (set(types)-{'key_bundle'} and 'wordheat-live.readiness' not in domains):fail('GATE_ROOT_CONFIGURATION_INVALID')
        keys[kid]={'public_key':raw_key(item['public_key_b64url']),'domains':tuple(domains),'types':tuple(types),'test_only':item['test_only']}
    return keys

def release_keys(path,purpose='release_artifact'):
    if path is None:return {}
    value=parse_private_json(path);keys={}
    if set(value)!={'version','keys'} or value['version']!=1 or not isinstance(value['keys'],list) or len(value['keys'])>128:fail('RELEASE_ROOT_CONFIGURATION_INVALID')
    for item in value['keys']:
        if not isinstance(item,dict) or set(item)!={'key_id','public_key_b64url','purposes'} or not isinstance(item['key_id'],str) or not item['key_id'] or item['key_id'] in keys or not isinstance(item['purposes'],list) or not set(item['purposes'])<={'release_artifact','candidate_manifest'} or not item['purposes']:fail('RELEASE_ROOT_CONFIGURATION_INVALID')
        if purpose in item['purposes']:keys[item['key_id']]=raw_key(item['public_key_b64url'])
    return keys

def connector_registry(path,environment):
    if path is None:return {}
    from app.modules.connectors.protocol import registered_target
    value=parse_private_json(path)
    if set(value)!={'version','connectors'} or value['version']!=1 or not isinstance(value['connectors'],dict) or len(value['connectors'])>32:fail('CONNECTOR_CONFIGURATION_INVALID')
    for kid,item in value['connectors'].items():
        if not isinstance(kid,str) or not 1<=len(kid)<=128 or not isinstance(item,dict) or set(item)!={'url','test_only'} or type(item['test_only']) is not bool:fail('CONNECTOR_CONFIGURATION_INVALID')
        registered_target(kid,value['connectors'],test_mode=environment=='test')
    return value['connectors']

@dataclass(frozen=True)
class SignedControlReceipt:
    object_id:str
    type:str
    digest:str
    issuer_revision:int
    imported:bool
    status:str='applied'

class SignedControlImporter:
    """Repository verification remains authoritative after native file confirmation."""
    def __init__(self,trust):self.trust=trust
    def __call__(self,c,raw,*,native_command_id,confirmation_id):
        from app.modules.auth.evidence import parse_envelope
        if not native_command_id or not confirmation_id:raise DomainError('NATIVE_CONFIRMATION_REQUIRED','Bound native confirmation required',403)
        envelope=parse_envelope(raw);domain=envelope.get('domain');kind=envelope.get('type')
        if (domain,kind)==('wordheat-live.trust','key_bundle'):
            value=self.trust.import_key_bundle(c,raw,native_command_id=native_command_id,confirmation_id=confirmation_id)
            return SignedControlReceipt(value.object_id,kind,value.digest,value.issuer_revision,value.imported)
        if domain=='wordheat-live.readiness' and kind in {'gate_evidence','live_grant','revocation'}:
            value=self.trust.import_envelope(c,raw)
            if not isinstance(value,dict) or type(value.get('imported')) is not bool:raise DomainError('NATIVE_EXECUTION_UNVERIFIED','Repository verification receipt required',403)
            return SignedControlReceipt(value['object_id'],kind,value['digest'],envelope['payload']['issuer_revision'],value['imported'])
        code='FRESHNESS_PROOF_CHANNEL_REQUIRED' if (domain,kind)==('wordheat-live.readiness','freshness_proof') else 'EVIDENCE_INVALID'
        raise DomainError(code,'This signed control object is not accepted through a native file',403)

class DeviceCredentials:
    """Mac long-term seed comes only from private Settings; tokens stay in memory."""
    def __init__(self,private_key,pairing_id,secure_store=None):
        self._seed=private_key;self.pairing_id=pairing_id;self.secure_store=secure_store;self.value=None
    def initialize(self):
        if self.secure_store is not None:raise DomainError('DEVICE_SECURE_STORE_UNAVAILABLE','Encrypted native-store adapter is not available in this development runtime',503)
        if self._seed is None or self.pairing_id is None:return
        from app.modules.connectors.transport import NativeDeviceCredential
        if not isinstance(self._seed,bytes) or len(self._seed)!=32:fail('DEVICE_CREDENTIAL_CONFIGURATION_INVALID')
        key=Ed25519PrivateKey.from_private_bytes(self._seed)
        self.value=NativeDeviceCredential(self.pairing_id,key,None,0)
    def __call__(self,scope):
        if self.value is None:raise DomainError('AUTH_REQUIRED','Native device credential is not configured',401)
        return self.value(scope)
    def refresh_if_due(self,port,scope):
        if self.value is None:raise DomainError('AUTH_REQUIRED','Native device credential is not configured',401)
        self.value.refresh_if_due(port,scope)
    def install_verified(self,credential):
        from app.modules.connectors.transport import NativeDeviceCredential
        if not isinstance(credential,NativeDeviceCredential):raise DomainError('FORBIDDEN','Verified native pairing credential required',403)
        self.value=credential;self.pairing_id=credential.pairing_id
    def clear(self):self.value=None

class LazyPostgresTools:
    """Absent maintenance config is an operation failure, not a local startup failure."""
    def __init__(self,path,installation_id):self.path=path;self.installation_id=installation_id;self.value=None
    def load(self):
        if self.value is None:
            from app.modules.operations.runtime_postgres import PostgresTools
            self.value=PostgresTools.from_registry(self.path,self.installation_id)
        return self.value
    def __getattr__(self,name):return getattr(self.load(),name)

class RegisteredBackupExecutor:
    def __init__(self,attest,*,tools,registry,core):self.attest=attest;self.tools=tools;self.registry=registry;self.core=core
    def executor(self,stage):
        from app.modules.operations.runtime_backup import BackupExecutor
        if stage in {'STAGE_MODEL','WARM_MODEL','SELF_CHECK','ACTIVATE_POINTER','INSTALL_VERSION'}:
            return BackupExecutor(self.attest,tools=self.tools,registry=self.registry,database=self.core.db,source_key=None,source_schema=None,namespace_marker=None,verification_target=None,scorer=self.core.scorer,context_provider=self.core.export_verified_bundle)
        tools=self.tools.load();source=tools.registry.get('source',{})
        namespace=getattr(self.core.db,'test_namespace',None)
        if namespace is not None and (source.get('schema')!=namespace.namespace or source.get('namespace_marker')!=namespace.comment):raise DomainError('TEST_NAMESPACE_BACKUP_SCOPE_REJECTED','Test backup source must be the registered isolated namespace',503)
        if set(source)-{'key','schema','namespace_marker'} or not source.get('key') or not source.get('schema'):fail('POSTGRES_REGISTRY_INVALID')
        return BackupExecutor(self.attest,tools=tools,registry=self.registry,database=self.core.db,source_key=source['key'],source_schema=source['schema'],namespace_marker=source.get('namespace_marker'),verification_target=tools.registry.get('verification_target'),scorer=self.core.scorer,context_provider=self.core.export_verified_bundle)
    def run_stage(self,stage,plan,prior):return self.executor(stage).run_stage(stage,plan,prior)
    def reconcile(self,stage,plan,prior):return self.executor(stage).reconcile(stage,plan,prior)

def sha_file(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()

def file_identity(path):
    info=path.stat();return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_mode)

def safe_file(root,entry,verified=None):
    if not isinstance(entry,dict) or set(entry)!={'path','bytes','sha256'} or not isinstance(entry['path'],str) or '\\' in entry['path'] or PurePosixPath(entry['path']).is_absolute() or any(p in {'','.','..'} or p.startswith('.env') or p in {'.git','.local','secrets'} for p in entry['path'].split('/')) or type(entry['bytes']) is not int or entry['bytes']<0 or not isinstance(entry['sha256'],str) or len(entry['sha256'])!=64:fail('CANDIDATE_MANIFEST_INVALID')
    try:bytes.fromhex(entry['sha256'])
    except ValueError:fail('CANDIDATE_MANIFEST_INVALID')
    path=root/entry['path']
    if any(p.is_symlink() for p in [path,*path.parents]) or not path.is_file() or not path.resolve(strict=True).is_relative_to(root.resolve(strict=True)):fail('CANDIDATE_ARTIFACT_CHANGED')
    before=file_identity(path)
    if before[2]!=entry['bytes'] or sha_file(path)!=entry['sha256'] or file_identity(path)!=before:fail('CANDIDATE_ARTIFACT_CHANGED')
    if verified is not None:verified[str(path)]=before
    return path

def verify_envelope(path,keys,domain,kind=None):
    try:
        if any(p.is_symlink() for p in [path,*path.parents]) or not path.is_file() or path.stat().st_mode&0o022 or path.stat().st_size>4*1024*1024:fail('CANDIDATE_MANIFEST_INVALID')
        value=json.loads(path.read_bytes(),object_pairs_hook=unique,parse_constant=lambda _:fail('CANDIDATE_MANIFEST_INVALID'));rfc8785.dumps(value)
    except DomainError:raise
    except Exception:fail('CANDIDATE_MANIFEST_INVALID')
    expected={'key_id','domain','version','payload','signature'}|({'type'} if kind else set())
    if set(value)!=expected or value['domain']!=domain or type(value['version']) is not int or value['version']!=1 or (kind and value['type']!=kind) or value.get('key_id') not in keys:fail('CANDIDATE_SIGNATURE_INVALID')
    try:Ed25519PublicKey.from_public_bytes(keys[value['key_id']]).verify(raw_key(value['signature'],64),rfc8785.dumps({k:v for k,v in value.items() if k!='signature'}))
    except Exception:fail('CANDIDATE_SIGNATURE_INVALID')
    if not isinstance(value['payload'],dict):fail('CANDIDATE_MANIFEST_INVALID')
    return value

class SignedCandidateResolver:
    def __init__(self,settings,core,source_root):
        self.settings=settings;self.core=core;self.source_root=source_root;self.lock=threading.Lock();self._digest=None;self._inventory={};self.paths=[];self.fingerprints={};self.task=None;self.stopping=None;self.last_error='SIGNED_CANDIDATE_REQUIRED'
    def current_digest(self):
        with self.lock:return self._digest
    def inventory(self):
        with self.lock:return dict(self._inventory)
    def invalidate(self,reason):
        with self.lock:self._digest=None;self._inventory={};self.last_error=reason
    @staticmethod
    def signature(path):
        info=path.stat();return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_mode)
    def actual_joint(self):
        with self.core.db.engine.connect() as c:return c.execute(sa.select(s.catalog.c.joint_identity).where(s.catalog.c.id=='global')).scalar_one_or_none() or {}
    def required(self,name):
        source=self.source_root
        if name=='api':
            values=[p for folder in ['apps/api/app','apps/api/migrations'] for p in (source/folder).rglob('*') if p.is_file() and p.suffix in {'.py','.sql'} and '__pycache__' not in p.parts]
            return {p.relative_to(source).as_posix() for p in values}|{'apps/api/pyproject.toml','apps/api/uv.lock'}
        if name=='scorer':return {p.relative_to(source).as_posix() for p in (source/'apps/scorer/app').rglob('*.py')}|{'apps/scorer/pyproject.toml','apps/scorer/uv.lock'}
        return {p.relative_to(source).as_posix() for p in (source/'packages/contracts').rglob('*') if p.is_file() and p.suffix in {'.json','.yaml','.yml','.ts'}}
    def verify(self):
        settings=self.settings
        if settings.candidate_manifest_file is None or settings.candidate_root is None:fail('SIGNED_CANDIDATE_REQUIRED')
        initial={}
        for path in [settings.candidate_manifest_file,settings.candidate_public_keys_file,settings.release_public_keys_file]:
            if path:initial[str(path)]=file_identity(path)
        candidate_keys=release_keys(settings.candidate_public_keys_file,'candidate_manifest')
        envelope=verify_envelope(settings.candidate_manifest_file,candidate_keys,'wordheat-live.candidate','candidate_manifest');payload=envelope['payload']
        for folder in ['apps/api/app','apps/api/migrations','apps/scorer/app','packages/contracts']:
            initial[str(self.source_root/folder)]=file_identity(self.source_root/folder)
            for path in (self.source_root/folder).rglob('*'):
                if path.is_dir() and '__pycache__' not in path.parts:initial[str(path)]=file_identity(path)
        if set(payload)!={'candidate_id','installation_id','compat','bundle','components','release_manifest'} or payload['installation_id']!=settings.instance_id or not isinstance(payload['candidate_id'],str) or not payload['candidate_id']:fail('CANDIDATE_MANIFEST_INVALID')
        compat=payload['compat'];components=payload['components'];joint=self.actual_joint()
        if not isinstance(compat,dict) or compat.get('contract_version')!=C1 or compat.get('be_extension_version')!=BE or compat.get('schema_version')!=1 or payload['bundle']!=joint or not joint:fail('CANDIDATE_MISMATCH')
        observation=self.core.scorer.readiness()
        if observation.get('ready') is not True or getattr(self.core.scorer,'manifest_sha256',None)!=joint['model_manifest_sha256'] or observation.get('manifest_id')!=joint['model_manifest_id']:fail('CANDIDATE_MISMATCH')
        if not isinstance(components,dict) or set(components)!=COMPONENTS:fail('CANDIDATE_INCOMPLETE')
        watched=[settings.candidate_manifest_file];seen=set();inventory={}
        for name,component in components.items():
            if not isinstance(component,dict) or set(component)-{'version','files','prebuilt_web'} or not isinstance(component.get('version'),str) or not component['version'] or not isinstance(component.get('files'),list) or not component['files']:fail('CANDIDATE_INCOMPLETE')
            root=self.source_root if name in {'api','scorer','contract'} else settings.candidate_root
            names=set()
            for entry in component['files']:
                key=(str(root),entry.get('path')) if isinstance(entry,dict) else None
                if key in seen:fail('CANDIDATE_MANIFEST_INVALID')
                seen.add(key);names.add(entry.get('path'));watched.append(safe_file(root,entry,initial))
            if name in {'api','scorer','contract'} and names!=self.required(name):fail('CANDIDATE_INCOMPLETE')
            if name in {'api','scorer'}:
                version=tomllib.loads((self.source_root/f'apps/{name}/pyproject.toml').read_text())['project']['version']
                if component['version']!=version:fail('CANDIDATE_MISMATCH')
            inventory[name]=hashlib.sha256(rfc8785.dumps(component)).hexdigest()
        if len(seen)>10000:fail('CANDIDATE_MANIFEST_INVALID')
        if components['contract']['version']!=C1 or components['model']['version']!=joint['model_manifest_id'] or components['wordbank']['version']!=joint['wordbank_version'] or components['calibration']['version']!=joint['calibration_version']:fail('CANDIDATE_MISMATCH')
        def component_json(name):
            entries=[item for item in components[name]['files'] if item['path'].endswith('.json')]
            if len(entries)!=1:fail('CANDIDATE_INCOMPLETE')
            raw=(settings.candidate_root/entries[0]['path']).read_bytes()
            if len(raw)>64*1024*1024:fail('CANDIDATE_MANIFEST_INVALID')
            value=json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:fail('CANDIDATE_MANIFEST_INVALID'));rfc8785.dumps(value)
            return value
        if component_json('model')!=getattr(self.core.scorer,'manifest_metadata',None):fail('CANDIDATE_MISMATCH')
        if self.core.describe_bundle_resources(component_json('wordbank'))!=joint:fail('CANDIDATE_MISMATCH')
        if component_json('calibration')!={'version':1,'model_manifest_sha256':joint['model_manifest_sha256'],'calibration_version':joint['calibration_version'],'entries':joint['puzzles']}:fail('CANDIDATE_MISMATCH')
        windows=components['windows'];web=windows.get('prebuilt_web')
        if not isinstance(web,dict) or set(web)!={'archive','root','files'} or not isinstance(web['files'],list) or not web['files']:fail('CANDIDATE_INCOMPLETE')
        archive=next((x for x in windows['files'] if x['path']==web['archive']),None)
        if archive is None:fail('CANDIDATE_INCOMPLETE')
        import zipfile
        with zipfile.ZipFile(settings.candidate_root/web['archive']) as zipped:
            names=zipped.namelist()
            if len(names)>10000 or len(names)!=len(set(names)):fail('CANDIDATE_MANIFEST_INVALID')
            for info in zipped.infolist():
                normalized=info.filename.rstrip('/')
                if not normalized or normalized.startswith('/') or '\\' in normalized or any(part in {'','.','..'} for part in normalized.split('/')) or (info.external_attr>>16)&0o170000==0o120000:fail('CANDIDATE_MANIFEST_INVALID')
            expected=set();root=web['root']
            if not isinstance(root,str) or not root or root.startswith('/') or any(x in {'','.','..'} for x in root.split('/')):fail('CANDIDATE_MANIFEST_INVALID')
            for item in web['files']:
                if set(item)!={'path','bytes','sha256'} or type(item['bytes']) is not int or item['bytes']<0 or not isinstance(item['sha256'],str) or len(item['sha256'])!=64 or not isinstance(item['path'],str) or item['path'].startswith('/') or '\\' in item['path'] or any(x in {'','.','..'} for x in item['path'].split('/')):fail('CANDIDATE_MANIFEST_INVALID')
                key=root+'/'+item['path']
                if key in expected:fail('CANDIDATE_MANIFEST_INVALID')
                expected.add(key);info=zipped.getinfo(key)
                if info.file_size!=item['bytes'] or info.file_size>32*1024*1024:fail('CANDIDATE_ARTIFACT_CHANGED')
                with zipped.open(info) as stream:
                    h=hashlib.sha256()
                    for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
                if h.hexdigest()!=item['sha256']:fail('CANDIDATE_ARTIFACT_CHANGED')
            actual={name for name in zipped.namelist() if name.startswith(root+'/') and not name.endswith('/')}
            if expected!=actual or root+'/index.html' not in actual:fail('CANDIDATE_INCOMPLETE')
        release_path=safe_file(settings.candidate_root,payload['release_manifest'],initial);watched.append(release_path)
        release=verify_envelope(release_path,release_keys(settings.release_public_keys_file),'wordheat-live.maintenance')['payload']
        if release.get('installation_id')!=settings.instance_id or release.get('bundle')!=joint or release.get('schema_version')!=compat['schema_version'] or release.get('app_version')!=components['api']['version']:fail('CANDIDATE_MISMATCH')
        for key in ('contract_version','be_extension_version'):
            if release.get(key,compat[key])!=compat[key]:fail('CANDIDATE_MISMATCH')
        allowed_compat={'contract_version','be_extension_version','schema_version','runtime_platforms','api_version','scorer_version','relay_version','platform_version','windows_version','minimum_schema_version','maximum_schema_version'}
        if set(compat)-allowed_compat:fail('CANDIDATE_COMPATIBILITY_UNSUPPORTED')
        import sys
        if 'runtime_platforms' in compat and (not isinstance(compat['runtime_platforms'],list) or sys.platform not in compat['runtime_platforms']):fail('CANDIDATE_MISMATCH')
        for name in ('api','scorer','relay','platform','windows'):
            key=name+'_version'
            if key in compat and compat[key]!=components[name]['version']:fail('CANDIDATE_MISMATCH')
            if key in release and release[key]!=components[name]['version']:fail('CANDIDATE_MISMATCH')
        for data in (compat,release):
            if 'minimum_schema_version' in data and (type(data['minimum_schema_version']) is not int or data['minimum_schema_version']>1):fail('CANDIDATE_MISMATCH')
            if 'maximum_schema_version' in data and (type(data['maximum_schema_version']) is not int or data['maximum_schema_version']<1):fail('CANDIDATE_MISMATCH')
        for key in ('compat','compatibility'):
            if key in release and release[key]!=compat:fail('CANDIDATE_MISMATCH')
        if 'components' in release and release['components']!=components:fail('CANDIDATE_MISMATCH')
        release_files=release.get('files')
        if not isinstance(release_files,list) or len(release_files)>10000:fail('CANDIDATE_MANIFEST_INVALID')
        release_seen=set()
        for item in release_files:
            if not isinstance(item,dict) or item.get('path') in release_seen:fail('CANDIDATE_MANIFEST_INVALID')
            release_seen.add(item.get('path'));safe_file(settings.candidate_root,item,initial)
        # Every component artifact outside running source must also be sealed by
        # the existing release manifest, not a second unrelated signed package.
        declared={x['path']:x for x in release.get('files',[])}
        for name,component in components.items():
            if name not in {'api','scorer','contract'}:
                for item in component['files']:
                    if declared.get(item['path'])!=item:fail('CANDIDATE_INCOMPLETE')
        for path in [settings.candidate_public_keys_file,settings.release_public_keys_file]:
            if path:watched.append(path)
        for folder in ['apps/api/app','apps/api/migrations','apps/scorer/app','packages/contracts']:
            watched.extend(p for p in (self.source_root/folder).rglob('*') if p.is_dir() and '__pycache__' not in p.parts)
            watched.append(self.source_root/folder)
        fingerprints=initial
        if any(file_identity(Path(name))!=value for name,value in fingerprints.items()):fail('CANDIDATE_ARTIFACT_CHANGED')
        if payload['bundle']!=self.actual_joint() or self.core.scorer.readiness().get('ready') is not True:fail('CANDIDATE_MISMATCH')
        return hashlib.sha256(rfc8785.dumps({k:v for k,v in envelope.items() if k!='signature'})).hexdigest(),inventory,watched,fingerprints,joint
    def refresh(self):
        try:
            digest,inventory,paths,fingerprints,joint=self.verify()
            with self.lock:self._digest=digest;self._inventory=inventory;self.paths=paths;self.fingerprints=fingerprints;self.joint=joint;self.last_error=None
        except Exception as error:self.invalidate(getattr(error,'code','CANDIDATE_MANIFEST_INVALID'))
    def changed(self):
        try:return self.actual_joint()!=self.joint or any(self.signature(Path(name))!=value for name,value in self.fingerprints.items())
        except Exception:return True
    async def start(self):
        if self.task is not None:return
        self.stopping=asyncio.Event();self.task=asyncio.create_task(self.run())
    async def run(self):
        while not self.stopping.is_set():
            if self.current_digest() is None or await asyncio.to_thread(self.changed):
                self.invalidate('CANDIDATE_REVALIDATING');await asyncio.to_thread(self.refresh)
            try:await asyncio.wait_for(self.stopping.wait(),timeout=.5)
            except TimeoutError:pass
    async def stop(self):
        if self.task is None:return
        self.stopping.set();await self.task;self.task=None
