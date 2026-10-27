"""Explicit process-local relay configuration; never merge API configuration."""
from dataclasses import dataclass, field
from pathlib import Path
import os
import stat
from urllib.parse import urlsplit


@dataclass(frozen=True)
class RelaySettings:
    database_url: str = field(repr=False)
    database_name: str
    database_marker: str
    environment: str
    synthetic_test_enabled: bool = False
    douyin_app_id:str|None=None
    douyin_app_secret:str|None=field(default=None,repr=False)
    douyin_push_secret:str|None=field(default=None,repr=False)
    douyin_identity_key_b64url:str|None=field(default=None,repr=False)
    douyin_request_profile:str|None=None
    douyin_channel_profile:str='unverified'
    douyin_channel_id:str|None=None
    douyin_external_calls_enabled:bool=False
    douyin_rsa_key_version:str|None=None
    douyin_rsa_private_key_b64:str|None=field(default=None,repr=False)
    douyin_platform_public_key_b64:str|None=field(default=None,repr=False)

    def __post_init__(self):
        if self.synthetic_test_enabled and self.environment!='test':
            raise ValueError('SYNTHETIC_PROVIDER_REQUIRES_TEST_ENV')
        if self.douyin_request_profile not in (None,'data_open_access_token','openapi_rsa'):raise ValueError('CONFIG_INVALID:DOUYIN_REQUEST_PROFILE')
        if self.douyin_channel_profile not in ('unverified','controlled_test_live','production_verified'):raise ValueError('CONFIG_INVALID:DOUYIN_CHANNEL_PROFILE')

    @classmethod
    def from_mapping(cls, values):
        names=('RELAY_DATABASE_URL','RELAY_DATABASE_NAME','RELAY_DATABASE_MARKER','WORDHEAT_ENV')
        for name in names:
            if not values.get(name) or 'example' in values[name].lower():
                raise ValueError('CONFIG_REQUIRED:'+name)
        database_url=values['RELAY_DATABASE_URL']
        try:
            parsed=urlsplit(database_url.replace('postgresql+psycopg://','postgresql://'))
            valid='?' not in database_url and '#' not in database_url and parsed.scheme=='postgresql' and parsed.hostname and parsed.username and parsed.password and parsed.path=='/'+values['RELAY_DATABASE_NAME']
        except ValueError:
            valid=False
        if not valid: raise ValueError('CONFIG_INVALID:RELAY_DATABASE_URL')
        environment=values['WORDHEAT_ENV']
        if environment not in ('development','test','production'): raise ValueError('CONFIG_INVALID:WORDHEAT_ENV')
        synthetic=values.get('RELAY_SYNTHETIC_TEST_ENABLED')=='true'
        if synthetic and environment!='test': raise ValueError('SYNTHETIC_PROVIDER_REQUIRES_TEST_ENV')
        external=values.get('DOUYIN_EXTERNAL_CALLS_ENABLED','false')
        if external not in ('true','false'):raise ValueError('CONFIG_INVALID:DOUYIN_EXTERNAL_CALLS_ENABLED')
        return cls(database_url,values['RELAY_DATABASE_NAME'],values['RELAY_DATABASE_MARKER'],environment,synthetic,
            douyin_app_id=values.get('DOUYIN_APP_ID') or None,douyin_app_secret=values.get('DOUYIN_APP_SECRET') or None,douyin_push_secret=values.get('DOUYIN_PUSH_SECRET') or None,
            douyin_identity_key_b64url=values.get('DOUYIN_IDENTITY_HMAC_KEY_B64URL') or None,douyin_request_profile=values.get('DOUYIN_REQUEST_PROFILE') or None,
            douyin_channel_profile=values.get('DOUYIN_CHANNEL_PROFILE') or 'unverified',douyin_channel_id=values.get('DOUYIN_CHANNEL_ID') or None,douyin_external_calls_enabled=external=='true',
            douyin_rsa_key_version=values.get('DOUYIN_RSA_KEY_VERSION') or None,douyin_rsa_private_key_b64=values.get('DOUYIN_RSA_PRIVATE_KEY_B64') or None,douyin_platform_public_key_b64=values.get('DOUYIN_PLATFORM_PUBLIC_KEY_B64') or None)

    @classmethod
    def from_file(cls, filename):
        path=Path(filename)
        if not path.is_file(): raise ValueError('CONFIG_FILE_REQUIRED')
        if os.name=='posix' and (stat.S_IMODE(path.stat().st_mode)!=0o600 or path.stat().st_uid!=os.getuid()):
            raise ValueError('CONFIG_FILE_PERMISSIONS_INVALID')
        allowed={'RELAY_DATABASE_URL','RELAY_DATABASE_NAME','RELAY_DATABASE_MARKER','WORDHEAT_ENV','RELAY_SYNTHETIC_TEST_ENABLED','DOUYIN_APP_ID','DOUYIN_APP_SECRET','DOUYIN_PUSH_SECRET','DOUYIN_IDENTITY_HMAC_KEY_B64URL','DOUYIN_REQUEST_PROFILE','DOUYIN_CHANNEL_PROFILE','DOUYIN_CHANNEL_ID','DOUYIN_EXTERNAL_CALLS_ENABLED','DOUYIN_RSA_KEY_VERSION','DOUYIN_RSA_PRIVATE_KEY_B64','DOUYIN_PLATFORM_PUBLIC_KEY_B64'}
        values={}
        for line in path.read_text().splitlines():
            if not line or line.lstrip().startswith('#'): continue
            key,sep,value=line.partition('=')
            if sep and key in allowed: values[key]=value.strip().strip('"\'')
        return cls.from_mapping(values)
