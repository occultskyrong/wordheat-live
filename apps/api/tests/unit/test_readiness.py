import unittest
from datetime import datetime, timezone, timedelta
from app.modules.operations.readiness import evaluate_readiness

NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)
class ReadinessTests(unittest.TestCase):
    def context(self, mode='rehearsal', purpose='local', profile=None):
        return dict(session_id='s', mode=mode, run_purpose=purpose, live_profile=profile, app_id='app', room_binding_id='room', installation_id='i',candidate_digest='c', model_manifest_id='m',wordbank_manifest_id='w',calibration_id='cal',authorization_id='a')
    def checks(self):
        return {key: {'status':'pass','reason_code':'OK','message':'','scope':self.context()} for key in ('db','model','wordbank','relay','lease','display')}
    def test_no_context_and_unknown_status_fail_closed(self):
        result = evaluate_readiness(None, {}, {}, None, NOW)
        self.assertFalse(any(result[k] for k in ('can_rehearse','can_test_live','can_go_live')))
        self.assertEqual(result['profiles']['local']['blockers'][0]['reason_code'],'SESSION_CONTEXT_REQUIRED')
        checks = self.checks(); checks['db'] = True
        result = evaluate_readiness(self.context(), checks, {}, {'operator_permissions':['session.control']}, NOW)
        self.assertFalse(result['can_rehearse'])
    def test_local_is_independent_of_live_gates(self):
        gates = {k:{'status':'fail','reason_code':'FAILED','message':''} for k in ('G0','G1_Q','G1_R','G2','rc_verified')}
        result = evaluate_readiness(self.context(), self.checks(), gates, {'operator_permissions':['session.control']}, NOW)
        self.assertTrue(result['can_rehearse'])
        self.assertEqual(result['checks']['relay']['status'],'not_applicable')
        self.assertFalse(result['can_test_live']); self.assertFalse(result['can_go_live'])
        self.assertEqual(result['gates']['G2']['status'],'fail')
    def test_display_requires_same_session_and_all_blockers_are_ordered(self):
        checks=self.checks(); checks['display']['scope']['session_id']='other'; checks['db']['status']='pending'
        result=evaluate_readiness(self.context(),checks,{},None,NOW)
        blockers=result['profiles']['local']['blockers']
        self.assertEqual([b['item'] for b in blockers],['db','display','authorization'])
    def test_browser_flags_do_not_authorize_official(self):
        context=self.context('official','test_live','beta')
        result=evaluate_readiness(context,self.checks(),{}, {'verified':True,'allowed':True,'platform_authorized':True},NOW)
        self.assertFalse(result['can_test_live'])
    def test_expired_check_and_missing_scope_fail_closed(self):
        checks=self.checks(); checks['db']['expires_at']=(NOW-timedelta(seconds=1)).isoformat(); checks['display'].pop('scope')
        result=evaluate_readiness(self.context(),checks,{}, {'operator_permissions':['session.control']},NOW)
        self.assertFalse(result['can_rehearse'])
        self.assertEqual(result['checks']['db']['status'],'pending')

class SignedReadinessTests(ReadinessTests):
    def setUp(self):
        import base64, rfc8785
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        self.private=Ed25519PrivateKey.generate(); self.encode=base64.urlsafe_b64encode; self.canonical=rfc8785.dumps
        self.keys={'root':{'public_key':self.private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw),'domains':['wordheat-live.readiness'],'types':['gate_evidence','live_grant','freshness_proof']}}
        self.revision=0
    def evidence(self,payload,kind):
        from app.modules.auth.evidence import verify_signed_envelope
        self.revision+=1
        if kind!='freshness_proof': payload={**payload,'object_id':str(self.revision),'issuer_id':'issuer','issuer_revision':self.revision}
        obj=dict(key_id='root',domain='wordheat-live.readiness',type=kind,version=1,payload=payload)
        obj['signature']=self.encode(self.private.sign(self.canonical(obj))).rstrip(b'=').decode()
        return verify_signed_envelope(obj,self.keys)
    def ready(self, profile):
        from app.modules.auth.evidence import accept_freshness_proof,NonceRegistry
        context=self.context('official','production' if profile=='production' else 'test_live',profile)
        gates={key:{'status':'pass','reason_code':'OK','message':'','evidence':self.evidence({'gate':key,'scope':context,'result':'pass','evaluated_at':NOW.isoformat(),'revocation_revision':1,'expires_at':(NOW+timedelta(hours=1)).isoformat()},'gate_evidence')} for key in ('G0','G1_Q','G1_R','rc_verified','G2')}
        grant=self.evidence({**context,'kind':{'beta':'controlled_beta','g2_evidence':'g2_capture','production':'production_release'}[profile],'allowed_profiles':[profile],'not_before':NOW.isoformat(),'expires_at':(NOW+timedelta(hours=1)).isoformat(),'max_session_duration_ms':10000},'live_grant')
        proof=self.evidence(dict(authorization_id='a',candidate_digest='c',installation_id='i',boot_id='b',connection_nonce='cn',request_nonce='rn',revocation_revision=1,server_time=NOW.isoformat(),ttl_ms=1000),'freshness_proof')
        nonce=NonceRegistry('b','cn'); nonce.issue('rn',0)
        for entry in [*(v['evidence'] for v in gates.values()),grant]: nonce.evidence_registry.accept(entry)
        nonce.evidence_registry.apply_revocations([],1)
        runtime=accept_freshness_proof(proof,grant=grant,required_evidence=[gates[k]['evidence'] for k in {'beta':('G0','G1_Q'),'g2_evidence':('G0','G1_Q','G1_R','rc_verified'),'production':('G0','G1_Q','G1_R','rc_verified','G2')}[profile]],nonces=nonce,revocation_revision=1,lease_mono_safe_deadline=1_000_000_000,now_ns=lambda:1,clock_trusted=True)
        auth={'operator_permissions':['session.control'],'grant':grant,'runtime':runtime,'platform_binding':{'status':'pass','scope':context}}
        return context,self.checks(),gates,auth
    def test_all_live_profiles_and_optional_known_failure(self):
        for profile in ('beta','g2_evidence','production'):
            with self.subTest(profile=profile):
                context,checks,gates,auth=self.ready(profile)
                result=evaluate_readiness(context,checks,gates,auth,NOW)
                self.assertTrue(result['profiles'][profile]['allowed'])
                self.assertEqual(result['can_go_live'],profile=='production')
                self.assertEqual(result['can_test_live'],profile!='production')
                gates['G2']={'status':'pending','reason_code':'NOT_TESTED','message':''}
                self.assertEqual(evaluate_readiness(context,checks,gates,auth,NOW)['profiles'][profile]['allowed'],profile!='production')
                gates['G2']={'status':'fail','reason_code':'FAILED','message':''}
                self.assertFalse(evaluate_readiness(context,checks,gates,auth,NOW)['profiles'][profile]['allowed'])
    def test_required_gates_grant_expiry_binding_and_revocation(self):
        context,checks,gates,auth=self.ready('beta')
        gates['G1_Q']['status']='pending'
        self.assertFalse(evaluate_readiness(context,checks,gates,auth,NOW)['can_test_live'])
        gates['G1_Q']['status']='pass'
        for update in ({'installation_id':'other'},{'candidate_digest':'other'},{'room_binding_id':'other'},{'authorization_id':'other'}):
            with self.subTest(update=update): self.assertFalse(evaluate_readiness({**context,**update},checks,gates,auth,NOW)['can_test_live'])
        self.assertFalse(evaluate_readiness(context,checks,gates,auth,NOW+timedelta(hours=2))['can_test_live'])
        revoked={**auth['grant'].payload,'revoked_at':NOW.isoformat()}
        auth['grant']=self.evidence(revoked,'live_grant')
        self.assertFalse(evaluate_readiness(context,checks,gates,auth,NOW)['can_test_live'])

    def test_gate_scope_wrong_gate_not_before_and_duration_caps(self):
        context,checks,gates,auth=self.ready('beta')
        for update in ({'scope':{**context,'model_manifest_id':'wrong'}},{'gate':'G2'},{'expires_at':NOW.isoformat()},{'not_before':(NOW+timedelta(seconds=1)).isoformat()},{'result':'fail'}):
            with self.subTest(update=update):
                _,_,fresh_gates,_=self.ready('beta')
                fresh_gates['G1_Q']['evidence']=self.evidence({**fresh_gates['G1_Q']['evidence'].payload,**update},'gate_evidence')
                self.assertFalse(evaluate_readiness(context,checks,fresh_gates,auth,NOW)['can_test_live'])
        for update in ({'expires_at':(NOW+timedelta(days=2)).isoformat()},{'max_session_duration_ms':0},{'not_before':(NOW+timedelta(seconds=1)).isoformat()},{'kind':'production_release'}):
            with self.subTest(update=update):
                changed={**auth,'grant':self.evidence({**auth['grant'].payload,**update},'live_grant')}
                self.assertFalse(evaluate_readiness(context,checks,gates,changed,NOW)['can_test_live'])

    def test_signed_failure_cannot_hide_behind_outer_pending(self):
        context,checks,gates,auth=self.ready('beta')
        gates['G2']['status']='pending'
        gates['G2']['evidence']=self.evidence({**gates['G2']['evidence'].payload,'result':'fail'},'gate_evidence')
        result=evaluate_readiness(context,checks,gates,auth,NOW)
        self.assertFalse(result['can_test_live'])
        self.assertEqual(result['gates']['G2']['status'],'fail')
    def test_signed_revoked_gate_cannot_hide_behind_pending(self):
        context,checks,gates,auth=self.ready('beta')
        gates['G2']['status']='pending'
        gates['G2']['evidence']=self.evidence({**gates['G2']['evidence'].payload,'revoked_at':NOW.isoformat()},'gate_evidence')
        result=evaluate_readiness(context,checks,gates,auth,NOW)
        self.assertFalse(result['can_test_live'])
        self.assertEqual(result['gates']['G2']['reason_code'],'EVIDENCE_REVOKED')
    def test_runtime_must_bind_current_required_gate_identity(self):
        context,checks,gates,auth=self.ready('beta')
        gates['G1_Q']['evidence']=self.evidence({**gates['G1_Q']['evidence'].payload},'gate_evidence')
        result=evaluate_readiness(context,checks,gates,auth,NOW)
        self.assertFalse(result['can_test_live'])
        self.assertEqual(result['profiles']['beta']['blockers'][-1]['reason_code'],'FRESHNESS_EVIDENCE_MISMATCH')
    def test_missing_future_and_expired_evaluated_at_gate_rejected(self):
        for changes in ({'evaluated_at':None},{'evaluated_at':(NOW+timedelta(seconds=1)).isoformat()},{'revocation_revision':None}):
            context,checks,gates,auth=self.ready('beta')
            gates['G1_Q']['evidence']=self.evidence({**gates['G1_Q']['evidence'].payload,**changes},'gate_evidence')
            self.assertFalse(evaluate_readiness(context,checks,gates,auth,NOW)['can_test_live'])

    def test_spec_minimal_grant_allows_readiness_and_rejects_changed_components(self):
        from app.modules.auth.evidence import accept_freshness_proof
        context,checks,gates,auth=self.ready('beta')
        allowed=('authorization_id','kind','app_id','room_binding_id','installation_id','candidate_digest','allowed_profiles','not_before','expires_at','max_session_duration_ms')
        grant=self.evidence({key:value for key,value in auth['grant'].payload.items() if key in allowed},'live_grant')
        nonce=auth['runtime'].nonces;nonce.evidence_registry.accept(grant);nonce.issue('minimal',0)
        proof=self.evidence(dict(authorization_id='a',candidate_digest='c',installation_id='i',boot_id='b',connection_nonce='cn',request_nonce='minimal',revocation_revision=1,server_time=NOW.isoformat(),ttl_ms=1000),'freshness_proof')
        runtime=accept_freshness_proof(proof,grant=grant,required_evidence=[gates[k]['evidence'] for k in ('G0','G1_Q')],nonces=nonce,revocation_revision=1,lease_mono_safe_deadline=1_000_000_000,now_ns=lambda:1,clock_trusted=True)
        auth={**auth,'grant':grant,'runtime':runtime}
        self.assertTrue(evaluate_readiness(context,checks,gates,auth,NOW)['can_test_live'])
        for key in ('model_manifest_id','wordbank_manifest_id','calibration_id'):
            with self.subTest(key=key):
                self.assertFalse(evaluate_readiness({**context,key:'changed'},checks,gates,auth,NOW)['can_test_live'])
