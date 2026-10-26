"""Retrospective coverage of saved outputs; zero model calls, Docker required."""
import argparse
import json
from pathlib import Path
import shutil

from al0.data import digest
from scripts.development_tasks import evaluate_submissions
from scripts.generation_journal import encoded, pointer
from scripts.git_pair_evaluation import ROOT, dump, git
from scripts.score_deepseek_pilot import extract_single_fence


EXTENSION = ROOT/'experiments/query_outcomes_v2.json'


def extend_package(package, output):
    """Copy metadata only; keep the same host endpoint and frozen catalog.

    Existing v1 fixtures/results stay untouched. One new case is scored here;
    it is not quietly added to the original four-case task denominator.
    """
    package, output = Path(package), Path(output)
    spec = json.loads(EXTENSION.read_text(encoding='utf-8'))
    task = json.loads((package/'task.json').read_text(encoding='utf-8'))
    if task['id'] != spec['task'] or task['suite_version'] != 1:
        raise ValueError('coverage extension requires the v1 conditional-release package')
    output.mkdir(parents=True, exist_ok=False)
    task.update(base_contract_digest=task['contract_digest'], contract_digest=digest(spec),
                suite_version=2, cases=[spec['case']],
                requirement=task['requirement']+' '+spec['requirement_addition'])
    dump(output/'task.json', task)
    for name in ('catalog.json', 'bindings.py', 'reference.al0', 'reference.py'):
        shutil.copyfile(package/name, output/name)
    return task


def check(bundle_path, package, output, builder_image):
    bundle = json.loads(Path(bundle_path).read_text(encoding='utf-8'))
    package, output = Path(package), Path(output)
    for name in ('task.json', 'catalog.json', 'bindings.py'):
        if (package/'participant'/name).read_text(encoding='utf-8') != bundle['participant'][name]:
            raise ValueError('retrospective probe requires exact original participant inputs')
    output.mkdir(parents=True, exist_ok=False)
    extended = output/'package'
    task = extend_package(package, extended)
    references = evaluate_submissions(extended, {'al0': extended/'reference.al0', 'sdk': extended/'reference.py'},
                                      output/'references', builder_image=builder_image)
    if not references['equal_reference_ir'] or not all(a['task_success'] for a in references['arms'].values()):
        raise AssertionError('both reference programs must handle the new unknown-outcome case')
    trials = []
    for index, trial in enumerate(bundle['trials']):
        request, receipt = trial['request'], trial['receipt']
        if receipt['status'] != 'output':
            continue
        if receipt['request_id'] != request['id'] or request['arm'] not in ('al0', 'sdk'):
            raise ValueError('invalid saved trial identity')
        original = pointer(receipt['raw_response'], receipt['output_pointer']).replace('\r\n', '\n').replace('\r', '\n')
        source, changed = extract_single_fence(original)
        candidate = output/(str(index)+('.al0' if request['arm'] == 'al0' else '.py'))
        candidate.write_bytes(source.encode('utf-8'))
        evaluation = evaluate_submissions(extended, {request['arm']: candidate}, output/str(index),
                                          builder_image=builder_image)
        # Model failures are measured results; infrastructure errors are not.
        if any(str(r['failure']).startswith('infrastructure:') or r['failure'] == 'static_tool_failure'
               for r in evaluation['rows']):
            raise RuntimeError('retrospective probe infrastructure failed')
        trials.append({'request_id': request['id'], 'arm': request['arm'], 'ordinal': request['ordinal'],
                       'extracted_single_fence': changed, 'original_source_digest': digest(original),
                       'evaluated_source_digest': digest(source), 'evaluation': evaluation})
    report = {'version': 2, 'kind': 'posthoc_query_outcome_coverage', 'model_calls': 0,
              'code_commit': git('-C', ROOT, 'rev-parse', 'HEAD'), 'bundle_digest': digest(encoded(bundle)),
              'base_contract_digest': task['base_contract_digest'], 'contract_digest': task['contract_digest'],
              'format_policy': 'symmetric_single_fence_extraction_only', 'references': references, 'trials': trials,
              'interpretation': 'One retrospective case. Preserve original pilot outcomes; no new generation, repair, or causal estimate.'}
    dump(output/'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--package', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--builder-image', required=True)
    args = vars(parser.parse_args())
    args['bundle_path'] = args.pop('bundle')
    result = check(**args)
    print(json.dumps({'scored_trials': len(result['trials']), 'model_calls': 0}))


if __name__ == '__main__':
    main()
