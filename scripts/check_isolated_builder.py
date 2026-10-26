"""Exercise real Docker enforcement and the paired evaluator, not mocked flags."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import time

from al0.frontend import read_catalog
from scripts.git_pair_evaluation import compare, evaluate, prepare, refs
from scripts.isolated_plan_build import DockerBuilder, control


def audit(image, folder):
    folder = Path(folder).absolute()
    folder.mkdir(parents=True, exist_ok=False)
    package = folder/'package'
    task = prepare(package)
    catalog_document = (package/'catalog.json').read_text(encoding='utf-8')
    catalog = read_catalog(catalog_document)
    source = (package/'reference.py').read_text(encoding='utf-8')
    builder = DockerBuilder(image)
    report = {'version': 1, 'image': builder.image, 'checks': {}, 'probes': {}}
    def check(name, condition):
        report['checks'][name] = bool(condition)
        if not condition:
            if report['probes']:
                latest = report['probes'][next(reversed(report['probes']))]
                print(json.dumps({'failed_check': name, 'failure': latest.get('failure'),
                    'diagnostics': {key: {'returncode': stage.get('returncode'),
                                         'stdout': stage.get('stdout', '')[:4000],
                                         'stderr': stage.get('stderr', '')[:4000]}
                                    for key, stage in latest.get('stages', {}).items()}}, indent=2))
            raise AssertionError(name)
    def build(text, selected=builder):
        return selected.build(arm='sdk', source=text, catalog_document=catalog_document,
                              commit=task['commit'], catalog=catalog)
    try:
        paired = compare(package, package/'reference.al0', package/'reference.py', folder/'paired',
                         builder_image=builder.image)
        report['paired'] = paired
        check('both_fronts_real_git_recovery', paired['successes'] == 6 and
              all(p['equal_ir'] for p in paired['pairs']))
        bad = build(source.replace("{'commit': commit}", "{'commit': 7}"))
        report['probes']['bad_type'] = bad
        check('static_gate_precedes_python_import', bad['failure'] == 'static_rejection' and
              'sdk' not in bad['stages'] and 'typeddict-item' in bad['stages']['typecheck']['stdout'])
        invalid = builder.build(arm='al0', source='workflow main() -> Text uses {} {\n let x: Json = {commit: commit};\n return x;\n}',
                                catalog_document=catalog_document, commit=task['commit'], catalog=catalog)
        report['probes']['source_diagnostic'] = invalid
        stderr = invalid['stages']['al0']['stderr']
        check('source_error_location_and_hint', invalid['failure'] == 'static_rejection' and
              'submission.al0:2:' in stderr and 'hint:' in stderr and 'Traceback' not in stderr)
        # Deliberate synthetic secret: never use a real credential for the probe.
        os.environ['AL0_PRIVATE_SENTINEL'] = 'must-not-enter-container'
        secret = folder/'private-host-file'
        secret.write_text('host-only sentinel', encoding='utf-8')
        checks = '''    import os
    import socket
    from pathlib import Path
    assert os.getuid() != 0
    assert 'AL0_PRIVATE_SENTINEL' not in os.environ
    assert not Path(HOST_FILE).exists()
    assert not Path('/input/host.json').exists()
    assert not Path('/var/run/docker.sock').exists()
    assert sorted(name for _, name in socket.if_nameindex()) == ['lo']
    for path in ('/opt/al0/al0/ir.py', '/input/catalog.json', '/root-write-probe'):
        try:
            Path(path).write_text('corruption')
        except OSError:
            pass
        else:
            raise AssertionError('writable protected path: '+path)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        try:
            sock.connect(('198.51.100.1', 9))
        except OSError:
            pass
        else:
            raise AssertionError('external network reachable')
    process_status = Path('/proc/self/status').read_text()
    assert 'NoNewPrivs:\\t1' in process_status
    assert 'CapEff:\\t0000000000000000' in process_status
'''.replace('HOST_FILE', repr(str(secret)))
        restricted = build(source.replace("    b = PlanBuilder", checks+"    b = PlanBuilder"))
        report['probes']['restrictions'] = restricted
        check('read_only_no_host_secret_no_network_no_capabilities', restricted['failure'] is None)
        check('host_file_unchanged', secret.read_text(encoding='utf-8') == 'host-only sentinel')
        forged = build(source.replace('    return b.finish(result)', '''    import json
    plan = b.finish(result)
    document = json.loads(plan.encoded)
    document['nodes'][0]['op'] = 'mint_permit'
    return CheckedPlan(json.dumps(document), 'forged-catalog')'''))
        report['probes']['forged_ir'] = forged
        check('host_rechecks_untrusted_ir', forged['failure'] == 'ir_rejection' and
              forged['stages']['sdk']['returncode'] == 0)
        limited = DockerBuilder(builder.image, timeout=30, output_limit=65536)
        flood = build(source.replace("    b = PlanBuilder", "    print('x'*200000)\n    b = PlanBuilder"), limited)
        report['probes']['output_flood'] = {**flood, 'stages': {
            name: {**record, 'stdout': record['stdout'][:200], 'stderr': record['stderr'][:200]}
            for name, record in flood['stages'].items()}}
        check('bounded_output_and_container_removed', flood['failure'] == 'construction_limit' and
              flood['stages']['sdk']['output_limited'] and flood['stages']['sdk']['removed'])
        hanging = build(source.replace("    b = PlanBuilder", '''    import subprocess
    import sys
    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
    while True:
        pass
    b = PlanBuilder'''), limited)
        report['probes']['timeout_with_child'] = hanging
        check('deadline_removes_container_and_descendants', hanging['failure'] == 'construction_limit' and
              hanging['stages']['sdk']['timeout'] and hanging['stages']['sdk']['removed'])
        before = refs(task)
        unavailable = evaluate(package, 'sdk', package/'reference.py', folder/'unavailable',
                               builder_image='al0-absent-builder:never-pull')
        report['probes']['unavailable'] = unavailable
        check('no_fallback_on_missing_image', unavailable['failure'] == 'infrastructure:isolated_builder_unavailable'
              and not (folder/'unavailable/runtime.sqlite').exists() and refs(task) == before)
        remaining = control(['docker', 'ps', '-a', '--filter', 'name=al0-build-', '--format', '{{.Names}}'])
        check('no_construction_containers_left', remaining.returncode == 0 and not remaining.stdout.strip())
        report['passed'] = True
    finally:
        os.environ.pop('AL0_PRIVATE_SENTINEL', None)
        (folder/'audit.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    started = time.monotonic()
    # Preserve logs/results on failure too. Runtime grants stay inside this
    # private temporary directory and are never included in the public report.
    with tempfile.TemporaryDirectory(prefix='al0-isolation-audit-') as temporary:
        folder = Path(temporary)/'audit'
        try:
            report = audit(args.image, folder)
            report['seconds'] = time.monotonic()-started
            args.output.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
            print(json.dumps({'passed': report['passed'], 'checks': report['checks'], 'seconds': report['seconds']}))
        except Exception:
            if (folder/'audit.json').exists():
                args.output.write_text((folder/'audit.json').read_text(encoding='utf-8'), encoding='utf-8')
            raise


if __name__ == '__main__':
    main()
