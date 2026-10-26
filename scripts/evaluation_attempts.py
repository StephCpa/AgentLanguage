"""Append-only evaluation identities for explicitly configured new journals."""
import json
from pathlib import Path
import sys
import time
import uuid

from al0.data import digest
from scripts.source_protocol import parse_source


def create_table(db):
    db.execute('''CREATE TABLE evaluation_attempts(id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL, binding TEXT NOT NULL, phase TEXT NOT NULL, result TEXT,
        started_at REAL NOT NULL, finished_at REAL, UNIQUE(request_id,ordinal))''')


def source_record(state, row):
    from scripts import generation_journal as j
    receipt = json.loads(row['receipt'])
    parsed = parse_source(j.pointer(receipt['raw_response'], receipt['output_pointer']))
    binding = {'request_id': row['id'], 'arm': row['arm'], 'raw_digest': parsed['raw_digest'],
               'source_digest': parsed['source_digest'], 'parser_version': parsed['parser_version'],
               'contract_digest': state['task']['contract_digest'], 'catalog_fingerprint': state['task']['catalog_fingerprint']}
    return parsed, j.encoded(binding)


def begin(folder, request_id, *, retry_infrastructure=False):
    from scripts import generation_journal as j
    with j.database(folder) as db:
        state = j.configuration(db)
        if 'max_evaluation_attempts' not in state['config']:
            raise ValueError('append-only evaluation was not enabled at initialization')
        row = db.execute('SELECT * FROM rounds WHERE id=?', (request_id,)).fetchone()
        if row is None or row['receipt'] is None or json.loads(row['receipt'])['status'] != 'output':
            raise ValueError('a saved output receipt is required')
        parsed, binding = source_record(state, row)
        previous = db.execute('SELECT * FROM evaluation_attempts WHERE request_id=? ORDER BY ordinal DESC', (request_id,)).fetchall()
        if previous:
            latest = previous[0]
            if any(p['binding'] != binding for p in previous):
                raise ValueError('request/source/contract/catalog changed across evaluation attempts')
            if latest['phase'] == 'evaluating':
                return {'id': latest['id'], 'ordinal': latest['ordinal'], 'created': False, 'phase': 'evaluating'}
            result = json.loads(latest['result'])
            if not retry_infrastructure:
                return {'id': latest['id'], 'created': False, 'phase': 'evaluated', 'result': result}
            if result['category'] != 'infrastructure_failure':
                raise ValueError('only infrastructure failures permit same-source reevaluation')
        elif retry_infrastructure:
            raise ValueError('no infrastructure evaluation exists to retry')
        if len(previous) >= state['config']['max_evaluation_attempts']:
            raise ValueError('evaluation attempt limit reached')
        identifier = str(uuid.uuid4())
        ordinal = len(previous)+1
        db.execute('INSERT INTO evaluation_attempts VALUES (?,?,?,?,?,?,?,NULL)',
                   (identifier, request_id, ordinal, binding, 'evaluating', None, time.time()))
        db.execute('UPDATE rounds SET phase=? WHERE id=?', ('evaluating', request_id))
        return {'id': identifier, 'ordinal': ordinal, 'created': True, 'phase': 'evaluating'}


def complete(folder, request_id, evaluation_id, report, provenance, *, remote=False):
    from scripts import generation_journal as j
    if not evaluation_id:
        raise ValueError('explicit evaluation_attempt identity required')
    with j.database(folder) as db:
        state = j.configuration(db)
        row = db.execute('SELECT * FROM rounds WHERE id=?', (request_id,)).fetchone()
        attempt = db.execute('SELECT * FROM evaluation_attempts WHERE id=? AND request_id=?',
                             (evaluation_id, request_id)).fetchone()
        if attempt is None or row is None:
            raise ValueError('unknown evaluation attempt')
        parsed, binding = source_record(state, row)
        if binding != attempt['binding']:
            raise ValueError('evaluation binding changed')
        j.verify_evaluation(state, row, report)
        if remote and any(r['construction_mode'] != 'docker' for r in report['rows']):
            raise ValueError('remote source execution requires Docker')
        result = {**j.feedback(report, row['arm']), 'evaluation_id': evaluation_id,
                  'evaluation_ordinal': attempt['ordinal'], 'source_protocol': parsed,
                  'provenance': provenance, 'report': report}
        result = json.loads(j.encoded(result))
        if attempt['phase'] == 'evaluated':
            if j.encoded(result) != attempt['result']:
                raise ValueError('evaluation result is immutable')
            return result
        latest = db.execute('SELECT id FROM evaluation_attempts WHERE request_id=? ORDER BY ordinal DESC LIMIT 1',
                            (request_id,)).fetchone()[0]
        if latest != evaluation_id:
            raise ValueError('stale evaluation cannot control progress')
        # Completed attempts are never overwritten; rounds is only the current
        # progress view used by reserve and summary.
        db.execute('UPDATE evaluation_attempts SET phase=?,result=?,finished_at=? WHERE id=?',
                   ('evaluated', j.encoded(result), time.time(), evaluation_id))
        db.execute('UPDATE rounds SET phase=?,evaluation=?,evaluated_at=? WHERE id=?',
                   ('evaluated', j.encoded(result), time.time(), request_id))
        return result


def evaluate(folder, request_id, *, builder_image=None, trusted_fixture=False, python=sys.executable,
             retry_infrastructure=False):
    from scripts import generation_journal as j
    from scripts.git_pair_evaluation import dump, git, ROOT
    folder = Path(folder).absolute()
    with j.database(folder) as db:
        state = j.configuration(db)
        row = db.execute('SELECT * FROM rounds WHERE id=?', (request_id,)).fetchone()
        if row is None or row['receipt'] is None:
            raise ValueError('record the output first')
        if row['phase'] == 'evaluated' and not retry_infrastructure:
            return json.loads(row['evaluation'])
        if json.loads(row['receipt'])['status'] != 'output':
            raise ValueError('generation outcomes without source cannot be reevaluated')
        if not builder_image and not (trusted_fixture and state['config']['evidence_mode'] == 'fixture'):
            raise ValueError('provider submissions require Docker')
    attempt = begin(folder, request_id, retry_infrastructure=retry_infrastructure)
    if attempt['phase'] == 'evaluated':
        return attempt['result']
    parsed, _ = source_record(state, row)
    work = folder/('evaluation-'+attempt['id'])
    output = work/'evaluation'
    if not attempt['created']:
        if not (output/'summary.json').is_file():
            raise ValueError('evaluation already started; inspect its process/artifacts, do not repeat side effects')
        report = json.loads((output/'summary.json').read_text(encoding='utf-8'))
        provenance = json.loads((work/'provenance.json').read_text(encoding='utf-8'))
    else:
        work.mkdir(exist_ok=False)
        provenance = {'executed_commit_sha': git('-C', ROOT, 'rev-parse', 'HEAD'), 'builder_image': builder_image}
        dump(work/'provenance.json', provenance)
        package = work/'package'
        package.mkdir()
        for name, text in state['frozen'].items():
            (package/name).write_text(text, encoding='utf-8')
        dump(work/'source-protocol.json', parsed)
        if parsed['accepted']:
            candidate = work/('submission.al0' if row['arm'] == 'al0' else 'submission.py')
            candidate.write_bytes(parsed['source'].encode('utf-8'))
            report = j.evaluate_submissions(package, {row['arm']: candidate}, output,
                                             python=python, builder_image=builder_image)
        else:
            output.mkdir()
            original = parsed['raw_text'].replace('\r\n', '\n').replace('\r', '\n')
            rows = [{'arm': row['arm'], 'case': c['id'], 'failure': 'format_rejection',
                     'safe_task_success': False, 'business_goal_completed': False, 'false_success_claim': False,
                     'submission_digest': digest(original), 'catalog_fingerprint': state['task']['catalog_fingerprint'],
                     'construction_mode': 'not_started', 'stages': {'static': {'message': parsed['error']}}}
                    for c in state['task']['cases']]
            report = {'arms': {row['arm']: {'task_success': False}}, 'contract_digest': state['task']['contract_digest'], 'rows': rows}
        report['source_protocol'] = parsed
        # Save binding before committing result. Crash recovery imports this
        # same artifact rather than launching another evaluator.
        dump(output/'summary.json', report)
    return complete(folder, request_id, attempt['id'], report, provenance)
