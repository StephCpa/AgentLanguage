"""Run a deterministic local repair scenario with a separate endpoint journal.

No model provider, real repository or network is accessed. The endpoint performs
atomic version checks and business deduplication, while charging each attempt.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import tempfile

from .data import Principal, canonical, digest
from .execute import Broker, Interpreter, Response
from .ir import Catalog, Tool, check_plan
from .store import Store


def catalog(*, reconciliation: bool=False) -> Catalog:
    result = Catalog()
    result.schema('Patch', {'patch': 'Text', 'base_version': 'Int'})
    result.register(Tool.define('code-model', 'infer', 'Text', 'Patch', 'model', {'units': 2}))
    result.register(Tool.define('patch-tests', 'check', 'Patch', 'Bool', 'sandbox', {'units': 1}, predicate='TestsPassed'))
    result.register(Tool.define('repo.merge', 'action', 'Patch', 'Json', 'repo', {'units': 1},
                                predicates=('TestsPassed',), retry_window=300))
    if reconciliation:
        result.register(Tool.define('repo.query', 'reconcile', 'Json', 'Json', 'repo', {'units': 1}, reconciles='repo.merge'))
    return result


class Endpoint:
    def __init__(self, path):
        self.path = str(path)
        db = sqlite3.connect(self.path)
        try:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS repo(id INTEGER PRIMARY KEY, version INTEGER, patch TEXT);
              INSERT OR IGNORE INTO repo VALUES(1,7,'old');
              CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, intent TEXT, outcome TEXT, output TEXT);
              CREATE TABLE IF NOT EXISTS charges(attempt TEXT PRIMARY KEY, units INTEGER, operation TEXT);
              CREATE TABLE IF NOT EXISTS query_charges(attempt TEXT PRIMARY KEY, units INTEGER);
            ''')
            if 'operation' not in {r[1] for r in db.execute('PRAGMA table_info(charges)')}:
                # Old demo charges remain unknown rather than being assigned to
                # an invented operation. New calls have explicit ownership.
                db.execute('ALTER TABLE charges ADD COLUMN operation TEXT')
            db.commit()
        finally:
            db.close()

    def merge(self, ticket):
        db = sqlite3.connect(self.path, isolation_level=None)
        try:
            db.execute('BEGIN IMMEDIATE')
            # This endpoint records each physical call as a billable attempt.
            db.execute('INSERT INTO charges(attempt,units,operation) VALUES(?,1,?)', (ticket.attempt, ticket.operation))
            previous = db.execute('SELECT intent,outcome,output FROM operations WHERE id=?', (ticket.operation,)).fetchone()
            if previous:
                if previous[0] != ticket.intent.encoded:
                    raise ValueError('idempotency key reused with a different effect')
                outcome, output = previous[1], json.loads(previous[2])
            else:
                args = ticket.intent.value['args']
                version = db.execute('SELECT version FROM repo WHERE id=1').fetchone()[0]
                if version != args['base_version']:
                    outcome, output = 'ProvenNoEffect', {'reason': 'version-conflict'}
                else:
                    db.execute('UPDATE repo SET version=version+1,patch=? WHERE id=1', (args['patch'],))
                    outcome, output = 'Applied', {'version': version + 1}
                db.execute('INSERT INTO operations VALUES(?,?,?,?)', (ticket.operation, ticket.intent.encoded, outcome, canonical(output)))
            db.commit()
            return Response(output, {'units': 1}, outcome)
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def state(self):
        db = sqlite3.connect(self.path)
        try:
            version, patch = db.execute('SELECT version,patch FROM repo WHERE id=1').fetchone()
            return {'version': version, 'patch': patch,
                    'business_operations': db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],
                    'attempt_charges': db.execute('SELECT COALESCE(SUM(units),0) FROM charges').fetchone()[0],
                    'query_charges': db.execute('SELECT COALESCE(SUM(units),0) FROM query_charges').fetchone()[0]}
        finally:
            db.close()

    def query(self, ticket):
        request = ticket.intent.value['args']
        db = sqlite3.connect(self.path, isolation_level=None)
        try:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO query_charges VALUES(?,1)', (ticket.attempt,))
            row = db.execute('SELECT intent,outcome,output FROM operations WHERE id=?', (request['operation'],)).fetchone()
            result = {'operation': request['operation'], 'intent_digest': request['intent_digest'],
                      'business': None, 'bills': []}
            if row is not None:
                if digest(json.loads(row[0])) != request['intent_digest']:
                    raise ValueError('query descriptor does not match recorded effect')
                result['business'] = {'evidence_id': f"endpoint-business:{request['operation']}",
                                      'outcome': row[1], 'details': {'output': json.loads(row[2])}}
                result['bills'] = [{'attempt': a, 'evidence_id': f'endpoint-bill:{a}', 'actual': {'units': n}}
                                   for a, n in db.execute('SELECT attempt,units FROM charges WHERE operation=? ORDER BY rowid', (request['operation'],))]
            db.commit()
            return Response(result, {'units': 1})
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()


def adapters(endpoint, *, reconciliation=False):
    result = {
        'code-model': lambda ticket: Response({'patch': 'return items[index - 1]', 'base_version': 7}, {'units': 2}),
        'patch-tests': lambda ticket: Response(ticket.intent.value['args']['patch'] == 'return items[index - 1]', {'units': 1}),
        'repo.merge': endpoint.merge,
    }
    if reconciliation:
        result['repo.query'] = endpoint.query
    return result


def demo(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    store = Store(directory / 'runtime.sqlite')
    endpoint = Endpoint(directory / 'endpoint.sqlite')
    specs = catalog()
    plan = check_plan((Path(__file__).resolve().parents[1] / 'examples/repair.ir.json').read_text(encoding='utf-8'), specs)
    principal = Principal('demo', 'developer')
    run = store.create_run(principal, plan.fingerprint, {'units': 10})
    grants = {name: store.grant(run, name, spec.scope, expires=store.clock()+3600, max_attempts=2)
              for name, spec in specs.tools.items()}
    executor = Interpreter(Broker(store, specs, adapters(endpoint)))
    result = executor.run(plan, run, principal, grants)
    event_count = len(store.events(run, principal))
    replayed = executor.run(plan, run, principal, grants)
    return {'result': result.data, 'replayed_result': replayed.data,
            'replay_added_events': len(store.events(run, principal))-event_count,
            'budget': store.budget(run, principal), 'endpoint': endpoint.state()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', help='optional output directory; defaults to a temporary directory')
    args = parser.parse_args()
    if args.directory:
        result = demo(args.directory)
    else:
        with tempfile.TemporaryDirectory(prefix='al0-demo-') as folder:
            result = demo(folder)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
