import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

class CompletionTests(unittest.TestCase):
    def checker(self):
        path = ROOT / "scripts/backend/20261025-check-completion.py"
        self.assertTrue(path.is_file(), "completion checker is missing")
        spec = importlib.util.spec_from_file_location("completion_checker", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_no_evidence_never_complete(self):
        result = self.checker().evaluate(ROOT, [])
        self.assertFalse(result["all_complete"])
        self.assertEqual(result["passed"], 0)
        self.assertGreater(len(result["checks"]), 50)

    def test_manifest_rebinding_all_ids_to_fake_local_test_is_invalid(self):
        import json
        checker = self.checker()
        manifest = json.loads((ROOT/'docs/acceptance/backend-checks.json').read_text())
        for item in manifest['checks']:
            item['required_environment'] = 'local'
            item['implementation_paths'] = ['fake.py']
            item['verification'] = [{'node_id':'fake.py::Test::test_fake','environment':'local','junit_classname':'fake.Test','junit_name':'test_fake'}]
        self.assertFalse(checker.validate_manifest(manifest))

    def test_environment_owner_and_source_each_have_fixed_guards(self):
        import copy
        import json
        checker = self.checker()
        baseline = json.loads((ROOT/'docs/acceptance/backend-checks.json').read_text())
        for key,value in [('owner','completion'),('required_environment','local')]:
            manifest = copy.deepcopy(baseline)
            next(item for item in manifest['checks'] if item['id']=='WT01')[key] = value
            self.assertFalse(checker.validate_manifest(manifest,ROOT))
        manifest = copy.deepcopy(baseline)
        item = next(item for item in manifest['checks'] if item['id']=='COMPLETION-TOOL')
        item['verification'][0]['node_id'] = 'scripts/backend/tests/test_completion.py::CompletionTests::test_does_not_exist'
        self.assertFalse(checker.validate_manifest(manifest,ROOT))

    def test_runner_rejects_non_test_paths_and_uses_actual_pytest(self):
        import hashlib
        import json
        import tempfile
        checker=self.checker()
        self.assertTrue(callable(getattr(checker,'run_pytest_evidence',None)), 'fixed pytest runner missing')
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root=Path(directory)
            source=root/checker.SPEC_PATH;source.parent.mkdir(parents=True);source.write_text('synthetic spec fixture')
            checker.SPEC_SHA256=hashlib.sha256(source.read_bytes()).hexdigest()
            manifest=root/'docs/acceptance/backend-checks.json';manifest.parent.mkdir(parents=True);manifest.write_text(json.dumps({'checks':[]}))
            test=root/'tests/unit/test_actual_runner.py';test.parent.mkdir(parents=True);test.write_text('def test_pass():\n    assert 2+2 == 4\n')
            manifest.write_text(json.dumps({'checks':[],'component_subcases':[{'subcase_id':'fixture-unit','node_id':'tests/unit/test_actual_runner.py::test_pass','environment':'local','stage':'test','group_refs':['BE-A01'],'test_source_sha256':hashlib.sha256(test.read_bytes()).hexdigest()}]}))
            with self.assertRaises(ValueError):checker.run_pytest_evidence(root,['fake.py'],'local','docs/acceptance/evidence')
            report=checker.run_pytest_evidence(root,['tests/unit/test_actual_runner.py'],'local','docs/acceptance/evidence')
            self.assertEqual(report['exit_code'],0)
            self.assertEqual(report['collected_node_ids'],['tests/unit/test_actual_runner.py::test_pass'])
            self.assertTrue((root/report['junit_path']).is_file())
            self.assertEqual(report.get('subcase_results',[{}])[0].get('subcase_id'),'fixture-unit')
            self.assertFalse(report['subcase_results'][0]['entire_acceptance_group_complete'])
            self.assertEqual(report['component_results'][0]['status'],'passed')
            self.assertTrue((root/'docs/acceptance/backend-progress.json').is_file())
            self.assertTrue(callable(getattr(checker,'build_progress',None)))
            self.assertEqual(checker.build_progress(root,[report])['component_results'][0]['status'],'passed')
            reviewed=source.read_bytes()
            source.write_text('changed Spec after actual trusted test run')
            stale=checker.build_progress(root,[report])
            self.assertTrue(all(component['status']!='passed' for component in stale['component_results']))
            self.assertTrue(all(stage['passed_components']==0 for stage in stale['stages'].values()))
            source.unlink()
            missing=checker.build_progress(root,[report])
            self.assertTrue(all(component['status']!='passed' for component in missing['component_results']))
            source.write_bytes(reviewed)
            test.write_text('def test_pass():\n    assert False\n')
            self.assertEqual(checker.build_progress(root,[report])['component_results'][0]['status'],'stale')

    def test_runner_cannot_promote_declared_external_or_model_environment(self):
        import hashlib
        import json
        import tempfile
        import sys
        checker=self.checker()
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root=Path(directory)
            source=root/checker.SPEC_PATH;source.parent.mkdir(parents=True);source.write_text('synthetic spec fixture')
            checker.SPEC_SHA256=hashlib.sha256(source.read_bytes()).hexdigest()
            manifest=root/'docs/acceptance/backend-checks.json';manifest.parent.mkdir(parents=True);manifest.write_text(json.dumps({'checks':[]}))
            test=root/'tests/unit/test_synthetic.py';test.parent.mkdir(parents=True);test.write_text('def test_pass():\n    assert True\n')
            for environment in ('windows','windows_obs','platform'):
                with self.subTest(environment=environment), self.assertRaises(ValueError):
                    checker.run_pytest_evidence(root,['tests/unit/test_synthetic.py'],environment,'docs/acceptance/evidence')
            report=checker.run_pytest_evidence(root,['tests/unit/test_synthetic.py'],'model','docs/acceptance/evidence')
            self.assertNotEqual(report['component_results'][0]['status'],'passed')
            self.assertEqual(report['observed_runtime']['sys_platform'],sys.platform)
            self.assertEqual(report['declared_environment'],'model')

    def test_runner_rootdir_and_service_imports_are_isolated(self):
        import hashlib
        import json
        import tempfile
        checker=self.checker()
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root=Path(directory)
            source=root/checker.SPEC_PATH;source.parent.mkdir(parents=True);source.write_text('synthetic spec fixture')
            checker.SPEC_SHA256=hashlib.sha256(source.read_bytes()).hexdigest()
            manifest=root/'docs/acceptance/backend-checks.json';manifest.parent.mkdir(parents=True);manifest.write_text(json.dumps({'checks':[]}))
            api=root/'apps/api';(api/'app').mkdir(parents=True);(api/'app/__init__.py').write_text('SERVICE="api"\n')
            (api/'pyproject.toml').write_text('[tool.pytest.ini_options]\ntestpaths=["tests"]\n')
            test=api/'tests/unit/test_service.py';test.parent.mkdir(parents=True);test.write_text('from app import SERVICE\ndef test_api():\n    assert SERVICE == "api"\n')
            scorer=root/'apps/scorer/tests/unit/test_service.py';scorer.parent.mkdir(parents=True);scorer.write_text('def test_scorer():\n    assert True\n')
            with self.assertRaisesRegex(ValueError,'cross-service'):
                checker.run_pytest_evidence(root,['apps/api/tests/unit/test_service.py','apps/scorer/tests/unit/test_service.py'],'local','docs/acceptance/evidence')
            report=checker.run_pytest_evidence(root,['apps/api/tests/unit/test_service.py'],'local','docs/acceptance/evidence')
            self.assertEqual(report['exit_code'],0)
            self.assertEqual(report['collected_node_ids'],['apps/api/tests/unit/test_service.py::test_api'])
            self.assertEqual(report['observed_runtime']['service'],'api')

    def test_timestamp_order_uses_utc_instants(self):
        checker = self.checker()
        self.assertTrue(callable(getattr(checker, 'report_time', None)), 'aware UTC timestamp helper missing')
        old_green = checker.report_time('2026-10-25T03:00:00+08:00')
        new_failure = checker.report_time('2026-10-25T00:00:00Z')
        self.assertLess(old_green, new_failure)
        self.assertEqual(new_failure.utcoffset().total_seconds(),0)

    def test_empty_report_is_not_evidence(self):
        result = self.checker().evaluate(ROOT, [{"report_version":1,"checks":[]}])
        self.assertEqual(result["passed"], 0)

    def test_skipped_and_old_spec_do_not_pass(self):
        checker = self.checker()
        report = {"report_version":1,"spec_sha256":"old", "checks":[{"id":"BE-A01", "outcome":"passed", "node_id":"arbitrary"}]}
        result = checker.evaluate(ROOT, [report])
        self.assertEqual(result["passed"], 0)

    def test_forged_success_without_junit_cannot_pass(self):
        import json
        checker = self.checker()
        report = {"report_version":1, "spec_sha256":checker.SPEC_SHA256,
                  "run_id":"synthetic", "started_at":"2026-10-25T00:00:00Z", "finished_at":"2026-10-25T00:00:01Z",
                  "checks":[{"id":"COMPLETION-TOOL", "node_id":"test_completion.CompletionTests", "outcome":"passed", "executed":True, "exit_code":0,"tests_run":4,"skipped":0,"environment":"local"}]}
        result = checker.evaluate(ROOT, [report])
        self.assertEqual(result["passed"], 0)

    def test_junit_skipped_and_empty_are_rejected(self):
        checker = self.checker()
        self.assertTrue(callable(getattr(checker, 'junit_passed', None)), 'JUnit verifier is missing')
        self.assertFalse(checker.junit_passed(b'<testsuites/>', 'X', 'test_x'))
        self.assertFalse(checker.junit_passed(b'<testsuite><testcase classname="X" name="test_x"><skipped/></testcase></testsuite>', 'X', 'test_x'))
        self.assertFalse(checker.junit_passed(b'<testsuite><testcase classname="X" name="test_x"><failure/></testcase></testsuite>', 'X', 'test_x'))
        self.assertTrue(checker.junit_passed(b'<testsuite><testcase classname="X" name="test_x"/></testsuite>', 'X', 'test_x'))

    def test_matching_junit_passes_then_implementation_drift_fails(self):
        import hashlib
        import json
        import tempfile
        import shutil
        checker = self.checker()
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            (root/'docs/acceptance').mkdir(parents=True)
            shutil.copytree(ROOT/'packages/contracts', root/'packages/contracts')
            shutil.copytree(ROOT/'scripts/backend', root/'scripts/backend')
            source = root/checker.SPEC_PATH
            source.parent.mkdir(parents=True)
            source.write_bytes(b'synthetic Spec fixture, not acceptance evidence')
            checker.SPEC_SHA256 = hashlib.sha256(source.read_bytes()).hexdigest()
            manifest = json.loads((ROOT/'docs/acceptance/backend-checks.json').read_text())
            # Preserve other declared groups in this synthetic fixture as real bindings grow.
            for check in manifest['checks']:
                if check['id'] != 'COMPLETION-TOOL':
                    for path in check['implementation_paths']:
                        target = root/path
                        if not target.exists():
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(ROOT/path, target)
            manifest['spec_sha256'] = checker.SPEC_SHA256
            for check in manifest['checks']:
                check['spec_sha256'] = checker.SPEC_SHA256
            item = next(c for c in manifest['checks'] if c['id']=='COMPLETION-TOOL')
            test_path = 'scripts/backend/tests/test_fixture.py'
            implementation_path = 'scripts/backend/20261025-check-completion.py'
            node = test_path+'::TestFixture::test_actual'
            item['implementation_paths'] = [implementation_path,test_path]
            item['verification'] = [{'node_id':node,'command':['python','-m','pytest',node], 'environment':'local', 'junit_classname':'scripts.backend.tests.test_fixture.TestFixture', 'junit_name':'test_actual'}]
            (root/'docs/acceptance/backend-checks.json').write_text(json.dumps(manifest))
            implementation = root/implementation_path
            (root/test_path).write_text('class TestFixture:\n    def test_actual(self):\n        assert 1 == 1\n')
            junit = root/'actual.xml'
            junit.write_text('<testsuites><testsuite><testcase classname="scripts.backend.tests.test_fixture.TestFixture" name="test_actual"/></testsuite></testsuites>')
            entry = {'id':'COMPLETION-TOOL','node_id':node,'outcome':'passed','executed':True,'exit_code':0,'tests_run':1,'skipped':0,'environment':'local','artifact_format':'junit','artifact_path':'actual.xml','artifact_sha256':hashlib.sha256(junit.read_bytes()).hexdigest(),'implementation_sha256':{path:hashlib.sha256((root/path).read_bytes()).hexdigest() for path in item['implementation_paths']}}
            report = {'report_version':1,'spec_sha256':checker.SPEC_SHA256,'run_id':'unit-fixture','collected_node_ids':[node],'started_at':'2026-10-25T00:00:00Z','finished_at':'2026-10-25T00:00:01Z','checks':[entry]}
            result = checker.evaluate(root, [report])
            self.assertEqual(result['passed'], 1)
            self.assertFalse(result['all_complete'])
            self.assertFalse(result['contracts_complete'])
            self.assertEqual(checker.evaluate(root,[dict(report,collected_node_ids=[])])['passed'],0)
            old_green = dict(report, started_at='2026-10-25T03:00:00+08:00', finished_at='2026-10-25T03:00:01+08:00')
            same_time_broken_artifact = dict(report,checks=[dict(entry,artifact_sha256='0'*64)])
            self.assertEqual(checker.evaluate(root,[report,same_time_broken_artifact])['passed'],0)
            new_failure = dict(report, started_at='2026-10-25T00:00:00Z', finished_at='2026-10-25T00:00:01Z', checks=[dict(entry,outcome='failed',exit_code=1)])
            self.assertEqual(checker.evaluate(root,[old_green,new_failure])['passed'],0)
            implementation.write_text('value = 2\n')
            self.assertEqual(checker.evaluate(root, [report])['passed'], 0)


    def a19_fixture(self, root, checker):
        """Synthetic evidence tests the checker only; it is never product acceptance."""
        import hashlib
        import json
        source = root / checker.SPEC_PATH
        source.parent.mkdir(parents=True)
        source.write_text('synthetic A19 checker fixture')
        checker.SPEC_SHA256 = hashlib.sha256(source.read_bytes()).hexdigest()
        manifest = json.loads((ROOT/'docs/acceptance/backend-checks.json').read_text())
        manifest['spec_sha256'] = checker.SPEC_SHA256
        for check in manifest['checks']:
            check['spec_sha256'] = checker.SPEC_SHA256
            check['implementation_paths'] = []
            check['verification'] = []
        item = next(check for check in manifest['checks'] if check['id'] == 'BE-A19')
        dependencies = [
            'apps/api/app/modules/wordbank/selection.py',
            'apps/api/app/modules/wordbank/catalog.py',
            'apps/api/app/modules/wordbank/service.py',
            'apps/api/app/modules/rounds/worker.py',
        ]
        test_path = 'apps/api/tests/integration/sessions/test_selection_fixture.py'
        node = test_path + '::test_selection'
        item['implementation_paths'] = ['apps/api/app/modules/sessions/service.py', *dependencies, test_path]
        item['verification'] = [{
            'node_id': node, 'command': ['python', '-m', 'pytest', node],
            'environment': 'postgres',
            'junit_classname': 'apps.api.tests.integration.sessions.test_selection_fixture',
            'junit_name': 'test_selection',
        }]
        for path in item['implementation_paths']:
            target = root/path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('def test_selection():\n    assert True\n' if path == test_path else 'VALUE = 1\n')
        target = root/'docs/acceptance/backend-checks.json'
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(manifest))
        junit = root/'fixture.xml'
        junit.write_text('<testsuites><testsuite><testcase classname="apps.api.tests.integration.sessions.test_selection_fixture" name="test_selection"/></testsuite></testsuites>')
        entry = {
            'id': 'BE-A19', 'node_id': node, 'outcome': 'passed', 'executed': True,
            'exit_code': 0, 'tests_run': 1, 'skipped': 0, 'environment': 'postgres',
            'artifact_format': 'junit', 'artifact_path': 'fixture.xml',
            'artifact_sha256': hashlib.sha256(junit.read_bytes()).hexdigest(),
            'implementation_sha256': {path: hashlib.sha256((root/path).read_bytes()).hexdigest()
                                      for path in item['implementation_paths']},
        }
        report = {'report_version': 1, 'spec_sha256': checker.SPEC_SHA256,
                  'run_id': 'synthetic-checker-fixture', 'collected_node_ids': [node],
                  'started_at': '2026-10-25T00:00:00Z', 'finished_at': '2026-10-25T00:00:01Z',
                  'checks': [entry]}
        return manifest, item, dependencies, report

    def test_a19_exact_cross_module_dependencies_bind_and_each_drift_invalidates(self):
        import tempfile
        checker = self.checker()
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            manifest, item, dependencies, report = self.a19_fixture(root, checker)
            self.assertTrue(checker.validate_manifest(manifest, root),
                            'A19 must bind its four exact cross-module source dependencies')
            self.assertEqual(checker.evaluate(root, [report])['passed'], 1)
            import copy
            for dependency in dependencies:
                with self.subTest(omitted_dependency=dependency):
                    incomplete = copy.deepcopy(manifest)
                    bound = next(check for check in incomplete['checks'] if check['id'] == 'BE-A19')
                    bound['implementation_paths'].remove(dependency)
                    self.assertFalse(checker.validate_manifest(incomplete, root))
            for dependency in dependencies:
                with self.subTest(dependency=dependency):
                    target = root/dependency
                    original = target.read_bytes()
                    target.write_text('VALUE = 2\n')
                    self.assertEqual(checker.evaluate(root, [report])['passed'], 0)
                    target.write_bytes(original)

    def test_a19_evidence_guards_reject_stale_spec_junit_and_newer_failure(self):
        import copy
        import tempfile
        checker = self.checker()
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            manifest, item, dependencies, report = self.a19_fixture(root, checker)
            self.assertEqual(checker.evaluate(root, [report])['passed'], 1)
            for mutation in ['missing_collection', 'stale_spec', 'wrong_junit_hash',
                             'missing_dependency_hash', 'newer_failure']:
                with self.subTest(mutation=mutation):
                    changed = copy.deepcopy(report)
                    entry = changed['checks'][0]
                    if mutation == 'missing_collection':
                        changed['collected_node_ids'] = []
                    elif mutation == 'stale_spec':
                        changed['spec_sha256'] = '0' * 64
                    elif mutation == 'wrong_junit_hash':
                        entry['artifact_sha256'] = '0' * 64
                    elif mutation == 'missing_dependency_hash':
                        entry['implementation_sha256'].pop(dependencies[0])
                    else:
                        changed['finished_at'] = '2026-10-25T00:00:02Z'
                        entry.update(outcome='failed', exit_code=1)
                    self.assertEqual(checker.evaluate(root, [changed] if mutation != 'newer_failure'
                                                      else [report, changed])['passed'], 0)
            (root/'fixture.xml').write_text('<testsuites><testsuite><testcase classname="wrong" name="test_selection"/></testsuite></testsuites>')
            import hashlib
            report['checks'][0]['artifact_sha256'] = hashlib.sha256((root/'fixture.xml').read_bytes()).hexdigest()
            self.assertEqual(checker.evaluate(root, [report])['passed'], 0)
            (root/checker.SPEC_PATH).write_text('drifted Spec fixture')
            self.assertFalse(checker.evaluate(root, [report])['source_matches'])
            self.assertEqual(checker.evaluate(root, [report])['passed'], 0)

    def test_a19_omitted_dependencies_cannot_leave_old_evidence_passed(self):
        import hashlib
        import json
        import tempfile
        checker = self.checker()
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            manifest, item, dependencies, report = self.a19_fixture(root, checker)
            # Reproduce the old sessions-only binding: its hash list omits actual dependencies.
            item['implementation_paths'] = [path for path in item['implementation_paths'] if path not in dependencies]
            report['checks'][0]['implementation_sha256'] = {
                path: hashlib.sha256((root/path).read_bytes()).hexdigest() for path in item['implementation_paths']}
            (root/'docs/acceptance/backend-checks.json').write_text(json.dumps(manifest))
            (root/dependencies[0]).write_text('VALUE = 2\n')
            self.assertEqual(checker.evaluate(root, [report])['passed'], 0,
                             'unbound selection source drift must not retain passed A19 evidence')
            self.assertFalse(checker.validate_manifest(manifest, root))

    def test_a19_dependency_allowlist_preserves_owner_environment_and_path_guards(self):
        import copy
        import tempfile
        checker = self.checker()
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            manifest, item, dependencies, report = self.a19_fixture(root, checker)
            for field, value in [('owner', 'api.wordbank'), ('owner', 'fakeowner'),
                                 ('required_environment', 'local')]:
                with self.subTest(field=field, value=value):
                    changed = copy.deepcopy(manifest)
                    next(check for check in changed['checks'] if check['id'] == 'BE-A19')[field] = value
                    self.assertFalse(checker.validate_manifest(changed, root))
            for path in ['apps/api/app/modules/wordbank/unrelated.py',
                         'apps/api/app/modules/wordbank/selection.py.extra',
                         'apps/api/app/modules/rounds/unrelated.py']:
                with self.subTest(path=path):
                    changed = copy.deepcopy(manifest)
                    next(check for check in changed['checks'] if check['id'] == 'BE-A19')['implementation_paths'].append(path)
                    self.assertFalse(checker.validate_manifest(changed))
            changed = copy.deepcopy(manifest)
            other = next(check for check in changed['checks'] if check['id'] == 'BE-A20')
            other['implementation_paths'] = list(item['implementation_paths'])
            other['verification'] = copy.deepcopy(item['verification'])
            self.assertFalse(checker.validate_manifest(changed))

    def test_manifest_omission_is_invalid(self):
        import json
        checker = self.checker()
        self.assertTrue(callable(getattr(checker, 'validate_manifest', None)), 'manifest coverage guard missing')
        self.assertFalse(checker.validate_manifest({"checks":[], "spec_sha256":checker.SPEC_SHA256}))
        manifest = json.loads((ROOT/"docs/acceptance/backend-checks.json").read_text())
        self.assertTrue(checker.validate_manifest(manifest))
        manifest["checks"] = manifest["checks"][:-1]
        self.assertFalse(checker.validate_manifest(manifest))

    def test_manifest_covers_expected_acceptance_ids(self):
        import json
        checker = self.checker()
        manifest = json.loads((ROOT/"docs/acceptance/backend-checks.json").read_text())
        ids = {c["id"] for c in manifest["checks"]}
        self.assertTrue({f"BE-{i:02}" for i in range(1,11)} <= ids)
        self.assertTrue({f"BE-A{i:02}" for i in range(1,22)} <= ids)
        self.assertTrue({f"WT{i:02}" for i in range(1,25)} <= ids)
        self.assertTrue({f"T{i:02}" for i in range(1,28)} <= ids)

if __name__ == "__main__":
    unittest.main()
