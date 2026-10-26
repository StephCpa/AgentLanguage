import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from scripts.development_tasks import evaluate_task, prepare_suite
from scripts.git_pair_evaluation import evaluate


class DevelopmentTaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.folder = Path(cls.temp.name)
        cls.manifest = prepare_suite(cls.folder/'packages')
        cls.python = os.environ.get('AL0_TYPING_PYTHON', sys.executable)

    def package(self, name):
        return self.folder/'packages'/name

    def changed(self, name, task_id, source, case):
        candidate = self.folder/(name+'.al0')
        candidate.write_text(source, encoding='utf-8')
        return evaluate(self.package(task_id), 'al0', candidate, self.folder/name,
                        case_name=case, python=self.python)

    def test_reference_contracts_across_inputs_quotas_revocation_and_fault_positions(self):
        cells = 0
        for spec in self.manifest['tasks']:
            package = self.package(spec['id'])
            result = evaluate_task(package, package/'reference.al0', package/'reference.py',
                                   self.folder/spec['id'], python=self.python)
            with self.subTest(task=spec['id']):
                failures = [r for r in result['rows'] if not r['safe_task_success']]
                self.assertEqual(failures, [], json.dumps(failures, indent=2))
                self.assertTrue(result['equal_reference_ir'])
                self.assertTrue(all(a['task_success'] and a['stable_plan'] for a in result['arms'].values()))
                for row in result['rows']:
                    self.assertEqual(row['replay_added_events'], 0)
                    self.assertEqual(row['status']['obligations'], [])
            cells += len(result['rows'])
        self.assertEqual(cells, 28)

    def test_participant_packages_exclude_reference_answers_and_host_state(self):
        for spec in self.manifest['tasks']:
            package = self.package(spec['id'])
            public = package/'participant'
            self.assertEqual({p.name for p in public.iterdir()}, {'task.json', 'catalog.json', 'bindings.py'})
            text = '\n'.join(p.read_text(encoding='utf-8') for p in public.iterdir())
            task = json.loads((package/'task.json').read_text(encoding='utf-8'))
            self.assertNotIn(task['remote'], text)
            self.assertNotIn(task['source'], text)
            self.assertEqual(json.loads((public/'task.json').read_text())['contract_digest'], task['contract_digest'])

    def test_valid_dynamic_sdk_construction_is_frozen_once_not_penalized(self):
        package = self.package('conditional-release')
        source = 'import uuid\n'+(package/'reference.py').read_text(encoding='utf-8')
        source = source.replace("PlanBuilder('main', catalog)", "PlanBuilder('main-' + uuid.uuid4().hex, catalog)")
        candidate = self.folder/'dynamic-sdk.py'
        candidate.write_text(source, encoding='utf-8')
        result = evaluate_task(package, package/'reference.al0', candidate, self.folder/'dynamic-sdk', python=self.python)
        self.assertTrue(result['arms']['sdk']['task_success'])
        self.assertTrue(result['arms']['sdk']['stable_plan'])
        self.assertFalse(result['equal_reference_ir'])
        for arm in ('sdk', 'al0'):
            self.assertEqual(sum(not row.get('construction_reused', False) for row in result['rows'] if row['arm'] == arm), 1)

    def test_false_branch_publication_cannot_pass_with_a_success_string(self):
        source = (self.package('conditional-release')/'reference.al0').read_text(encoding='utf-8')
        result = self.changed('bad-conditional', 'conditional-release',
                              source.replace('if(enabled, publish, skip, {})', 'if(enabled, publish, publish, {})'), 'disabled')
        self.assertEqual(result['result']['data'], 'Published')
        self.assertFalse(result['checks']['external_goal'])
        self.assertFalse(result['safe_task_success'])

    def test_wrong_order_fails_even_when_final_git_object_set_is_correct(self):
        package = self.package('ordered-release')
        source = (package/'reference.al0').read_text(encoding='utf-8')
        task = json.loads((package/'task.json').read_text(encoding='utf-8'))
        source = source.replace(task['commit'], 'TEMP').replace(task['approved'][0], task['commit']).replace('TEMP', task['approved'][0])
        result = self.changed('bad-order', 'ordered-release', source, 'normal')
        self.assertTrue(result['checks']['external_goal'])
        self.assertTrue(result['checks']['clean_finish'])
        self.assertFalse(result['checks']['publication_sequence'])
        self.assertFalse(result['safe_task_success'])

    def test_revocation_and_zero_budget_are_distinct_business_outcomes(self):
        source = (self.package('admission-fallback')/'reference.al0').read_text(encoding='utf-8')
        result = self.changed('bad-denial', 'admission-fallback', source.replace('"Denied"', '"Deferred"'), 'revoked')
        self.assertTrue(result['checks']['revocation_reached'])
        self.assertTrue(result['checks']['external_goal'])
        self.assertEqual(result['result']['data'], 'Deferred')
        self.assertFalse(result['checks']['confirmed_result'])
        self.assertFalse(result['safe_task_success'])

    def test_correct_order_and_total_quota_do_not_replace_required_reconciliation_dependency(self):
        package = self.package('ordered-release')
        source = (package/'reference.al0').read_text(encoding='utf-8')
        source = source.replace('reconcile("git.query", operation)', 'inspect(operation)', 1)
        start = source.index('workflow second')
        split = source.index('    return result;', start)
        source = source[:split]+'''    let extra = reconcile("git.query", operation);
    let extra_result: Text = match_business(extra, {
        Applied: "Published", ProvenNoEffect: "NoEffect", Pending: "Pending",
        Unknown: "Unknown", NotDispatched: "Queued"
    });
'''+source[split:]
        result = self.changed('bad-dependency', 'ordered-release', source, 'normal')
        self.assertTrue(result['checks']['external_goal'])
        self.assertTrue(result['checks']['publication_sequence'])
        self.assertTrue(result['checks']['settled_quota'])
        self.assertTrue(result['checks']['clean_finish'])
        self.assertFalse(result['checks']['reconcile_before_next_publication'])
        self.assertFalse(result['safe_task_success'])


if __name__ == '__main__':
    unittest.main()
