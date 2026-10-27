import unittest
from datetime import datetime, timezone, timedelta
from app.errors import DomainError
from app.modules.auth.evidence import canonicalize_jcs, parse_envelope, compute_mono_deadline, NonceRegistry, EvidenceRegistry, verify_signed_envelope

class EvidenceTests(unittest.TestCase):
    def test_integer_jcs_utf16_sort_and_invalid_values(self):
        self.assertEqual(canonicalize_jcs({'z':1,'a':'汉字'}), '{"a":"汉字","z":1}'.encode())
        self.assertEqual(canonicalize_jcs({'\ue000':1,'😀':2}).decode(),'{"😀":2,"\ue000":1}')
        for value in (float('nan'), 2**53, '\ud800', {1:'a'}):
            with self.subTest(value=repr(value)), self.assertRaises(DomainError): canonicalize_jcs(value)
    def test_duplicate_keys_and_noncanonical_number_rejected(self):
        for raw in ('{"payload":{"x":1,"x":2}}','{"x":9007199254740992}','{"x":NaN}'):
            with self.subTest(raw=raw), self.assertRaises(DomainError): parse_envelope(raw)
    def test_deadline_uses_sent_anchor_and_minimum_budget(self):
        now=datetime(2026,10,25,tzinfo=timezone.utc)
        kwargs=dict(mono_sent_ns=100_000_000,now_ns=200_000_000,ttl_ms=1000,server_time=now,grant_expires_at=now+timedelta(seconds=3),evidence_expires_at=[now+timedelta(seconds=2)],lease_mono_safe_deadline=5_000_000_000)
        self.assertEqual(compute_mono_deadline(**kwargs),1_100_000_000)
        self.assertEqual(compute_mono_deadline(**kwargs,session_mono_start_ns=0,max_session_duration_ms=500),500_000_000)
        kwargs['now_ns']=1_100_000_000
        with self.assertRaises(DomainError): compute_mono_deadline(**kwargs)
    def test_nonce_consumption_binding_and_reset(self):
        registry=NonceRegistry('b','c'); registry.issue('n',10)
        with self.assertRaises(DomainError): registry.consume({'boot_id':'bad','connection_nonce':'c','request_nonce':'n'})
        self.assertEqual(registry.consume({'boot_id':'b','connection_nonce':'c','request_nonce':'n'}),10)
        with self.assertRaises(DomainError): registry.consume({'boot_id':'b','connection_nonce':'c','request_nonce':'n'})
        registry.issue('next',20); registry.reset('new','new')
        with self.assertRaises(DomainError): registry.consume({'boot_id':'new','connection_nonce':'new','request_nonce':'next'})
    def test_unknown_key_fails_before_crypto_import(self):
        with self.assertRaises(DomainError): verify_signed_envelope({'key_id':'bad','domain':'wordheat-live.readiness','type':'live_grant','version':1,'payload':{},'signature':'x'}, {})

class RealSignatureTests(unittest.TestCase):
    def setUp(self):
        import base64
        import rfc8785
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
        self.private=Ed25519PrivateKey.generate()
        self.keys={'root':{'public_key':self.private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw),'domains':['wordheat-live.readiness'],'types':['gate_evidence','live_grant','freshness_proof','revocation']}}
        self.canonicalize=rfc8785.dumps
        self.base64=base64
    def signed(self, payload=None, kind='live_grant'):
        obj={'key_id':'root','domain':'wordheat-live.readiness','type':kind,'version':1,'payload':payload or {'object_id':'o','issuer_id':'i','issuer_revision':1}}
        obj['signature']=self.base64.urlsafe_b64encode(self.private.sign(self.canonicalize(obj))).rstrip(b'=').decode()
        return obj
    def test_real_signature_tampering_type_scope_and_revision(self):
        obj=self.signed(); verified=verify_signed_envelope(obj,self.keys)
        registry=EvidenceRegistry(); self.assertTrue(registry.accept(verified)); self.assertFalse(registry.accept(verified))
        tampered=dict(obj); tampered['payload']={**obj['payload'],'issuer_revision':2}
        with self.assertRaises(DomainError): verify_signed_envelope(tampered,self.keys)
        with self.assertRaises(DomainError): verify_signed_envelope(obj,self.keys,expected_type='revocation')
        with self.assertRaises(DomainError): verify_signed_envelope(obj,self.keys,expected_scope={'candidate_digest':'x'})
        changed=verify_signed_envelope(self.signed({'object_id':'o','issuer_id':'i','issuer_revision':2}),self.keys)
        with self.assertRaises(DomainError): registry.accept(changed)
        old=verify_signed_envelope(self.signed({'object_id':'other','issuer_id':'i','issuer_revision':1}),self.keys)
        with self.assertRaises(DomainError): registry.accept(old)
    def bundle(self, profile='beta', short_q=False):
        now=datetime(2026,10,25,tzinfo=timezone.utc)
        context=dict(authorization_id='a',candidate_digest='c',installation_id='i',app_id='app',room_binding_id='room',model_manifest_id='m',wordbank_manifest_id='w',calibration_id='cal')
        def verified(payload,kind): return verify_signed_envelope(self.signed(payload,kind),self.keys)
        kind={'beta':'controlled_beta','g2_evidence':'g2_capture','production':'production_release'}[profile]
        grant=verified(dict(context,object_id='g',issuer_id='i',issuer_revision=1,kind=kind,allowed_profiles=[profile],not_before=now.isoformat(),expires_at=(now+timedelta(hours=1)).isoformat(),max_session_duration_ms=5000),'live_grant')
        names={'beta':('G0','G1_Q'),'g2_evidence':('G0','G1_Q','G1_R','rc_verified'),'production':('G0','G1_Q','G1_R','rc_verified','G2')}[profile]
        gates=[verified(dict(object_id=name,issuer_id='i',issuer_revision=index+2,gate=name,scope=context,result='pass',evaluated_at=now.isoformat(),revocation_revision=3,expires_at=(now+timedelta(milliseconds=10) if name=='G1_Q' and short_q else now+timedelta(hours=1)).isoformat()),'gate_evidence') for index,name in enumerate(names)]
        registry=EvidenceRegistry()
        for e in [grant,*gates]: registry.accept(e)
        registry.apply_revocations([],3)
        nonces=NonceRegistry('b','cn');nonces.evidence_registry=registry;nonces.issue('rn',0)
        payload=dict(authorization_id='a',candidate_digest='c',installation_id='i',boot_id='b',connection_nonce='cn',request_nonce='rn',revocation_revision=3,server_time=now.isoformat(),ttl_ms=1000)
        proof=verified(payload,'freshness_proof')
        params=dict(grant=grant,required_evidence=gates,nonces=nonces,revocation_revision=3,lease_mono_safe_deadline=2_000_000_000,now_ns=lambda:1,clock_trusted=True)
        return proof,params
    def test_full_jcs_floating_point_and_freshness(self):
        from app.modules.auth.evidence import accept_freshness_proof
        self.assertEqual(canonicalize_jcs({'x':1.5}),b'{"x":1.5}')
        proof,params=self.bundle();current=[1];params['now_ns']=lambda:current[0]
        runtime=accept_freshness_proof(proof,**params);runtime.require_current()
        self.assertEqual(runtime.mono_deadline,1_000_000_000)
        with self.assertRaises(DomainError):accept_freshness_proof(proof,**params)
        current[0]=1_000_000_000
        with self.assertRaises(DomainError):runtime.require_current()

    def test_runtime_rejected_after_new_revocation_or_boot(self):
        from app.modules.auth.evidence import RuntimeAuthorization
        nonce=NonceRegistry('b','cn')
        nonce.evidence_registry.apply_revocations([],1)
        runtime=RuntimeAuthorization('digest','a','c','i','b','cn',1,1000,lambda:1,nonce)
        nonce.advance_revocation(1)
        runtime.require_current()
        nonce.advance_revocation(2)
        with self.assertRaises(DomainError): runtime.require_current()
        nonce.reset('new','new')
        with self.assertRaises(DomainError): runtime.require_current()
        with self.assertRaises(DomainError): nonce.advance_revocation(-1)

    def test_envelope_schema_signature_encoding_and_key_policy(self):
        for change in ({'domain':'other'},{'version':2},{'type':'unknown'},{'signature':'x'},{'signature':'A'*86}):
            with self.subTest(change=change),self.assertRaises(DomainError):
                verify_signed_envelope({**self.signed(),**change},self.keys)
        self.keys['root']['types']=[]
        with self.assertRaises(DomainError): verify_signed_envelope(self.signed(),self.keys)
    def test_freshness_rejects_missing_clock_scope_revision_or_evidence(self):
        from app.modules.auth.evidence import accept_freshness_proof
        cases=(({},False,'CLOCK_UNCERTAIN'),({'candidate_digest':'other'},True,'FRESHNESS_SCOPE_MISMATCH'),({'revocation_revision':0},True,'REVOCATION_REVISION_ROLLBACK'),({'boot_id':'old'},True,'FRESHNESS_BOOT_MISMATCH'))
        for change,trust,code in cases:
            proof,params=self.bundle();params['clock_trusted']=trust
            proof=verify_signed_envelope(self.signed({**proof.payload,**change},'freshness_proof'),self.keys)
            with self.subTest(change=change),self.assertRaises(DomainError) as error:accept_freshness_proof(proof,**params)
            self.assertEqual(error.exception.code,code)
    def test_required_gate_set_missing_duplicate_and_extra_rejected(self):
        from app.modules.auth.evidence import accept_freshness_proof
        for mode in ('missing','duplicate','extra'):
            proof,params=self.bundle();gates=params['required_evidence']
            if mode=='missing':params['required_evidence']=gates[:1]
            elif mode=='duplicate':params['required_evidence']=gates+[gates[0]]
            else:
                extra=verify_signed_envelope(self.signed({**gates[0].payload,'object_id':'extra','gate':'G2','issuer_revision':9},'gate_evidence'),self.keys)
                params['required_evidence']=gates+[extra]
            with self.subTest(mode=mode),self.assertRaises(DomainError) as error:accept_freshness_proof(proof,**params)
            self.assertEqual(error.exception.code,'REQUIRED_GATE_SET_MISMATCH')
    def test_shortest_required_gate_caps_runtime(self):
        from app.modules.auth.evidence import accept_freshness_proof
        proof,params=self.bundle(short_q=True)
        runtime=accept_freshness_proof(proof,**params)
        self.assertEqual(runtime.mono_deadline,10_000_000)
    def test_persisted_revocation_rejects_gate_and_grant(self):
        from app.modules.auth.evidence import accept_freshness_proof
        for kind in ('gate','grant'):
            proof,params=self.bundle()
            target=params['required_evidence'][0] if kind=='gate' else params['grant']
            params['nonces'].evidence_registry.apply_revocations([target.payload['object_id']],3)
            with self.subTest(kind=kind),self.assertRaises(DomainError) as error:accept_freshness_proof(proof,**params)
            self.assertEqual(error.exception.code,'EVIDENCE_REVOKED')
    def test_imported_revocation_invalidates_existing_runtime(self):
        from app.modules.auth.evidence import accept_freshness_proof
        proof,params=self.bundle();runtime=accept_freshness_proof(proof,**params)
        params['nonces'].evidence_registry.apply_revocations(['G1_Q'],3)
        with self.assertRaises(DomainError) as error:runtime.require_current()
        self.assertEqual(error.exception.code,'EVIDENCE_REVOKED')

    def test_all_profiles_require_their_exact_gate_set(self):
        from app.modules.auth.evidence import accept_freshness_proof
        for profile in ('beta','g2_evidence','production'):
            proof,params=self.bundle(profile)
            runtime=accept_freshness_proof(proof,**params)
            self.assertEqual(runtime.profile,profile)
            self.assertEqual(len(runtime.evidence_bindings),{'beta':2,'g2_evidence':4,'production':5}[profile])
            proof,params=self.bundle(profile);params['required_evidence']=params['required_evidence'][:-1]
            with self.assertRaises(DomainError) as error:accept_freshness_proof(proof,**params)
            self.assertEqual(error.exception.code,'REQUIRED_GATE_SET_MISMATCH')
    def test_gate_lifecycle_rejections_are_independent(self):
        from app.modules.auth.evidence import accept_freshness_proof
        now=datetime(2026,10,25,tzinfo=timezone.utc)
        cases=(({'revoked_at':now.isoformat()},'EVIDENCE_REVOKED'),({'result':'fail'},'EVIDENCE_RESULT_MISMATCH'),({'evaluated_at':None},'EVIDENCE_INVALID'),({'evaluated_at':(now+timedelta(seconds=1)).isoformat()},'EVIDENCE_NOT_YET_VALID'),({'expires_at':now.isoformat(),'evaluated_at':(now-timedelta(seconds=1)).isoformat()},'EVIDENCE_EXPIRED'),({'revocation_revision':4},'REVOCATION_REVISION_ROLLBACK'))
        for changes,code in cases:
            proof,params=self.bundle()
            old=params['required_evidence'][1]
            changed=verify_signed_envelope(self.signed({**old.payload,**changes,'object_id':'changed','issuer_revision':10},'gate_evidence'),self.keys)
            params['nonces'].evidence_registry.accept(changed)
            params['required_evidence'][1]=changed
            with self.subTest(changes=changes):
                with self.assertRaises(DomainError) as error:accept_freshness_proof(proof,**params)
                self.assertEqual(error.exception.code,code)

    def test_proof_requires_exact_complete_persisted_revocation_revision(self):
        from app.modules.auth.evidence import accept_freshness_proof
        for persisted,caller,code in ((0,3,'REVOCATION_STATE_INCOMPLETE'),(4,4,'REVOCATION_REVISION_ROLLBACK'),(3,2,'REVOCATION_STATE_INCOMPLETE')):
            proof,params=self.bundle()
            registry=params['nonces'].evidence_registry
            registry.revocation_revision=persisted
            params['revocation_revision']=caller
            with self.subTest(persisted=persisted,caller=caller):
                with self.assertRaises(DomainError) as error:accept_freshness_proof(proof,**params)
                self.assertEqual(error.exception.code,code)
    def test_spec_minimal_grant_uses_candidate_context_for_components(self):
        from app.modules.auth.evidence import accept_freshness_proof
        proof,params=self.bundle()
        source=params['grant'].payload
        for key in ('model_manifest_id','wordbank_manifest_id','calibration_id'):source.pop(key)
        source.update(object_id='minimal-grant',issuer_revision=20)
        grant=verify_signed_envelope(self.signed(source),self.keys)
        params['nonces'].evidence_registry.accept(grant)
        params['grant']=grant
        runtime=accept_freshness_proof(proof,**params)
        context={**source,'live_profile':'beta','model_manifest_id':'m','wordbank_manifest_id':'w','calibration_id':'cal'}
        self.assertTrue(runtime.matches(grant,context))
        for key in ('model_manifest_id','wordbank_manifest_id','calibration_id'):
            with self.subTest(key=key):self.assertFalse(runtime.matches(grant,{**context,key:'changed'}))

    def test_runtime_rejects_incomplete_restored_revocation_state(self):
        from app.modules.auth.evidence import accept_freshness_proof
        proof,params=self.bundle()
        params['nonces'].evidence_registry.apply_revocations([],3)
        runtime=accept_freshness_proof(proof,**params)
        params['nonces'].evidence_registry.revocation_revision=0
        with self.assertRaises(DomainError) as error:runtime.require_current()
        self.assertEqual(error.exception.code,'REVOCATION_STATE_INCOMPLETE')
