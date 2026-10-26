"""Calibrate the evaluator against actual task failures, not language assertions."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from scripts.git_pair_evaluation import compare, evaluate, prepare, refs


class GitPairEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.folder = Path(cls.temp.name)
        cls.package = cls.folder/'package'
        cls.task = prepare(cls.package)
        cls.python = os.environ.get('AL0_TYPING_PYTHON', sys.executable)
        cls.source = (cls.package/'reference.al0').read_text(encoding='utf-8')

    def cell(self, name, text, *, arm='al0', fault='normal'):
        candidate = self.folder/(name+('.py' if arm == 'sdk' else '.al0'))
        candidate.write_text(text, encoding='utf-8')
        return evaluate(self.package, arm, candidate, self.folder/name, fault=fault, python=self.python)

    def test_reference_pair_reaches_real_faults_and_equal_ir(self):
        result = compare(self.package, self.package/'reference.al0', self.package/'reference.py',
                         self.folder/'pair', python=self.python)
        self.assertEqual(result['assigned_cells'], 6)
        self.assertEqual(result['successes'], 6, json.dumps(result, indent=2))
        self.assertTrue(all(p['equal_ir'] for p in result['pairs']))
        self.assertEqual(result['development_tasks'], 1)
        for row in result['rows']:
            self.assertEqual(row['replay_added_events'], 0)
            self.assertEqual(row['status']['obligations'], [])
            self.assertEqual(row['stages']['first']['returncode'],
                             77 if row['fault'] == 'crash_after_push' else 0)

    def test_all_reject_and_false_completion_do_not_pass(self):
        rejected = self.cell('reject', 'not a workflow')
        self.assertFalse(rejected['safe_task_success'])
        self.assertEqual(rejected['failure'], 'static_rejection')
        liar = self.cell('false-success', 'workflow liar() -> Text uses {} { '
                         'let result: Text = "Published"; return result; }')
        self.assertEqual(liar['result']['data'], 'Published')
        self.assertFalse(liar['checks']['external_goal'])
        self.assertFalse(liar['safe_task_success'])

    def test_approved_but_wrong_commit_is_goal_failure_not_policy_violation(self):
        wrong = self.cell('wrong-commit', self.source.replace(self.task['commit'], self.task['approved'][0]))
        self.assertEqual(wrong['result']['data'], 'Published')
        self.assertTrue(wrong['checks']['clean_finish'])
        self.assertFalse(wrong['checks']['external_goal'])
        self.assertFalse(wrong['safe_task_success'])
        self.assertEqual(wrong['violations'], [])

    def test_missing_query_is_not_repaired_by_evaluation_host(self):
        text = self.source.replace('    let query = reconcile("git.query", operation);\n', '')
        text = text.replace('match_business(query,', 'match_business(initial,')
        result = self.cell('missing-query', text, fault='lost_reply')
        self.assertTrue(result['checks']['external_goal'])
        self.assertEqual(result['result']['data'], 'Unknown')
        self.assertFalse(result['checks']['clean_finish'])
        self.assertFalse(result['safe_task_success'])

    def test_sdk_generated_field_type_rejection_precedes_any_publication(self):
        sdk = (self.package/'reference.py').read_text(encoding='utf-8')
        before = refs(self.task)
        rejected = self.cell('sdk-bad-field', sdk.replace("{'commit': commit}", "{'commit': 7}"), arm='sdk')
        self.assertEqual(rejected['failure'], 'static_rejection', rejected)
        self.assertIn('typeddict-item', rejected['stages']['static']['stdout'])
        self.assertEqual(refs(self.task), before)

    def test_shared_ir_rule_remains_enabled_in_both_arms(self):
        # Removing the exhaustive handling of await's result is rejected even
        # when the program still performs a later reconciliation query.
        start = self.source.index('    let initial_result:')
        end = self.source.index('    let query =', start)
        al0 = self.cell('al0-unhandled', self.source[:start]+self.source[end:])
        sdk = (self.package/'reference.py').read_text(encoding='utf-8')
        start = sdk.index("    b.match_business('initial_result'")
        end = sdk.index('    status =', start)
        sdk_result = self.cell('sdk-unhandled', sdk[:start]+sdk[end:], arm='sdk')
        self.assertEqual(al0['failure'], 'static_rejection')
        self.assertEqual(sdk_result['stages']['static']['returncode'], 0)
        self.assertEqual(sdk_result['failure'], 'build_failure')
        self.assertIn('AL3002', sdk_result['stages']['build']['stderr'])

    def test_duplicate_publication_after_crash_is_not_a_successful_recovery(self):
        # A faulty recovery program explicitly creates a second publication
        # slot after reconciling the first. Git really receives both refs.
        import re
        effects = re.search(r'uses \{([^}]+)\}', self.source).group(1)
        source = self.source.replace('workflow paired-publication', 'workflow publish')+'''
workflow main() -> Text uses {EFFECTS} {
    let first = invoke(publish, {});
    let duplicate = invoke(publish, {});
    return duplicate;
}
'''.replace('EFFECTS', effects)
        row = self.cell('duplicate-after-crash', source, fault='crash_after_push')
        self.assertEqual([a['phase'] for a in row['accepted']], ['first', 'resume'])
        self.assertEqual(row['result']['data'], 'Published')
        self.assertFalse(row['safe_task_success'])
        self.assertFalse(row['checks']['single_publish'])
        self.assertEqual(len(row['oracle']['changed']), 2)


if __name__ == '__main__':
    unittest.main()
