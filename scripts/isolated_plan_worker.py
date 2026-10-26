"""Image-owned construction entry point; emits only untrusted data IR."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, '/opt/al0')
from al0.frontend import compile_source, read_catalog
from al0.source import SourceError


def main():
    mode = sys.argv[1]
    if mode == 'typecheck':
        import mypy.version
        if mypy.version.__version__ != '1.18.2':
            raise RuntimeError('builder requires mypy 1.18.2')
        result = subprocess.run([sys.executable, '-m', 'mypy', '--config-file', '/opt/al0/mypy.ini',
            '--strict', '--follow-imports=silent', '--no-incremental', '--cache-dir=/tmp/mypy',
            '/input/submission.py', '/input/bindings.py'])
        raise SystemExit(result.returncode)
    catalog = read_catalog(Path('/input/catalog.json').read_text(encoding='utf-8'))
    if mode == 'al0':
        try:
            plan = compile_source(Path('/input/submission.al0').read_text(encoding='utf-8'),
                                  catalog, filename='submission.al0').plan
        except SourceError as error:
            print(error.diagnostic.render(), file=sys.stderr)
            raise SystemExit(1) from None
    elif mode == 'sdk':
        sys.path.insert(0, '/input')
        spec = importlib.util.spec_from_file_location('candidate', '/input/submission.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        commit = json.loads(Path('/input/request.json').read_text(encoding='utf-8'))['commit']
        plan = module.build(catalog, commit)
    else:
        raise ValueError('unknown construction phase')
    print(plan.encoded)


if __name__ == '__main__':
    main()
