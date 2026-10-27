"""Internal, authenticated relay control. Vendor adapters are explicit ports.

No room fact, release gate or public key is accepted from a Web assertion.
"""
import base64
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
import hashlib
import hmac
import json
import secrets
from uuid import uuid4
import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature
from psycopg.types.json import Jsonb
from .core import RelayError,VerifiedGrant,SourceMessage,opaque_id
from .trust_keys import initial_state,apply_bundle,parse_bundle,TrustKeyError,historical_keys

DOMAIN='wordheat-live.readiness'
TYPES={'live_grant','gate_evidence','revocation','freshness_proof','pairing_intent'}
PROFILE_GATES={'beta':('G0','G1_Q'),'g2_evidence':('G0','G1_Q','G1_R','rc_verified'),'production':('G0','G1_Q','G1_R','rc_verified','G2')}
GATE_FIELDS={'G0':('app_id','candidate_digest'),'G1_Q':('candidate_digest','model_manifest_id','wordbank_manifest_id','calibration_id'),'G1_R':('candidate_digest','installation_id'),'rc_verified':('candidate_digest','installation_id'),'G2':('candidate_digest','installation_id','app_id','room_binding_id')}


def participant_key(raw_identity,key,app_id):
    """Normalize trusted vendor identity at ingress; never persist the raw UID."""
    if not isinstance(raw_identity,str) or not raw_identity or not isinstance(key,bytes) or len(key)<32:raise RelayError('PLATFORM_IDENTITY_REQUIRED',403)
    return 'p:'+hmac.new(key,rfc8785.dumps([app_id,raw_identity]),hashlib.sha256).hexdigest()


def normalized_comment(*,app_id,source_msg_id,raw_identity,display_name,text,identity_key,provider_test=False):
    opaque_id(source_msg_id)
    if not isinstance(text,str) or not isinstance(display_name,str):raise RelayError('PLATFORM_MESSAGE_INVALID',403)
    safe_name=''.join(ch for ch in display_name if ord(ch)>=32).strip()[:80] or 'Viewer'
    if type(provider_test) is not bool:raise RelayError('PLATFORM_MESSAGE_INVALID',403)
    return SourceMessage(source_msg_id,'comment',{'text':text},participant_key(raw_identity,identity_key,app_id),safe_name,provider_test)


def stamp(value):
    try:
        result=value if isinstance(value,datetime) else datetime.fromisoformat(value.replace('Z','+00:00'))
        if result.tzinfo is None:raise ValueError()
        return result
    except (ValueError,TypeError,AttributeError):raise RelayError('EVIDENCE_INVALID',403) from None


def strict(raw):
    def pairs(items):
        obj={}
        for key,value in items:
            if key in obj:raise RelayError('EVIDENCE_INVALID',403)
            obj[key]=value
        return obj
    try:
        if isinstance(raw,(str,bytes)):
            if len(raw)>1048576:raise ValueError()
            value=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        else:value=raw
        rfc8785.dumps(value)
        if not isinstance(value,dict):raise ValueError()
        return value
    except (ValueError,TypeError,UnicodeError):raise RelayError('EVIDENCE_INVALID',403) from None


def unbase(value,size):
    try:
        result=base64.urlsafe_b64decode(value+'='*((4-len(value)%4)%4))
        if len(result)!=size or base64.urlsafe_b64encode(result).rstrip(b'=').decode()!=value:raise ValueError()
        return result
    except (ValueError,TypeError):raise RelayError('EVIDENCE_INVALID',403) from None


def verify_envelope(raw,keys,expected_type=None):
    obj=strict(raw)
    if set(obj)!={'key_id','domain','type','version','payload','signature'} or obj['domain']!=DOMAIN or obj['type'] not in TYPES or type(obj['version']) is not int or obj['version']!=1 or not isinstance(obj['payload'],dict):raise RelayError('EVIDENCE_INVALID',403)
    if expected_type and obj['type']!=expected_type:raise RelayError('EVIDENCE_INVALID',403)
    key=keys.get(obj['key_id'])
    if not key or DOMAIN not in key.get('domains',()) or obj['type'] not in key.get('types',()):raise RelayError('TRUSTED_SIGNER_REQUIRED',403)
    if key.get('revoked'):raise RelayError('EVIDENCE_REVOKED',403)
    unsigned={k:v for k,v in obj.items() if k!='signature'}
    try:Ed25519PublicKey.from_public_bytes(key['public_key']).verify(unbase(obj['signature'],64),rfc8785.dumps(unsigned))
    except (InvalidSignature,ValueError,KeyError,TypeError):raise RelayError('EVIDENCE_INVALID',403) from None
    p=obj['payload']
    if obj['type']=='pairing_intent':
        from .pairing_intent import validate
        try:validate(p)
        except ValueError:raise RelayError('PAIRING_INTENT_INVALID',403) from None
    if obj['type']!='freshness_proof' and (not isinstance(p.get('object_id'),str) or not p['object_id'] or not isinstance(p.get('issuer_id'),str) or not p['issuer_id'] or type(p.get('issuer_revision')) is not int or p['issuer_revision']<1):raise RelayError('EVIDENCE_INVALID',403)
    return obj


class FreshnessSigner:
    def __init__(self,key_id,private_key):self.key_id=key_id;self._private_key=private_key
    def sign(self,payload):
        obj=dict(key_id=self.key_id,domain=DOMAIN,type='freshness_proof',version=1,payload=payload)
        return obj|{'signature':base64.urlsafe_b64encode(self._private_key.sign(rfc8785.dumps(obj))).rstrip(b'=').decode()}


@dataclass(frozen=True)
class ManagementIntent:
    actor_id:str
    app_id:str
    room_id:str
    room_binding_id:str
    installation_id:str
    expires_at:datetime
    device_key_sha256:str|None=None
    anchor_open_id_sha256:str|None=None
    object_id:str|None=None
    digest:str|None=None
    provider_test:bool=False


@dataclass(frozen=True)
class VerifiedRoom:
    app_id:str
    room_id:str
    actor_id:str
    verification_id:str
    expires_at:datetime
    provider_test:bool=False
    anchor_open_id_sha256:str|None=None


class RelayControlPlane:
    def __init__(self,service,*,trusted_keys=None,management_verifier=None,room_verifier=None,signer=None,candidate_digest=None):
        self.service=service;self.keys=dict(trusted_keys or {});self.management=management_verifier;self.rooms=room_verifier;self.signer=signer
        self.candidate_digest=candidate_digest

    def key_state(self,c):
        try:
            state=initial_state(self.keys)
            rows=c.execute('SELECT * FROM relay.key_bundles ORDER BY ordinal LIMIT 1001').fetchall()
            if len(rows)>1000:raise RelayError('TRUST_KEY_CAPACITY_EXCEEDED',503)
            for expected,row in enumerate(rows,1):
                if row['ordinal']!=expected:raise RelayError('TRUST_KEY_INVALID',403)
                result=apply_bundle(row['envelope'],*state)
                if result.digest!=row['digest'] or result.object_id!=row['object_id'] or list(result.root_chain)!=row['root_chain']:raise RelayError('TRUST_KEY_INVALID',403)
            return state
        except TrustKeyError:raise RelayError('TRUST_KEY_INVALID',403) from None

    def effective_keys(self,c):return self.key_state(c)[0]

    def ingress_provenance(self,app,room,*,channel_profile,channel_id,request_profile):
        from .provider import ChannelProvenance
        if channel_profile=='unverified' or not channel_id:return None
        with self.service.tx() as c:
            owner=c.execute('SELECT * FROM relay.room_owners WHERE app_id=%s AND room_id=%s',(app,room)).fetchone();now=self.service.clock()
            if not owner or owner.get('revoked_at') or now>=min(owner['expires_at'],owner['authorization_expires_at']) or self.owner_failure(c,owner,now):return None
            if owner['run_purpose']=='test_live' and channel_profile=='controlled_test_live':return ChannelProvenance(channel_profile,True,'controlled-scope:'+owner['authorization_id'],min(owner['expires_at'],owner['authorization_expires_at']))
            if owner['run_purpose']!='production' or channel_profile!='production_verified':return None
            rows=c.execute("SELECT envelope,payload,revoked FROM relay.trusted_objects WHERE type='gate_evidence' AND payload->>'gate'='G0' AND payload->'scope'->>'app_id'=%s AND payload->'scope'->>'candidate_digest'=%s ORDER BY issuer_revision DESC",(app,owner['candidate_digest'])).fetchall()
            if not rows:return None
            gate=rows[0];provenance=gate['payload'].get('provenance',{})
            if gate['revoked'] or provenance!={'channel_id':channel_id,'request_profile':request_profile,'callback_profile':'data_open_md5','comment_source_verified':True}:return None
            return ChannelProvenance(channel_profile,False,'signed-g0:'+gate['payload']['object_id'],min(owner['expires_at'],owner['authorization_expires_at'],stamp(gate['payload']['expires_at'])))

    def revocation_state(self,c,keys):
        rows=c.execute("SELECT envelope FROM relay.trusted_objects WHERE type='revocation'").fetchall();ids=set();revision=0
        for row in rows:
            p=verify_envelope(row['envelope'],historical_keys(keys),expected_type='revocation')['payload']
            if type(p.get('revocation_revision')) is not int or not isinstance(p.get('revoked_object_ids'),list):raise RelayError('REVOCATION_STATE_INCOMPLETE',403)
            revision=max(revision,p['revocation_revision']);ids.update(p['revoked_object_ids'])
        state=c.execute("SELECT revision,object_ids FROM relay.revocation_state WHERE id='current'").fetchone()
        if (state is None and revision) or (state and (state['revision']!=revision or set(state['object_ids'])!=ids)):raise RelayError('REVOCATION_STATE_INCOMPLETE',403)
        return revision,ids

    def import_key_bundle(self,raw,*,native_command_id,confirmation_id):
        if not native_command_id or not confirmation_id:raise RelayError('NATIVE_CONFIRMATION_REQUIRED',403)
        with self.service.tx() as c:
            c.execute('SELECT pg_advisory_xact_lock(786542396)')
            try:result=apply_bundle(raw,*self.key_state(c));obj=parse_bundle(raw)
            except TrustKeyError:raise RelayError('TRUST_KEY_INVALID',403) from None
            if result.imported:
                ordinal=c.execute('SELECT coalesce(max(ordinal),0)+1 AS n FROM relay.key_bundles').fetchone()['n']
                c.execute('INSERT INTO relay.key_bundles(object_id,ordinal,digest,envelope,root_chain,native_command_id,confirmation_id) VALUES(%s,%s,%s,%s,%s,%s,%s)',(result.object_id,ordinal,result.digest,Jsonb(obj),Jsonb(list(result.root_chain)),native_command_id,confirmation_id))
            return result

    def import_envelope(self,raw):
        with self.service.tx() as c:keys=self.effective_keys(c)
        obj=verify_envelope(raw,keys);p=obj['payload'];kind=obj['type']
        if kind=='freshness_proof':raise RelayError('EVIDENCE_INVALID',403)
        if kind=='gate_evidence' and p.get('result')=='pass' and (keys[obj['key_id']].get('test_only') or p.get('provider_test') is True or p.get('source_mode') in ('synthetic','manual','replay','bridge') or p.get('evidence_kind') in ('fixture','component','synthetic')):raise RelayError('EVIDENCE_INVALID',403)
        if kind=='gate_evidence':
            if p.get('gate') not in GATE_FIELDS or p.get('result') not in ('pass','fail') or not isinstance(p.get('scope'),dict) or any(not p['scope'].get(k) for k in GATE_FIELDS[p['gate']]) or stamp(p.get('expires_at'))<=stamp(p.get('evaluated_at')):raise RelayError('EVIDENCE_INVALID',403)
        digest=hashlib.sha256(rfc8785.dumps({k:v for k,v in obj.items() if k!='signature'})).hexdigest()
        with self.service.tx() as c:
            c.execute('SELECT pg_advisory_xact_lock(786542396)')
            verify_envelope(raw,self.effective_keys(c))
            old=c.execute('SELECT digest FROM relay.trusted_objects WHERE object_id=%s FOR UPDATE',(p['object_id'],)).fetchone()
            if old:
                if old['digest']!=digest:raise RelayError('EVIDENCE_INVALID',403)
                return {'imported':False,'object_id':p['object_id']}
            issuer=c.execute('SELECT revision FROM relay.trusted_issuers WHERE issuer_id=%s FOR UPDATE',(p['issuer_id'],)).fetchone()
            if issuer and issuer['revision']>=p['issuer_revision']:raise RelayError('REVOCATION_REVISION_ROLLBACK',403)
            c.execute('INSERT INTO relay.trusted_objects(object_id,issuer_id,issuer_revision,type,digest,envelope,payload) VALUES(%s,%s,%s,%s,%s,%s,%s)',(p['object_id'],p['issuer_id'],p['issuer_revision'],kind,digest,Jsonb(obj),Jsonb(p)))
            c.execute('INSERT INTO relay.trusted_issuers VALUES(%s,%s) ON CONFLICT(issuer_id) DO UPDATE SET revision=excluded.revision',(p['issuer_id'],p['issuer_revision']))
            if kind=='revocation':
                if set(p)!={'object_id','issuer_id','issuer_revision','revocation_revision','revoked_object_ids'} or type(p['revocation_revision']) is not int or not isinstance(p['revoked_object_ids'],list) or any(not isinstance(v,str) or not v for v in p['revoked_object_ids']):raise RelayError('EVIDENCE_INVALID',403)
                state=c.execute("SELECT * FROM relay.revocation_state WHERE id='current' FOR UPDATE").fetchone()
                if p['revocation_revision']<=(state['revision'] if state else 0):raise RelayError('REVOCATION_REVISION_ROLLBACK',403)
                ids=set(state['object_ids'] if state else [])|set(p['revoked_object_ids'])
                c.execute("INSERT INTO relay.revocation_state VALUES('current',%s,%s) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,object_ids=excluded.object_ids",(p['revocation_revision'],Jsonb(sorted(ids))))
                c.execute('UPDATE relay.trusted_objects SET revoked=true WHERE object_id=ANY(%s)',(list(ids),))
        return {'imported':True,'object_id':p['object_id']}

    def authenticate_management(self,credential,launch_token=None):
        if self.management is None or self.rooms is None:raise RelayError('PLATFORM_AUTHORIZATION_REQUIRED',503)
        intent=self.management.verify(credential)
        if not isinstance(intent,ManagementIntent) or stamp(intent.expires_at)<=self.service.clock():raise RelayError('AUTH_REQUIRED',403)
        for field in ('actor_id','app_id','room_id','room_binding_id','installation_id'):opaque_id(getattr(intent,field))
        room=self.rooms.verify(intent.app_id,intent.room_id,intent.actor_id,launch_token=launch_token) if launch_token is not None else self.rooms.verify(intent.app_id,intent.room_id,intent.actor_id)
        if not isinstance(room,VerifiedRoom) or (room.app_id,room.room_id,room.actor_id)!=(intent.app_id,intent.room_id,intent.actor_id) or stamp(room.expires_at)<=self.service.clock() or not room.verification_id:raise RelayError('PLATFORM_AUTHORIZATION_REQUIRED',403)
        if intent.anchor_open_id_sha256 and intent.anchor_open_id_sha256!=room.anchor_open_id_sha256:raise RelayError('PLATFORM_ANCHOR_MISMATCH',403)
        return intent,room

    def pair(self,credential,body):
        if body.get('action') in ('revoke','query'):
            if self.management is None:raise RelayError('AUTH_REQUIRED',503)
            intent=self.management.verify(credential)
            if not isinstance(intent,ManagementIntent) or stamp(intent.expires_at)<=self.service.clock():raise RelayError('AUTH_REQUIRED',403)
            with self.service.tx() as c:
                row=c.execute('SELECT * FROM relay.device_pairings WHERE pairing_id=%s FOR UPDATE',(body.get('pairing_id'),)).fetchone()
                if not row or (row['app_id'],row['room_id'],row['installation_id'],row['actor_id'])!=(intent.app_id,intent.room_id,intent.installation_id,intent.actor_id):raise RelayError('FORBIDDEN',403)
                if body['action']=='revoke':
                    now=self.service.clock()
                    c.execute("UPDATE relay.device_pairings SET status='revoked',revoked_at=coalesce(revoked_at,%s) WHERE pairing_id=%s",(now,row['pairing_id']))
                    owner=c.execute('SELECT * FROM relay.room_owners WHERE app_id=%s AND room_id=%s',(row['app_id'],row['room_id'])).fetchone()
                    if owner and owner.get('device_binding_id')==row['pairing_id']:
                        o,w,st,current=self.service.lock(c,row['app_id'],row['room_id']);self.service.expire(c,o,w,st,current)
                    return {'pairing_id':row['pairing_id'],'status':'revoked','provider_test':row['provider_test']}
                return {'pairing_id':row['pairing_id'],'status':row['status'],'installation_id':row['installation_id'],'room_binding_id':row['room_binding_id'],'provider_test':row['provider_test'],'room_verified_until':row['room_verified_until'].isoformat()}
        intent,room=self.authenticate_management(credential,body.get('launch_token'))
        now=self.service.clock()
        if body.get('action','issue')=='issue':
            public=unbase(body.get('device_public_key',''),32);pairing_id=str(uuid4())
            if not room.provider_test and intent.device_key_sha256!=hashlib.sha256(public).hexdigest():raise RelayError('PAIRING_INTENT_MISMATCH',403)
            challenge=rfc8785.dumps(dict(domain='wordheat-live.pairing',pairing_id=pairing_id,nonce=secrets.token_urlsafe(32),app_id=intent.app_id,room_id=intent.room_id,room_binding_id=intent.room_binding_id,installation_id=intent.installation_id))
            expires=min(now+timedelta(seconds=60),intent.expires_at,room.expires_at)
            with self.service.tx() as c:
                if intent.object_id:
                    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('pairing-intent:'+intent.object_id,))
                    previous=c.execute('SELECT * FROM relay.pairing_intent_consumptions WHERE object_id=%s FOR UPDATE',(intent.object_id,)).fetchone()
                    if previous:
                        if previous['digest']!=intent.digest or previous['device_key_sha256']!=hashlib.sha256(public).hexdigest():raise RelayError('PAIRING_INTENT_REUSED',403)
                        existing=c.execute('SELECT * FROM relay.device_pairings WHERE pairing_id=%s',(previous['pairing_id'],)).fetchone()
                        return self.pair_view(existing)
                c.execute('INSERT INTO relay.device_pairings(pairing_id,app_id,room_id,room_binding_id,installation_id,actor_id,device_public_key,challenge,status,expires_at,room_verified_until,verification_id,provider_test) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(pairing_id,intent.app_id,intent.room_id,intent.room_binding_id,intent.installation_id,intent.actor_id,public,challenge,'pending',expires,room.expires_at,room.verification_id,room.provider_test))
                if intent.provider_test:c.execute('UPDATE relay.device_pairings SET provider_test=true WHERE pairing_id=%s',(pairing_id,))
                if intent.object_id:c.execute('INSERT INTO relay.pairing_intent_consumptions VALUES(%s,%s,%s,%s)',(intent.object_id,intent.digest,hashlib.sha256(public).hexdigest(),pairing_id))
                row=c.execute('SELECT * FROM relay.device_pairings WHERE pairing_id=%s',(pairing_id,)).fetchone()
                return self.pair_view(row)
        if body.get('action')!='complete':raise RelayError('INPUT_INVALID',422)
        with self.service.tx() as c:
            row=c.execute('SELECT * FROM relay.device_pairings WHERE pairing_id=%s FOR UPDATE',(body.get('pairing_id'),)).fetchone()
            if not row or row['status']!='pending' or row['expires_at']<=now or row['provider_test']!=(room.provider_test or intent.provider_test) or (row['actor_id'],row['app_id'],row['room_id'],row['installation_id'],row['room_binding_id'])!=(intent.actor_id,intent.app_id,intent.room_id,intent.installation_id,intent.room_binding_id):raise RelayError('PAIRING_INVALID',403)
            if not room.provider_test and intent.device_key_sha256!=hashlib.sha256(bytes(row['device_public_key'])).hexdigest():raise RelayError('PAIRING_INTENT_MISMATCH',403)
            try:Ed25519PublicKey.from_public_bytes(bytes(row['device_public_key'])).verify(unbase(body.get('signature',''),64),bytes(row['challenge']))
            except (InvalidSignature,ValueError):raise RelayError('PAIRING_INVALID',403) from None
            token=secrets.token_urlsafe(32);expiry=min(now+timedelta(minutes=15),room.expires_at)
            c.execute("UPDATE relay.device_pairings SET status='bound',device_token_hash=%s,device_token_expires_at=%s WHERE pairing_id=%s",(hashlib.sha256(token.encode()).hexdigest(),expiry,row['pairing_id']))
            updated=c.execute('SELECT * FROM relay.device_pairings WHERE pairing_id=%s',(row['pairing_id'],)).fetchone()
            return self.pair_view(updated)|{'device_token':token,'expires_at':expiry.isoformat(),'server_time':now.isoformat(),'ttl_ms':int((expiry-now).total_seconds()*1000)}

    @staticmethod
    def pair_view(row):
        return {'pairing_id':row['pairing_id'],'status':row['status'],'app_id':row['app_id'],'room_id':row['room_id'],'room_binding_id':row['room_binding_id'],'installation_id':row['installation_id'],'actor_id':row['actor_id'],'challenge_b64':base64.b64encode(bytes(row['challenge'])).decode() if row['status']=='pending' else None,'expires_at':row['expires_at'].isoformat(),'room_verified_until':row['room_verified_until'].isoformat(),'verification_id':row['verification_id'],'device_key_sha256':hashlib.sha256(bytes(row['device_public_key'])).hexdigest(),'provider_test':row['provider_test']}

    def device(self,c,token,now):
        if not isinstance(token,str) or not 0<len(token)<=256:raise RelayError('AUTH_REQUIRED',403)
        digest=hashlib.sha256(token.encode()).hexdigest()
        row=c.execute("SELECT * FROM relay.device_pairings WHERE status='bound' AND revoked_at IS NULL AND (device_token_hash=%s OR (previous_token_hash=%s AND previous_token_expires_at>%s)) FOR UPDATE",(digest,digest,now)).fetchone()
        if not row or row['device_token_expires_at']<=now or row['room_verified_until']<=now:raise RelayError('DEVICE_AUTHORIZATION_EXPIRED',403)
        return row

    def token_challenge(self,token,pairing_id):
        now=self.service.clock()
        with self.service.tx() as c:
            # A challenge discloses no authority. After a restart an expired
            # short token is replaced only by proving the already-paired key.
            pair=c.execute("SELECT * FROM relay.device_pairings WHERE pairing_id=%s AND status='bound' AND revoked_at IS NULL FOR UPDATE",(pairing_id,)).fetchone()
            if not pair or pair['room_verified_until']<=now:raise RelayError('DEVICE_AUTHORIZATION_EXPIRED',403)
            outstanding=c.execute('SELECT count(*) AS n FROM relay.device_token_challenges WHERE pairing_id=%s AND consumed_at IS NULL AND expires_at>%s',(pairing_id,now)).fetchone()['n']
            if outstanding>=3:raise RelayError('RATE_LIMITED',429)
            key=str(uuid4());raw=rfc8785.dumps({'domain':'wordheat-live.device-token','pairing_id':pairing_id,'challenge_id':key,'nonce':secrets.token_urlsafe(32)})
            expires=min(now+timedelta(seconds=60),pair['room_verified_until'])
            c.execute('INSERT INTO relay.device_token_challenges VALUES(%s,%s,%s,%s,NULL)',(key,pairing_id,raw,expires))
            return {'challenge_id':key,'challenge_b64':base64.b64encode(raw).decode(),'expires_at':expires.isoformat()}

    def rotate_token(self,token,challenge_id,signature):
        now=self.service.clock()
        with self.service.tx() as c:
            reference=c.execute('SELECT pairing_id FROM relay.device_token_challenges WHERE challenge_id=%s',(challenge_id,)).fetchone()
            if not reference:raise RelayError('PAIRING_INVALID',403)
            pair=c.execute("SELECT * FROM relay.device_pairings WHERE pairing_id=%s AND status='bound' AND revoked_at IS NULL FOR UPDATE",(reference['pairing_id'],)).fetchone()
            if not pair or pair['room_verified_until']<=now:raise RelayError('DEVICE_AUTHORIZATION_EXPIRED',403)
            challenge=c.execute('SELECT * FROM relay.device_token_challenges WHERE challenge_id=%s AND pairing_id=%s FOR UPDATE',(challenge_id,pair['pairing_id'])).fetchone()
            if not challenge or challenge['consumed_at'] or challenge['expires_at']<=now:raise RelayError('PAIRING_INVALID',403)
            try:Ed25519PublicKey.from_public_bytes(bytes(pair['device_public_key'])).verify(unbase(signature,64),bytes(challenge['challenge']))
            except (InvalidSignature,ValueError):raise RelayError('PAIRING_INVALID',403) from None
            replacement=secrets.token_urlsafe(32);expires=min(now+timedelta(minutes=15),pair['room_verified_until'])
            c.execute('UPDATE relay.device_token_challenges SET consumed_at=%s WHERE challenge_id=%s',(now,challenge_id))
            c.execute('UPDATE relay.device_pairings SET previous_token_hash=device_token_hash,previous_token_expires_at=%s,device_token_hash=%s,device_token_expires_at=%s WHERE pairing_id=%s',(min(now+timedelta(seconds=30),pair['device_token_expires_at']),hashlib.sha256(replacement.encode()).hexdigest(),expires,pair['pairing_id']))
            return {'pairing_id':pair['pairing_id'],'device_token':replacement,'expires_at':expires.isoformat(),'server_time':now.isoformat(),'ttl_ms':int((expires-now).total_seconds()*1000),'provider_test':pair['provider_test']}

    def verify(self,credential):
        try:
            raw=base64.urlsafe_b64decode(credential+'='*((4-len(credential)%4)%4));request=strict(raw)
            if set(request)!={'device_token','authorization_id','session_id'} or any(not isinstance(v,str) or not v for v in request.values()):raise ValueError()
        except (ValueError,TypeError):raise RelayError('AUTH_REQUIRED',403) from None
        now=self.service.clock()
        with self.service.tx() as c:
            c.execute('SELECT pg_advisory_xact_lock_shared(786542396)')
            keys=self.effective_keys(c)
            pair=self.device(c,request['device_token'],now)
            rows=c.execute("SELECT * FROM relay.trusted_objects WHERE type='live_grant' AND payload->>'authorization_id'=%s",(request['authorization_id'],)).fetchall()
            if len(rows)!=1:raise RelayError('LIVE_AUTHORIZATION_REQUIRED',403)
            row=rows[0];obj=verify_envelope(row['envelope'],keys,expected_type='live_grant');p=obj['payload']
            actual=self.candidate_digest() if callable(self.candidate_digest) else self.candidate_digest
            if not actual or actual!=p.get('candidate_digest'):raise RelayError('CANDIDATE_MISMATCH',403)
            revision,revoked_ids=self.revocation_state(c,keys)
            if row['revoked'] or p['object_id'] in revoked_ids or p.get('revoked_at'):raise RelayError('LIVE_AUTHORIZATION_REVOKED',403)
            profile={'controlled_beta':'beta','g2_capture':'g2_evidence','production_release':'production'}.get(p.get('kind'))
            if profile is None or profile not in p.get('allowed_profiles',[]) or (pair['provider_test'] and profile!='beta') or (keys[obj['key_id']].get('test_only') and profile!='beta'):raise RelayError('LIVE_PROFILE_MISMATCH',403)
            if any(p.get(k)!=pair[k] for k in ('app_id','room_binding_id','installation_id')):raise RelayError('LIVE_AUTHORIZATION_INVALID',403)
            start=stamp(p.get('not_before'));end=stamp(p.get('expires_at'))
            if not start<=now<end or end-start>timedelta(days=30 if profile=='production' else 1) or type(p.get('max_session_duration_ms')) is not int or p['max_session_duration_ms']<=0:raise RelayError('LIVE_AUTHORIZATION_EXPIRED',403)
            for gate in GATE_FIELDS:
                candidates=c.execute("SELECT * FROM relay.trusted_objects WHERE type='gate_evidence' AND payload->>'gate'=%s AND payload->'scope'->>'candidate_digest'=%s ORDER BY issuer_revision DESC",(gate,p.get('candidate_digest'))).fetchall()
                matching=[entry for entry in candidates if all(v==p.get(k) for k,v in entry['payload'].get('scope',{}).items() if k in p)]
                if not matching:
                    if gate in PROFILE_GATES[profile]:raise RelayError('READINESS_NOT_MET',403)
                    continue
                evidence=matching[0];ep=verify_envelope(evidence['envelope'],keys,expected_type='gate_evidence')['payload']
                if evidence['revoked'] or ep['object_id'] in revoked_ids or ep.get('result')=='fail':raise RelayError('READINESS_NOT_MET',403)
                if gate in PROFILE_GATES[profile] and (ep.get('result')!='pass' or any(not ep.get('scope',{}).get(k) for k in GATE_FIELDS[gate]) or stamp(ep.get('expires_at'))<=now or stamp(ep.get('evaluated_at'))>now or ep.get('revocation_revision',revision)>revision):raise RelayError('READINESS_NOT_MET',403)
            return VerifiedGrant(pair['app_id'],pair['room_id'],pair['installation_id'],request['session_id'],'production' if profile=='production' else 'test_live',end,authorization_id=p['authorization_id'],candidate_digest=p['candidate_digest'],live_profile=profile,grant_digest=row['digest'],room_binding_id=pair['room_binding_id'],revocation_revision=revision,device_binding_id=pair['pairing_id'],trusted=True,provider_test=pair['provider_test'],max_session_duration_ms=p['max_session_duration_ms'])

    def freshness(self,grant,challenge):
        if self.signer is None:raise RelayError('TRUSTED_SIGNER_REQUIRED',503)
        expected={'authorization_id','candidate_digest','installation_id','boot_id','connection_nonce','request_nonce','revocation_revision'}
        if set(challenge)!=expected or any(challenge[k]!=getattr(grant,k) for k in ('authorization_id','candidate_digest','installation_id')):raise RelayError('FRESHNESS_SCOPE_MISMATCH',403)
        with self.service.tx() as c:
            owner,window,stream,now=self.service.lock(c,grant.app_id,grant.room_id)
            self.service.expire(c,owner,window,stream,now)
            self.service.authorize(owner,owner['lease_id'],now)
            if challenge['connection_nonce']!=owner['connection_nonce'] or type(challenge['revocation_revision']) is not int or challenge['revocation_revision']>grant.revocation_revision:raise RelayError('FRESHNESS_SCOPE_MISMATCH',403)
            if not all(isinstance(challenge[k],str) and 0<len(challenge[k])<=128 for k in ('boot_id','request_nonce')):raise RelayError('INPUT_INVALID',422)
            exists=c.execute('SELECT 1 FROM relay.freshness_challenges WHERE pairing_id=%s AND boot_id=%s AND connection_nonce=%s AND request_nonce=%s',(grant.device_binding_id,challenge['boot_id'],challenge['connection_nonce'],challenge['request_nonce'])).fetchone()
            if exists:raise RelayError('FRESHNESS_REPLAY',403)
            c.execute('INSERT INTO relay.freshness_challenges VALUES(%s,%s,%s,%s,%s)',(grant.device_binding_id,challenge['boot_id'],challenge['connection_nonce'],challenge['request_nonce'],now))
            payload=challenge|{'revocation_revision':grant.revocation_revision,'server_time':now.isoformat(),'ttl_ms':10000}
            produced=self.signer.sign(payload)
            verify_envelope(produced,self.effective_keys(c),expected_type='freshness_proof')
            return produced

    def owner_failure(self,c,owner,now):
        if not owner.get('authorization_id'):return None
        actual=self.candidate_digest() if callable(self.candidate_digest) else self.candidate_digest
        if not actual or actual!=owner.get('candidate_digest'):return 'CANDIDATE_MISMATCH'
        c.execute('SELECT pg_advisory_xact_lock_shared(786542396)')
        keys=self.effective_keys(c)
        if owner.get('device_binding_id'):
            pair=c.execute('SELECT status,revoked_at,room_verified_until,device_token_expires_at FROM relay.device_pairings WHERE pairing_id=%s',(owner['device_binding_id'],)).fetchone()
            if not pair or pair['status']!='bound' or pair['revoked_at'] or pair['room_verified_until']<=now or pair['device_token_expires_at']<=now:return 'PLATFORM_AUTHORIZATION_REQUIRED'
        try:revision,revoked_ids=self.revocation_state(c,keys)
        except RelayError:return 'REVOCATION_STATE_INCOMPLETE'
        row=c.execute("SELECT revoked,payload FROM relay.trusted_objects WHERE type='live_grant' AND payload->>'authorization_id'=%s",(owner['authorization_id'],)).fetchone()
        if not row or row['revoked'] or row['payload'].get('revoked_at'):return 'LIVE_AUTHORIZATION_REVOKED'
        if row['payload']['object_id'] in revoked_ids:return 'LIVE_AUTHORIZATION_REVOKED'
        original=c.execute("SELECT envelope FROM relay.trusted_objects WHERE type='live_grant' AND payload->>'authorization_id'=%s",(owner['authorization_id'],)).fetchone()
        try:verify_envelope(original['envelope'],keys,expected_type='live_grant')
        except RelayError:return 'EVIDENCE_REVOKED'
        if row['payload'].get('candidate_digest')!=owner.get('candidate_digest'):return 'CANDIDATE_MISMATCH'
        if stamp(row['payload']['expires_at'])<=now:return 'LIVE_AUTHORIZATION_EXPIRED'
        for gate in GATE_FIELDS:
            candidates=c.execute("SELECT * FROM relay.trusted_objects WHERE type='gate_evidence' AND payload->>'gate'=%s AND payload->'scope'->>'candidate_digest'=%s ORDER BY issuer_revision DESC",(gate,owner.get('candidate_digest'))).fetchall()
            relevant=[entry for entry in candidates if all(v==row['payload'].get(k) for k,v in entry['payload'].get('scope',{}).items() if k in row['payload'])]
            if not relevant:
                if gate in PROFILE_GATES.get(owner.get('live_profile'),()):return 'READINESS_NOT_MET'
                continue
            entry=relevant[0]
            try:ep=verify_envelope(entry['envelope'],keys,expected_type='gate_evidence')['payload']
            except RelayError:return 'EVIDENCE_REVOKED'
            if entry['revoked'] or ep['object_id'] in revoked_ids or ep.get('result')=='fail':return 'EVIDENCE_REVOKED' if entry['revoked'] or ep['object_id'] in revoked_ids else 'READINESS_NOT_MET'
            if gate in PROFILE_GATES.get(owner.get('live_profile'),()) and stamp(ep.get('expires_at'))<=now:return 'EVIDENCE_EXPIRED'
        return None


class SignedPairingManagementVerifier:
    """Only keys explicitly permitted for pairing_intent may manage a device."""
    def __init__(self,plane):self.plane=plane
    def verify(self,credential):
        from .pairing_intent import validate
        try:raw=base64.urlsafe_b64decode(credential+'='*((4-len(credential)%4)%4));obj=strict(raw)
        except (ValueError,TypeError):raise RelayError('AUTH_REQUIRED',403) from None
        with self.plane.service.tx() as c:
            keys=self.plane.effective_keys(c);verified=verify_envelope(obj,keys,expected_type='pairing_intent');p=verified['payload']
            try:validate(p,self.plane.service.clock())
            except ValueError:raise RelayError('PAIRING_INTENT_INVALID',403) from None
            revision,revoked=self.plane.revocation_state(c,keys)
            if p['object_id'] in revoked:raise RelayError('EVIDENCE_REVOKED',403)
            current=self.plane.candidate_digest() if callable(self.plane.candidate_digest) else self.plane.candidate_digest
            if not current or current!=p['candidate_digest']:raise RelayError('CANDIDATE_MISMATCH',403)
            test=keys[obj['key_id']].get('test_only',False)
        self.plane.import_envelope(obj)
        digest=hashlib.sha256(rfc8785.dumps({k:v for k,v in obj.items() if k!='signature'})).hexdigest()
        return ManagementIntent(p['actor_id'],p['app_id'],p['room_id'],p['room_binding_id'],p['installation_id'],stamp(p['expires_at']),p['device_key_sha256'],p['anchor_open_id_sha256'],p['object_id'],digest,test)
