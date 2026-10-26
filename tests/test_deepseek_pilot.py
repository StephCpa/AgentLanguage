import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from al0.data import digest
from scripts.development_tasks import prepare_suite
from scripts.deepseek_pilot import initialize_pilot, send, ingest, token_cost_upper, export, PROFILE
from scripts.generation_journal import (summary, database, configuration, rounds, import_evaluation,
                                        refine_cost_bound, reserve)
from scripts.score_deepseek_pilot import score


class DeepSeekPilotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        prepare_suite(cls.root/'tasks')
        cls.package = cls.root/'tasks'/'conditional-release'

    def pilot(self):
        folder = self.root/self.id().split('.')[-1]
        initialize_pilot(folder, self.package)
        return folder

    def response(self, *, prompt=100, completion=50):
        return {'id': 'test-provider-response', 'model': 'deepseek-flash',
                'choices': [{'message': {'content': 'invalid source'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': prompt, 'completion_tokens': completion}}

    def call(self, folder, response):
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(response).encode())
        with patch('scripts.deepseek_pilot.credential', return_value='test-secret-do-not-export'), \
             patch('scripts.deepseek_pilot.urllib.request.build_opener', return_value=opener):
            result = send(folder, 'al0')
        self.assertEqual(opener.open.call_count, 1)
        return result

    def test_real_usage_narrows_reservation_without_inventing_a_bill(self):
        folder = self.pilot()
        result = self.call(folder, self.response())
        self.assertEqual(result['cost_upper_usd'], '0.000090000')
        report = summary(folder/'journal')
        self.assertIsNone(report['account']['total_cost_usd'])
        self.assertEqual(report['account']['reserved_usd'], '0.000090000')
        self.assertEqual(report['account']['known_cost_usd'], '0')
        with patch('scripts.deepseek_pilot.urllib.request.build_opener', side_effect=AssertionError('network')):
            self.assertTrue(ingest(folder, result['request_id'])['already_recorded'])
        destination = folder/'export.json'
        export(folder, destination)
        self.assertNotIn('test-secret-do-not-export', destination.read_text())
        self.assertEqual(json.loads(destination.read_text())['trials'][0]['receipt']['raw_response'], self.response())

    def test_timeout_keeps_full_bound_and_does_not_retry(self):
        folder = self.pilot()
        opener = Mock()
        opener.open.side_effect = TimeoutError('connection lost')
        with patch('scripts.deepseek_pilot.credential', return_value='secret'), \
             patch('scripts.deepseek_pilot.urllib.request.build_opener', return_value=opener):
            result = send(folder, 'al0')
            with self.assertRaisesRegex(ValueError, 'budget unavailable'):
                send(folder, 'al0')
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(summary(folder/'journal')['account']['reserved_usd'], '0.319488000')
        self.assertEqual(result['phase'], 'evaluated')

    def test_invalid_usage_cannot_release_money_or_allow_more_calls(self):
        folder = self.pilot()
        self.call(folder, self.response(completion=4097))
        self.assertEqual(summary(folder/'journal')['account']['reserved_usd'], '0.319488000')
        with patch('scripts.deepseek_pilot.credential', return_value='secret'):
            with self.assertRaisesRegex(ValueError, 'unverified prior usage'):
                send(folder, 'sdk')

    def test_price_or_model_profile_drift_is_rejected_before_reservation(self):
        folder = self.pilot()
        profile = json.loads((folder/'profile.json').read_text())
        profile['peak_usd_per_million']['input_cache_miss'] = '0'
        (folder/'profile.json').write_text(json.dumps(profile))
        with patch('scripts.deepseek_pilot.credential', return_value='secret'):
            with self.assertRaisesRegex(ValueError, 'profile changed'):
                send(folder, 'al0')
        self.assertEqual(summary(folder/'journal')['requests'], [])

    def test_cost_bounds_use_full_uncached_input_and_round_up(self):
        profile = json.loads(PROFILE.read_text())
        self.assertEqual(token_cost_upper(1, 1, profile), '0.000001500')
        for args in ((True, 1), (-1, 1), (1048577, 1), (1, 4097)):
            with self.assertRaises(ValueError):
                token_cost_upper(*args, profile)

    def test_remote_report_handles_fractional_experiment_timestamps(self):
        folder = self.pilot()
        self.call(folder, self.response())
        bundle = folder/'bundle.json'
        export(folder, bundle)
        with patch('scripts.score_deepseek_pilot.prepare_suite'), \
             patch('scripts.score_deepseek_pilot.evaluate_submissions', return_value={'test_fixture': True}):
            result = score(bundle, folder/'scored', self.root, 'fixture-image')
        self.assertEqual(len(result['trials']), 1)
        self.assertTrue((folder/'scored'/'summary.json').is_file())
        self.assertEqual(result['model_calls_from_ci'], 0)

    def test_remote_import_requires_exact_arm_source_catalog_and_cases(self):
        folder = self.pilot()
        result = self.call(folder, self.response())
        with database(folder/'journal') as db:
            task = configuration(db)['task']
        report = {'arms': {'al0': {'task_success': False}}, 'contract_digest': task['contract_digest'], 'rows': [
            {'arm': 'al0', 'case': c['id'], 'submission_digest': digest('invalid source'),
             'catalog_fingerprint': task['catalog_fingerprint'], 'construction_mode': 'docker',
             'safe_task_success': False, 'failure': 'static_rejection', 'stages': {}} for c in task['cases']]}
        provenance = {'run': 1, 'commit': 'test-commit', 'artifact': 1}
        for field, value in (('submission_digest', 'different'), ('catalog_fingerprint', 'different'),
                             ('construction_mode', 'trusted-local'), ('arm', 'sdk')):
            changed = copy.deepcopy(report)
            changed['rows'][0][field] = value
            with self.assertRaises(ValueError):
                import_evaluation(folder/'journal', result['request_id'], changed, provenance)
        imported = import_evaluation(folder/'journal', result['request_id'], report, provenance)
        self.assertEqual(imported['category'], 'submission_failure')
        self.assertEqual(import_evaluation(folder/'journal', result['request_id'], report, provenance), imported)
        next_request = reserve(folder/'journal', 'al0', '0.319488000')
        self.assertIn('static_rejection', next_request['messages'][-1]['content'])


if __name__ == '__main__':
    unittest.main()
