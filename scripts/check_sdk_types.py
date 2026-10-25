"""Run the pinned static SDK acceptance gate, with exact negative-case matching.

Run from the project root: python -m scripts.check_sdk_types --python <python>
The selected interpreter must have the optional typing dependency installed.
"""
import argparse
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
VERSION = '1.18.2'
POSITIVE = ('al0/sdk.py', 'examples/repair_sdk.py', 'tests/type_cases/positive.py', 'tests/type_cases/runtime_only.py',
            'al0/codegen.py', 'examples/generated_repair_bindings.py', 'examples/generated_repair.py',
            'tests/type_cases/codegen_catalog.py', 'tests/type_cases/generated_catalog.py',
            'tests/type_cases/codegen_positive.py', 'tests/type_cases/inference_responses.py')
NEGATIVE = 'tests/type_cases/negative.py'
GENERATED_NEGATIVE = 'tests/type_cases/codegen_negative.py'


def surface():
    tree = ast.parse((ROOT/'al0/sdk.py').read_text(encoding='utf-8'))
    builder = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'PlanBuilder')
    public = {n.name for n in builder.body if isinstance(n, ast.FunctionDef) and not n.name.startswith('_')}
    missing = []
    for n in builder.body:
        if isinstance(n, ast.FunctionDef) and n.name in public:
            arguments = n.args.posonlyargs+n.args.args+n.args.kwonlyargs
            arguments += [a for a in (n.args.vararg, n.args.kwarg) if a is not None]
            if n.returns is None or any(a.annotation is None for a in arguments if a.arg != 'self'):
                missing.append(n.name)
    if missing:
        raise AssertionError('public SDK methods missing annotations: '+', '.join(missing))
    fixtures = ast.parse((ROOT/'tests/type_cases/positive.py').read_text(encoding='utf-8'))
    called = {n.func.attr for n in ast.walk(fixtures) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    if public-called:
        raise AssertionError('public SDK methods without positive fixtures: '+', '.join(sorted(public-called)))
    return sorted(public)


def expected_errors(path=NEGATIVE):
    result = {}
    for line, value in enumerate((ROOT/path).read_text(encoding='utf-8').splitlines(), 1):
        if '# E:' in value:
            result[line] = value.split('# E:', 1)[1].strip()
    if not result or len(set(result.values())) != len(result):
        raise AssertionError('negative fixture needs unique named error cases')
    return result


def command(python, arguments):
    env = dict(os.environ)
    # Keep the audit tied to this checkout and the selected environment rather
    # than an unrelated replacement stub path in the caller's shell.
    env.pop('MYPYPATH', None)
    env.pop('PYTHONPATH', None)
    env['PYTHONIOENCODING'] = 'utf-8'
    return subprocess.run([str(python), *arguments], cwd=ROOT, env=env, capture_output=True,
                          text=True, encoding='utf-8', timeout=180)


def negative_cases(python, common, path):
    rejected = command(python, [*common, path])
    expected = expected_errors(path)
    observed = {}
    for line in rejected.stdout.splitlines():
        match = re.match(r'^(.*?):(\d+)(?::\d+)?: error: (.*?)\s+\[([^\]]+)\]$', line)
        if match:
            source, number, message, code = match.groups()
            if Path(source.replace('\\', '/')).name != Path(path).name:
                raise AssertionError('unexpected diagnostic source: '+line)
            observed.setdefault(int(number), []).append({'code': code, 'message': message})
        elif ': error:' in line:
            raise AssertionError('unparsed diagnostic: '+line)
    if rejected.returncode != 1 or set(observed) != set(expected) or rejected.stderr:
        raise AssertionError('negative fixture mismatch:\n'+rejected.stdout+rejected.stderr)
    return [{'case': expected[line], 'line': line, 'diagnostics': observed[line]} for line in sorted(expected)]


def audit(python):
    started = time.monotonic()
    methods = surface()
    from al0.codegen import generate_bindings
    from al0.demo import catalog as demo_catalog
    from tests.type_cases.codegen_catalog import catalog as fixture_catalog
    for path, catalog in [('examples/generated_repair_bindings.py', demo_catalog()),
                          ('tests/type_cases/generated_catalog.py', fixture_catalog())]:
        if (ROOT/path).read_text(encoding='utf-8') != generate_bindings(catalog):
            raise AssertionError('stale generated bindings: '+path)
    version = command(python, ['-m', 'mypy', '--version'])
    if version.returncode or not version.stdout.startswith('mypy '+VERSION+' '):
        raise AssertionError('typing gate requires mypy '+VERSION+'; '+version.stdout+version.stderr)
    common = ['-m', 'mypy', '--config-file', str(ROOT/'pyproject.toml'), '--strict',
              '--follow-imports=silent', '--show-error-codes', '--no-pretty', '--no-color-output']
    accepted = command(python, [*common, *POSITIVE])
    if accepted.returncode:
        raise AssertionError('positive type gate failed:\n'+accepted.stdout+accepted.stderr)
    rejected = negative_cases(python, common, NEGATIVE)
    generated_rejected = negative_cases(python, common, GENERATED_NEGATIVE)
    # Positive source acceptance is paired with actual construction and shared
    # IR validation; static acceptance alone cannot validate strings or effects.
    runtime = command(python, ['-c', 'import json; from tests.type_cases.positive import build_all; '
        'from tests.type_cases.runtime_only import verify; '
        'from tests.type_cases.codegen_positive import build_all as generated; '
        'print(json.dumps({"plans":len(build_all()),"generated_plans":len(generated()),"shared_checker_cases":verify()}))'])
    if runtime.returncode:
        raise AssertionError('shared checker fixtures failed:\n'+runtime.stdout+runtime.stderr)
    runtime_result = json.loads(runtime.stdout)
    return {'passed': True, 'mypy': version.stdout.strip(), 'python': str(python),
            'scope': 'public SDK authoring API, Catalog generator, generated bindings and fixtures; imported kernel bodies are not strictly checked',
            'public_methods': methods, 'positive_sources': list(POSITIVE),
            'positive_plans_accepted_by_ir': runtime_result['plans'],
            'negative_cases': rejected,
            'generated_binding_negative_cases': generated_rejected,
            'generated_binding_plans_accepted_by_ir': runtime_result['generated_plans'],
            'statically_accepted_but_shared_checker_rejected': runtime_result['shared_checker_cases'],
            'dynamic_boundaries': ['runtime-selected schema/tool bindings', 'structural TypedDict values',
                                   'parameter dictionaries', 'general compute/field/record/resources result provenance',
                                   'affine ownership and concrete intent identity', 'JSON-decoded plans'],
            'elapsed_seconds': round(time.monotonic()-started, 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--output', type=Path, default=ROOT/'models/sdk_type_audit.json')
    options = parser.parse_args()
    report = audit(options.python)
    options.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print('SDK typing passed:', len(report['public_methods']), 'public methods,',
          report['positive_plans_accepted_by_ir'], 'IR-accepted plans,',
          report['generated_binding_plans_accepted_by_ir'], 'generated-binding IR-accepted plans,',
          len(report['negative_cases']), 'expected static rejections,',
          len(report['generated_binding_negative_cases']), 'generated-binding rejections,',
          len(report['statically_accepted_but_shared_checker_rejected']), 'shared-checker-only rejections.')


if __name__ == '__main__':
    main()
