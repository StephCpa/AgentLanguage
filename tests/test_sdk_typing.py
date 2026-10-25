import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest

from scripts.check_sdk_types import audit, surface
from tests.type_cases.positive import build_all
from tests.type_cases.runtime_only import CASES, verify


class SDKTypingTests(unittest.TestCase):
    def test_public_builder_surface_has_annotations_and_positive_calls(self):
        self.assertGreater(len(surface()), 40)

    def test_positive_typed_programs_are_accepted_by_shared_ir(self):
        plans = build_all()
        self.assertEqual(len(plans), 26)
        self.assertTrue(all(p.result_type is not None and p.fingerprint for p in plans))

    def test_shared_checker_obligations_are_not_mislabeled_python_guarantees(self):
        self.assertEqual(verify(), [build.__name__ for build, _ in CASES])

    def test_optional_pinned_mypy_acceptance_gate(self):
        python = os.environ.get('AL0_TYPING_PYTHON')
        if python is None:
            if importlib.util.find_spec('mypy') is None:
                self.skipTest('install .[typing] or set AL0_TYPING_PYTHON to run the required standalone type gate')
            python = sys.executable
        report = audit(python)
        self.assertTrue(report['passed'])
        self.assertEqual(len(report['negative_cases']), 40)
        self.assertEqual(len(report['generated_binding_negative_cases']), 14)
        self.assertEqual(report['generated_binding_plans_accepted_by_ir'], 9)
        output = os.environ.get('AL0_TYPING_REPORT')
        if output:
            Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


if __name__ == '__main__':
    unittest.main()
