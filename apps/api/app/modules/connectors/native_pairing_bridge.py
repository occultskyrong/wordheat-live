"""Private native prepare/confirm bridge; no Web setter for keys/tokens/paths."""
import base64
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from uuid import uuid4
import sqlalchemy as sa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PrivateFormat,PublicFormat,NoEncryption
from app.errors import DomainError
from app.infrastructure.db import schema as s
from app.modules.auth.evidence import canonicalize_jcs
from . import models as m


def utc():return datetime.now(timezone.utc)


@dataclass(frozen=True,repr=False)
class _Permit:
    issuer:object
    actor_id:str
    command_id:str
    fingerprint:str


class NativePairingBridge:
    def __init__(self,core,*,env_path,control_root,pairing_client=None,credential_provider=None):
        self.core=core;self.db=core.db;self.env=Path(env_path).absolute();self.control_root=Path(control_root).absolute()
        self.client=pairing_client;self.credentials=credential_provider;self.validator=None;self._issuer=object()
        if self.client:self.client.authorize=self._permit

    def bind_native_validator(self,callback):self.validator=callback

    def _guard(self,actor,context):
        if self.validator is None or self.validator(context,actor) is not True:raise DomainError('NATIVE_CONTEXT_REQUIRED','Verified local OS peer context required',403)
        with self.db.engine.connect() as c:
            row=c.execute(sa.select(s.operators).where(s.operators.c.id==actor)).mappings().first()
            if not row or row['disabled'] or 'maintenance.request' not in row['permissions']:raise DomainError('FORBIDDEN','Native maintenance permission required',403)

    def _read_env(self):
        if os.name!='posix':raise DomainError('WINDOWS_NATIVE_CREDENTIAL_STORE_REQUIRED','Windows uses its native protected credential store',503)
        try:
            fd=os.open(self.env,os.O_RDONLY|os.O_NOFOLLOW)
            with os.fdopen(fd,'rb') as handle:
                meta=os.fstat(handle.fileno())
                if not stat.S_ISREG(meta.st_mode) or meta.st_uid!=os.getuid() or stat.S_IMODE(meta.st_mode)!=0o600:raise ValueError()
                return handle.read(1048577)
        except Exception:raise DomainError('NATIVE_CONFIG_UNSAFE','Product root configuration must be private and owned',403) from None

    def _seed(self):
        raw=self._read_env()
        if len(raw)>1048576:raise DomainError('NATIVE_CONFIG_UNSAFE','Configuration size exceeds native bound',403)
        values=[]
        for line in raw.decode().splitlines():
            key,sep,value=line.partition('=')
            if sep and key.strip()=='LOCAL_DEVICE_PRIVATE_KEY_B64URL':values.append(value.strip().strip('"\''))
        if not values or values==['']:return None
        try:
            if len(values)!=1:raise ValueError()
            value=values[0];seed=base64.urlsafe_b64decode(value+'='*((4-len(value)%4)%4))
            if len(seed)!=32 or base64.urlsafe_b64encode(seed).rstrip(b'=').decode()!=value:raise ValueError()
            return seed
        except Exception:raise DomainError('DEVICE_KEY_INVALID','Existing device seed cannot be overwritten',409) from None

    @staticmethod
    def _public(seed):
        key=Ed25519PrivateKey.from_private_bytes(seed);public=key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
        return {'device_public_key_b64url':base64.urlsafe_b64encode(public).rstrip(b'=').decode(),'device_key_sha256':hashlib.sha256(public).hexdigest()}

    def _write_field(self,name,value,*,only_absent=False):
        import fcntl
        if name not in ('LOCAL_DEVICE_PRIVATE_KEY_B64URL','LOCAL_DEVICE_PAIRING_ID') or not re.fullmatch('[A-Za-z0-9_-]{1,256}',value):raise DomainError('INPUT_INVALID','Native field is not allowed',422)
        lock_fd=os.open(str(self.env)+'.device.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            os.fchmod(lock_fd,0o600);fcntl.flock(lock_fd,fcntl.LOCK_EX)
            raw=self._read_env();lines=raw.splitlines(keepends=True);matches=[]
            for index,line in enumerate(lines):
                key,sep,existing=line.partition(b'=')
                if sep and key.strip()==name.encode():matches.append((index,existing.strip().strip(b'"\'')))
            if len(matches)>1:raise DomainError('NATIVE_CONFIG_UNSAFE','Duplicate native identity field',409)
            if matches and matches[0][1]:
                if only_absent:return False
                if matches[0][1]==value.encode():return False
            newline=(name+'='+value+'\n').encode()
            if matches:lines[matches[0][0]]=newline
            else:
                if lines and not lines[-1].endswith(b'\n'):lines[-1]+=b'\n'
                lines.append(newline)
            fd,temporary=tempfile.mkstemp(prefix='.wordheat-device-',dir=self.env.parent)
            try:
                os.fchmod(fd,0o600)
                with os.fdopen(fd,'wb') as handle:handle.write(b''.join(lines));handle.flush();os.fsync(handle.fileno())
                os.replace(temporary,self.env)
                directory=os.open(self.env.parent,os.O_RDONLY)
                try:os.fsync(directory)
                finally:os.close(directory)
            finally:
                if os.path.exists(temporary):os.unlink(temporary)
            return True
        finally:os.close(lock_fd)

    def _review(self,actor,operation,summary,source_id=None,source_hash=None,fingerprint=None):
        fingerprint=fingerprint or hashlib.sha256(canonicalize_jcs(summary)).hexdigest();nonce=secrets.token_urlsafe(32);cid=str(uuid4())
        with self.db.engine.begin() as c:c.execute(m.native_commands.insert().values(id=cid,actor_id=actor,operation=operation,source_id=source_id,source_sha256=source_hash,fingerprint=fingerprint,nonce_hash=hashlib.sha256(nonce.encode()).hexdigest(),installation_id=self.db.settings.instance_id,boot_id=self.core.boot_id,expires_at=utc()+timedelta(minutes=5),status='pending',result=summary))
        return summary|{'command_id':cid,'fingerprint':fingerprint,'confirmation_nonce':nonce,'installation_id':self.db.settings.instance_id,'operation':operation}

    def prepare_device_key(self,actor_id,*,native_context):
        self._guard(actor_id,native_context);seed=self._seed()
        summary={'existing_key':seed is not None,**(self._public(seed) if seed else {})}
        return self._review(actor_id,'initialize_device_key',summary)

    def _confirm(self,actor,cid,fingerprint,nonce,installation,operation):
        with self.db.engine.begin() as c:
            row=c.execute(sa.select(m.native_commands).where(m.native_commands.c.id==cid).with_for_update()).mappings().first()
            if not row or row['actor_id']!=actor or row['installation_id']!=installation or installation!=self.db.settings.instance_id or row['boot_id']!=self.core.boot_id or row['expires_at']<=utc() or row['fingerprint']!=fingerprint or not isinstance(nonce,str) or not hmac.compare_digest(row['nonce_hash'],hashlib.sha256(nonce.encode()).hexdigest()):raise DomainError('NATIVE_CONFIRMATION_INVALID','Native confirmation changed or expired',403)
            if row['operation']!=operation:raise DomainError('NATIVE_CONFIRMATION_INVALID','Wrong native operation',403)
            if row['status']=='running':raise DomainError('NATIVE_COMMAND_IN_FLIGHT','Native command is already executing',409)
            if row['status']=='failed':raise DomainError(row['last_error'] or 'NATIVE_COMMAND_FAILED','Prepare a new native intent before retrying',409)
            if row['status']=='pending':c.execute(m.native_commands.update().where(m.native_commands.c.id==cid).values(status='running'))
            return dict(row)

    def _finish(self,row,result,error=None):
        with self.db.engine.begin() as c:
            c.execute(m.native_commands.update().where(m.native_commands.c.id==row['id'],m.native_commands.c.status=='running').values(status='failed' if error else 'applied',result=result,last_error=error))
            audit=getattr(self.core,'audit',None)
            if audit:audit(c,row['actor_id'],row['operation'],row['id'],reason=error or 'NATIVE_CONFIRMED',details={'resource_digest':row['fingerprint']})

    def confirm_device_key(self,actor_id,cid,fingerprint,nonce,installation_id,*,native_context):
        self._guard(actor_id,native_context);row=self._confirm(actor_id,cid,fingerprint,nonce,installation_id,'initialize_device_key')
        if row['operation']!='initialize_device_key':raise DomainError('NATIVE_CONFIRMATION_INVALID','Wrong native operation',403)
        if row['status']=='applied':return row['result']
        try:
            seed=self._seed()
            if row['result'].get('device_key_sha256') and (seed is None or self._public(seed)['device_key_sha256']!=row['result']['device_key_sha256']):raise DomainError('DEVICE_KEY_CHANGED','Confirmed native key changed',409)
            if seed is None:
                generated=Ed25519PrivateKey.generate().private_bytes(Encoding.Raw,PrivateFormat.Raw,NoEncryption())
                self._write_field('LOCAL_DEVICE_PRIVATE_KEY_B64URL',base64.urlsafe_b64encode(generated).rstrip(b'=').decode(),only_absent=True);seed=self._seed()
            result={'status':'device_key_ready',**self._public(seed)};self._finish(row,result)
            callback=getattr(self.credentials,'reload_device_seed',None)
            if callback:callback(seed)
            return result
        except DomainError as exc:self._finish(row,{},exc.code);raise

    def _source(self,source_id):
        if not isinstance(source_id,str) or not re.fullmatch('[A-Za-z0-9_-]{1,80}',source_id):raise DomainError('CONTROL_SOURCE_INVALID','Registered logical source id required',422)
        root=self.control_root.resolve(strict=True);path=root/(source_id+'.json')
        try:
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
            with os.fdopen(fd,'rb') as handle:
                meta=os.fstat(handle.fileno())
                if not stat.S_ISREG(meta.st_mode) or meta.st_uid!=os.getuid() or meta.st_mode&0o022:raise ValueError()
                raw=handle.read(1048577)
            if len(raw)>1048576:raise ValueError()
            return raw
        except Exception:raise DomainError('CONTROL_SOURCE_INVALID','Signed control source is unavailable or unsafe',403) from None

    def prepare_pair(self,actor_id,source_id,*,native_context):
        self._guard(actor_id,native_context)
        if self.client is None:raise DomainError('PAIRING_UNAVAILABLE','Native pairing client is not composed',503)
        seed=self._seed()
        if seed is None:raise DomainError('DEVICE_KEY_REQUIRED','Initialize native device key first',409)
        raw=self._source(source_id)
        # The registry selects one explicit pairing target, never a body URL.
        target=getattr(self.client,'default_connector_id',None)
        if not target:
            if len(self.client.registry)!=1:raise DomainError('CONNECTOR_SELECTION_REQUIRED','One registered native pairing target is required',409)
            target=next(iter(self.client.registry))
        summary=self.client.prepare(actor_id=actor_id,connector_id=target,intent_bytes=raw,device_private_key=Ed25519PrivateKey.from_private_bytes(seed))
        return self._review(actor_id,'pair_device',summary,source_id,hashlib.sha256(raw).hexdigest(),summary['resource_digest'])

    def _permit(self,permit,actor,operation,digest):
        if not isinstance(permit,_Permit) or permit.issuer is not self._issuer or (permit.actor_id,operation,permit.fingerprint)!=(actor,'pair_device',digest):raise DomainError('NATIVE_CONFIRMATION_REQUIRED','Bound native pairing permit required',403)
        return permit.command_id,permit.command_id

    def confirm_pair(self,actor_id,cid,fingerprint,nonce,installation_id,launch_token,*,native_context):
        self._guard(actor_id,native_context);row=self._confirm(actor_id,cid,fingerprint,nonce,installation_id,'pair_device')
        if row['operation']!='pair_device':raise DomainError('NATIVE_CONFIRMATION_INVALID','Wrong native operation',403)
        if row['status']=='applied':return row['result']
        if not isinstance(launch_token,str) or not launch_token:raise DomainError('OFFICIAL_LAUNCH_TOKEN_REQUIRED','Native launch token required in memory',403)
        raw=self._source(row['source_id'])
        if hashlib.sha256(raw).hexdigest()!=row['source_sha256']:raise DomainError('CONTROL_SOURCE_CHANGED','Confirmed signed source changed',409)
        seed=self._seed();key=Ed25519PrivateKey.from_private_bytes(seed)
        try:
            result=self.client.pair_device(actor_id=actor_id,connector_id=row['result']['connector_id'],intent_bytes=raw,device_private_key=key,launch_token=launch_token,native_permit=_Permit(self._issuer,actor_id,cid,fingerprint))
            self._write_field('LOCAL_DEVICE_PAIRING_ID',result.pairing_id)
            public=result.public_status();self._finish(row,public)
            callback=getattr(self.credentials,'remember_pairing',None)
            if callback:callback(result.pairing_id,result.credential)
            return public
        except DomainError as exc:self._finish(row,{},exc.code);raise

    def lookup_command(self,c,command_id,actor_id,permissions):
        row=c.execute(sa.select(m.native_commands).where(m.native_commands.c.id==command_id)).mappings().first()
        if not row:return None
        if row['actor_id']!=actor_id or 'maintenance.request' not in permissions:raise DomainError('FORBIDDEN','Native command ownership required',403)
        return {'command_id':command_id,'status':'pending' if row['status'] in ('pending','running') else row['status'],'resource_version':1,'state':row['operation'],'error':{'code':row['last_error'],'message':row['last_error'],'retryable':False} if row['last_error'] else None}
