"""Exercise repair journaling with labelled fixtures and actual Git evaluation."""
import argparse
import json
from pathlib import Path
import sys

from scripts.development_tasks import prepare_suite
from scripts.generation_journal import initialize, reserve, receive, evaluate_round, settle, summary
from scripts.git_pair_evaluation import dump


def check(folder, *, builder_image=None, python=sys.executable):
    folder = Path(folder).absolute()
    folder.mkdir(parents=True, exist_ok=False)
    prepare_suite(folder/'tasks')
    package = folder/'tasks'/'conditional-release'
    journal = folder/'journal'
    initialize(journal, package, {'provider': 'synthetic-fixture', 'model': 'fixture-v1',
        'parameters': {}, 'max_attempts': 2, 'budget_usd_per_arm': '1.00', 'evidence_mode': 'fixture'},
        {'shared': 'Fixture validation only; no model receives this material.',
         'al0': 'Fixture validation of AL-0 diagnostics.', 'sdk': 'Fixture validation of typed SDK diagnostics.'})
    evaluations = []
    for arm in ('al0', 'sdk'):
        for number, source in enumerate(('not valid source', (package/('reference.al0' if arm == 'al0' else 'reference.py')).read_text(encoding='utf-8'))):
            request = reserve(journal, arm, '0.25')
            receive(journal, request['id'], {'request_id': request['id'], 'model': 'fixture-v1',
                'status': 'output', 'raw_response': {'text': source}, 'output_pointer': '/text',
                'usage': None, 'cost_usd': None if number == 0 else '0.00'})
            result = evaluate_round(journal, request['id'], builder_image=builder_image,
                                    trusted_fixture=not builder_image, python=python)
            evaluations.append({'arm': arm, 'round': number+1, **result})
            if number == 0:
                assert not result['task_success'] and result['category'] == 'submission_failure'
                assert summary(journal)['account']['total_cost_usd'] is None
                settle(journal, request['id'], '0.00', 'Synthetic fixture: no provider call or charge occurred.')
            else:
                assert result['task_success']
    report = {'version': 1, 'kind': 'generation_journal_fixture_acceptance', 'model_calls': 0,
        'construction_mode': 'docker' if builder_image else 'trusted-local',
        'summary': summary(journal), 'evaluations': evaluations}
    report['passed'] = all(a['task_success'] and not a['first_attempt_success'] and a['attempts'] == 2
                           for a in report['summary']['arms'].values())
    dump(folder/'summary.json', report)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--builder-image')
    p.add_argument('--python', default=sys.executable)
    args = vars(p.parse_args())
    result = check(args.pop('output'), **args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
