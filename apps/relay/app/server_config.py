"""Fixed-root relay server configuration. No ambient environment or API config."""
import base64
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from .config import RelaySettings

RUNTIME_FIELDS=frozenset({'RELAY_DATABASE_URL','RELAY_DATABASE_NAME','RELAY_DATABASE_MARKER','WORDHEAT_ENV','RELAY_SYNTHETIC_TEST_ENABLED','DOUYIN_APP_ID','DOUYIN_APP_SECRET','DOUYIN_PUSH_SECRET','DOUYIN_IDENTITY_HMAC_KEY_B64URL','DOUYIN_REQUEST_PROFILE','DOUYIN_CHANNEL_PROFILE','DOUYIN_CHANNEL_ID','DOUYIN_EXTERNAL_CALLS_ENABLED','DOUYIN_RSA_KEY_VERSION','DOUYIN_RSA_PRIVATE_KEY_B64','DOUYIN_PLATFORM_PUBLIC_KEY_B64','RELAY_TRUST_ROOTS_FILE','RELAY_ALLOWED_CANDIDATES_FILE','RELAY_FRESHNESS_KEY_ID','RELAY_FRESHNESS_PRIVATE_KEY_B64URL','RELAY_TEST_NAMESPACE','RELAY_TEST_NAMESPACE_MARKER'})
MIGRATION_FIELDS=frozenset({'RELAY_MIGRATION_DATABASE_URL','RELAY_DATABASE_NAME','RELAY_DATABASE_MARKER','WORDHEAT_ENV','RELAY_RUNTIME_ROLE','RELAY_TEST_NAMESPACE','RELAY_TEST_NAMESPACE_MARKER'})
FORBIDDEN=frozenset({'ACCESS_TOKEN','DOUYIN_ACCESS_TOKEN','RELAY_DEVICE_TOKEN','DEVICE_TOKEN','LAUNCH_TOKEN','BOOT_KEY','RELAY_BOOT_KEY'})
DOMAIN='wordheat-live.readiness'
READINESS_TYPES=frozenset({'gate_evidence','live_grant','revocation','freshness_proof','pairing_intent'})


def _root(path):
    p=Path(path)
    if not p.is_absolute():raise ValueError('CONFIG_ROOT_ABSOLUTE_REQUIRED')
    try:
        for component in [p,*p.parents]:
            if stat.S_ISLNK(component.lstat().st_mode):raise ValueError('CONFIG_SYMLINK_FORBIDDEN')
        info=p.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o022:raise ValueError('CONFIG_ROOT_PERMISSIONS_INVALID')
    except OSError:raise ValueError('CONFIG_ROOT_UNAVAILABLE') from None
    return p


def _read(root,name):
    if not isinstance(name,str) or (name!='.env' and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',name)) or name in {'.','..'}:raise ValueError('CONFIG_FILENAME_INVALID')
    # A directory fd and O_NOFOLLOW protect the fixed file even if replaced.
    try:
        directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
        finally:os.close(directory)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size>1048576:raise ValueError('CONFIG_FILE_PERMISSIONS_INVALID')
            raw=stream.read(1048577)
            if len(raw)>1048576:raise ValueError('CONFIG_FILE_TOO_LARGE')
            return raw
    except OSError:raise ValueError('CONFIG_FILE_UNAVAILABLE') from None


def _fields(root,allowed):
    try:lines=_read(root,'.env').decode('utf-8').splitlines()
    except UnicodeError:raise ValueError('CONFIG_ENCODING_INVALID') from None
    result={}
    for line in lines:
        if not line.strip() or line.lstrip().startswith('#'):continue
        key,sep,value=line.partition('=');key=key.strip();value=value.strip()
        if key in FORBIDDEN and value:raise ValueError('EPHEMERAL_CREDENTIAL_CONFIGURATION_FORBIDDEN')
        if key not in allowed:continue
        if not sep or key in result:raise ValueError('CONFIG_FIELD_INVALID')
        if value[:1] in {'"',"'"}:
            if len(value)<2 or value[-1]!=value[0]:raise ValueError('CONFIG_FIELD_INVALID')
            value=value[1:-1]
        result[key]=value
    return result


def _json(raw):
    def pairs(items):
        value={}
        for key,item in items:
            if key in value:raise ValueError()
            value[key]=item
        return value
    try:return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    except (ValueError,UnicodeError,TypeError):raise ValueError('CONFIG_JSON_INVALID') from None


def _key(value):
    try:
        raw=base64.urlsafe_b64decode(value+'='*((-len(value))%4))
        if len(raw)!=32 or base64.urlsafe_b64encode(raw).rstrip(b'=').decode()!=value:raise ValueError()
        return raw
    except (ValueError,TypeError):raise ValueError('CONFIG_KEY_INVALID') from None


def load_roots(root,filename,environment):
    if not filename:return {}
    obj=_json(_read(root,filename));keys={}
    if not isinstance(obj,dict) or set(obj)!={'version','keys'} or type(obj['version']) is not int or obj['version']!=1 or not isinstance(obj['keys'],list) or len(obj['keys'])>128:raise ValueError('CONFIG_ROOTS_INVALID')
    for row in obj['keys']:
        if not isinstance(row,dict) or set(row)!={'key_id','public_key_b64url','domains','types','test_only'}:raise ValueError('CONFIG_ROOTS_INVALID')
        kid=row['key_id'];domains=row['domains'];types=row['types']
        if not isinstance(kid,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',kid) or kid in keys or not isinstance(domains,list) or not isinstance(types,list) or not domains or not types or any(not isinstance(v,str) for v in domains+types) or len(set(domains))!=len(domains) or len(set(types))!=len(types) or type(row['test_only']) is not bool:raise ValueError('CONFIG_ROOTS_INVALID')
        if set(domains)=={DOMAIN} and set(types)<=READINESS_TYPES:pass
        elif set(domains)=={'wordheat-live.trust'} and set(types)=={'key_bundle'}:pass
        else:raise ValueError('CONFIG_KEY_PURPOSE_INVALID')
        if row['test_only'] and environment!='test':raise ValueError('CONFIG_TEST_KEY_FORBIDDEN')
        public=_key(row['public_key_b64url'])
        if any(item['public_key']==public for item in keys.values()):raise ValueError('CONFIG_PUBLIC_KEY_CONFLICT')
        keys[kid]={'public_key':public,'domains':tuple(domains),'types':tuple(types),'test_only':row['test_only']}
    return keys


@dataclass(frozen=True)
class AllowedCandidates:
    allowed:frozenset[str]=frozenset()
    def permits(self,digest):return isinstance(digest,str) and digest in self.allowed


def load_candidates(root,filename):
    if not filename:return AllowedCandidates()
    obj=_json(_read(root,filename))
    if not isinstance(obj,dict) or set(obj)!={'version','allowed_candidate_digests'} or type(obj['version']) is not int or obj['version']!=1:raise ValueError('CONFIG_CANDIDATES_INVALID')
    values=obj['allowed_candidate_digests']
    if not isinstance(values,list) or len(values)>256 or any(not isinstance(v,str) or not re.fullmatch('[0-9a-f]{64}',v) for v in values) or len(set(values))!=len(values):raise ValueError('CONFIG_CANDIDATES_INVALID')
    return AllowedCandidates(frozenset(values))


def _namespace(values):
    name=values.get('RELAY_TEST_NAMESPACE') or 'relay';marker=values.get('RELAY_TEST_NAMESPACE_MARKER') or None
    if name=='relay':
        if marker:raise ValueError('CONFIG_NAMESPACE_INVALID')
    elif values.get('WORDHEAT_ENV')!='test' or not re.fullmatch(r'wordheat_relay_pytest_[0-9a-f]{32}',name) or not marker or not re.fullmatch(r'[0-9a-f]{32}',marker):raise ValueError('CONFIG_NAMESPACE_INVALID')
    return name,marker


@dataclass(frozen=True)
class RuntimeConfig:
    settings:RelaySettings
    roots:dict=field(repr=False)
    candidates:AllowedCandidates
    signer_key_id:str|None=None
    signer_seed:bytes|None=field(default=None,repr=False)
    namespace:str='relay'
    namespace_marker:str|None=None


def load_runtime(config_root):
    root=_root(config_root);values=_fields(root,RUNTIME_FIELDS)
    if not values.get('DOUYIN_EXTERNAL_CALLS_ENABLED'):values.pop('DOUYIN_EXTERNAL_CALLS_ENABLED',None)
    settings=RelaySettings.from_mapping(values)
    roots=load_roots(root,values.get('RELAY_TRUST_ROOTS_FILE'),settings.environment)
    candidates=load_candidates(root,values.get('RELAY_ALLOWED_CANDIDATES_FILE'))
    kid=values.get('RELAY_FRESHNESS_KEY_ID') or None;encoded=values.get('RELAY_FRESHNESS_PRIVATE_KEY_B64URL') or None;seed=None
    if bool(kid)!=bool(encoded):raise ValueError('CONFIG_SIGNER_INCOMPLETE')
    if kid:
        seed=_key(encoded);public=Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
        key=roots.get(kid)
        if not key or key['public_key']!=public or set(key['domains'])!={DOMAIN} or set(key['types'])!={'freshness_proof'}:raise ValueError('CONFIG_SIGNER_PURPOSE_INVALID')
    namespace,marker=_namespace(values)
    return RuntimeConfig(settings,roots,candidates,kid,seed,namespace,marker)


@dataclass(frozen=True)
class MigrationConfig:
    database_url:str=field(repr=False)
    database_name:str
    database_marker:str
    runtime_role:str
    namespace:str='relay'
    namespace_marker:str|None=None


def load_migration(config_root):
    values=_fields(_root(config_root),MIGRATION_FIELDS)
    adapted=dict(values);adapted['RELAY_DATABASE_URL']=values.get('RELAY_MIGRATION_DATABASE_URL','')
    settings=RelaySettings.from_mapping(adapted);role=values.get('RELAY_RUNTIME_ROLE','')
    if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,62}',role) or urlsplit(settings.database_url.replace('postgresql+psycopg://','postgresql://')).username==role:raise ValueError('CONFIG_RUNTIME_ROLE_INVALID')
    namespace,marker=_namespace(values)
    return MigrationConfig(settings.database_url,settings.database_name,settings.database_marker,role,namespace,marker)
