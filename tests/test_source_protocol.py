import os
from pathlib import Path
import sys
import tempfile
import unittest

from al0.data import digest
from scripts.source_protocol import parse_source, VERSION
from scripts.check_teaching_materials import check


class SourceProtocolTests(unittest.TestCase):
    def test_common_protocol_retains_raw_and_exact_internal_source(self):
        source = '  significant_indent()\r\n'
        for label in ('', 'al0', 'python', 'py'):
            raw = '\n```'+label+'\r\n'+source+'```\n\n'
            result = parse_source(raw)
            self.assertTrue(result['accepted'])
            self.assertEqual(result['source'], '  significant_indent()\n')
            self.assertEqual(result['raw_text'], raw)
            self.assertEqual(result['raw_digest'], digest(raw))
            self.assertEqual(result['source_digest'], digest(result['source']))
            self.assertEqual(result['parser_version'], VERSION)
        self.assertEqual(parse_source(source)['source'], '  significant_indent()\n')

    def test_mixed_multiple_empty_and_incomplete_outputs_are_rejected(self):
        for source in ('', '  \n', '```\n\n```', 'explanation\n```al0\ncode\n```',
                       '```al0\ncode\n```\nexplanation', '```py\na\n```\n```py\nb\n```',
                       '```al0\nunfinished', '```json\n{}\n```'):
            with self.subTest(source=source):
                result = parse_source(source)
                self.assertFalse(result['accepted'])
                self.assertIsNone(result['source'])
                self.assertIsNone(result['source_digest'])

    def test_all_prospective_examples_compile_and_sdk_passes_pinned_typing(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = check(Path(temporary)/'teaching', python=os.environ.get('AL0_TYPING_PYTHON', sys.executable))
        self.assertTrue(report['equal_ir'])
        self.assertEqual(len(report['examples']), 2)


if __name__ == '__main__':
    unittest.main()
