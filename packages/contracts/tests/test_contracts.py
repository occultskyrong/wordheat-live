import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

class ContractTests(unittest.TestCase):
    def checker(self):
        path = ROOT / "packages/contracts/check_contracts.py"
        self.assertTrue(path.is_file(), "executable contract checker is missing")
        spec = importlib.util.spec_from_file_location("contracts_checker", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def copy_checked_source(self,root):
        import json,shutil
        package=root/'packages/contracts';shutil.copytree(ROOT/'packages/contracts',package)
        inventory=json.loads((package/'source-inventory.json').read_text())
        paths=[e['path'] for group in inventory['applications'].values() for e in group['source_files']]+[e['path'] for e in inventory['generator_inputs']]+[self.checker().SPEC_PATH]
        for path in paths:
            target=root/path
            if target.exists():continue
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/path,target)
        self.assertTrue(self.checker().check(root)['valid'])
        return package

    def test_current_contracts_define_all_current_backend_wires(self):
        result = self.checker().check(ROOT)
        self.assertTrue(result["valid"], result)
        self.assertTrue(result["all_contracts_defined"], result)
        self.assertGreater(result["defined_operations"], 0)

    def test_empty_combination_and_definition_set_are_invalid(self):
        import json
        import shutil
        import tempfile
        checker = self.checker()
        for schema in ({"allOf":[{}]}, {"oneOf":[{"type":"string"},{}]}, {"$ref":"#/$defs/Loop","$defs":{"Loop":{"$ref":"#/$defs/Loop"}}}):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                checker.assert_typed(schema)
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            package = root/'packages/contracts'
            self.copy_checked_source(root)
            document = json.loads((package/'openapi.yaml').read_text())
            document['x-undefined-operations'] += [{'method':m.upper(),'path':p} for p, ops in document['paths'].items() for m in ops]
            document['paths'] = {}
            (package/'openapi.yaml').write_text(json.dumps(document))
            self.assertFalse(checker.check(root)['valid'])

    def test_be_operation_cannot_disappear_from_coverage(self):
        import json
        import shutil
        import tempfile
        checker = self.checker()
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            package = root/'packages/contracts'
            self.copy_checked_source(root)
            document = json.loads((package/'openapi.yaml').read_text())
            document['paths'].pop('/api/v1/live-sessions/{session_id}/analytics/rounds',None)
            document['x-undefined-operations'] = [x for x in document['x-undefined-operations'] if not x['path'].endswith('/analytics/rounds')]
            (package/'openapi.yaml').write_text(json.dumps(document))
            self.assertFalse(checker.check(root)['valid'])

    def test_readiness_has_explicit_c1_and_be_negotiation(self):
        import json
        document = json.loads((ROOT/'packages/contracts/openapi.yaml').read_text())
        operation = document['paths']['/api/v1/health/ready']['get']
        parameters = {(p['in'],p['name']):p for p in operation['parameters']}
        self.assertIn(('header','X-BE-Extension-Version'), parameters)
        self.assertFalse(parameters[('header','X-BE-Extension-Version')]['required'])
        self.assertIn(('query','session_id'), parameters)
        response = document['components']['schemas']['ReadinessResponse']
        self.assertEqual(len(response['oneOf']),2)
        baseline = document['components']['schemas']['C1ReadinessResponse']['properties']['data']
        self.assertNotIn('be_extension_version', baseline['required'])
        self.assertEqual(operation['x-be-negotiation']['absent'], 'C1ReadinessResponse')

    def test_added_requests_have_positive_and_rejecting_samples(self):
        import json
        from jsonschema import Draft202012Validator
        doc = json.loads((ROOT/'packages/contracts/openapi.yaml').read_text())
        self.assertIn('/internal/v1/embeddings',doc['paths'])
        self.assertIn('post',doc['paths']['/api/v1/auth/sessions'])
        for path in ['/api/v1/auth/sessions','/api/v1/display-tickets','/api/v1/display-sessions','/api/v1/rehearsal/guesses','/internal/v1/embeddings']:
            operation=doc['paths'][path]['post']
            media=operation['requestBody']['content']['application/json']
            validator=Draft202012Validator(dict(media['schema'],components=doc['components']))
            self.assertTrue(validator.is_valid(media['example']),path)
            self.assertFalse(validator.is_valid(operation['x-invalid-request-example']),path)
            self.assertIn('403',operation['responses'])
            self.assertIn('x-role',operation)
            success=operation['responses']['200']['content']['application/json']
            self.assertIn('example',success,path)
            self.assertTrue(Draft202012Validator(dict(success['schema'],components=doc['components'])).is_valid(success['example']),path)
            self.assertFalse(Draft202012Validator(dict(success['schema'],components=doc['components'])).is_valid(operation['x-invalid-response-example']),path)

    def test_merged_core_http_and_websocket_contracts_are_concrete(self):
        import json
        checker=self.checker()
        document=json.loads((ROOT/'packages/contracts/openapi.yaml').read_text())
        http=[(method,path) for path,ops in document['paths'].items() for method in ops]
        self.assertGreaterEqual(len(http),35)
        self.assertTrue(all(method in ('get','post','patch','delete','put','head','options','trace') for method,path in http))
        self.assertIn('/api/v1/display/ws',document['x-websocket-protocols'])
        self.assertIn('/relay/v1/streams',document['x-websocket-protocols'])
        self.assertTrue((ROOT/'packages/contracts/local-websocket.schema.json').is_file())
        for name,schema in document['components']['schemas'].items():
            checker.assert_typed(schema,document)
        with self.assertRaises(ValueError):checker.assert_typed({'type':'object','additionalProperties':True})
        checker.assert_typed({'type':'object','properties':{},'additionalProperties':False,'maxProperties':0})

    def test_analytics_be_header_is_mandatory_for_pagination_branches(self):
        import json
        from jsonschema import Draft202012Validator
        document=json.loads((ROOT/'packages/contracts/openapi.yaml').read_text())
        rounds=document['paths']['/api/v1/live-sessions/{session_id}/analytics/rounds']['get']
        header=next(p for p in rounds['parameters'] if p['name']=='X-BE-Extension-Version')
        self.assertTrue(header['required'])
        self.assertEqual(header['schema']['const'],'LW-BE/1.1.0')
        aggregate=document['paths']['/api/v1/live-sessions/{session_id}/analytics']['get']
        self.assertIn('x-request-schema',aggregate)
        validator=Draft202012Validator(aggregate['x-request-schema'])
        base={'headers':{'X-Contract-Version':'LW-C1/1.0.0'},'query':{}}
        self.assertTrue(validator.is_valid(base))
        for query in ({'snapshot_id':'snapshot-1'},{'section':'minutes'},{'cursor':'cursor-1'},{'format':'csv'},{'fresh':True}):
            with self.subTest(query=query):
                body=dict(base,query=query)
                self.assertFalse(validator.is_valid(body))
                body['headers']={'X-Contract-Version':'LW-C1/1.0.0','X-BE-Extension-Version':'LW-BE/1.1.0'}
                self.assertTrue(validator.is_valid(body))

    def test_current_routes_and_native_protocol_are_separate_and_source_bound(self):
        import json
        package=ROOT/'packages/contracts'
        inventory=json.loads((package/'source-inventory.json').read_text())
        document=json.loads((package/'openapi.yaml').read_text())
        self.assertEqual(set(inventory['applications']),{'api','scorer','relay'})
        for app,source in inventory['applications'].items():
            self.assertTrue(source['source_files'])
            for route in source['http_operations']:
                self.assertIn(route['path'],document['paths'])
                self.assertIn(route['method'],document['paths'][route['path']])
        self.assertIn('/api/v1/runtime/commands',document['paths'])
        self.assertIn('/relay/v1/device-pairings',document['paths'])
        self.assertIn('/relay/v1/douyin/callback',document['paths'])
        self.assertFalse(any('/native' in p for p in document['paths']))
        native=json.loads((package/'native-control.schema.json').read_text())
        self.assertEqual(native['x-transport'],'private-unix-socket')
        self.checker().assert_typed(native)

    def test_current_backend_wire_fixtures(self):
        import json
        from jsonschema import Draft202012Validator
        package=ROOT/'packages/contracts';doc=json.loads((package/'openapi.yaml').read_text())
        cases=json.loads((package/'fixtures/backend-wire.json').read_text())['cases']
        for case in cases:
            with self.subTest(case=case['id']):
                if 'request' in case:
                    operation=doc['paths'][case['path']]['post']
                    schema=operation['requestBody']['content']['application/json']['schema'];positive=case['request']
                    if case.get('success_status')=='204':self.assertNotIn('content',operation['responses']['204'])
                else:schema={'$ref':'#/components/schemas/'+case['schema']};positive=case['response']
                validator=Draft202012Validator(dict(schema,components=doc['components']))
                self.assertTrue(validator.is_valid(positive),list(validator.iter_errors(positive)))
                self.assertFalse(validator.is_valid(case['invalid']))

    def test_204_cannot_be_replaced_with_a_fake_json_envelope(self):
        import json,shutil,tempfile
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root=Path(directory);package=self.copy_checked_source(root)
            document=json.loads((package/'openapi.yaml').read_text())
            document['paths']['/relay/v1/douyin/callback']['post']['responses']['204']['content']={'application/json':{'schema':{'type':'string'}}}
            (package/'openapi.yaml').write_text(json.dumps(document))
            self.assertFalse(self.checker().check(root)['valid'])

    def test_cross_app_schema_name_collision_is_rejected(self):
        spec=importlib.util.spec_from_file_location('exporter',ROOT/'scripts/backend/20261026-export-contracts.py')
        exporter=importlib.util.module_from_spec(spec);spec.loader.exec_module(exporter)
        schemas={'Shared':{'type':'string'}}
        exporter.merge_schemas(schemas,{'Shared':{'type':'string'}})
        with self.assertRaises(ValueError):exporter.merge_schemas(schemas,{'Shared':{'type':'integer'}})

    def test_protected_relay_operations_declare_machine_auth_and_bootstrap_key(self):
        import json
        doc=json.loads((ROOT/'packages/contracts/openapi.yaml').read_text())
        for path,operations in doc['paths'].items():
            if path.startswith('/relay/') and path!='/relay/v1/douyin/callback' and not path.startswith('/relay/v1/device-tokens/'):
                with self.subTest(path=path):self.assertEqual(operations['post']['security'],[{'RelayBearer':[]}])
        for path in ('/api/v1/runtime/commands','/api/v1/connectors/{connector_id}/commands'):
            self.assertEqual(doc['paths'][path]['post']['security'],[{'OperatorCookie':[]}])
        bootstrap=doc['paths']['/relay/v1/streams/bootstrap']['post']
        headers={p['name']:p for p in bootstrap['parameters'] if p['in']=='header'}
        self.assertTrue(headers['Idempotency-Key']['required'])

    def test_recursive_marker_cannot_authorize_unproductive_infinite_shapes(self):
        checker=self.checker()
        loops=[{'$ref':'#/$defs/Loop','$defs':{'Loop':{'x-recursive-json-value':True,'$ref':'#/$defs/Loop'}}},
               {'$ref':'#/$defs/Loop','$defs':{'Loop':{'x-recursive-json-value':True,'type':'object','properties':{'next':{'$ref':'#/$defs/Loop'}},'required':['next'],'additionalProperties':False}}}]
        for schema in loops:
            with self.subTest(schema=schema),self.assertRaises(ValueError):checker.assert_typed(schema)
        checker.assert_typed({'$ref':'#/$defs/Value','$defs':{'Value':{'anyOf':[{'type':'null'},{'type':'array','items':{'$ref':'#/$defs/Value'}}]}}})

    def test_missing_indexed_source_is_rejected(self):
        import json,shutil,tempfile
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);package=self.copy_checked_source(root)
            inventory=json.loads((package/'source-inventory.json').read_text())
            paths=[e['path'] for group in inventory['applications'].values() for e in group['source_files']]+[e['path'] for e in inventory['generator_inputs']]
            for path in paths:
                source=ROOT/path;target=root/path
                if target.exists():continue
                target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
            (root/'apps/relay/app/control_routes.py').unlink()
            result=self.checker().check(root)
            self.assertFalse(result['valid']);self.assertIn('control_routes.py',str(result['errors']))

    def test_session_commands_require_command_specific_args(self):
        import json
        from jsonschema import Draft202012Validator
        doc=json.loads((ROOT/'packages/contracts/openapi.yaml').read_text())
        validator=Draft202012Validator({'$ref':'#/components/schemas/SessionCommand','components':doc['components']})
        for command in ['start','select_next','configure']:
            with self.subTest(command=command):self.assertFalse(validator.is_valid({'command':command,'expected_version':1}))
        self.assertTrue(validator.is_valid({'command':'start','expected_version':1,'args':{'run_purpose':'local'}}))
        self.assertFalse(validator.is_valid({'command':'start','expected_version':1,'args':{'strategy':'random'}}))
        self.assertTrue(validator.is_valid({'command':'end','expected_version':1}))

    def test_native_requests_do_not_accept_os_identity_or_web_confirmation_claims(self):
        import json
        from jsonschema import Draft202012Validator
        schema=json.loads((ROOT/'packages/contracts/native-control.schema.json').read_text());validate=Draft202012Validator(schema).is_valid
        self.assertTrue(validate({'action':'device.prepare','payload':{}}))
        self.assertFalse(validate({'action':'device.prepare','payload':{'peer_uid':0,'verified':True}}))
        self.assertFalse(validate({'action':'confirm','payload':{'command_id':'synthetic','confirmed':True}}))
        self.assertFalse(validate({'action':'shell','payload':{'command':'arbitrary'}}))

    def test_checker_rejects_dropped_machine_auth_metadata(self):
        import json,shutil,tempfile
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);package=self.copy_checked_source(root)
            inventory=json.loads((package/'source-inventory.json').read_text())
            for entry in [e for group in inventory['applications'].values() for e in group['source_files']]+inventory['generator_inputs']:
                source=ROOT/entry['path'];target=root/entry['path']
                if not target.exists():target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
            document=json.loads((package/'openapi.yaml').read_text());document['paths']['/relay/v1/freshness']['post'].pop('security')
            (package/'openapi.yaml').write_text(json.dumps(document))
            result=self.checker().check(root);self.assertFalse(result['valid']);self.assertIn('Bearer',str(result['errors']))

    def test_c1_digest_is_pinned(self):
        checker = self.checker()
        self.assertEqual(checker.C1_SHA256, "d69ae165a6d1169e00f20bb03b4cd66bdc9ed2fe7ef2a730c2a130934a88b4e8")
        self.assertFalse(checker.verify_baseline(b"{}\n"))

    def test_untyped_object_is_rejected(self):
        checker = self.checker()
        for schema in ({"type":"object", "additionalProperties":True}, {"type":["object","null"], "additionalProperties":True}):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                checker.assert_typed(schema)

    def test_missing_response_schema_invalidates_contract(self):
        import json
        import shutil
        import tempfile
        checker = self.checker()
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            package = root/'packages/contracts'
            self.copy_checked_source(root)
            document = json.loads((package/'openapi.yaml').read_text())
            document['paths']['/api/v1/health/ready']['get']['responses']['200'] = {'description':'untyped'}
            (package/'openapi.yaml').write_text(json.dumps(document))
            self.assertFalse(checker.check(root)['valid'])

    def test_schema_rejects_numeric_wire_sequence(self):
        import json
        from jsonschema import Draft202012Validator
        schema = json.loads((ROOT/'packages/contracts/relay.schema.json').read_text())
        data = {"kind":"relay.frame","lease_id":"l","installation_id":"i","persisted_seq":"0","body_b64":"e30=", "immutable_sha256":"a"*64, "stream_id":"s", "stream_seq":123,
                "delivery_generation":"2", "connection_nonce":"n"}
        self.assertFalse(Draft202012Validator(schema).is_valid(data))
        data["stream_seq"] = "123"
        self.assertTrue(Draft202012Validator(schema).is_valid(data))
        data["answer"] = "forbidden"
        self.assertFalse(Draft202012Validator(schema).is_valid(data))

    def test_reference_must_resolve(self):
        checker = self.checker()
        with self.assertRaises(ValueError):
            checker.assert_typed({"$ref":"#/missing"}, {})

if __name__ == "__main__":
    unittest.main()
