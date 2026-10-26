"""Score exported provider source in Docker; never call a model from CI."""
import argparse
import json
from pathlib import Path

from al0.data import digest
from scripts.development_tasks import prepare_suite, evaluate_submissions
from scripts.generation_journal import pointer, encoded
from scripts.git_pair_evaluation import dump, git, ROOT


def score(bundle_path, output, fixture_root, builder_image):
    bundle = json.loads(Path(bundle_path).read_text(encoding='utf-8'))
    output, fixture_root = Path(output), Path(fixture_root)
    output.mkdir(parents=True, exist_ok=False)
    prepare_suite(fixture_root/'tasks')
    package = fixture_root/'tasks'/'conditional-release'
    for name, text in bundle['participant'].items():
        if name not in ('task.json', 'catalog.json', 'bindings.py') or (package/'participant'/name).read_text(encoding='utf-8') != text:
            raise ValueError('CI fixture does not match frozen participant inputs')
    trials = []
    for index, trial in enumerate(bundle['trials']):
        request, receipt = trial['request'], trial['receipt']
        if receipt['status'] != 'output':
            continue
        if receipt['request_id'] != request['id'] or request['arm'] not in ('al0', 'sdk'):
            raise ValueError('invalid trial identity')
        candidate = output/(str(index)+('.al0' if request['arm'] == 'al0' else '.py'))
        source = pointer(receipt['raw_response'], receipt['output_pointer']).replace('\r\n', '\n').replace('\r', '\n')
        candidate.write_bytes(source.encode('utf-8'))
        evaluation = evaluate_submissions(package, {request['arm']: candidate}, output/str(index), builder_image=builder_image)
        trials.append({'request_id': request['id'], 'arm': request['arm'], 'ordinal': request['ordinal'], 'evaluation': evaluation})
    report = {'version': 1, 'kind': 'deepseek_provider_submission_evaluation', 'model_calls_from_ci': 0,
              # Experiment metadata contains timestamps with fractions; hash
              # its canonical text, not the language's integer-only data domain.
              'bundle_digest': digest(encoded(bundle)), 'code_commit': git('-C', ROOT, 'rev-parse', 'HEAD'), 'trials': trials}
    dump(output/'summary.json', report)
    # A model's failed task is a valid measured outcome, not broken CI. Invalid
    # fixture/host execution raises above and fails the CI step separately.
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bundle', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--fixture-root', required=True, type=Path)
    p.add_argument('--builder-image', required=True)
    args = vars(p.parse_args())
    args['bundle_path'] = args.pop('bundle')
    result = score(**args)
    print(json.dumps({'scored_trials': len(result['trials']), 'model_calls_from_ci': 0}))


if __name__ == '__main__':
    main()
