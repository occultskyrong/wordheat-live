"""Fixed private native pairing use case, never a Web runtime operation."""
import base64
from dataclasses import dataclass
import hashlib
from datetime import datetime,timezone
import httpx
import sqlalchemy as sa
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from app.errors import DomainError
from app.infrastructure.db import schema as s
from app.modules.auth.evidence import verify_signed_envelope,parse_time,canonicalize_jcs
from app.modules.auth.pairing_intent import validate
from app.modules.operations.trusted_control import VerifiedRoomBinding
from .protocol import registered_target
from .transport import NativeDeviceCredential,RelayControlPort
from urllib.parse import urlsplit,urlunsplit


@dataclass(frozen=True,repr=False)
class NativePairingResult:
    pairing_id:str
    binding_id:str
    device_key_sha256:str
    credential:object
    def public_status(self):return {'pairing_id':self.pairing_id,'room_binding_id':self.binding_id,'device_key_sha256':self.device_key_sha256,'status':'paired'}


class NativePairingClient:
    def __init__(self,core,trusted_control,registry,*,native_authorizer=None,test_mode=False,client_factory=None,on_paired=None):
        self.core=core;self.trust=trusted_control;self.registry=registry;self.authorize=native_authorizer;self.test_mode=test_mode
        self.client_factory=client_factory or (lambda:httpx.Client(timeout=5,trust_env=False,follow_redirects=False));self.on_paired=on_paired

    def prepare(self,*,actor_id,connector_id,intent_bytes,device_private_key):
        with self.core.db.engine.begin() as c:
            actor=c.execute(sa.select(s.operators).where(s.operators.c.id==actor_id,s.operators.c.disabled.is_(False))).mappings().first()
            if not actor or 'maintenance.request' not in actor['permissions']:raise DomainError('FORBIDDEN','Native maintenance permission required',403)
            value=verify_signed_envelope(intent_bytes,self.trust.effective_keys(c),expected_type='pairing_intent');p=value.payload
            try:validate(p,datetime.now(timezone.utc))
            except ValueError:raise DomainError('PAIRING_INTENT_INVALID','Management intent not current',403) from None
            if p['object_id'] in self.trust.registry(c).revoked_objects:raise DomainError('EVIDENCE_REVOKED','Pairing intent revoked',403)
            actual=self.trust.candidate_digest() if callable(self.trust.candidate_digest) else self.trust.candidate_digest
            if not actual or actual!=p['candidate_digest']:raise DomainError('CANDIDATE_MISMATCH','Installed candidate does not match management intent',409)
        fp=hashlib.sha256(device_private_key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).hexdigest()
        if p['actor_id']!=actor_id or p['installation_id']!=self.core.db.settings.instance_id or p['device_key_sha256']!=fp:raise DomainError('PAIRING_INTENT_INVALID','Actor/device scope mismatch',403)
        registered_target(connector_id,self.registry,test_mode=self.test_mode)
        raw=intent_bytes if isinstance(intent_bytes,bytes) else intent_bytes.encode()
        summary={'operation':'pair_device','actor_id':actor_id,'installation_id':p['installation_id'],'connector_id':connector_id,'intent_sha256':hashlib.sha256(raw).hexdigest(),'device_key_sha256':fp,'app_id':p['app_id'],'room_id':p['room_id'],'room_binding_id':p['room_binding_id']}
        return summary|{'resource_digest':hashlib.sha256(canonicalize_jcs(summary)).hexdigest()}

    def pair_device(self,*,actor_id,connector_id,intent_bytes,device_private_key,launch_token,native_permit):
        if self.authorize is None:raise DomainError('NATIVE_CONFIRMATION_REQUIRED','Native confirmation adapter is unavailable',403)
        with self.core.db.engine.begin() as c:
            actor=c.execute(sa.select(s.operators).where(s.operators.c.id==actor_id,s.operators.c.disabled.is_(False))).mappings().first()
            if not actor or 'maintenance.request' not in actor['permissions']:raise DomainError('FORBIDDEN','Native maintenance permission required',403)
            envelope=verify_signed_envelope(intent_bytes,self.trust.effective_keys(c),expected_type='pairing_intent');p=envelope.payload
            try:validate(p,datetime.now(timezone.utc))
            except ValueError:raise DomainError('PAIRING_INTENT_INVALID','Management intent is not current',403) from None
            if p['object_id'] in self.trust.registry(c).revoked_objects:raise DomainError('EVIDENCE_REVOKED','Pairing management intent revoked',403)
            actual=self.trust.candidate_digest() if callable(self.trust.candidate_digest) else self.trust.candidate_digest
            if not actual or actual!=p['candidate_digest']:raise DomainError('CANDIDATE_MISMATCH','Installed candidate changed',409)
        public=device_private_key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw);fingerprint=hashlib.sha256(public).hexdigest()
        digest=hashlib.sha256(intent_bytes if isinstance(intent_bytes,bytes) else intent_bytes.encode()).hexdigest()
        if p['actor_id']!=actor_id or p['installation_id']!=self.core.db.settings.instance_id or p['device_key_sha256']!=fingerprint:raise DomainError('PAIRING_INTENT_INVALID','Native actor/device scope mismatch',403)
        proposal=canonicalize_jcs({'operation':'pair_device','actor_id':actor_id,'installation_id':p['installation_id'],'connector_id':connector_id,'intent_sha256':digest,'device_key_sha256':fingerprint,'app_id':p['app_id'],'room_id':p['room_id'],'room_binding_id':p['room_binding_id']})
        proposal_digest=hashlib.sha256(proposal).hexdigest()
        command_id,confirmation_id=self.authorize(native_permit,actor_id,'pair_device',proposal_digest)
        if not command_id or not confirmation_id:raise DomainError('NATIVE_CONFIRMATION_REQUIRED','Bound native confirmation required',403)
        target=registered_target(connector_id,self.registry,test_mode=self.test_mode);u=urlsplit(target['url']);url=urlunsplit(('https' if u.scheme=='wss' else 'http',u.netloc,'/relay/v1/device-pairings','',''))
        # Canonical envelope preserves signed fields; short launch token is
        # present only in the TLS request, never in audits/DB/env/argv.
        from app.modules.auth.evidence import parse_envelope
        obj=parse_envelope(intent_bytes);credential=base64.urlsafe_b64encode(canonicalize_jcs(obj)).rstrip(b'=').decode()
        def request(body):
            try:
                with self.client_factory() as client:response=client.post(url,headers={'Authorization':'Bearer '+credential,'X-BE-Extension-Version':'LW-BE/1.1.0'},json=body)
                value=response.json()
                if response.status_code!=200:raise DomainError(value.get('error',{}).get('code','PAIRING_FAILED'),'Relay pairing was rejected',response.status_code)
                if value.get('be_extension_version')!='LW-BE/1.1.0':raise DomainError('BE_EXTENSION_VERSION_UNSUPPORTED','Pairing extension not negotiated',409)
                for key in ('app_id','room_id','room_binding_id','installation_id','actor_id'):
                    if value.get(key)!=p[key]:raise DomainError('PAIRING_INTENT_INVALID','Returned pairing scope changed',403)
                if value.get('device_key_sha256')!=fingerprint:raise DomainError('PAIRING_INTENT_INVALID','Returned device key changed',403)
                return value
            except DomainError:raise
            except Exception:raise DomainError('PAIRING_UNAVAILABLE','Registered relay pairing request failed',503) from None
        issued=request({'action':'issue','device_public_key':base64.urlsafe_b64encode(public).rstrip(b'=').decode(),'launch_token':launch_token})
        if issued['status']=='pending':
            raw=base64.b64decode(issued['challenge_b64'],validate=True);challenge=__import__('json').loads(raw)
            if challenge.get('pairing_id')!=issued['pairing_id'] or challenge.get('installation_id')!=p['installation_id'] or challenge.get('room_binding_id')!=p['room_binding_id']:raise DomainError('PAIRING_INTENT_INVALID','Server device challenge scope changed',403)
            signature=base64.urlsafe_b64encode(device_private_key.sign(raw)).rstrip(b'=').decode()
            paired=request({'action':'complete','pairing_id':issued['pairing_id'],'signature':signature,'launch_token':launch_token})
        elif issued['status']=='bound':paired=issued
        else:raise DomainError('PAIRING_FAILED','Pairing is not bound',403)
        native_credential=NativeDeviceCredential(paired['pairing_id'],device_private_key,paired.get('device_token'),paired.get('ttl_ms',0),provider_test=paired['provider_test'])
        if not paired.get('device_token'):
            port=RelayControlPort(self.registry,native_credential,test_mode=self.test_mode,client_factory=self.client_factory)
            native_credential.refresh_if_due(port,{'connector_id':connector_id})
        fact=VerifiedRoomBinding(p['room_binding_id'],p['app_id'],p['room_id'],p['installation_id'],connector_id,(actor_id,),parse_time(paired['room_verified_until']),paired['verification_id'],paired['provider_test'])
        with self.core.db.engine.begin() as c:
            current=c.execute(sa.select(s.operators).where(s.operators.c.id==actor_id).with_for_update(read=True)).mappings().first()
            if not current or current['disabled'] or 'maintenance.request' not in current['permissions']:raise DomainError('FORBIDDEN','Native permission changed before binding commit',403)
            self.trust.register_room_binding(c,fact)
            audit=getattr(self.core,'audit',None)
            if audit:audit(c,actor_id,'pair_device',p['room_binding_id'],reason='NATIVE_CONFIRMED_PLATFORM_VERIFIED',details={'command_id':command_id,'confirmation_id':confirmation_id,'intent_sha256':digest,'device_key_sha256':fingerprint,'pairing_id':paired['pairing_id']})
        result=NativePairingResult(paired['pairing_id'],p['room_binding_id'],fingerprint,native_credential)
        if self.on_paired:self.on_paired(result)
        return result
