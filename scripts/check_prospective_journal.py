"""Exercise the new request chain using labelled fixtures, never a provider."""
import argparse
import json
from pathlib import Path
import sys

from scripts.development_tasks import prepare_suite
from scripts.evaluation_contract import checkout_provenance
from scripts.git_pair_evaluation import dump, git, ROOT
from scripts import generation_journal as j
from scripts.next_pilot import initialize_task
from scripts.shared_budget import initialize as initialize_budget, inspect
from scripts.source_protocol import VERSION


def check(folder, *, builder_image=None, python=sys.executable):
    folder = Path(folder).absolute()
    folder.mkdir(parents=True, exist_ok=False)
    prepare_suite(folder/'tasks')
    package = folder/'tasks'/'conditional-release'
    task = json.loads((package/'task.json').read_text(encoding='utf-8'))
    ledger = folder/'budget.sqlite'
    initialize_budget(ledger, '1')
    pilot = folder/'pilot'
    initialize_task(pilot, package, ledger, {'status': 'preflight', 'contract_digest': task['contract_digest'],
        'source_protocol': VERSION, 'model_attempts_per_arm': 1, 'call_order': ['sdk', 'al0'],
        'max_evaluation_attempts': 2}, builder_image=builder_image, python=python, evidence_mode='fixture')
    history = []
    for arm in ('sdk', 'al0'):
        request = j.reserve(pilot/'journal', arm, '0.2')
        assert 'record(' in request['messages'][1]['content']
        source = (package/('reference.py' if arm=='sdk' else 'reference.al0')).read_text(encoding='utf-8')
        j.receive(pilot/'journal', request['id'], {'request_id': request['id'], 'model': request['model'],
            'status': 'output', 'raw_response': {'text': '```'+('python' if arm=='sdk' else 'al0')+'\n'+source+'\n```'},
            'output_pointer': '/text', 'cost_usd': None, 'usage': None})
        # An actually unavailable image exercises the ordinary Docker failure
        # path. Recovery evaluates the exact saved output with the ready image.
        failed = j.evaluate_round(pilot/'journal', request['id'], builder_image='al0-absent-builder:never-pull')
        assert failed['category'] == 'infrastructure_failure'
        try:
            j.reserve(pilot/'journal', arm, '0.1')
        except ValueError as error:
            assert 'reevaluation' in str(error)
        else:
            raise AssertionError('new model attempt was allowed')
        recovered = j.evaluate_round(pilot/'journal', request['id'], builder_image=builder_image,
            trusted_fixture=not builder_image, python=python, retry_infrastructure=True)
        assert recovered['task_success']
        history.append({'arm': arm, 'initial': failed, 'recovered': recovered})
    status = j.summary(pilot/'journal')
    assert len(status['requests']) == 2 and len(status['evaluation_attempts']) == 4
    assert inspect(ledger)['committed_usd'] == '0.4'
    report = {'passed': True, 'kind': 'prospective_request_chain_fixture', 'model_calls': 0,
              'construction_mode': 'docker' if builder_image else 'trusted-local',
              'shared_account': inspect(ledger), 'history': history,
              'request_count': 2, 'evaluation_attempt_count': 4,
              **checkout_provenance(git('-C', ROOT, 'rev-parse', 'HEAD'))}
    dump(folder/'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--builder-image')
    parser.add_argument('--python', default=sys.executable)
    args = vars(parser.parse_args())
    result = check(args.pop('output'), **args)
    print(json.dumps({'passed': result['passed'], 'model_calls': 0}))


if __name__ == '__main__':
    main()
