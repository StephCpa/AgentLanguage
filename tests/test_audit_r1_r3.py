"""Audit regressions: synthetic process faults are labelled, Git tests are real."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.development_tasks import prepare_suite
from scripts.evaluation_contract import require_valid_evaluations
from scripts.git_pair_evaluation import evaluate
from scripts.isolated_plan_build import DockerBuilder, BuilderUnavailable, construction_failure
from scripts.score_deepseek_pilot import score


class AuditR1R3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        prepare_suite(cls.root/'tasks')
        cls.package = cls.root/'tasks'/'conditional-release'

    def bundle(self, folder, source):
        folder.mkdir()
        bundle = {'participant': {p.name: p.read_text(encoding='utf-8') for p in (self.package/'participant').iterdir()},
                  'trials': [{'request': {'id': 'saved-output', 'arm': 'al0', 'ordinal': 1},
                              'receipt': {'request_id': 'saved-output', 'status': 'output',
                                          'raw_response': {'text': source}, 'output_pointer': '/text'}}]}
        path = folder/'bundle.json'
        path.write_text(json.dumps(bundle), encoding='utf-8')
        return path

    def test_both_primary_and_sensitivity_fail_closed_after_saving_reports(self):
        normal = {'rows': [{'failure': 'static_rejection'}]}
        bad = {'rows': [{'failure': 'infrastructure:isolated_builder_unavailable'}]}
        for phase, results in (('primary', [bad, normal]), ('sensitivity', [normal, bad])):
            folder = self.root/phase
            bundle = self.bundle(folder, '```al0\ninvalid source\n```')
            with self.subTest(phase=phase), patch('scripts.score_deepseek_pilot.prepare_suite'), \
                 patch('scripts.score_deepseek_pilot.evaluate_submissions', side_effect=results):
                with self.assertRaisesRegex(RuntimeError, 'reports preserved'):
                    score(bundle, folder/'scored', self.root, 'fake-image')
                saved = json.loads((folder/'scored'/'summary.json').read_text())
                self.assertEqual(saved['trials'][0]['evaluation'], results[0])
                self.assertEqual(saved['sensitivity']['trials'][0]['evaluation'], results[1])
        require_valid_evaluations([normal, {'rows': [{'failure': 'task_or_recovery_failure'},
                                                   {'failure': 'construction_limit'}]}])

    def test_missing_docker_cli_exits_nonzero_and_keeps_full_evaluation(self):
        folder = self.root/'cli'
        bundle = self.bundle(folder, (self.package/'reference.al0').read_text(encoding='utf-8'))
        # Exercise the real CLI/evaluator in a subprocess. Only Docker startup
        # is replaced with a deterministic unavailable-daemon fault.
        script = '''from unittest.mock import patch
from scripts.isolated_plan_build import BuilderUnavailable
from scripts.score_deepseek_pilot import main
with patch('scripts.score_deepseek_pilot.prepare_suite'):
    with patch('scripts.isolated_plan_build.DockerBuilder', side_effect=BuilderUnavailable('synthetic daemon unavailable')):
        main()
'''
        process = subprocess.run([sys.executable, '-c', script, '--bundle', str(bundle),
            '--output', str(folder/'scored'), '--fixture-root', str(self.root), '--builder-image', 'absent'],
            capture_output=True, text=True)
        self.assertNotEqual(process.returncode, 0, process.stdout)
        saved = json.loads((folder/'scored'/'summary.json').read_text())
        self.assertEqual(len(saved['trials'][0]['evaluation']['rows']), 4)
        self.assertTrue(all(r['failure'] == 'infrastructure:isolated_builder_unavailable'
                            for r in saved['trials'][0]['evaluation']['rows']))

    def test_synthetic_resource_terminations_have_one_cross_stage_policy(self):
        for mode in ('al0', 'typecheck', 'sdk'):
            for extra in ({'timeout': True}, {'output_limited': True}, {'container_state': {'OOMKilled': True}}):
                with self.subTest(mode=mode, fault=extra):
                    self.assertEqual(construction_failure(mode, {'returncode': 137, **extra}), 'construction_limit')
        for mode in ('al0', 'typecheck'):
            self.assertEqual(construction_failure(mode, {'returncode': 2, 'stderr': 'compiler crashed'}), 'static_tool_failure')
        self.assertEqual(construction_failure('al0', {'returncode': 65, 'stderr': 'not a source diagnostic'}), 'static_tool_failure')

    def test_known_source_error_and_compiler_exception_differ(self):
        catalog_text = (self.package/'catalog.json').read_text(encoding='utf-8')
        from scripts import isolated_plan_worker as worker
        for failure, expected in ((None, 65), (RuntimeError('compiler bug'), None)):
            err = io.StringIO()
            with patch.object(sys, 'argv', ['worker', 'al0']), patch('pathlib.Path.read_text',
                    side_effect=[catalog_text, 'not valid source']), patch('sys.stderr', err):
                if failure:
                    with patch.object(worker, 'compile_source', side_effect=failure), self.assertRaises(RuntimeError):
                        worker.main()
                    self.assertEqual(construction_failure('al0', {'returncode': 1, 'stderr': err.getvalue()}), 'static_tool_failure')
                else:
                    with self.assertRaises(SystemExit) as stopped:
                        worker.main()
                    self.assertEqual(stopped.exception.code, expected)
                    self.assertEqual(construction_failure('al0', {'returncode': expected, 'stderr': err.getvalue()}), 'static_rejection')

    def test_malformed_inspect_is_reported_and_cleanup_requested(self):
        for index, body in enumerate(('not JSON', '{}', '{"State":{}}')):
            builder = object.__new__(DockerBuilder)
            builder.docker, builder.image, builder.timeout, builder.output_limit = 'docker', 'sha256:fixture', 10, 4096
            process = Mock(stdout=io.BytesIO(), stderr=io.BytesIO(), returncode=1)
            process.poll.return_value = 1
            def control(args, **kwargs):
                return subprocess.CompletedProcess(args, 0, body if 'inspect' in args else '', '')
            with patch('scripts.isolated_plan_build.control', side_effect=control) as calls, \
                 patch('scripts.isolated_plan_build.subprocess.Popen', return_value=process), \
                 patch('scripts.isolated_plan_build.DockerBuilder', return_value=builder):
                row = evaluate(self.package, 'al0', self.package/'reference.al0', self.root/('inspect-'+str(index)),
                               case_name='enabled', builder_image='fixture')
            self.assertEqual(row['failure'], 'infrastructure:isolated_builder_unavailable')
            self.assertTrue((self.root/('inspect-'+str(index))/'report.json').is_file())
            self.assertFalse((self.root/('inspect-'+str(index))/'runtime.sqlite').exists())
            self.assertTrue(any('rm' in call.args[0] and '--force' in call.args[0] for call in calls.call_args_list))


if __name__ == '__main__':
    unittest.main()
