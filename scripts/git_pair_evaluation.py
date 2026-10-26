"""Paired Git publication evaluator and reference calibration, not a causal study.

All Git endpoints are local real bare repositories. SDK submissions are Python
programs: local construction is for trusted code only. Select --builder-image
to construct both fronts in the explicit Docker boundary before host execution.
"""
import argparse
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time

from al0.codegen import generate_bindings
from al0.data import Principal, digest
from al0.frontend import compile_source, read_catalog
from al0.git_adapter import GitPublisher, PUBLISH, QUERY
from al0.ir import check_plan
from al0.store import Store

ROOT = Path(__file__).absolute().parents[1]
FAULTS = ('normal', 'lost_reply', 'crash_after_push')
WHO = Principal('paired-publication', 'host')
SDK_REFERENCE = '''from al0.ir import Catalog, CheckedPlan
from al0.sdk import PlanBuilder
from bindings import Bindings

def build(catalog: Catalog, commit: str) -> CheckedPlan[str]:
    b = PlanBuilder('paired-publication', catalog)
    api = Bindings(b)
    request = api.literal_GitPublishRequest('request', {'commit': commit})
    intent = api.prepare_git_publish('intent', request)
    permission = b.authorize('permission', intent)
    operation = b.admit('operation', intent, permission)
    initial = b.await_result('initial', operation)
    b.match_business('initial_result', initial, 'Text', applied='Published',
        proven_no_effect='NoEffect', pending='Pending', unknown='Unknown', not_dispatched='Queued')
    status = api.reconcile_git_query('query', operation)
    result = b.match_business('result', status, 'Text', applied='Published',
        proven_no_effect='NoEffect', pending='Pending', unknown='Unknown', not_dispatched='Queued')
    return b.finish(result)
'''


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def environment():
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME='AL0 evaluation', GIT_AUTHOR_EMAIL='evaluation@example.invalid',
        GIT_COMMITTER_NAME='AL0 evaluation', GIT_COMMITTER_EMAIL='evaluation@example.invalid',
        GIT_AUTHOR_DATE='2000-01-01T00:00:00+0000', GIT_COMMITTER_DATE='2000-01-01T00:00:00+0000',
        PYTHONPATH=str(ROOT), PYTHONIOENCODING='utf-8')
    env.pop('MYPYPATH', None)
    return env


def command(args, *, cwd=ROOT, timeout=90, env=None):
    start = time.monotonic()
    try:
        result = subprocess.run([str(x) for x in args], cwd=cwd, env=env or environment(),
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
        return {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr,
                'timeout': False, 'seconds': time.monotonic()-start}
    except subprocess.TimeoutExpired:
        return {'returncode': None, 'stdout': '', 'stderr': 'worker deadline exceeded',
                'timeout': True, 'seconds': time.monotonic()-start}
    except OSError as error:
        return {'returncode': None, 'stdout': '', 'stderr': str(error),
                'timeout': False, 'seconds': time.monotonic()-start}


def git(*args):
    result = command(['git', *args])
    if result['returncode'] != 0:
        raise RuntimeError('Git fixture/oracle failed: '+result['stderr'])
    return result['stdout'].strip()


def catalog_record(catalog):
    return {'catalog_version': 1, 'schemas': catalog.record_schemas(), 'tools': [
        {'name': t.name, 'kind': t.kind, 'version': t.version, 'input_type': t.input_type,
         'output_type': t.output_type, 'scope': t.scope, 'upper_bound': json.loads(t.upper_bound_json),
         'retry_window': t.retry_window, 'reconciles': t.reconciles, 'shared_write': t.shared_write,
         'recipients': t.flow_policy['recipients'], 'output_label': t.output_label.to_data()}
        for t in catalog.tools.values()]}


def reference_source(catalog, commit):
    scope = catalog.tools[PUBLISH].scope
    effects = ', '.join(json.dumps(x) for x in (
        'authorize:'+PUBLISH+'@'+scope, 'admit:'+PUBLISH+'@'+scope, 'reconcile:'+QUERY))
    return '''workflow paired-publication() -> Text uses {EFFECTS} {
    let request: GitPublishRequest = {commit: COMMIT};
    let intent = prepare("git.publish", request);
    let permission = authorize(intent);
    let operation = admit(intent, permission);
    let initial = await(operation);
    let initial_result: Text = match_business(initial, {
        Applied: "Published", ProvenNoEffect: "NoEffect", Pending: "Pending",
        Unknown: "Unknown", NotDispatched: "Queued"
    });
    let query = reconcile("git.query", operation);
    let result: Text = match_business(query, {
        Applied: "Published", ProvenNoEffect: "NoEffect", Pending: "Pending",
        Unknown: "Unknown", NotDispatched: "Queued"
    });
    return result;
}
'''.replace('EFFECTS', effects).replace('COMMIT', json.dumps(commit))


def prepare(folder):
    folder = Path(folder).absolute()
    folder.mkdir(parents=True, exist_ok=False)
    source, remote = folder/'objects', folder/'endpoint.git'
    source.mkdir()
    git('init', '--quiet', str(source))
    git('init', '--quiet', '--bare', str(remote))
    commits = []
    for contents in ('previous approved report\n', 'requested approved report\n'):
        (source/'report.txt').write_text(contents, encoding='utf-8')
        git('-C', str(source), 'add', 'report.txt')
        git('-C', str(source), 'commit', '--quiet', '-m', contents.strip())
        commits.append(git('-C', str(source), 'rev-parse', 'HEAD'))
    adapter = GitPublisher(source, str(remote), recipients=['public'], commits=commits,
                           environment=environment())
    record = catalog_record(adapter.catalog)
    assert read_catalog(json.dumps(record)).fingerprint() == adapter.catalog.fingerprint()
    dump(folder/'catalog.json', record)
    (folder/'bindings.py').write_text(generate_bindings(adapter.catalog), encoding='utf-8')
    (folder/'reference.py').write_text(SDK_REFERENCE, encoding='utf-8')
    (folder/'reference.al0').write_text(reference_source(adapter.catalog, commits[-1]), encoding='utf-8')
    task = {'version': 1, 'id': 'publish-approved-commit', 'approved': commits, 'commit': commits[-1],
        'source': str(source), 'remote': str(remote), 'budget': {'git_dispatches': 4},
        'catalog_fingerprint': adapter.catalog.fingerprint(), 'faults': list(FAULTS),
        'requirement': 'Publish the requested commit exactly once, reconcile uncertain delivery, '
        'return Published only after confirmation, and resume without another dispatch or obligation.',
        'sampling_unit': 'one development task; fault schedules are repeated observations',
        'scope': 'publication/recovery only; no code-repair, model-generation or developer-benefit claim'}
    dump(folder/'task.json', task)
    (folder/'TASK.txt').write_text(task['requirement']+'\nRequested commit: '+task['commit']+
        '\nUse catalog.json and bindings.py. SDK entry: build(catalog, commit). '
        'AL-0 entry: a parameterless Text workflow with the requested commit literal.\n'
        'Budget: 4 git_dispatches. Expected publication plus explicit reconciliation costs 2.\n'
        'Faults: normal; accepted push with lost reply; process exit after accepted push.\n'
        'Both arms must handle all business cases. The host only resumes the submitted plan; '
        'it never adds a missing query. Replays must add no events.\n'
        'Reference files are calibration answers, not development-study participant inputs.\n', encoding='utf-8')
    return task


def refs(task):
    # An oracle independent of adapter query parsing and language return values.
    output = git('--git-dir', task['remote'], 'for-each-ref', '--format=%(refname) %(objectname)')
    return dict(line.split() for line in output.splitlines())


def evaluate(package, arm, submission, output, *, fault='normal', python=sys.executable, builder_image=None, case_name=None,
             _prepared=None):
    if arm not in ('al0', 'sdk') or fault not in FAULTS:
        raise ValueError('unknown arm or fault schedule')
    package, output = Path(package).absolute(), Path(output).absolute()
    python = str(Path(python).absolute())
    output.mkdir(parents=True, exist_ok=False)
    task = json.loads((package/'task.json').read_text(encoding='utf-8'))
    cases = task.get('cases', [])
    case = next((c for c in cases if c['id'] == case_name), None)
    if (cases and case is None) or (case_name is not None and not cases):
        raise ValueError('select a case declared in the trusted task package')
    case = case or {}
    fault = case.get('fault', fault)
    budget_limit = case.get('budget', task['budget'])
    expected = case.get('expected', {'commits': [task['commit']], 'result': 'Published', 'dispatches': 2})
    catalog = read_catalog((package/'catalog.json').read_text(encoding='utf-8'))
    if catalog.fingerprint() != task['catalog_fingerprint']:
        raise ValueError('task catalog changed')
    if (package/'bindings.py').read_text(encoding='utf-8') != generate_bindings(catalog):
        raise ValueError('generated SDK bindings changed')
    original = Path(submission).read_text(encoding='utf-8')
    candidate = output/('submission.py' if arm == 'sdk' else 'submission.al0')
    candidate.write_text(original, encoding='utf-8')
    shutil.copyfile(package/'bindings.py', output/'bindings.py')
    report = {'version': 1, 'task': task['id'], 'task_digest': digest(task), 'arm': arm, 'fault': fault,
        'submission_digest': digest(original), 'catalog_fingerprint': catalog.fingerprint(),
        'stages': {}, 'safe_task_success': False, 'violations': [], 'failure': None,
        'construction_mode': 'docker' if builder_image else 'trusted-local'}
    if cases:
        report.update(version=2, case=case_name, case_digest=digest(case), expected=expected)
    def finish(failure):
        report['failure'] = failure
        dump(output/'report.json', report)
        return report
    plan_path = output/'plan.json'
    if _prepared is not None:
        # Trusted task runner cache; never read from candidate output or the CLI.
        # Freeze one construction per submission, including its static failures.
        if (_prepared['arm'], _prepared['submission_digest'], _prepared['catalog_fingerprint']) != (
                arm, report['submission_digest'], report['catalog_fingerprint']):
            raise ValueError('construction cache does not match this submission and catalog')
        report['stages'] = json.loads(json.dumps(_prepared['stages']))
        report['construction_reused'] = True
        report['prepared_from_case'] = _prepared['case']
        if _prepared.get('builder_image'):
            report['builder_image'] = _prepared['builder_image']
        if _prepared['failure']:
            return finish(_prepared['failure'])
        plan_path.write_text(_prepared['plan'], encoding='utf-8')
    elif builder_image:
        from scripts.isolated_plan_build import BuilderUnavailable, DockerBuilder
        try:
            builder = DockerBuilder(builder_image)
            built = builder.build(arm=arm, source=original,
                catalog_document=(package/'catalog.json').read_text(encoding='utf-8'),
                commit=task['commit'], catalog=catalog)
        except BuilderUnavailable as error:
            report['stages']['construction'] = {'error': str(error)}
            return finish('infrastructure:isolated_builder_unavailable')
        report['stages']['construction'] = {k: v for k, v in built.items() if k != 'plan'}
        report['builder_image'] = builder.image
        if built['failure']:
            return finish(built['failure'])
        plan_path.write_text(built['plan'], encoding='utf-8')
    elif arm == 'sdk':
        version = command([python, '-m', 'mypy', '--version'])
        report['stages']['mypy_version'] = version
        if version['returncode'] != 0 or not version['stdout'].startswith('mypy 1.18.2 '):
            return finish('infrastructure:mypy_1.18.2_required')
        env = environment()
        env['MYPYPATH'] = str(ROOT)
        typed = command([python, '-m', 'mypy', '--config-file', ROOT/'pyproject.toml', '--strict',
            '--follow-imports=silent', '--no-incremental', candidate, output/'bindings.py'], cwd=output, env=env)
        report['stages']['static'] = typed
        if typed['returncode'] != 0:
            from scripts.isolated_plan_build import mypy_failure
            return finish(mypy_failure(typed))
        dump(output/'build.json', {'submission': str(candidate), 'catalog': str(package/'catalog.json'),
                                  'commit': task['commit'], 'plan': str(plan_path)})
        built = command([python, '-m', 'scripts.git_pair_worker', 'build', output/'build.json'])
        report['stages']['build'] = built
        if built['returncode'] != 0:
            return finish('build_failure')
    else:
        started = time.monotonic()
        try:
            plan_path.write_text(compile_source(original, catalog).plan.encoded, encoding='utf-8')
            report['stages']['static'] = {'seconds': time.monotonic()-started, 'accepted': True}
        except Exception as error:
            report['stages']['static'] = {'error': type(error).__name__, 'message': str(error)}
            return finish('static_rejection')
    try:
        plan = check_plan(plan_path.read_text(encoding='utf-8'), catalog)
    except Exception as error:
        report['stages']['ir'] = {'error': type(error).__name__, 'message': str(error)}
        return finish('ir_rejection')
    report['plan_fingerprint'] = plan.fingerprint
    before_refs = refs(task)
    store = Store(output/'runtime.sqlite')
    run = store.create_run(WHO, plan.fingerprint, budget_limit)
    grants = {n: store.grant(run, n, spec.scope, expires=store.clock()+3600)
              for n, spec in catalog.tools.items()}
    config = {**task, 'database': store.path, 'plan': str(plan_path), 'run': run, 'grants': grants,
        'accepted': str(output/'accepted.jsonl'), 'result': str(output/'result.json'), 'fault': fault,
        'fault_after': case.get('fault_after', 1), 'inputs': case.get('inputs', {}),
        'revoke_after_authorize': case.get('revoke_after_authorize', False)}
    # Local host state includes grants; do not publish this file as experiment evidence.
    dump(output/'host.json', config)
    worker = [python, '-m', 'scripts.git_pair_worker']
    first = command([*worker, 'first', output/'host.json'])
    report['stages']['first'] = first
    # Always resume from persisted state in a fresh process, including failures.
    resumed = command([*worker, 'resume', output/'host.json'])
    report['stages']['resume'] = resumed
    result_path = output/'result.json'
    resumed_result = json.loads(result_path.read_text(encoding='utf-8')) if result_path.exists() else None
    store.finish_run(run, WHO)
    before_replay = store.events(run, WHO)
    replay = command([*worker, 'replay', output/'host.json'])
    report['stages']['replay'] = replay
    events = store.events(run, WHO)
    dump(output/'events.json', events)
    after_refs = refs(task)
    changed = {k: v for k, v in after_refs.items() if before_refs.get(k) != v}
    removed = sorted(set(before_refs)-set(after_refs))
    overwritten = sorted(set(before_refs) & set(changed))
    operations = [{'id': e['data']['operation'], **store.operation_info(e['data']['operation'], WHO)}
                  for e in events if e['kind'] == 'Admitted']
    snapshots = [store.snapshot(op['id'], WHO) for op in operations]
    accepted_path = output/'accepted.jsonl'
    accepted = [json.loads(s) for s in accepted_path.read_text(encoding='utf-8').splitlines()] if accepted_path.exists() else []
    result = json.loads(result_path.read_text(encoding='utf-8')) if result_path.exists() else None
    report.update(oracle={'before': before_refs, 'after': after_refs, 'changed': changed,
                          'removed': removed, 'overwritten': overwritten},
        result=result, budget=store.budget(run, WHO), status=store.status(run, WHO),
        accepted=accepted, replay_added_events=len(events)-len(before_replay), snapshots=snapshots)
    if any(not ref.startswith('refs/heads/al0/publish/') or oid not in task['approved']
           for ref, oid in changed.items()) or removed or overwritten:
        report['violations'].append('publication_outside_host_policy')
    budget = report['budget']['git_dispatches']
    if any(budget[k] < 0 for k in ('free', 'reserved', 'confirmed', 'retired')) or sum(
            budget[k] for k in ('free', 'reserved', 'confirmed', 'retired')) != budget_limit['git_dispatches']:
        report['violations'].append('quota_conservation')
    actions = [op for op in operations if op['intent']['tool'] == PUBLISH]
    ordered_reconciliation = True
    for earlier, later in zip(actions, actions[1:]):
        queries = {op['id'] for op in operations if op['intent']['tool'] == QUERY
                   and op['intent']['args'].get('operation') == earlier['id']}
        completions = [e['sequence'] for e in events if e['kind'] == 'BusinessEvidence'
                       and e['data'].get('operation') in queries and e['data'].get('outcome') == 'Applied']
        dispatches = [e['sequence'] for e in events if e['kind'] == 'DispatchStarted'
                      and e['data'].get('operation') == later['id']]
        ordered_reconciliation &= bool(completions and dispatches and min(completions) < min(dispatches))
    report['checks'] = {
        'external_goal': sorted(changed.values()) == sorted(expected['commits']) and not removed and not overwritten,
        ('publication_sequence' if cases else 'single_publish'): len(actions) == len(expected['commits'])
            and [a['output']['commit'] for a in accepted] == expected['commits'],
        'confirmed_result': result is not None and result['data'] == expected['result'],
        'clean_finish': report['status']['status'] == 'CompletedClean',
        'settled_quota': budget['reserved'] == 0 and budget['confirmed'] == expected['dispatches'],
        'replay_stable': events == before_replay and result == resumed_result,
        'worker_outcomes': first['returncode'] == (77 if fault == 'crash_after_push' else 0)
            and resumed['returncode'] == 0 and replay['returncode'] == 0,
        'fault_reached': fault == 'normal' or any(a['phase'] == 'first' and a['fault'] == fault
            and a['ordinal'] == config['fault_after'] for a in accepted),
    }
    if case.get('revoke_after_authorize'):
        report['checks']['revocation_reached'] = any(e['kind'] == 'GrantRevoked' for e in events)
    if len(expected['commits']) > 1:
        report['checks']['reconcile_before_next_publication'] = ordered_reconciliation
    report['safe_task_success'] = all(report['checks'].values()) and not report['violations']
    return finish(None if report['safe_task_success'] else 'task_or_recovery_failure')


def compare(package, al0, sdk, folder, *, python=sys.executable, seed=20261026, kind='submitted_pair', builder_image=None):
    folder = Path(folder).absolute()
    folder.mkdir(parents=True, exist_ok=False)
    order = [(fault, arm) for fault in FAULTS for arm in ('al0', 'sdk')]
    random.Random(seed).shuffle(order)
    rows = [evaluate(package, arm, al0 if arm == 'al0' else sdk,
        folder/(fault+'-'+arm), fault=fault, python=python, builder_image=builder_image) for fault, arm in order]
    paired = [{ 'fault': fault, 'al0_success': next(r['safe_task_success'] for r in rows if r['arm']=='al0' and r['fault']==fault),
        'sdk_success': next(r['safe_task_success'] for r in rows if r['arm']=='sdk' and r['fault']==fault),
        'equal_ir': all((folder/(fault+'-'+arm)/'plan.json').exists() for arm in ('al0', 'sdk')) and
                    (folder/(fault+'-al0')/'plan.json').read_text(encoding='utf-8') ==
                    (folder/(fault+'-sdk')/'plan.json').read_text(encoding='utf-8')}
        for fault in FAULTS]
    report = {'version': 1, 'kind': kind, 'seed': seed, 'order': order,
        'development_tasks': 1, 'fault_schedules': len(FAULTS), 'assigned_cells': len(rows),
        'successes': sum(r['safe_task_success'] for r in rows), 'pairs': paired,
        'causal_language_benefit': 'not_measured', 'rows': rows}
    sources = [*sorted((ROOT/'al0').rglob('*.py')), Path(__file__).absolute(), ROOT/'scripts/git_pair_worker.py']
    report['implementation'] = {'base_commit': git('-C', str(ROOT), 'rev-parse', 'HEAD'),
        'source_digest': digest({str(p.relative_to(ROOT)).replace('\\', '/'): p.read_text(encoding='utf-8') for p in sources}),
        'python': command([python, '--version'])['stdout'].strip(), 'git': git('--version')}
    dump(folder/'summary.json', report)
    return report


def pilot(folder, *, python=sys.executable, seed=20261026, builder_image=None):
    folder = Path(folder).absolute()
    folder.mkdir(parents=True, exist_ok=False)
    package = folder/'package'
    prepare(package)
    return compare(package, package/'reference.al0', package/'reference.py', folder/'evaluation',
                   python=python, seed=seed, kind='reference_calibration', builder_image=builder_image)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    for name in ('prepare', 'pilot'):
        p = sub.add_parser(name)
        p.add_argument('--output', required=True, type=Path)
        if name == 'pilot':
            p.add_argument('--python', default=sys.executable)
            p.add_argument('--seed', type=int, default=20261026)
            p.add_argument('--builder-image')
    p = sub.add_parser('evaluate')
    p.add_argument('--package', required=True, type=Path)
    p.add_argument('--arm', required=True, choices=('al0', 'sdk'))
    p.add_argument('--submission', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--fault', choices=FAULTS, default='normal')
    p.add_argument('--case', dest='case_name')
    p.add_argument('--python', default=sys.executable)
    p.add_argument('--builder-image')
    p = sub.add_parser('compare')
    p.add_argument('--package', required=True, type=Path)
    p.add_argument('--al0', required=True, type=Path)
    p.add_argument('--sdk', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--python', default=sys.executable)
    p.add_argument('--seed', type=int, default=20261026)
    p.add_argument('--builder-image')
    args = vars(parser.parse_args())
    mode, output = args.pop('mode'), args.pop('output')
    if mode == 'prepare':
        result = prepare(output)
    elif mode == 'pilot':
        result = pilot(output, **args)
    elif mode == 'compare':
        result = compare(folder=output, **args)
    else:
        result = evaluate(output=output, **args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if mode == 'pilot' and result['successes'] != result['assigned_cells']:
        raise SystemExit(1)
    if mode == 'pilot' and not all(p['equal_ir'] for p in result['pairs']):
        raise SystemExit(1)
    if mode == 'evaluate' and not result['safe_task_success']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
