"""Prospective task entry: shared budget, real teaching material and v2 journal.

Initialization never authorizes sending. A frozen plan must be supplied before
provider calls; this remediation uses only preflight/fixture execution.
"""
import argparse
import json
from pathlib import Path
import sys

from scripts.deepseek_pilot import PROFILE, send, ingest, export
from scripts.generation_journal import initialize, evaluate_round, summary, money
from scripts.git_pair_evaluation import ROOT, dump
from scripts.source_protocol import VERSION


def initialize_task(folder, package, ledger, plan, *, builder_image=None, python=sys.executable, evidence_mode='provider_import'):
    from scripts.check_teaching_materials import check
    folder, package = Path(folder).absolute(), Path(package).absolute()
    python = str(Path(python).absolute())
    task = json.loads((package/'task.json').read_text(encoding='utf-8'))
    if (plan.get('status') not in ('preflight', 'frozen') or plan.get('contract_digest') != task['contract_digest']
            or plan.get('source_protocol') != VERSION):
        raise ValueError('plan must bind the exact task and common source protocol')
    attempts = plan.get('model_attempts_per_arm')
    order = plan.get('call_order')
    if (type(attempts) is not int or not 1 <= attempts <= 3 or not isinstance(order, list)
            or order.count('sdk') != attempts or order.count('al0') != attempts or len(order) != attempts*2):
        raise ValueError('freeze a matched, explicit call order and attempt limit')
    if plan['status'] == 'frozen' and not all(plan.get(k) for k in ('task_set_id', 'independence_basis', 'order_basis')):
        raise ValueError('freeze task-set and ordering rationale before calls')
    profile = json.loads(PROFILE.read_text(encoding='utf-8'))
    if evidence_mode == 'provider_import':
        from scripts.shared_budget import require_history, inspect
        historical = json.loads((PROFILE.parent/'captured.json').read_text(encoding='utf-8'))
        require_history(ledger, [t['request']['id'] for t in historical['trials']])
        if money(inspect(ledger)['total_usd']) > money(profile['total_budget_usd']):
            raise ValueError('shared cap exceeds the original total authorization')
    folder.mkdir(parents=True, exist_ok=False)
    check(folder/'teaching-validation', builder_image=builder_image, python=python)
    profile.update(version=2, task=task['id'], max_attempts_per_arm=attempts,
                   generation_order=order, prospective_plan=plan)
    dump(folder/'profile.json', profile)
    materials = {n: (PROFILE.parent/(n+'.txt')).read_text(encoding='utf-8') for n in ('shared', 'al0', 'sdk')}
    materials['shared'] = materials['shared'].replace('with no Markdown fences',
        'as raw source or exactly one complete code fence (al0/python/py/unlabelled), without surrounding prose')
    materials['shared'] += '\nPublication completion uses the exact multiset of required commits, including required counts.\n'
    teaching = ROOT/'experiments/teaching_v2'
    for arm, extension in (('al0', 'al0'), ('sdk', 'py')):
        materials[arm] += '\nUnrelated teaching example with its own ExampleRecord schema {value: Text}; use the actual task Catalog in your solution:\n'
        materials[arm] += (teaching/('dynamic_record.'+extension)).read_text(encoding='utf-8')
    materials['al0'] += '\nDynamic fields are named references: record("SchemaName", {field: existing_variable}). JSON literals cannot contain variable references.\n'
    return initialize(folder/'journal', package, {'provider': 'deepseek', 'model': profile['model'],
        'parameters': profile['parameters'], 'max_attempts': attempts, 'budget_usd_per_arm': profile['budget_usd_per_arm'],
        'evidence_mode': evidence_mode, 'provider_profile': profile, 'shared_ledger': str(Path(ledger).absolute()),
        'source_protocol': VERSION, 'max_evaluation_attempts': plan['max_evaluation_attempts'],
        'execution_gate': plan['status'], 'call_order': order}, materials)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pilot', required=True, type=Path)
    sub = parser.add_subparsers(dest='mode', required=True)
    p = sub.add_parser('init')
    for name in ('package', 'ledger', 'plan'):
        p.add_argument('--'+name, type=Path, required=True)
    p.add_argument('--builder-image')
    p.add_argument('--python', default=sys.executable)
    p = sub.add_parser('send'); p.add_argument('--arm', choices=('al0', 'sdk'), required=True)
    p = sub.add_parser('ingest'); p.add_argument('--request-id', required=True)
    p = sub.add_parser('evaluate'); p.add_argument('--request-id', required=True)
    p.add_argument('--builder-image', required=True); p.add_argument('--retry-infrastructure', action='store_true')
    p = sub.add_parser('export'); p.add_argument('--output', type=Path, required=True)
    sub.add_parser('summary')
    args = vars(parser.parse_args())
    mode, folder = args.pop('mode'), args.pop('pilot')
    if mode == 'init':
        args['plan'] = json.loads(args['plan'].read_text(encoding='utf-8'))
        result = initialize_task(folder, **args)
    elif mode == 'evaluate':
        result = evaluate_round(folder/'journal', **args)
    elif mode == 'summary':
        result = summary(folder/'journal')
    else:
        result = {'send': send, 'ingest': ingest, 'export': export}[mode](folder, **args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
