"""Versioned development contracts and reference calibration for one task family."""
import argparse
import json
from pathlib import Path
import random
import shutil
import sys

from al0.data import digest
from al0.frontend import read_catalog
from scripts.git_pair_evaluation import ROOT, SDK_REFERENCE, dump, evaluate, prepare, reference_source

MANIFEST = ROOT/'experiments/development_tasks_v1.json'


def references(task_id, catalog, target, previous):
    effects = ', '.join(json.dumps(x) for x in ('authorize:git.publish@'+catalog.tools['git.publish'].scope,
        'admit:git.publish@'+catalog.tools['git.publish'].scope, 'reconcile:git.query'))
    def sdk_function(name, plan_name):
        return SDK_REFERENCE.replace('def build(', 'def '+name+'(').replace("'paired-publication'", repr(plan_name))
    def al0_function(name, commit):
        return reference_source(catalog, commit).replace('workflow paired-publication', 'workflow '+name)
    if task_id == 'conditional-release':
        sdk = sdk_function('publish', 'publish')+'''
def build(catalog: Catalog, commit: str) -> CheckedPlan[str]:
    yes = publish(catalog, commit)
    b = PlanBuilder('skip', catalog)
    no = b.finish(Bindings(b).literal_Text('result', 'Skipped'))
    b = PlanBuilder('main', catalog)
    enabled = Bindings(b).parameter_Bool('enabled')
    return b.finish(b.if_else('result', enabled, then=yes, otherwise=no))
'''
        al0 = al0_function('publish', target)+'''
workflow skip() -> Text uses {} {
    let result: Text = "Skipped";
    return result;
}
workflow main(enabled: Bool) -> Text uses {EFFECTS} {
    let result = if(enabled, publish, skip, {});
    return result;
}
'''.replace('EFFECTS', effects)
    elif task_id == 'ordered-release':
        sdk = sdk_function('first', 'first')+sdk_function('second', 'second')+'''
def build(catalog: Catalog, commit: str) -> CheckedPlan[str]:
    a = first(catalog, commit)
    z = second(catalog, PREVIOUS)
    b = PlanBuilder('main', catalog)
    b.invoke('first_result', a)
    return b.finish(b.invoke('result', z))
'''.replace('PREVIOUS', repr(previous))
        al0 = al0_function('first', target)+al0_function('second', previous)+'''
workflow main() -> Text uses {EFFECTS} {
    let first_result = invoke(first, {});
    let result = invoke(second, {});
    return result;
}
'''.replace('EFFECTS', effects)
    elif task_id == 'admission-fallback':
        sdk = '''from al0.ir import Catalog, CheckedPlan
from al0.sdk import PlanBuilder
from bindings import Bindings

def build(catalog: Catalog, commit: str) -> CheckedPlan[str]:
    b = PlanBuilder('accepted', catalog)
    api = Bindings(b)
    operation = api.operation_parameter_git_publish('result')
    initial = b.await_result('initial', operation)
    b.match_business('initial_result', initial, 'Text', applied='Published',
        proven_no_effect='NoEffect', pending='Pending', unknown='Unknown', not_dispatched='Queued')
    status = api.reconcile_git_query('query', operation)
    accepted = b.finish(b.match_business('answer', status, 'Text', applied='Published',
        proven_no_effect='NoEffect', pending='Pending', unknown='Unknown', not_dispatched='Queued'))
    b = PlanBuilder('returned', catalog)
    api = Bindings(b)
    request = api.intent_parameter_git_publish('request')
    b.permit_parameter('result', request)
    api.parameter_Json('reason')
    returned = b.finish(api.literal_Text('text', 'Deferred'))
    b = PlanBuilder('denied', catalog)
    api = Bindings(b)
    api.parameter_Json('reason')
    denied = b.finish(api.literal_Text('text', 'Denied'))
    b = PlanBuilder('main', catalog)
    api = Bindings(b)
    message = api.literal_GitPublishRequest('message', {'commit': commit})
    request = api.prepare_git_publish('request', message)
    permission = b.authorize('permission', request)
    admission = b.try_admit('admission', request, permission)
    return b.finish(b.match_admission('result', admission, admitted=accepted,
        returned=returned, no_permit=denied, inputs={'request': request}))
'''
        al0 = '''workflow accepted(result: Operation<"git.publish">) -> Text uses {"reconcile:git.query"} {
    let initial = await(result);
    let initial_result: Text = match_business(initial, {
        Applied: "Published", ProvenNoEffect: "NoEffect", Pending: "Pending",
        Unknown: "Unknown", NotDispatched: "Queued"
    });
    let query = reconcile("git.query", result);
    let answer: Text = match_business(query, {
        Applied: "Published", ProvenNoEffect: "NoEffect", Pending: "Pending",
        Unknown: "Unknown", NotDispatched: "Queued"
    });
    return answer;
}
workflow returned(request: Intent<"git.publish">, result: Permit<request>, reason: Json)
-> Text uses {} {
    let text: Text = "Deferred";
    return text;
}
workflow denied(reason: Json) -> Text uses {} {
    let text: Text = "Denied";
    return text;
}
workflow main() -> Text uses {EFFECTS} {
    let message: GitPublishRequest = {commit: TARGET};
    let request = prepare("git.publish", message);
    let permission = authorize(request);
    let admission: Admission<"git.publish"> = try_admit(request, permission);
    let result = match_admission(admission,
        {Admitted: accepted, Returned: returned, NoPermit: denied}, {request: request});
    return result;
}
'''.replace('EFFECTS', effects).replace('TARGET', json.dumps(target))
    else:
        raise ValueError('unknown development contract')
    return al0, sdk


def prepare_suite(folder):
    folder = Path(folder).absolute()
    folder.mkdir(parents=True, exist_ok=False)
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    dump(folder/'manifest.json', manifest)
    for spec in manifest['tasks']:
        package = folder/spec['id']
        task = prepare(package)
        commits = {'target': task['commit'], 'previous': task['approved'][0]}
        task.update(id=spec['id'], suite_version=manifest['version'], family=manifest['family'],
            split=manifest['split'], contract_digest=digest(spec), requirement=spec['requirement'], cases=[])
        for definition in spec['cases']:
            case = {k: v for k, v in definition.items() if k not in ('quota', 'commits', 'result', 'dispatches')}
            case.update(budget={'git_dispatches': definition['quota']}, expected={
                'commits': [commits[c] for c in definition['commits']], 'result': definition['result'],
                'dispatches': definition['dispatches']})
            task['cases'].append(case)
        dump(package/'task.json', task)
        catalog = read_catalog((package/'catalog.json').read_text(encoding='utf-8'))
        al0, sdk = references(spec['id'], catalog, commits['target'], commits['previous'])
        (package/'reference.al0').write_text(al0, encoding='utf-8')
        (package/'reference.py').write_text(sdk, encoding='utf-8')
        # A model/developer receives only this allowlisted directory. Runtime
        # cases, endpoint, objects, grants, oracle and reference answers stay out.
        public = package/'participant'
        public.mkdir()
        for name in ('catalog.json', 'bindings.py'):
            shutil.copyfile(package/name, public/name)
        dump(public/'task.json', {'id': task['id'], 'family': task['family'], 'split': task['split'],
            'version': 1, 'contract_digest': task['contract_digest'], 'requirement': task['requirement'],
            'target': commits['target'], 'previous': commits['previous'],
            'sdk_entry': 'build(catalog: Catalog, commit: str) -> CheckedPlan[str]; commit is target',
            'al0_entry': 'main; conditional-release has Bool parameter enabled; others have no parameters',
            'allowed_tools': ['git.publish', 'git.query'], 'dispatch_cost': 1,
            'recovery': 'same checked program resumes; original publication is never resent by the host'})
        # Replace the old calibration instruction so this package never presents
        # the generic single-publication requirement as its development task.
        (package/'TASK.txt').write_text(spec['requirement']+'\nSee participant/task.json.\n', encoding='utf-8')
    return manifest


def evaluate_submissions(package, submissions, folder, *, python=sys.executable, builder_image=None, seed=20261026):
    """Evaluate one or both frontends; never fill an absent arm with a reference."""
    if not submissions or set(submissions) - {'al0', 'sdk'}:
        raise ValueError('provide al0 and/or sdk submissions')
    package, folder = Path(package).absolute(), Path(folder).absolute()
    folder.mkdir(parents=True, exist_ok=False)
    task = json.loads((package/'task.json').read_text(encoding='utf-8'))
    frozen = folder/'task-snapshot'
    frozen.mkdir()
    dump(frozen/'task.json', task)
    for name in ('catalog.json', 'bindings.py'):
        shutil.copyfile(package/name, frozen/name)
    package = frozen
    order = [(case['id'], arm) for case in task['cases'] for arm in sorted(submissions)]
    random.Random(seed).shuffle(order)
    snapshots = {}
    for arm, source in submissions.items():
        path = folder/('submitted.al0' if arm == 'al0' else 'submitted.py')
        path.write_text(Path(source).read_text(encoding='utf-8'), encoding='utf-8')
        snapshots[arm] = path
    prepared, rows = {}, []
    for case, arm in order:
        cell = folder/(case+'-'+arm)
        row = evaluate(package, arm, snapshots[arm], cell, python=python,
            builder_image=builder_image, case_name=case, _prepared=prepared.get(arm))
        rows.append(row)
        if arm not in prepared:
            prepared[arm] = {key: row[key] for key in ('arm', 'submission_digest', 'catalog_fingerprint', 'case')}
            prepared[arm].update(builder_image=row.get('builder_image'),
                stages={k: v for k, v in row['stages'].items() if k not in ('first', 'resume', 'replay')},
                failure=None if 'plan_fingerprint' in row else row['failure'],
                plan=(cell/'plan.json').read_text(encoding='utf-8') if 'plan_fingerprint' in row else None)
    arms = {}
    for arm in sorted(submissions):
        selected = [r for r in rows if r['arm'] == arm]
        fingerprints = {r.get('plan_fingerprint') for r in selected}
        stable = None not in fingerprints and len(fingerprints) == 1
        arms[arm] = {'assigned_cases': len(selected), 'case_successes': sum(r['safe_task_success'] for r in selected),
            'business_goal_completed_cases': sum(r['business_goal_completed'] for r in selected),
            'false_success_claim_cases': sum(r['false_success_claim'] for r in selected),
            'stable_plan': stable, 'task_success': stable and all(r['safe_task_success'] for r in selected)}
    plans = [folder/(case+'-'+arm)/'plan.json' for case, arm in order]
    equal = all(p.exists() for p in plans) and len({p.read_text(encoding='utf-8') for p in plans}) == 1
    report = {'version': 1, 'task': task['id'], 'contract_digest': task['contract_digest'],
        'family': task['family'], 'split': task['split'], 'seed': seed, 'order': order,
        'arms': arms, 'equal_reference_ir': equal if len(submissions) == 2 else None, 'rows': rows}
    dump(folder/'summary.json', report)
    return report


def evaluate_task(package, al0, sdk, folder, **kwargs):
    return evaluate_submissions(package, {'al0': al0, 'sdk': sdk}, folder, **kwargs)


def calibrate(folder, *, python=sys.executable, builder_image=None):
    folder = Path(folder).absolute()
    folder.mkdir(parents=True, exist_ok=False)
    manifest = prepare_suite(folder/'packages')
    results = []
    for spec in manifest['tasks']:
        package = folder/'packages'/spec['id']
        results.append(evaluate_task(package, package/'reference.al0', package/'reference.py', folder/spec['id'],
                                     python=python, builder_image=builder_image))
    report = {'version': 1, 'kind': 'development_suite_reference_calibration', 'manifest_digest': digest(manifest),
        'task_families': 1, 'task_contracts': len(results), 'assigned_cells': sum(len(r['rows']) for r in results),
        'passed': all(r['equal_reference_ir'] and all(a['task_success'] for a in r['arms'].values()) for r in results),
        'model_calls': 0, 'causal_language_benefit': 'not_measured', 'tasks': results}
    dump(folder/'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    for mode in ('prepare', 'calibrate', 'evaluate'):
        p = sub.add_parser(mode)
        p.add_argument('--output', required=True, type=Path)
        if mode != 'prepare':
            p.add_argument('--python', default=sys.executable)
            p.add_argument('--builder-image')
        if mode == 'evaluate':
            p.add_argument('--package', required=True, type=Path)
            p.add_argument('--al0', required=True, type=Path)
            p.add_argument('--sdk', required=True, type=Path)
    args = vars(parser.parse_args())
    mode, folder = args.pop('mode'), args.pop('output')
    result = prepare_suite(folder) if mode == 'prepare' else calibrate(folder, **args) if mode == 'calibrate' else evaluate_task(folder=folder, **args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if mode == 'calibrate' and not result['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
