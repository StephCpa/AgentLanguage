import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from unittest.mock import Mock

from scripts import generation_journal as j
from scripts.development_tasks import prepare_suite
from scripts.evaluation_attempts import begin, complete
from scripts.shared_budget import initialize as initialize_budget, inspect
from scripts.source_protocol import VERSION


class ProspectiveJournalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        prepare_suite(cls.root/'tasks')
        cls.package = cls.root/'tasks'/'conditional-release'
        cls.python = os.environ.get('AL0_TYPING_PYTHON', sys.executable)

    def session(self, name, *, limit=2, ledger=None, total='1'):
        folder = self.root/name
        if ledger is None:
            ledger = self.root/(name+'.budget.sqlite')
            initialize_budget(ledger, total)
        j.initialize(folder, self.package, {'provider': 'fixture', 'model': 'fixture-v1', 'parameters': {},
            'evidence_mode': 'fixture', 'max_attempts': 3, 'budget_usd_per_arm': '1',
            'source_protocol': VERSION, 'max_evaluation_attempts': limit, 'shared_ledger': str(ledger)},
            {'shared': 'shared', 'al0': 'al0', 'sdk': 'sdk'})
        return folder, ledger

    def output(self, folder, source, *, arm='al0', upper='0.2'):
        request = j.reserve(folder, arm, upper)
        j.receive(folder, request['id'], {'request_id': request['id'], 'model': 'fixture-v1', 'status': 'output',
            'raw_response': {'text': source}, 'output_pointer': '/text', 'cost_usd': None, 'usage': None})
        return request['id']

    def failed(self, folder, request_id):
        with patch('scripts.git_pair_evaluation.compile_source', side_effect=RuntimeError('synthetic compiler failure')):
            result = j.evaluate_round(folder, request_id, trusted_fixture=True, python=self.python)
        self.assertEqual(result['category'], 'infrastructure_failure')
        return result

    def test_append_only_retry_resolves_gate_without_new_model_call_or_charge(self):
        folder, ledger = self.session('recover')
        source = '```al0\n'+(self.package/'reference.al0').read_text(encoding='utf-8')+'\n```'
        request_id = self.output(folder, source)
        original = self.failed(folder, request_id)
        with self.assertRaisesRegex(ValueError, 'reevaluation'):
            j.reserve(folder, 'al0', '0.1')
        good = j.evaluate_round(folder, request_id, trusted_fixture=True, python=self.python, retry_infrastructure=True)
        self.assertTrue(good['task_success'])
        self.assertNotEqual(original['evaluation_id'], good['evaluation_id'])
        state = j.summary(folder)
        self.assertEqual(len(state['requests']), 1)
        self.assertEqual(len(state['evaluation_attempts']), 2)
        self.assertEqual(json.loads(state['evaluation_attempts'][0]['result']), original)
        self.assertEqual(state['evaluation_attempts'][0]['binding'], state['evaluation_attempts'][1]['binding'])
        self.assertEqual(inspect(ledger)['committed_usd'], '0.2')
        with self.assertRaisesRegex(ValueError, 'already succeeded'):
            j.reserve(folder, 'al0', '0.1')
        with self.assertRaisesRegex(ValueError, 'only infrastructure'):
            j.evaluate_round(folder, request_id, trusted_fixture=True, retry_infrastructure=True)

    def test_recovered_invalid_source_allows_actual_model_repair_not_infrastructure_retry(self):
        folder, _ = self.session('repair')
        request_id = self.output(folder, 'invalid source')
        self.failed(folder, request_id)
        result = j.evaluate_round(folder, request_id, trusted_fixture=True, retry_infrastructure=True)
        self.assertEqual(result['category'], 'submission_failure')
        next_request = j.reserve(folder, 'al0', '0.1')
        self.assertEqual(next_request['ordinal'], 2)
        self.assertIn('static_rejection', next_request['messages'][-1]['content'])

    def test_attempt_limit_pending_attempt_and_format_rejection(self):
        folder, _ = self.session('limit', limit=1)
        request_id = self.output(folder, 'invalid')
        self.failed(folder, request_id)
        with self.assertRaisesRegex(ValueError, 'attempt limit'):
            j.evaluate_round(folder, request_id, trusted_fixture=True, retry_infrastructure=True)
        folder, _ = self.session('pending')
        request_id = self.output(folder, 'invalid')
        first, second = begin(folder, request_id), begin(folder, request_id)
        self.assertEqual(first['id'], second['id'])
        with self.assertRaisesRegex(ValueError, 'already started'):
            j.evaluate_round(folder, request_id, trusted_fixture=True)
        folder, ledger = self.session('format')
        request_id = self.output(folder, 'Explanation\n```al0\nsource\n```')
        with patch('scripts.generation_journal.evaluate_submissions', side_effect=AssertionError('must not execute')):
            result = j.evaluate_round(folder, request_id, trusted_fixture=True)
        self.assertFalse(result['task_success'])
        self.assertEqual(result['category'], 'submission_failure')
        self.assertTrue(all(r['failure']=='format_rejection' for r in result['report']['rows']))
        self.assertEqual(inspect(ledger)['requests'], 1)

    def test_binding_validation_and_completed_attempt_immutability(self):
        folder, _ = self.session('binding')
        request_id = self.output(folder, 'invalid')
        result = self.failed(folder, request_id)
        bad = copy.deepcopy(result['report'])
        bad['rows'][0]['submission_digest'] = 'altered'
        with self.assertRaisesRegex(ValueError, 'differs'):
            complete(folder, request_id, result['evaluation_id'], bad, result['provenance'])
        with self.assertRaisesRegex(ValueError, 'immutable'):
            complete(folder, request_id, result['evaluation_id'], result['report'], {'changed': 'provenance'})

    def test_shared_budget_import_and_concurrent_journals_cannot_reset_cap(self):
        legacy, _ = self.session('historical')
        old = self.output(legacy, 'invalid', upper='0.2')
        ledger = self.root/'shared.sqlite'
        self.assertEqual(initialize_budget(ledger, '1', legacy_journals=[legacy])['committed_usd'], '0.2')
        a, _ = self.session('shared-a', ledger=ledger)
        b, _ = self.session('shared-b', ledger=ledger)
        def reserve(folder):
            try:
                return j.reserve(folder, 'al0', '0.5')['id']
            except ValueError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, [a, b]))
        self.assertEqual(results.count('shared budget unavailable'), 1)
        self.assertEqual(inspect(ledger)['requests'], 2)
        self.assertEqual(inspect(ledger)['committed_usd'], '0.7')
        # Historical settlement must be explicitly reconciled; a frozen import
        # cannot silently forget a changed liability.
        j.settle(legacy, old, '0.3', 'late bill fixture')
        with self.assertRaisesRegex(ValueError, 'historical liability changed'):
            j.reserve(b, 'sdk', '0.1')
        from scripts.shared_budget import reconcile_history
        self.assertEqual(reconcile_history(ledger, legacy)['committed_usd'], '0.8')
        # The source exceeded its own original 0.2 bound; liability is recorded
        # truthfully and all future journals stay stopped, even below USD 1.
        with self.assertRaisesRegex(ValueError, 'cost bound exceeded'):
            j.reserve(b, 'sdk', '0.1')

    def test_shared_reservation_tracks_usage_bounds_and_actual_settlement(self):
        folder, ledger = self.session('settlement')
        request_id = self.output(folder, 'invalid', upper='0.5')
        j.refine_cost_bound(folder, request_id, '0.1', {'fixture': 'usage'})
        self.assertEqual(inspect(ledger)['committed_usd'], '0.1')
        self.assertIsNone(inspect(ledger)['actual_total_usd'])
        j.settle(folder, request_id, '0.05', 'fixture invoice')
        self.assertEqual(inspect(ledger)['actual_total_usd'], '0.05')

    def test_new_entry_uses_same_parser_materials_order_and_budget_on_transport_path(self):
        from scripts.next_pilot import initialize_task
        from scripts.deepseek_pilot import send
        ledger = self.root/'entry-budget.sqlite'
        initialize_budget(ledger, '1')
        task = json.loads((self.package/'task.json').read_text(encoding='utf-8'))
        plan = {'status': 'frozen', 'contract_digest': task['contract_digest'], 'source_protocol': VERSION,
                'model_attempts_per_arm': 1, 'max_evaluation_attempts': 2, 'call_order': ['sdk', 'al0'],
                'task_set_id': 'synthetic-fixture', 'independence_basis': 'not an empirical study',
                'order_basis': 'fixture validation only'}
        pilot = self.root/'entry'
        initialize_task(pilot, self.package, ledger, plan, python=self.python, evidence_mode='fixture')
        source = (self.package/'reference.py').read_text(encoding='utf-8')
        raw = {'id': 'fixture-response', 'model': 'deepseek-flash',
               'choices': [{'message': {'content': '```python\n'+source+'\n```'}}],
               'usage': {'prompt_tokens': 100, 'completion_tokens': 100}}
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(raw).encode())
        with patch('scripts.deepseek_pilot.credential', return_value='fixture-secret'), \
             patch('scripts.deepseek_pilot.urllib.request.build_opener', return_value=opener):
            with self.assertRaisesRegex(ValueError, 'call order'):
                send(pilot, 'al0')
            result = send(pilot, 'sdk')
            with self.assertRaisesRegex(ValueError, 'resolve the current'):
                send(pilot, 'al0')
        self.assertEqual(opener.open.call_count, 1)
        request = j.show_request(pilot/'journal', result['request_id'])
        self.assertIn('ExampleRecord', request['messages'][1]['content'])
        self.assertIn('complete code fence', request['messages'][0]['content'])
        outcome = j.evaluate_round(pilot/'journal', result['request_id'], trusted_fixture=True, python=self.python)
        self.assertTrue(outcome['task_success'])
        self.assertEqual(outcome['source_protocol']['format'], 'single_fence')
        self.assertEqual(inspect(ledger)['requests'], 1)
        self.assertEqual(inspect(ledger)['committed_usd'], '0.000150000')
        self.assertIsNone(inspect(ledger)['actual_total_usd'])
        # A new real-provider task cannot accidentally start from a blank book.
        with self.assertRaisesRegex(ValueError, 'historical provider requests'):
            initialize_task(self.root/'unfunded-entry', self.package, ledger, plan, python=self.python)


if __name__ == '__main__':
    unittest.main()
