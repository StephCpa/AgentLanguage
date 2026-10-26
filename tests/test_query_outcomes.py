import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.check_query_outcomes import extend_package
from scripts.development_tasks import evaluate_submissions, prepare_suite
from scripts.generation_journal import feedback


class QueryOutcomeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        prepare_suite(cls.root/'tasks')
        cls.original = cls.root/'tasks'/'conditional-release'
        cls.before = (cls.original/'task.json').read_bytes()
        cls.package = cls.root/'extended'
        extend_package(cls.original, cls.package)
        cls.python = os.environ.get('AL0_TYPING_PYTHON', sys.executable)

    def test_both_references_preserve_unknown_with_real_git_and_replay(self):
        report = evaluate_submissions(self.package,
            {arm: self.package/('reference.py' if arm == 'sdk' else 'reference.al0') for arm in ('al0', 'sdk')},
            self.root/'references', python=self.python)
        for row in report['rows']:
            self.assertTrue(row['safe_task_success'], json.dumps(row, indent=2))
            self.assertEqual(row['result']['data'], 'Unknown')
            self.assertFalse(row['business_goal_completed'])
            self.assertFalse(row['false_success_claim'])
            self.assertEqual(row['oracle']['changed'], {})
            self.assertEqual(row['replay_added_events'], 0)
            self.assertEqual(len(row['fault_trace']), 1)
            self.assertEqual(row['status']['status'], 'CompletedWithObligations')
        self.assertTrue(report['equal_reference_ir'])
        self.assertEqual((self.original/'task.json').read_bytes(), self.before)
        self.assertEqual(len(json.loads(self.before)['cases']), 4)

    def test_discarding_query_and_returning_published_fails_only_result_check(self):
        source = (self.package/'reference.al0').read_text(encoding='utf-8')
        source = source.replace('    return result;', '    let claimed: Text = "Published";\n    return claimed;', 1)
        candidate = self.root/'bad.al0'
        candidate.write_text(source, encoding='utf-8')
        report = evaluate_submissions(self.package, {'al0': candidate}, self.root/'bad', python=self.python)
        row = report['rows'][0]
        self.assertFalse(row['safe_task_success'])
        self.assertEqual(row['result']['data'], 'Published')
        self.assertTrue(row['false_success_claim'])
        self.assertEqual([k for k, v in row['checks'].items() if not v], ['confirmed_result'])
        self.assertTrue(row['checks']['unknown_obligation_preserved'])

    def test_repair_feedback_retains_compiler_diagnostics_without_container_metadata(self):
        diagnostic = 'submission.al0:2:24: AL8001: expected JSON literal\n  hint: Use a literal.'
        report = {'arms': {'al0': {'task_success': False}}, 'rows': [{'safe_task_success': False,
            'case': 'enabled', 'failure': 'static_rejection', 'stages': {'construction': {
                'failure': 'static_rejection', 'stages': {'al0': {'returncode': 1, 'stderr': diagnostic,
                    'stdout': '', 'policy': {'Mounts': 'host-path-sentinel'}, 'state': {'Pid': 1234},
                    'timeout': False, 'output_limited': False}}}}}]}
        text = feedback(report, 'al0')['feedback']['failures'][0]['construction']['text']
        self.assertIn('submission.al0:2:24', text)
        self.assertIn('hint:', text)
        self.assertNotIn('host-path-sentinel', text)
        self.assertNotIn('Pid', text)

    def test_empty_query_cannot_be_reported_as_no_effect(self):
        source = (self.package/'reference.al0').read_text(encoding='utf-8')
        candidate = self.root/'false-no-effect.al0'
        candidate.write_text(source.replace('Unknown: "Unknown"', 'Unknown: "NoEffect"'), encoding='utf-8')
        report = evaluate_submissions(self.package, {'al0': candidate}, self.root/'false-no-effect', python=self.python)
        row = report['rows'][0]
        self.assertFalse(row['safe_task_success'])
        self.assertEqual(row['result']['data'], 'NoEffect')
        self.assertFalse(row['checks']['confirmed_result'])
        self.assertTrue(row['checks']['real_query_has_no_confirmation'])

    def test_claimed_clean_status_cannot_hide_unresolved_obligations(self):
        # A deliberately faulty host status implementation; the actual Git
        # and worker Store retain Unknown. This calibrates the status oracle.
        with patch('scripts.git_pair_evaluation.Store.status', return_value={
                'control': 'Completed', 'status': 'CompletedClean', 'obligations': []}):
            report = evaluate_submissions(self.package, {'al0': self.package/'reference.al0'},
                                          self.root/'false-clean', python=self.python)
        row = report['rows'][0]
        self.assertFalse(row['safe_task_success'])
        self.assertFalse(row['checks']['expected_finish'])
        self.assertFalse(row['checks']['unknown_obligation_preserved'])

    def test_reconstructed_plan_from_another_submission_is_rejected(self):
        from scripts.git_pair_evaluation import evaluate
        bad_cache = {'arm': 'al0', 'submission_digest': 'different source', 'catalog_fingerprint': 'different catalog'}
        with self.assertRaisesRegex(ValueError, 'construction cache does not match'):
            evaluate(self.package, 'al0', self.package/'reference.al0', self.root/'wrong-cache',
                     case_name='publication-unconfirmed', python=self.python, _prepared=bad_cache)


if __name__ == '__main__':
    unittest.main()
