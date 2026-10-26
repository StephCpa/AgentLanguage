"""Compile every registered prospective teaching example, with zero model calls."""
from pathlib import Path
import sys

from al0.codegen import generate_bindings
from al0.data import digest
from al0.frontend import compile_source
from al0.ir import Catalog
from scripts.git_pair_evaluation import ROOT, catalog_record, command, dump, environment
from scripts.isolated_plan_build import DockerBuilder


def check(folder, *, builder_image=None, python=sys.executable):
    folder = Path(folder).absolute()
    folder.mkdir(parents=True, exist_ok=False)
    catalog = Catalog()
    catalog.schema('ExampleRecord', {'value': 'Text'})
    materials = ROOT/'experiments/teaching_v2'
    paths = sorted(p for p in materials.iterdir() if p.suffix in ('.al0', '.py'))
    if {p.name for p in paths} != {'dynamic_record.al0', 'dynamic_record.py'}:
        raise ValueError('register and validate every teaching example')
    dump(folder/'catalog.json', catalog_record(catalog))
    (folder/'bindings.py').write_text(generate_bindings(catalog), encoding='utf-8')
    stages, plans = {}, {}
    for arm in ('al0', 'sdk'):
        path = materials/('dynamic_record.al0' if arm == 'al0' else 'dynamic_record.py')
        source = path.read_text(encoding='utf-8')
        if builder_image:
            built = DockerBuilder(builder_image).build(arm=arm, source=source,
                catalog_document=(folder/'catalog.json').read_text(encoding='utf-8'), commit='unused', catalog=catalog)
            stages[arm] = {k: v for k, v in built.items() if k != 'plan'}
            plans[arm] = built['plan']
        elif arm == 'al0':
            plans[arm] = compile_source(source, catalog, filename=path.name).plan.encoded
        else:
            candidate = folder/'submission.py'
            candidate.write_text(source, encoding='utf-8')
            version = command([python, '-m', 'mypy', '--version'])
            if not version['stdout'].startswith('mypy 1.18.2 '):
                raise ValueError('teaching validation requires mypy 1.18.2')
            env = environment()
            env['MYPYPATH'] = str(ROOT)
            typed = command([python, '-m', 'mypy', '--config-file', ROOT/'pyproject.toml', '--strict',
                '--follow-imports=silent', '--no-incremental', candidate, folder/'bindings.py'], cwd=folder, env=env)
            stages['typing'] = typed
            if typed['returncode']:
                raise AssertionError(typed)
            dump(folder/'build.json', {'submission': str(candidate), 'catalog': str(folder/'catalog.json'),
                                       'commit': 'unused', 'plan': str(folder/'plan.json')})
            built = command([python, '-m', 'scripts.git_pair_worker', 'build', folder/'build.json'])
            stages[arm] = built
            if built['returncode']:
                raise AssertionError(built)
            plans[arm] = (folder/'plan.json').read_text(encoding='utf-8')
    report = {'model_calls': 0, 'examples': {p.name: digest(p.read_text(encoding='utf-8')) for p in paths},
              'construction_mode': 'docker' if builder_image else 'trusted-local', 'stages': stages,
              'equal_ir': plans['al0'] is not None and plans['al0'] == plans['sdk']}
    dump(folder/'summary.json', report)
    if not report['equal_ir']:
        raise AssertionError('teaching examples did not produce equal checked IR')
    return report
