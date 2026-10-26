import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from scripts.development_tasks import prepare_suite
from scripts.generation_journal import (initialize, reserve, receive, settle, summary,
                                        evaluate_round, show_request, pointer, feedback)
from scripts.isolated_plan_build import mypy_failure


class GenerationJournalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        prepare_suite(cls.root/'tasks')
        cls.package = cls.root/'tasks'/'conditional-release'
        cls.python = os.environ.get('AL0_TYPING_PYTHON', sys.executable)

    def session(self, *, mode='fixture', budget='1.00', attempts=3):
        folder = self.root/self.id().split('.')[-1]
        initialize(folder, self.package, {'provider': 'fixture', 'model': 'fixture-v1', 'parameters': {},
            'evidence_mode': mode, 'max_attempts': attempts, 'budget_usd_per_arm': budget},
            {'shared': 'shared guide', 'al0': 'AL0 guide', 'sdk': 'SDK guide'})
        return folder

    def receipt(self, request, source='invalid source', *, cost=None, status='output', usage=None):
        return {'request_id': request['id'], 'model': request['model'], 'status': status,
                'raw_response': {'items': [{'text': source}]}, 'output_pointer': '/items/0/text',
                'cost_usd': cost, 'usage': usage}

    def test_real_repair_preserves_first_failure_and_uses_only_assigned_arm(self):
        folder = self.session()
        first = reserve(folder, 'al0', '0.20')
        receive(folder, first['id'], self.receipt(first))
        bad = evaluate_round(folder, first['id'], trusted_fixture=True, python=self.python)
        self.assertEqual(bad['category'], 'submission_failure')
        second = reserve(folder, 'al0', '0.20')
        self.assertEqual(second['messages'][2]['content'], 'invalid source')
        self.assertIn('static_rejection', second['messages'][3]['content'])
        text = (self.package/'reference.al0').read_text(encoding='utf-8')
        receive(folder, second['id'], self.receipt(second, text.replace('\n', '\r\n'), cost='0.03',
                usage={'input_tokens': 100, 'output_tokens': 50}))
        good = evaluate_round(folder, second['id'], trusted_fixture=True, python=self.python)
        self.assertTrue(good['task_success'])
        with patch('scripts.generation_journal.evaluate_submissions', side_effect=AssertionError('rerun')):
            self.assertEqual(evaluate_round(folder, second['id']), good)
        # Process exit after the evaluator saved its complete report but before
        # the journal committed that outcome must import the report, not rerun.
        with sqlite3.connect(folder/'journal.sqlite') as db:
            db.execute('UPDATE rounds SET phase=?,evaluation=NULL WHERE id=?', ('evaluating', second['id']))
        with patch('scripts.generation_journal.evaluate_submissions', side_effect=AssertionError('rerun')):
            self.assertEqual(evaluate_round(folder, second['id'], trusted_fixture=True), good)
        report = json.loads(Path(good['report']).read_text())
        self.assertEqual(set(report['arms']), {'al0'})
        self.assertIsNone(report['equal_reference_ir'])
        self.assertEqual(len(report['rows']), 4)
        result = summary(folder)
        self.assertFalse(result['arms']['al0']['first_attempt_success'])
        self.assertTrue(result['arms']['al0']['task_success'])
        self.assertEqual(result['arms']['sdk']['assigned_tasks'], 1)
        self.assertEqual(result['arms']['sdk']['attempts'], 0)
        self.assertIsNone(result['account']['total_cost_usd'])
        self.assertIsNone(result['usage_totals']['input_tokens'])
        self.assertEqual(result['fixture_receipts'], 2)
        self.assertEqual(result['imported_provider_receipts'], 0)
        with self.assertRaisesRegex(ValueError, 'already succeeded'):
            reserve(folder, 'al0', '0.01')

    def test_unknown_cost_reserves_budget_until_late_settlement_and_arms_are_independent(self):
        folder = self.session(budget='0.50')
        first = reserve(folder, 'al0', '0.50')
        receive(folder, first['id'], self.receipt(first, status='timeout'))
        with self.assertRaisesRegex(ValueError, 'budget unavailable'):
            reserve(folder, 'al0', '0.01')
        # A failure in one arm cannot consume the other arm's assigned budget.
        reserve(folder, 'sdk', '0.50')
        settle(folder, first['id'], '0.10', 'fixture late bill')
        reserve(folder, 'al0', '0.40')
        self.assertEqual(summary(folder)['account']['committed_usd'], '1.00')
        with self.assertRaisesRegex(ValueError, 'already settled'):
            settle(folder, first['id'], '0.00', 'overwrite')

    def test_excess_charge_is_recorded_and_blocks_further_requests(self):
        folder = self.session()
        request = reserve(folder, 'sdk', '0.10')
        receive(folder, request['id'], self.receipt(request, cost='0.20', status='refused'))
        self.assertTrue(summary(folder)['account']['request_bound_exceeded'])
        self.assertEqual(summary(folder)['account']['known_cost_usd'], '0.20')
        with self.assertRaisesRegex(ValueError, 'bound was exceeded'):
            reserve(folder, 'sdk', '0.10')

    def test_receipt_identity_idempotency_and_exact_source(self):
        folder = self.session()
        request = reserve(folder, 'al0', '0.10')
        self.assertEqual(show_request(folder, request['id']), request)
        receipt = self.receipt(request, '```al0\nnot silently repaired\n```', cost='0')
        wrong = dict(receipt, model='different-model')
        with self.assertRaisesRegex(ValueError, 'differs'):
            receive(folder, request['id'], wrong)
        receive(folder, request['id'], receipt)
        self.assertTrue(receive(folder, request['id'], receipt)['already_recorded'])
        with self.assertRaisesRegex(ValueError, 'different receipt'):
            receive(folder, request['id'], dict(receipt, cost_usd='0.01'))
        self.assertEqual(pointer(receipt['raw_response'], receipt['output_pointer']), '```al0\nnot silently repaired\n```')
        for path in ('/items/-1/text', '/items/00/text', '/items/0/te~2xt'):
            with self.assertRaises(ValueError):
                pointer(receipt['raw_response'], path)
        for usage in ({}, {'input_tokens': True, 'output_tokens': 1}, {'input_tokens': -1, 'output_tokens': 1}):
            with self.assertRaises(ValueError):
                receive(folder, request['id'], dict(receipt, usage=usage))

    def test_pending_request_and_concurrent_reservations_do_not_duplicate_calls(self):
        folder = self.session()
        def attempt():
            try:
                return reserve(folder, 'sdk', '0.10')
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(summary(folder)['arms']['sdk']['attempts'], 1)

    def test_provider_output_cannot_run_in_trusted_fixture_mode(self):
        folder = self.session(mode='provider_import')
        request = reserve(folder, 'sdk', '0.10')
        receive(folder, request['id'], self.receipt(request))
        with patch('scripts.generation_journal.evaluate_submissions', side_effect=AssertionError('executed')):
            with self.assertRaisesRegex(ValueError, 'require Docker'):
                evaluate_round(folder, request['id'], trusted_fixture=True)
        self.assertEqual(summary(folder)['arms']['sdk']['phases'], ['received'])

    def test_interrupted_evaluation_never_automatically_replays_effects(self):
        folder = self.session()
        request = reserve(folder, 'al0', '0.10')
        receive(folder, request['id'], self.receipt(request))
        with patch('scripts.generation_journal.evaluate_submissions', side_effect=RuntimeError('worker disconnected')):
            with self.assertRaises(RuntimeError):
                evaluate_round(folder, request['id'], trusted_fixture=True)
        with patch('scripts.generation_journal.evaluate_submissions', side_effect=AssertionError('rerun')):
            with self.assertRaisesRegex(ValueError, 'already started'):
                evaluate_round(folder, request['id'], trusted_fixture=True)
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            reserve(folder, 'al0', '0.10')

    def test_frozen_prompt_excludes_host_state_and_reference_answers(self):
        folder = self.session()
        request = reserve(folder, 'sdk', '0.10')
        prompt = json.dumps(request['messages'])
        task = json.loads((self.package/'task.json').read_text())
        self.assertNotIn(task['source'], prompt)
        self.assertNotIn(task['remote'], prompt)
        self.assertNotIn('reference.py', prompt)
        self.assertNotIn('expected', json.loads(request['messages'][1]['content'])['participant']['task.json'])
        with sqlite3.connect(folder/'journal.sqlite') as db:
            config = json.loads(db.execute('SELECT value FROM configuration').fetchone()[0])
        self.assertEqual(config['materials']['sdk'], 'SDK guide')
        self.assertIn('cases', config['task'])  # Host retains the full scoring contract.

    def test_attempt_limit_includes_refusals_and_transport_failures(self):
        folder = self.session(attempts=2)
        for status in ('refused', 'error'):
            request = reserve(folder, 'al0', '0.10')
            receive(folder, request['id'], self.receipt(request, status=status, cost='0.00'))
        with self.assertRaisesRegex(ValueError, 'attempt limit'):
            reserve(folder, 'al0', '0.10')
        arm = summary(folder)['arms']['al0']
        self.assertEqual(arm['categories'], ['generation_refused', 'generation_error'])
        self.assertFalse(arm['task_success'])

    def test_feedback_separates_infrastructure_and_excludes_oracle_and_grants(self):
        report = {'arms': {'sdk': {'task_success': False}}, 'rows': [{'case': 'normal',
            'safe_task_success': False, 'failure': 'infrastructure:isolated_builder_unavailable',
            'stages': {'construction': {'error': 'missing image'}},
            'oracle': {'private': 'do-not-export'}, 'grants': {'secret': 'do-not-export'}}]}
        result = feedback(report, 'sdk')
        self.assertEqual(result['category'], 'infrastructure_failure')
        self.assertNotIn('do-not-export', json.dumps(result))

    def test_mypy_syntax_error_is_submission_failure_but_tool_crashes_are_not(self):
        self.assertEqual(mypy_failure({'returncode': 2, 'stdout': 'submission.py:1: error: Invalid syntax  [syntax]'}),
                         'static_rejection')
        for code in (1, 2, 137, None):
            self.assertEqual(mypy_failure({'returncode': code, 'stderr': 'Traceback: tool startup failed'}),
                             'static_tool_failure')

    def test_infrastructure_failure_cannot_enable_an_extra_model_attempt(self):
        folder = self.session()
        request = reserve(folder, 'al0', '0.10')
        receive(folder, request['id'], self.receipt(request))
        with patch('scripts.git_pair_evaluation.compile_source', side_effect=RuntimeError('synthetic compiler crash')):
            result = evaluate_round(folder, request['id'], trusted_fixture=True, python=self.python)
        self.assertEqual(result['category'], 'infrastructure_failure')
        with self.assertRaisesRegex(ValueError, 'reevaluation of saved source'):
            reserve(folder, 'al0', '0.10')
        self.assertEqual(len(summary(folder)['requests']), 1)


if __name__ == '__main__':
    unittest.main()
