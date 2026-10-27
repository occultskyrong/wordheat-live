"""Pure readiness aggregation over server-collected checks and verified evidence.

Never bind HTTP JSON directly to this function: checks are observations produced
by database/model/display/relay adapters, not browser testimony. Gate/grant
success requires a VerifiedEnvelope; live success also requires a current
RuntimeAuthorization derived from a signed, single-use relay challenge.
"""
from datetime import datetime,timezone
from app.modules.auth.evidence import VerifiedEnvelope, RuntimeAuthorization, parse_time, validate_gate_lifecycle
from app.errors import DomainError

CHECKS = ('db','model','wordbank','relay','lease','display')
GATES = ('G0','G1_Q','G1_R','G2','rc_verified')
GATE_ORDER = ('G0','G1_Q','G1_R','rc_verified','G2')
CONTEXT_FIELDS = ('session_id','mode','run_purpose','live_profile','app_id','room_binding_id','installation_id','candidate_digest','model_manifest_id','wordbank_manifest_id','calibration_id','evaluated_at','authorization_id')
PROFILES = {
    'local': (('db','model','wordbank','display'),(),None),
    'beta': (CHECKS,('G0','G1_Q'),'controlled_beta'),
    'g2_evidence': (CHECKS,('G0','G1_Q','G1_R','rc_verified'),'g2_capture'),
    'production': (CHECKS,GATE_ORDER,'production_release'),
}
STATUSES = ('pass','fail','pending','not_applicable')


def _item(status='pending', reason='EVIDENCE_REQUIRED', message=''):
    return {'status':status,'reason_code':reason,'message':message}


def _matches(scope, context, fields):
    return isinstance(scope,dict) and all(context.get(key) is not None and scope.get(key)==context[key] for key in fields)


def _normalize(raw, context, now, *, fields=(), gate=False, registry=None):
    # The signed result/revocation is authoritative even if an outer projection
    # labels it pending. A projection can conservatively restrict success only.
    if not isinstance(raw,dict): return _item(reason='STATUS_UNKNOWN')
    evidence=raw.get('evidence')
    if gate and isinstance(evidence,VerifiedEnvelope) and evidence.type=='gate_evidence':
        payload=evidence.payload
        if payload.get('revoked_at') or (registry is not None and payload.get('object_id') in registry.revoked_objects):
            return _item('fail','EVIDENCE_REVOKED')
        if payload.get('result')=='fail': return _item('fail','EVIDENCE_RESULT_MISMATCH')
    if raw.get('status') not in STATUSES: return _item(reason='STATUS_UNKNOWN')
    result=_item(raw['status'],raw.get('reason_code') or 'EVIDENCE_REQUIRED',raw.get('message') or '')
    if result['status'] != 'pass': return result
    if gate:
        if not isinstance(evidence,VerifiedEnvelope) or evidence.type!='gate_evidence':
            return _item(reason='SIGNED_EVIDENCE_REQUIRED')
        payload=evidence.payload
        try:
            validate_gate_lifecycle(evidence,now,registry.revocation_revision if registry is not None and registry.revocation_revision else None)
        except DomainError as exc:
            status='pending' if exc.code in ('EVIDENCE_EXPIRED','EVIDENCE_NOT_YET_VALID') else 'fail'
            return _item(status,exc.code)
        scope=payload.get('scope');expiry=payload.get('expires_at')
    else:
        scope=raw.get('scope');expiry=raw.get('expires_at')
    if fields and not _matches(scope,context,fields): return _item('fail','EVIDENCE_SCOPE_MISMATCH')
    try:
        if expiry and parse_time(expiry)<=now: return _item(reason='EVIDENCE_EXPIRED')
    except DomainError: return _item('fail','EVIDENCE_INVALID')
    return result


def _profile_matches(profile, context):
    if profile=='local':
        return context.get('mode') in ('rehearsal','replay') and context.get('run_purpose')=='local' and context.get('live_profile') is None
    purpose='production' if profile=='production' else 'test_live'
    return context.get('mode')=='official' and context.get('run_purpose')==purpose and context.get('live_profile')==profile


def _authorization(profile, context, authorization, now, gates):
    authorization=authorization if isinstance(authorization,dict) else {}
    if 'session.control' not in authorization.get('operator_permissions',()):
        return _item('fail','OPERATOR_RESOURCE_PERMISSION_REQUIRED')
    if profile=='local': return _item('pass','OK')
    if authorization.get('failure'):
        code=authorization['failure']
        return _item('fail' if code in ('LIVE_AUTHORIZATION_REVOKED','CANDIDATE_MISMATCH','LIVE_AUTHORIZATION_INVALID','EVIDENCE_REVOKED') else 'pending',code)
    grant=authorization.get('grant')
    if not isinstance(grant,VerifiedEnvelope) or grant.type!='live_grant': return _item('pending','LIVE_AUTHORIZATION_REQUIRED')
    payload=grant.payload
    required=('app_id','room_binding_id','installation_id','candidate_digest')
    if not _matches(payload,context,required): return _item('fail','LIVE_AUTHORIZATION_SCOPE_MISMATCH')
    if payload.get('authorization_id')!=context.get('authorization_id') or not context.get('authorization_id'):
        return _item('fail','LIVE_AUTHORIZATION_SCOPE_MISMATCH')
    if payload.get('kind')!=PROFILES[profile][2] or profile not in payload.get('allowed_profiles',()):
        return _item('fail','LIVE_AUTHORIZATION_PROFILE_MISMATCH')
    if payload.get('revoked_at'): return _item('fail','LIVE_AUTHORIZATION_REVOKED')
    try:
        start=parse_time(payload.get('not_before')); end=parse_time(payload.get('expires_at'))
        max_days=30 if profile=='production' else 1
        if end<=start or (end-start).total_seconds()>max_days*86400:
            return _item('fail','LIVE_AUTHORIZATION_DURATION_INVALID')
        if now<start: return _item('pending','LIVE_AUTHORIZATION_NOT_YET_VALID')
        if now>=end: return _item('pending','LIVE_AUTHORIZATION_EXPIRED')
    except DomainError: return _item('fail','LIVE_AUTHORIZATION_INVALID')
    if type(payload.get('max_session_duration_ms')) is not int or payload['max_session_duration_ms']<=0:
        return _item('fail','LIVE_AUTHORIZATION_DURATION_INVALID')
    binding=authorization.get('platform_binding')
    if isinstance(binding,dict) and binding.get('status')=='fail':return _item('fail',binding.get('reason_code') or 'PLATFORM_AUTHORIZATION_REQUIRED')
    if not isinstance(binding,dict) or binding.get('status')!='pass' or not _matches(binding.get('scope'),context,required[:3]):
        return _item('pending','PLATFORM_AUTHORIZATION_REQUIRED')
    runtime=authorization.get('runtime')
    if not isinstance(runtime,RuntimeAuthorization) or not runtime.matches(grant,context):
        return _item('pending','FRESHNESS_PROOF_REQUIRED')
    try:
        runtime.require_evidence([gates[name]['evidence'] for name in PROFILES[profile][1]])
        runtime.require_current()
    except (KeyError,TypeError): return _item('pending','FRESHNESS_EVIDENCE_MISMATCH')
    except DomainError as exc: return _item('pending',exc.code)
    return _item('pass','OK')


def evaluate_readiness(context: dict | None, checks: dict, gates: dict,
                       authorization: dict | None, now: datetime) -> dict:
    now=parse_time(now)
    raw_context=context if isinstance(context,dict) else {}
    ctx={key:raw_context.get(key) for key in CONTEXT_FIELDS}
    ctx['evaluated_at']=now.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
    evaluated_checks={}
    for name in CHECKS:
        fields=('session_id',) if name=='display' else ('installation_id','candidate_digest')
        if name in ('relay','lease'): fields=('app_id','room_binding_id','installation_id','candidate_digest')
        evaluated_checks[name]=_normalize(checks.get(name),raw_context,now,fields=fields)
    if _profile_matches('local',raw_context):
        for name in ('relay','lease'): evaluated_checks[name]=_item('not_applicable','LOCAL_PROFILE')
    gate_fields={
        'G0':('app_id','candidate_digest'),
        'G1_Q':('candidate_digest','model_manifest_id','wordbank_manifest_id','calibration_id'),
        'G1_R':('candidate_digest','installation_id'),
        'rc_verified':('candidate_digest','installation_id'),
        'G2':('candidate_digest','installation_id','app_id','room_binding_id'),
    }
    runtime=authorization.get('runtime') if isinstance(authorization,dict) else None
    registry=runtime.nonces.evidence_registry if isinstance(runtime,RuntimeAuthorization) else None
    evaluated_gates={name:_normalize(gates.get(name),raw_context,now,fields=gate_fields[name],gate=True,registry=registry) for name in GATES}
    # A signed gate for another gate cannot be transplanted.
    for name in GATES:
        if evaluated_gates[name]['status']=='pass' and gates[name]['evidence'].payload.get('gate')!=name:
            evaluated_gates[name]=_item('fail','EVIDENCE_GATE_MISMATCH')
    profiles={}
    for profile,(required_checks,required_gates,_) in PROFILES.items():
        blockers=[]
        if not raw_context.get('session_id'):
            blockers.append({'item':'context','status':'pending','reason_code':'SESSION_CONTEXT_REQUIRED'})
        elif not _profile_matches(profile,raw_context):
            blockers.append({'item':'context','status':'fail','reason_code':'SESSION_PROFILE_MISMATCH'})
        else:
            for name in required_checks:
                item=evaluated_checks[name]
                if item['status']!='pass': blockers.append({'item':name,'status':item['status'],'reason_code':item['reason_code']})
            for name in GATE_ORDER:
                item=evaluated_gates[name]
                # Known optional failures still block controlled live testing.
                if (name in required_gates and item['status']!='pass') or (profile!='local' and item['status']=='fail'):
                    blockers.append({'item':name,'status':item['status'],'reason_code':item['reason_code']})
            auth=_authorization(profile,raw_context,authorization,now,gates)
            if auth['status']!='pass': blockers.append({'item':'authorization','status':auth['status'],'reason_code':auth['reason_code']})
        profiles[profile]={'allowed':not blockers,'blockers':blockers}
    selected=raw_context.get('live_profile')
    return {'be_extension_version':'LW-BE/1.1.0','context':ctx,'checks':evaluated_checks,'gates':evaluated_gates,'profiles':profiles,
            'can_rehearse':profiles['local']['allowed'],
            'can_test_live':selected in ('beta','g2_evidence') and profiles[selected]['allowed'],
            'can_go_live':profiles['production']['allowed']}
