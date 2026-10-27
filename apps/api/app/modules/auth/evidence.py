"""Readiness signed evidence primitives.

Signature canonicalization uses the mature rfc8785 package; verification never
substitutes json.dumps for JCS.
Ed25519 uses cryptography only; absent dependencies fail closed.

Registries are transaction-neutral: callers persist their state atomically with
imports/commands. They are not a replacement for database uniqueness/locking.
"""
import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Mapping
from app.errors import DomainError

DOMAIN = 'wordheat-live.readiness'
TYPES = frozenset(('gate_evidence', 'live_grant', 'revocation', 'freshness_proof','pairing_intent'))

REQUIRED_GATES = {
    'beta': ('G0', 'G1_Q'),
    'g2_evidence': ('G0', 'G1_Q', 'G1_R', 'rc_verified'),
    'production': ('G0', 'G1_Q', 'G1_R', 'rc_verified', 'G2'),
}
GRANT_PROFILES = {'controlled_beta':'beta','g2_capture':'g2_evidence','production_release':'production'}
GATE_SCOPE_FIELDS = {
    'G0': ('app_id','candidate_digest'),
    'G1_Q': ('candidate_digest','model_manifest_id','wordbank_manifest_id','calibration_id'),
    'G1_R': ('candidate_digest','installation_id'),
    'rc_verified': ('candidate_digest','installation_id'),
    'G2': ('candidate_digest','installation_id','app_id','room_binding_id'),
}


def _reject(message='invalid signed evidence', code='EVIDENCE_INVALID'):
    raise DomainError(code, message)


def canonicalize_jcs(value) -> bytes:
    try:
        import rfc8785
    except ImportError as exc:
        raise DomainError('DEPENDENCY_UNAVAILABLE', 'RFC8785 library unavailable', 503) from exc
    try:
        return rfc8785.dumps(value)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise DomainError('EVIDENCE_INVALID', 'invalid JCS/I-JSON value') from exc


def parse_envelope(raw: str | bytes) -> dict:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result: _reject('duplicate JSON key')
            result[key] = value
        return result
    def invalid_number(value): _reject('noncanonical or unsupported JSON number')
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_number)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise DomainError('EVIDENCE_INVALID', 'invalid JSON') from exc
    if not isinstance(value, dict): _reject('envelope must be an object')
    canonicalize_jcs(value)
    return value


def parse_time(value) -> datetime:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None: _reject('timezone required')
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError) as exc:
        raise DomainError('EVIDENCE_INVALID', 'invalid timestamp') from exc


@dataclass(frozen=True)
class VerifiedEnvelope:
    """Server-only verification result. Do not deserialize from HTTP dictionaries."""
    type: str
    payload_json: bytes
    digest: str
    key_id: str

    @property
    def payload(self):
        return json.loads(self.payload_json)


def verify_signed_envelope(envelope: str | bytes | dict, trusted_keys: Mapping,
                           *, expected_type: str | None = None,
                           expected_scope: dict | None = None,
                           canonicalizer=canonicalize_jcs) -> VerifiedEnvelope:
    obj = parse_envelope(envelope) if isinstance(envelope, (str, bytes)) else envelope
    if not isinstance(obj, dict) or set(obj) != {'key_id','domain','type','version','payload','signature'}:
        _reject('envelope fields mismatch')
    if obj['domain'] != DOMAIN or obj['type'] not in TYPES or type(obj['version']) is not int or obj['version'] != 1:
        _reject('unknown domain/type/version')
    if expected_type is not None and obj['type'] != expected_type: _reject('unexpected envelope type')
    key = trusted_keys.get(obj['key_id'])
    if not isinstance(key, Mapping): _reject('unknown signing key')
    if key.get('revoked'): _reject('signing key revoked','EVIDENCE_REVOKED')
    if obj['domain'] not in key.get('domains', ()) or obj['type'] not in key.get('types', ()):
        _reject('signing key not allowed for domain/type')
    payload = obj['payload']
    if not isinstance(payload, dict): _reject('payload must be object')
    if obj['type']=='pairing_intent':
        from .pairing_intent import validate
        try:validate(payload)
        except ValueError:_reject('invalid pairing management scope')
    # Freshness has its own fixed payload and revision semantics.
    if obj['type'] != 'freshness_proof':
        for field in ('object_id','issuer_id'):
            if not isinstance(payload.get(field),str) or not payload[field]: _reject('missing object identity')
        if type(payload.get('issuer_revision')) is not int or payload['issuer_revision'] < 1:
            _reject('invalid issuer revision')
    if expected_scope:
        scope = payload.get('scope', payload)
        if any(scope.get(field) != value for field, value in expected_scope.items()): _reject('scope mismatch')
    signature = obj['signature']
    if not isinstance(signature,str) or not re.fullmatch(r'[A-Za-z0-9_-]{86}',signature): _reject('invalid signature encoding')
    try:
        sig = base64.urlsafe_b64decode(signature + '==')
        if len(sig) != 64 or base64.urlsafe_b64encode(sig).rstrip(b'=').decode() != signature:
            _reject('noncanonical signature encoding')
        public_key = key['public_key']
        if not isinstance(public_key,bytes) or len(public_key) != 32: _reject('invalid public key')
        unsigned = {field:value for field,value in obj.items() if field != 'signature'}
        canonical = canonicalizer(unsigned)
        if not isinstance(canonical,bytes): _reject('canonicalizer must return bytes')
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
        try: Ed25519PublicKey.from_public_bytes(public_key).verify(sig,canonical)
        except InvalidSignature: _reject('signature verification failed')
    except ImportError as exc:
        raise DomainError('DEPENDENCY_UNAVAILABLE', 'Ed25519 library unavailable',503) from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise DomainError('EVIDENCE_INVALID','invalid signing material') from exc
    return VerifiedEnvelope(obj['type'], canonicalizer(payload), hashlib.sha256(canonical).hexdigest(),obj['key_id'])


class EvidenceRegistry:
    def __init__(self):
        self.objects = {}
        self.issuer_revisions = {}
        self.revoked_objects = set()
        self.revocation_revision = 0

    def accept(self, evidence: VerifiedEnvelope) -> bool:
        if not isinstance(evidence,VerifiedEnvelope): _reject('verified envelope required')
        payload=evidence.payload
        object_id=payload.get('object_id'); issuer=payload.get('issuer_id'); revision=payload.get('issuer_revision')
        if not object_id or not issuer or type(revision) is not int or revision < 1: _reject('object identity missing')
        old=self.objects.get(object_id)
        if old:
            if old != evidence.digest: _reject('object ID reused with different content')
            return False
        if revision <= self.issuer_revisions.get(issuer,0): _reject('issuer revision rollback')
        self.objects[object_id]=evidence.digest
        self.issuer_revisions[issuer]=revision
        return True

    def apply_revocations(self, object_ids, revision: int):
        """Trusted persistence adapter only, after authenticated revocation import.

        This accepts the parsed projection, not an unsigned wire envelope. The
        importing transaction must verify signature, issuer revision and the
        deployment's revocation payload schema before calling this method.
        Persist revoked_objects/revocation_revision atomically with that import.
        Revocation is permanent for an object ID; a new object requires new grant.
        """
        ids = tuple(object_ids)
        if (type(revision) is not int or revision < self.revocation_revision
            or any(not isinstance(value,str) or not value for value in ids)):
            _reject('invalid or stale revocation projection', 'REVOCATION_REVISION_ROLLBACK')
        self.revoked_objects.update(ids)
        self.revocation_revision = revision

    def require_active(self, evidence: VerifiedEnvelope):
        if not isinstance(evidence,VerifiedEnvelope): _reject('verified envelope required')
        payload=evidence.payload
        if payload.get('revoked_at') or payload.get('object_id') in self.revoked_objects:
            _reject('signed object revoked', 'EVIDENCE_REVOKED')
        if self.objects.get(payload.get('object_id')) != evidence.digest:
            _reject('object absent or changed in import registry', 'EVIDENCE_NOT_IMPORTED')



class NonceRegistry:
    def __init__(self, boot_id: str, connection_nonce: str, evidence_registry=None):
        self.evidence_registry = evidence_registry if evidence_registry is not None else EvidenceRegistry()
        self.reset(boot_id,connection_nonce)

    def reset(self, boot_id: str, connection_nonce: str):
        self.boot_id=boot_id; self.connection_nonce=connection_nonce; self._pending={}; self._used=set()
        self.revocation_revision = 0

    def advance_revocation(self, revision: int):
        """Call only after importing a verified revocation/proof transaction."""
        if type(revision) is not int or revision < self.revocation_revision:
            _reject('revocation revision rollback')
        self.revocation_revision = revision

    def issue(self, request_nonce: str, mono_sent_ns: int):
        if not isinstance(request_nonce,str) or not request_nonce or request_nonce in self._pending or request_nonce in self._used:
            _reject('nonce must be unique')
        if type(mono_sent_ns) is not int or mono_sent_ns < 0: _reject('invalid monotonic anchor')
        self._pending[request_nonce]=mono_sent_ns

    def consume(self, payload: dict) -> int:
        if payload.get('boot_id') != self.boot_id or payload.get('connection_nonce') != self.connection_nonce:
            _reject('boot/connection mismatch', 'FRESHNESS_BOOT_MISMATCH')
        nonce=payload.get('request_nonce')
        if nonce not in self._pending: _reject('unknown or consumed challenge')
        self._used.add(nonce)
        return self._pending.pop(nonce)


def compute_mono_deadline(*, mono_sent_ns: int, now_ns: int, ttl_ms: int,
                          server_time, grant_expires_at, evidence_expires_at: list,
                          lease_mono_safe_deadline: int,
                          session_mono_start_ns: int | None = None,
                          max_session_duration_ms: int | None = None) -> int:
    if (type(ttl_ms) is not int or not 1 <= ttl_ms <= 60000
        or type(mono_sent_ns) is not int or mono_sent_ns < 0
        or type(now_ns) is not int or now_ns < mono_sent_ns
        or type(lease_mono_safe_deadline) is not int or not evidence_expires_at):
        _reject('invalid monotonic/time budget', 'CLOCK_UNCERTAIN')
    server_time=parse_time(server_time)
    deadlines=[mono_sent_ns+ttl_ms*1_000_000,lease_mono_safe_deadline]
    for expiry in [grant_expires_at,*evidence_expires_at]:
        delta=parse_time(expiry)-server_time
        # Integer timedelta arithmetic avoids float rounding near expiration.
        remaining_ns=(delta.days*86400+delta.seconds)*1_000_000_000+delta.microseconds*1000
        deadlines.append(mono_sent_ns+remaining_ns)
    if session_mono_start_ns is not None:
        if (type(session_mono_start_ns) is not int or session_mono_start_ns < 0 or session_mono_start_ns > now_ns
            or type(max_session_duration_ms) is not int or max_session_duration_ms <= 0):
            _reject('invalid session budget','CLOCK_UNCERTAIN')
        deadlines.append(session_mono_start_ns+max_session_duration_ms*1_000_000)
    deadline=min(deadlines)
    if deadline <= now_ns: _reject('authorization time budget exhausted','LIVE_AUTHORIZATION_EXPIRED')
    return deadline


def validate_gate_lifecycle(evidence: VerifiedEnvelope, now, revocation_revision: int | None = None):
    if not isinstance(evidence,VerifiedEnvelope) or evidence.type!='gate_evidence':
        _reject('signed gate evidence required')
    payload=evidence.payload
    if payload.get('revoked_at'): _reject('signed evidence revoked','EVIDENCE_REVOKED')
    if payload.get('result') != 'pass': _reject('signed gate did not pass','EVIDENCE_RESULT_MISMATCH')
    evaluated=parse_time(payload.get('evaluated_at'));expires=parse_time(payload.get('expires_at'));now=parse_time(now)
    if evaluated > now: _reject('evidence evaluation is in future','EVIDENCE_NOT_YET_VALID')
    if expires <= evaluated: _reject('evidence evaluation/expiry interval invalid')
    if expires <= now: _reject('evidence expired','EVIDENCE_EXPIRED')
    if payload.get('not_before') and parse_time(payload['not_before'])>now:
        _reject('evidence not yet valid','EVIDENCE_NOT_YET_VALID')
    revision=payload.get('revocation_revision')
    if type(revision) is not int or revision<0 or (revocation_revision is not None and revision>revocation_revision):
        _reject('invalid evidence revocation revision','REVOCATION_REVISION_ROLLBACK')


def gate_bindings(evidences):
    return tuple(sorted((e.payload.get('gate'),e.payload.get('object_id'),e.payload.get('issuer_id'),
                         e.payload.get('issuer_revision'),e.digest) for e in evidences))


@dataclass(frozen=True)
class RuntimeAuthorization:
    grant_digest: str
    authorization_id: str
    candidate_digest: str
    installation_id: str
    boot_id: str
    connection_nonce: str
    revocation_revision: int
    mono_deadline: int
    now_ns: object
    nonces: NonceRegistry
    profile: str = ''
    evidence_bindings: tuple = ()
    required_evidence: tuple = ()
    grant: VerifiedEnvelope | None = None
    proof_key_id: str = ''

    def matches(self, grant: VerifiedEnvelope, context: dict) -> bool:
        return (self.grant_digest == grant.digest and self.profile == context.get('live_profile')
                and all(getattr(self,key)==context.get(key) for key in ('authorization_id','candidate_digest','installation_id'))
                and all(isinstance(e.payload.get('scope'),dict)
                        and all(context.get(key) is not None and e.payload['scope'].get(key)==context[key]
                                for key in GATE_SCOPE_FIELDS[e.payload['gate']])
                        for e in self.required_evidence))

    def require_evidence(self, evidences):
        if gate_bindings(evidences) != self.evidence_bindings:
            _reject('required evidence changed after freshness challenge', 'FRESHNESS_EVIDENCE_MISMATCH')

    def require_current(self):
        if self.nonces.boot_id != self.boot_id or self.nonces.connection_nonce != self.connection_nonce:
            _reject('boot/connection changed', 'FRESHNESS_PROOF_REQUIRED')
        registry=self.nonces.evidence_registry
        # Consult object-specific persisted revocations before revision diagnostics.
        if self.grant is not None:
            registry.require_active(self.grant)
            for evidence in self.required_evidence: registry.require_active(evidence)
        if registry.revocation_revision < self.revocation_revision:
            _reject('complete revocation state not restored', 'REVOCATION_STATE_INCOMPLETE')
        if self.nonces.revocation_revision != self.revocation_revision or registry.revocation_revision != self.revocation_revision:
            _reject('authorization revocation changed', 'LIVE_AUTHORIZATION_REVOKED')
        if self.now_ns() >= self.mono_deadline:
            _reject('authorization time budget exhausted','LIVE_AUTHORIZATION_EXPIRED')


def accept_freshness_proof(proof: VerifiedEnvelope, *, grant: VerifiedEnvelope,
                           required_evidence: list[VerifiedEnvelope], nonces: NonceRegistry,
                           revocation_revision: int, lease_mono_safe_deadline: int,
                           now_ns, clock_trusted: bool,
                           session_mono_start_ns: int | None = None) -> RuntimeAuthorization:
    if not isinstance(proof,VerifiedEnvelope) or proof.type!='freshness_proof': _reject('freshness proof required')
    if not isinstance(grant,VerifiedEnvelope) or grant.type!='live_grant': _reject('verified grant required')
    if clock_trusted is not True: _reject('relay time not trusted','CLOCK_UNCERTAIN')
    payload=proof.payload; grant_payload=grant.payload
    expected={'authorization_id','candidate_digest','installation_id','boot_id','connection_nonce','request_nonce','revocation_revision','server_time','ttl_ms'}
    if set(payload)!=expected: _reject('freshness fields mismatch')
    for field in ('authorization_id','candidate_digest','installation_id'):
        if not payload.get(field) or payload[field]!=grant_payload.get(field):
            _reject('freshness binding mismatch','FRESHNESS_SCOPE_MISMATCH')
    revision=payload.get('revocation_revision');registry=nonces.evidence_registry
    if (type(revision) is not int or type(revocation_revision) is not int
        or revision<max(revocation_revision,nonces.revocation_revision,registry.revocation_revision)):
        _reject('revocation revision rollback','REVOCATION_REVISION_ROLLBACK')
    if revision != registry.revocation_revision or revocation_revision != registry.revocation_revision:
        _reject('complete trusted revocation list required before accepting proof', 'REVOCATION_STATE_INCOMPLETE')
    profile=GRANT_PROFILES.get(grant_payload.get('kind'))
    if profile is None or profile not in grant_payload.get('allowed_profiles',()):
        _reject('grant profile invalid','LIVE_AUTHORIZATION_PROFILE_MISMATCH')
    expected_gates=REQUIRED_GATES[profile]
    if any(not isinstance(e,VerifiedEnvelope) or e.type!='gate_evidence' for e in required_evidence):
        _reject('signed required gate missing','REQUIRED_GATE_SET_MISMATCH')
    names=[e.payload.get('gate') for e in required_evidence]
    if len(names)!=len(expected_gates) or set(names)!=set(expected_gates):
        _reject('profile required gate set mismatch','REQUIRED_GATE_SET_MISMATCH')
    server_time=parse_time(payload['server_time'])
    registry.require_active(grant)
    start=parse_time(grant_payload.get('not_before'));end=parse_time(grant_payload.get('expires_at'))
    if server_time<start or server_time>=end: _reject('grant outside validity','LIVE_AUTHORIZATION_EXPIRED')
    days=30 if profile=='production' else 1
    if end<=start or (end-start).total_seconds()>days*86400 or type(grant_payload.get('max_session_duration_ms')) is not int or grant_payload['max_session_duration_ms']<=0:
        _reject('invalid grant duration','LIVE_AUTHORIZATION_DURATION_INVALID')
    for evidence in required_evidence:
        registry.require_active(evidence)
        validate_gate_lifecycle(evidence,server_time,revision)
        scope=evidence.payload.get('scope')
        fields=GATE_SCOPE_FIELDS[evidence.payload['gate']]
        if (not isinstance(scope,dict) or any(scope.get(k) is None for k in fields)
            or any(scope.get(k)!=grant_payload[k] for k in fields if k in grant_payload)):
            _reject('required gate scope mismatch','EVIDENCE_SCOPE_MISMATCH')
    # All trusted bindings are valid before challenge consumption.
    sent=nonces.consume(payload)
    deadline=compute_mono_deadline(mono_sent_ns=sent,now_ns=now_ns(),ttl_ms=payload['ttl_ms'],
        server_time=server_time,grant_expires_at=grant_payload.get('expires_at'),
        evidence_expires_at=[e.payload.get('expires_at') for e in required_evidence],
        lease_mono_safe_deadline=lease_mono_safe_deadline,session_mono_start_ns=session_mono_start_ns,
        max_session_duration_ms=grant_payload.get('max_session_duration_ms'))
    nonces.advance_revocation(revision)
    return RuntimeAuthorization(grant.digest,payload['authorization_id'],payload['candidate_digest'],payload['installation_id'],
                                payload['boot_id'],payload['connection_nonce'],revision,deadline,now_ns,nonces,
                                profile,gate_bindings(required_evidence),tuple(required_evidence),grant,proof.key_id)
