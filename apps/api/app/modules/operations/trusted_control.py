"""Restricted control-plane service. Never expose imports/key setters to Web routes.

Keys are supplied by controlled native initialization. Persistent objects retain
their original signed envelopes; each read re-verifies against current trust.
"""
from dataclasses import dataclass,replace
from datetime import datetime,timedelta,timezone
import time
from uuid import uuid4
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from app.errors import DomainError
from app.modules.auth.evidence import (verify_signed_envelope,parse_time,canonicalize_jcs,
    EvidenceRegistry,NonceRegistry,accept_freshness_proof,validate_gate_lifecycle,REQUIRED_GATES)
from app.modules.connectors import models as m
from app.infrastructure.db import schema as s
from app.modules.auth.trust_keys import initial_state,apply_bundle,parse_bundle,historical_keys,TrustKeyError


def fail(code,message=None,status=403):raise DomainError(code,message or code,status)
def utc():return datetime.now(timezone.utc)


@dataclass(frozen=True)
class VerifiedRoomBinding:
    binding_id:str
    app_id:str
    room_id:str
    installation_id:str
    connector_id:str
    actor_ids:tuple[str,...]
    expires_at:datetime
    verification_id:str
    provider_test:bool=False


class TrustedControl:
    def __init__(self,database,trusted_keys=None,*,candidate_digest=None,component_inventory=None,boot_id=None,now=utc,now_ns=time.monotonic_ns):
        self.db=database;self.keys=dict(trusted_keys or {});self.candidate_digest=candidate_digest
        self.boot_id=boot_id or str(uuid4());self.now=now;self.now_ns=now_ns
        self.component_inventory=dict(component_inventory or {})
        self._nonces={};self._runtime={}

    def key_state(self,c):
        try:
            state=initial_state(self.keys)
            rows=c.execute(sa.select(m.key_bundles).order_by(m.key_bundles.c.ordinal).limit(1001)).mappings().all()
            if len(rows)>1000:fail('TRUST_KEY_CAPACITY_EXCEEDED',status=503)
            for expected,row in enumerate(rows,1):
                if row['ordinal']!=expected:fail('TRUST_KEY_INVALID')
                result=apply_bundle(row['envelope'],*state)
                if result.digest!=row['digest'] or result.object_id!=row['object_id'] or list(result.root_chain)!=row['root_chain'].get('ids'):fail('TRUST_KEY_INVALID')
            return state
        except TrustKeyError:fail('TRUST_KEY_INVALID')

    def effective_keys(self,c):return self.key_state(c)[0]

    def import_key_bundle(self,c,raw,*,native_command_id,confirmation_id):
        if not native_command_id or not confirmation_id:fail('NATIVE_CONFIRMATION_REQUIRED')
        c.execute(sa.text('SELECT pg_advisory_xact_lock(786542395)'))
        state=self.key_state(c)
        try:result=apply_bundle(raw,*state);envelope=parse_bundle(raw)
        except TrustKeyError:fail('TRUST_KEY_INVALID')
        if result.imported:
            ordinal=(c.execute(sa.select(sa.func.max(m.key_bundles.c.ordinal))).scalar_one() or 0)+1
            c.execute(m.key_bundles.insert().values(id=str(uuid4()),ordinal=ordinal,object_id=result.object_id,digest=result.digest,envelope=envelope,root_chain={'ids':list(result.root_chain)},native_command_id=native_command_id,confirmation_id=confirmation_id))
        return result

    def import_envelope(self,c,raw):
        if not self.keys:fail('TRUSTED_CONTROL_PLANE_REQUIRED',status=503)
        if isinstance(raw,(str,bytes)) and len(raw)>1048576:fail('EVIDENCE_INVALID')
        keys=self.effective_keys(c);verified=verify_signed_envelope(raw,keys)
        if verified.type=='freshness_proof':fail('EVIDENCE_INVALID','Freshness proofs must answer a live challenge')
        p=verified.payload;key=keys[verified.key_id]
        if verified.type=='gate_evidence':
            if p.get('result')=='pass' and (key.get('test_only') or p.get('provider_test') is True or p.get('source_mode') in ('synthetic','manual','replay','bridge') or p.get('evidence_kind') in ('component','fixture','synthetic')):
                fail('EVIDENCE_INVALID','Synthetic/component evidence cannot establish a live gate')
            if p.get('gate') not in {'G0','G1_Q','G1_R','rc_verified','G2'} or p.get('result') not in ('pass','fail') or not isinstance(p.get('scope'),dict):fail('EVIDENCE_INVALID')
            if parse_time(p.get('expires_at'))<=parse_time(p.get('evaluated_at')):fail('EVIDENCE_INVALID')
        if verified.type=='live_grant':self.validate_grant(p,check_time=False,test_only=key.get('test_only',False))
        c.execute(sa.text('SELECT pg_advisory_xact_lock(786542395)'))
        old=c.execute(sa.select(m.trust_objects).where(m.trust_objects.c.object_id==p['object_id']).with_for_update()).mappings().first()
        if old:
            if old['digest']!=verified.digest:fail('EVIDENCE_INVALID','Object ID reused with different signed content')
            return {'imported':False,'object_id':p['object_id'],'digest':verified.digest}
        issuer=c.execute(sa.select(m.trust_issuers).where(m.trust_issuers.c.id==p['issuer_id']).with_for_update()).mappings().first()
        if issuer and p['issuer_revision']<=issuer['revision']:fail('EVIDENCE_INVALID','Issuer revision rollback')
        envelope=raw if isinstance(raw,dict) else __import__('json').loads(raw)
        c.execute(m.trust_objects.insert().values(id=str(uuid4()),object_id=p['object_id'],issuer_id=p['issuer_id'],issuer_revision=p['issuer_revision'],type=verified.type,key_id=verified.key_id,digest=verified.digest,envelope=envelope,payload=p))
        c.execute(insert(m.trust_issuers).values(id=p['issuer_id'],revision=p['issuer_revision']).on_conflict_do_update(index_elements=['id'],set_={'revision':p['issuer_revision']}))
        if verified.type=='revocation':
            expected={'object_id','issuer_id','issuer_revision','revocation_revision','revoked_object_ids'}
            if set(p)!=expected or type(p['revocation_revision']) is not int or p['revocation_revision']<0 or not isinstance(p['revoked_object_ids'],list) or any(not isinstance(v,str) or not v for v in p['revoked_object_ids']):fail('EVIDENCE_INVALID')
            state=c.execute(sa.select(m.trust_state).where(m.trust_state.c.id=='current').with_for_update()).mappings().first()
            revision=state['revision'] if state else 0
            if p['revocation_revision']<=revision:fail('REVOCATION_REVISION_ROLLBACK')
            ids=set(state['object_ids'].get('ids',[]) if state else [])|set(p['revoked_object_ids'])
            c.execute(insert(m.trust_state).values(id='current',revision=p['revocation_revision'],object_ids={'ids':sorted(ids)}).on_conflict_do_update(index_elements=['id'],set_={'revision':p['revocation_revision'],'object_ids':{'ids':sorted(ids)}}))
            c.execute(m.trust_objects.update().where(m.trust_objects.c.object_id.in_(ids)).values(revoked=True))
        return {'imported':True,'object_id':p['object_id'],'digest':verified.digest}

    def registry(self,c):
        registry=EvidenceRegistry()
        keys=self.effective_keys(c);historical=historical_keys(keys)
        revoked_ids=set();signed_revision=0
        rows=c.execute(sa.select(m.trust_objects).order_by(m.trust_objects.c.issuer_id,m.trust_objects.c.issuer_revision)).mappings().all()
        for row in rows:
            v=verify_signed_envelope(row['envelope'],historical)
            if v.digest!=row['digest']:fail('EVIDENCE_INVALID')
            registry.accept(v)
            if row['revoked'] or keys[v.key_id].get('revoked'):registry.revoked_objects.add(row['object_id'])
            if v.type=='revocation':
                p=v.payload
                if type(p.get('revocation_revision')) is not int or not isinstance(p.get('revoked_object_ids'),list):fail('REVOCATION_STATE_INCOMPLETE')
                signed_revision=max(signed_revision,p['revocation_revision']);revoked_ids.update(p['revoked_object_ids'])
        state=c.execute(sa.select(m.trust_state).where(m.trust_state.c.id=='current')).mappings().first()
        if (state is None and signed_revision) or (state and (state['revision']!=signed_revision or set(state['object_ids'].get('ids',[]))!=revoked_ids)):fail('REVOCATION_STATE_INCOMPLETE')
        registry.apply_revocations(revoked_ids,signed_revision)
        return registry

    def object(self,c,object_id):
        row=c.execute(sa.select(m.trust_objects).where(m.trust_objects.c.object_id==object_id)).mappings().first()
        if not row:fail('LIVE_AUTHORIZATION_REQUIRED')
        verified=verify_signed_envelope(row['envelope'],self.effective_keys(c))
        if verified.digest!=row['digest']:fail('EVIDENCE_INVALID')
        self.registry(c).require_active(verified)
        return verified

    def validate_grant(self,p,*,check_time=True,test_only=False):
        profiles={'controlled_beta':'beta','g2_capture':'g2_evidence','production_release':'production'}
        profile=profiles.get(p.get('kind'))
        if profile is None or p.get('allowed_profiles')!=[profile]:fail('LIVE_AUTHORIZATION_INVALID')
        for name in ('authorization_id','app_id','room_binding_id','installation_id','candidate_digest'):
            if not isinstance(p.get(name),str) or not p[name]:fail('LIVE_AUTHORIZATION_INVALID')
        start=parse_time(p.get('not_before'));end=parse_time(p.get('expires_at'))
        if end<=start or end-start>timedelta(days=30 if profile=='production' else 1) or type(p.get('max_session_duration_ms')) is not int or not 0<p['max_session_duration_ms']<=30*86400000:fail('LIVE_AUTHORIZATION_INVALID')
        if test_only and profile!='beta':fail('TEST_PROVIDER_FORBIDDEN')
        if check_time:
            if self.now()<start or self.now()>=end:fail('LIVE_AUTHORIZATION_EXPIRED')
        return profile

    def get_grant(self,c,authorization_id):
        rows=c.execute(sa.select(m.trust_objects.c.object_id).where(m.trust_objects.c.type=='live_grant',m.trust_objects.c.payload['authorization_id'].astext==authorization_id)).scalars().all()
        if len(rows)!=1:fail('LIVE_AUTHORIZATION_REQUIRED')
        verified=self.object(c,rows[0]);self.validate_grant(verified.payload,test_only=self.effective_keys(c)[verified.key_id].get('test_only',False))
        return verified

    def register_room_binding(self,c,fact):
        if not isinstance(fact,VerifiedRoomBinding) or parse_time(fact.expires_at)<=self.now() or not fact.verification_id or not fact.actor_ids:fail('PLATFORM_AUTHORIZATION_REQUIRED')
        for key in ('binding_id','app_id','room_id','installation_id','connector_id'):
            if not isinstance(getattr(fact,key),str) or not 0<len(getattr(fact,key))<=256:fail('PLATFORM_AUTHORIZATION_REQUIRED')
        if not isinstance(fact.actor_ids,(tuple,list)) or any(not isinstance(v,str) or not 0<len(v)<=256 for v in fact.actor_ids) or type(fact.provider_test) is not bool:fail('PLATFORM_AUTHORIZATION_REQUIRED')
        c.execute(sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:identity,0))"),{'identity':'room-binding:'+fact.binding_id})
        existing=c.execute(sa.select(m.room_bindings).where(m.room_bindings.c.id==fact.binding_id).with_for_update()).mappings().first()
        if existing and any(existing[key]!=getattr(fact,key) for key in ('app_id','room_id','installation_id','connector_id')):fail('ROOM_BINDING_IMMUTABLE','Changed room/installation/connector requires a new binding and authorization',409)
        values=dict(id=fact.binding_id,app_id=fact.app_id,room_id=fact.room_id,installation_id=fact.installation_id,connector_id=fact.connector_id,status='pass',expires_at=fact.expires_at,authorization={'actor_ids':list(fact.actor_ids),'verification_id':fact.verification_id,'provider_test':fact.provider_test})
        c.execute(insert(m.room_bindings).values(**values).on_conflict_do_update(index_elements=['id'],set_={k:v for k,v in values.items() if k!='id'}))

    def authorize_create(self,c,actor,body):
        profile=body.get('live_profile');purpose=body.get('run_purpose')
        if body.get('mode')!='official' or (purpose,profile) not in {('test_live','beta'),('test_live','g2_evidence'),('production','production')}:fail('LIVE_PROFILE_MISMATCH',status=409)
        binding=c.execute(sa.select(m.room_bindings).where(m.room_bindings.c.id==body.get('room_binding_id')).with_for_update(read=True)).mappings().first()
        if not binding or binding['status']!='pass' or not binding['expires_at'] or binding['expires_at']<=self.now() or actor not in binding['authorization'].get('actor_ids',[]):fail('PLATFORM_AUTHORIZATION_REQUIRED')
        if binding['installation_id']!=self.db.settings.instance_id:fail('INSTALLATION_MISMATCH')
        if binding['authorization'].get('provider_test'):fail('TEST_PROVIDER_FORBIDDEN','Synthetic room facts never authorize application official mode')
        digest=self.candidate_digest() if callable(self.candidate_digest) else self.candidate_digest
        if not digest:fail('CANDIDATE_MISMATCH',status=409)
        rows=c.execute(sa.select(m.trust_objects).where(m.trust_objects.c.type=='live_grant',m.trust_objects.c.revoked.is_(False)).order_by(m.trust_objects.c.issuer_revision.desc())).mappings().all()
        for row in rows:
            p=row['payload']
            expected=dict(app_id=binding['app_id'],room_binding_id=binding['id'],installation_id=binding['installation_id'],candidate_digest=digest)
            if any(p.get(k)!=v for k,v in expected.items()) or profile not in p.get('allowed_profiles',[]):continue
            grant=self.get_grant(c,p['authorization_id']);self.validate_grant(grant.payload)
            return expected|dict(room_id=binding['room_id'],live_profile=profile,authorization_id=p['authorization_id'],grant_digest=grant.digest,authorization_boot_id=self.boot_id,authorization_elapsed_ms=0,session_mono_start_ns=None)
        fail('LIVE_AUTHORIZATION_REQUIRED')

    def gate_items(self,c,context):
        result={};registry=self.registry(c);keys=self.effective_keys(c)
        rows=c.execute(sa.select(m.trust_objects).where(m.trust_objects.c.type=='gate_evidence').order_by(m.trust_objects.c.issuer_revision)).mappings().all()
        for row in rows:
            p=row['payload'];scope=p.get('scope',{})
            if scope.get('candidate_digest')!=context.get('candidate_digest'):continue
            verified=verify_signed_envelope(row['envelope'],historical_keys(keys))
            revoked=row['revoked'] or keys[verified.key_id].get('revoked')
            status='fail' if revoked or p.get('result')=='fail' else 'pass'
            if status=='pass':
                try:validate_gate_lifecycle(verified,self.now(),registry.revocation_revision)
                except DomainError:status='pending'
            result[p['gate']]={'status':status,'reason_code':'EVIDENCE_REVOKED' if revoked else 'SIGNED_EVIDENCE','message':'','evidence':verified}
        return result

    def challenge(self,c,session,connection):
        nonce=self._nonces.get(session['id'])
        if nonce is None or nonce.connection_nonce!=connection['connection_nonce']:
            nonce=NonceRegistry(self.boot_id,connection['connection_nonce'],self.registry(c));self._nonces[session['id']]=nonce
        request=str(uuid4());nonce.issue(request,self.now_ns())
        return dict(authorization_id=session['authorization_id'],candidate_digest=session['candidate_digest'],installation_id=session['installation_id'],boot_id=self.boot_id,connection_nonce=connection['connection_nonce'],request_nonce=request,revocation_revision=self.registry(c).revocation_revision)

    def accept_proof(self,c,session,proof,*,lease_deadline_ns,clock_trusted):
        grant=self.get_grant(c,session['authorization_id']);p=grant.payload
        if session.get('grant_digest')!=grant.digest or any(session.get(k)!=p.get(k) for k in ('app_id','room_binding_id','installation_id','candidate_digest')):fail('CANDIDATE_MISMATCH',status=409)
        gates=self.gate_items(c,session);required=[gates[k]['evidence'] for k in REQUIRED_GATES[session['live_profile']] if k in gates]
        nonce=self._nonces.get(session['id'])
        if nonce is None:fail('FRESHNESS_PROOF_REQUIRED')
        nonce.evidence_registry=self.registry(c)
        verified=verify_signed_envelope(proof,self.effective_keys(c),expected_type='freshness_proof')
        state=c.execute(sa.select(m.bindings).where(m.bindings.c.session_id==session['id']).with_for_update()).mappings().one()
        start=state['session_started_ns'] or None
        if start and state['boot_id']!=self.boot_id:fail('LIVE_AUTHORIZATION_EXPIRED','Cannot prove a previous boot session budget')
        runtime=accept_freshness_proof(verified,grant=grant,required_evidence=required,nonces=nonce,revocation_revision=nonce.evidence_registry.revocation_revision,lease_mono_safe_deadline=lease_deadline_ns,now_ns=self.now_ns,clock_trusted=clock_trusted,session_mono_start_ns=start)
        self._runtime[session['id']]=runtime
        c.execute(m.bindings.update().where(m.bindings.c.id==state['id']).values(authorization_deadline_ns=runtime.mono_deadline,boot_id=self.boot_id))
        return runtime

    def require_current(self,c,session):
        # A revocation import cannot commit between this guard and the result
        # transaction's commit. The importer takes the exclusive counterpart.
        c.execute(sa.text('SELECT pg_advisory_xact_lock_shared(786542395)'))
        runtime=self._runtime.get(session['id'])
        if runtime is None:fail('FRESHNESS_PROOF_REQUIRED')
        proof_key=self.effective_keys(c).get(runtime.proof_key_id)
        if not proof_key or proof_key.get('revoked') or 'wordheat-live.readiness' not in proof_key.get('domains',()) or 'freshness_proof' not in proof_key.get('types',()):fail('EVIDENCE_REVOKED','Freshness signing key revoked')
        runtime.nonces.evidence_registry=self.registry(c)
        runtime.nonces.advance_revocation(runtime.nonces.evidence_registry.revocation_revision)
        grant=self.get_grant(c,session['authorization_id'])
        context=dict(session)
        if 'joint_identity' in s.catalog.c:
            joint=c.execute(sa.select(s.catalog.c.joint_identity).where(s.catalog.c.id=='global')).scalar_one_or_none() or {}
            if joint:context.update(model_manifest_id=joint['model_manifest_id'],wordbank_manifest_id=joint['wordbank_version'],calibration_id=joint['calibration_version'])
        if not runtime.matches(grant,context):fail('CANDIDATE_MISMATCH',status=409)
        digest=self.candidate_digest() if callable(self.candidate_digest) else self.candidate_digest
        if digest!=session.get('candidate_digest'):fail('CANDIDATE_MISMATCH',status=409)
        runtime.require_current()
        return runtime

    def mark_started(self,c,session):
        runtime=self.require_current(c,session)
        state=c.execute(sa.select(m.bindings).where(m.bindings.c.session_id==session['id']).with_for_update()).mappings().one()
        start=state['session_started_ns'] or self.now_ns()
        if state['session_started_ns'] and state['boot_id']!=self.boot_id:fail('LIVE_AUTHORIZATION_EXPIRED')
        deadline=min(runtime.mono_deadline,start+self.get_grant(c,session['authorization_id']).payload['max_session_duration_ms']*1000000)
        if deadline<=self.now_ns():fail('LIVE_AUTHORIZATION_EXPIRED')
        self._runtime[session['id']]=replace(runtime,mono_deadline=deadline)
        c.execute(m.bindings.update().where(m.bindings.c.id==state['id']).values(session_started_ns=start,authorization_deadline_ns=deadline,boot_id=self.boot_id))
        if 'session_mono_start_ns' in s.sessions.c:
            c.execute(s.sessions.update().where(s.sessions.c.id==session['id']).values(session_mono_start_ns=start,authorization_boot_id=self.boot_id,authorization_elapsed_ms=state['elapsed_ms']))
        return start
