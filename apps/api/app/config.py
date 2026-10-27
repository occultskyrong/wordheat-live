"""Explicit process-specific configuration. No parent-env merge or root scanning."""
from dataclasses import dataclass,field
from pathlib import Path
import os,re,stat,base64
from sqlalchemy.engine import make_url

API_FIELDS=frozenset({'WORDHEAT_ENV','WORDHEAT_INSTANCE_ID','LOCAL_DATABASE_URL','LOCAL_ANALYTICS_CURSOR_KEY','LOCAL_SOURCE_ROOT','LOCAL_CANDIDATE_ROOT','LOCAL_CANDIDATE_MANIFEST_FILE','LOCAL_CANDIDATE_PUBLIC_KEYS_FILE','LOCAL_GATE_TRUST_ROOTS_FILE','LOCAL_CONNECTOR_REGISTRY_FILE','LOCAL_DEVICE_CREDENTIAL_FILE','LOCAL_DEVICE_PRIVATE_KEY_B64URL','LOCAL_DEVICE_PAIRING_ID','LOCAL_RELEASE_PUBLIC_KEYS_FILE','LOCAL_MAINTENANCE_REGISTRY_FILE','LOCAL_NATIVE_ROOT','LOCAL_MAINTENANCE_ROOT','LOCAL_CONTROL_ROOT','LOCAL_TEST_NAMESPACE_FILE'})
KNOWN_FIELDS=API_FIELDS|{'LOCAL_MIGRATION_DATABASE_URL','WORDHEAT_TEST_DATABASE_URL','WORDHEAT_TEST_MIGRATION_DATABASE_URL'}

def validate_dsn(value):
    try:
        url=make_url(value)
        if url.query or url.drivername not in {'postgresql','postgresql+psycopg'} or url.host not in {'127.0.0.1','localhost','::1'} or not url.database or not url.database.startswith('wordheat_') or 'prod' in url.database.lower() or not url.username or not url.password:raise ValueError()
        return url.set(drivername='postgresql+psycopg').render_as_string(hide_password=False)
    except Exception:raise ValueError('Invalid dedicated local PostgreSQL configuration') from None

def explicit_path(value):
    if value is None:return None
    path=Path(value)
    if not path.is_absolute() or '\x00' in str(path) or any(p=='..' for p in path.parts):raise ValueError('Explicit absolute server path required')
    return path

@dataclass(repr=False)
class Settings:
    database_url:str=field(repr=False)
    instance_id:str='wordheat-local-development'  # Component-only development identity; load_settings requires an explicit installation.
    allowed_hosts:tuple[str,...]=('127.0.0.1:8765',)
    allowed_origins:tuple[str,...]=('http://127.0.0.1:8765',)
    secure_cookies:bool=False
    analytics_cursor_key:bytes|None=field(default=None,repr=False)
    environment:str='development'
    server_root:Path|None=None
    source_root:Path|None=None
    candidate_root:Path|None=None
    candidate_manifest_file:Path|None=None
    candidate_public_keys_file:Path|None=None
    gate_trust_roots_file:Path|None=None
    connector_registry_file:Path|None=None
    device_credential_file:Path|None=field(default=None,repr=False)  # Future encrypted native-store adapter only.
    device_private_key:bytes|None=field(default=None,repr=False)
    device_pairing_id:str|None=None
    release_public_keys_file:Path|None=None
    maintenance_registry_file:Path|None=None
    native_root:Path|None=None
    maintenance_root:Path|None=None
    control_root:Path|None=None
    test_namespace_file:Path|None=field(default=None,repr=False)
    def __post_init__(self):
        self.database_url=validate_dsn(self.database_url)
        if self.analytics_cursor_key is not None and (not isinstance(self.analytics_cursor_key,bytes) or len(self.analytics_cursor_key)!=32):raise ValueError('Invalid analytics cursor key')
        if not isinstance(self.instance_id,str) or not re.fullmatch(r'wordheat-[A-Za-z0-9_.-]{1,112}',self.instance_id):raise ValueError('Invalid WordHeat instance identity')
        if self.environment=='testing':self.environment='test'  # Explicit direct component construction compatibility.
        if self.device_private_key is not None and (not isinstance(self.device_private_key,bytes) or len(self.device_private_key)!=32):raise ValueError('Invalid LOCAL_DEVICE_PRIVATE_KEY_B64URL')
        if self.device_pairing_id is not None and (not isinstance(self.device_pairing_id,str) or not re.fullmatch('[A-Za-z0-9_.:-]{1,128}',self.device_pairing_id)):raise ValueError('Invalid LOCAL_DEVICE_PAIRING_ID')
        if self.environment not in {'development','test','production'}:raise ValueError('Unsupported WORDHEAT_ENV')
        if self.test_namespace_file is not None and self.environment!='test':raise ValueError('LOCAL_TEST_NAMESPACE_FILE is restricted to test environment')
        if not self.allowed_hosts or not self.allowed_origins:raise ValueError('Explicit Host and Origin allowlists required')
        if any(origin.startswith('https://') for origin in self.allowed_origins):self.secure_cookies=True
        for name in ('server_root','source_root','candidate_root','candidate_manifest_file','candidate_public_keys_file','gate_trust_roots_file','connector_registry_file','device_credential_file','release_public_keys_file','maintenance_registry_file','native_root','maintenance_root','control_root','test_namespace_file'):
            setattr(self,name,explicit_path(getattr(self,name)))
        if self.server_root:
            self.native_root=self.native_root or self.server_root/'.local/runtime'
            self.maintenance_root=self.maintenance_root or self.server_root/'.local/maintenance'
            self.control_root=self.control_root or self.server_root/'.local/control'

def read_private_bytes(path,limit=256*1024):
    path=explicit_path(path)
    if path is None:raise ValueError('Private configuration path required')
    if any(p.is_symlink() for p in [path,*path.parents]):raise ValueError('Private configuration symlink rejected')
    try:
        fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_size>limit:raise ValueError('Private configuration owner or mode rejected')
            raw=stream.read(limit+1)
            if len(raw)>limit:raise ValueError('Private configuration too large')
            return raw
    except OSError:raise ValueError('Private configuration unavailable') from None

def read_fields(path,allowed):
    try:text=read_private_bytes(path).decode('utf-8')
    except UnicodeError:raise ValueError('Private configuration must use UTF-8') from None
    result={};seen=set()
    for line in text.splitlines():
        line=line.strip()
        if not line or line.startswith('#'):continue
        name,separator,value=line.partition('=');name=name.strip();value=value.strip()
        if not separator or not re.fullmatch('[A-Z][A-Z0-9_]{0,127}',name) or name not in KNOWN_FIELDS:raise ValueError('Unknown or malformed configuration field')
        if name in seen:raise ValueError('Duplicate configuration field: '+name)
        seen.add(name)
        if value.startswith(('"',"'")):
            if len(value)<2 or value[-1]!=value[0]:raise ValueError('Malformed quoted configuration field: '+name)
            value=value[1:-1]
        if '\x00' in value:raise ValueError('Invalid configuration value')
        if name in allowed:result[name]=value
    return result

def load_settings(root,*,source_root=None):
    root=explicit_path(root)
    if root is None or root.is_symlink() or not root.is_dir():raise ValueError('Explicit configuration root required')
    root=root.resolve(strict=True);fields=read_fields(root/'.env',API_FIELDS)
    if not fields.get('LOCAL_DATABASE_URL') or not fields.get('WORDHEAT_INSTANCE_ID'):raise ValueError('LOCAL_DATABASE_URL and WORDHEAT_INSTANCE_ID are required')
    key=fields.get('LOCAL_ANALYTICS_CURSOR_KEY')
    if key and not re.fullmatch('[0-9a-f]{64}',key):raise ValueError('Invalid LOCAL_ANALYTICS_CURSOR_KEY')
    environment=fields.get('WORDHEAT_ENV') or 'development'
    if environment not in {'development','test','production'}:raise ValueError('Unsupported WORDHEAT_ENV')
    encoded=fields.get('LOCAL_DEVICE_PRIVATE_KEY_B64URL') or None;seed=None
    if encoded:
        try:
            seed=base64.b64decode(encoded+'='*(-len(encoded)%4),altchars=b'-_',validate=True)
            if len(seed)!=32 or base64.urlsafe_b64encode(seed).rstrip(b'=').decode()!=encoded:raise ValueError()
        except Exception:raise ValueError('Invalid LOCAL_DEVICE_PRIVATE_KEY_B64URL') from None
    paths={name.removeprefix('LOCAL_').lower():explicit_path(fields.get(name) or None) for name in API_FIELDS if name.startswith('LOCAL_') and (name.endswith('_FILE') or name.endswith('_ROOT')) and name!='LOCAL_SOURCE_ROOT'}
    source=explicit_path(source_root or fields.get('LOCAL_SOURCE_ROOT') or root)
    return Settings(fields['LOCAL_DATABASE_URL'],instance_id=fields['WORDHEAT_INSTANCE_ID'],analytics_cursor_key=bytes.fromhex(key) if key else None,environment=environment,device_private_key=seed,device_pairing_id=fields.get('LOCAL_DEVICE_PAIRING_ID') or None,server_root=root,source_root=source,**paths)

def load_migration_dsn(root):
    fields=read_fields(explicit_path(root)/'.env',{'LOCAL_MIGRATION_DATABASE_URL'})
    if not fields.get('LOCAL_MIGRATION_DATABASE_URL'):raise ValueError('Missing LOCAL_MIGRATION_DATABASE_URL')
    return validate_dsn(fields['LOCAL_MIGRATION_DATABASE_URL'])
