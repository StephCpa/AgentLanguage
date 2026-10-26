"""Publish one host-approved commit, then reconcile through the same runtime.

This command performs a real Git push. Choose the destination, approved commit
and recipient policy explicitly. Its state directory must persist across runs.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from al0.data import Principal, canonical
from al0.errors import ReplayMismatch
from al0.execute import Broker, Interpreter
from al0.frontend import compile_source
from al0.git_adapter import GitPublisher, PUBLISH, QUERY
from al0.sdk import PlanBuilder
from al0.store import Store


def build(catalog, commit):
    b = PlanBuilder('git-publication', catalog)
    request = b.literal('request', 'GitPublishRequest', {'commit': commit})
    intent = b.prepare('intent', b.tool(PUBLISH), request)
    permission = b.authorize('permission', intent)
    operation = b.admit('operation', intent, permission)
    status = b.await_result('status', operation)
    return b.finish(b.match_business('result', status, 'Text', applied='Published',
        proven_no_effect='NoEffect', pending='Pending', unknown='Unknown', not_dispatched='Queued'))


def source(catalog, commit):
    effects = ', '.join(json.dumps(effect) for effect in (
        'authorize:'+PUBLISH+'@'+catalog.tools[PUBLISH].scope,
        'admit:'+PUBLISH+'@'+catalog.tools[PUBLISH].scope))
    return '''workflow git-publication() -> Text uses {EFFECTS} {
    let request: GitPublishRequest = {commit: COMMIT};
    let intent = prepare("git.publish", request);
    let permission = authorize(intent);
    let operation = admit(intent, permission);
    let status = await(operation);
    let result: Text = match_business(status, {
        Applied: "Published", ProvenNoEffect: "NoEffect", Pending: "Pending",
        Unknown: "Unknown", NotDispatched: "Queued"
    });
    return result;
}
'''.replace('EFFECTS', effects).replace('COMMIT', json.dumps(commit))


def run(repository, remote, commit, state_dir, *, recipients, query_key='query-0', environment=None):
    folder = Path(state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    adapter = GitPublisher(repository, remote, recipients=recipients, commits=[commit], environment=environment)
    plan = build(adapter.catalog, commit)
    compiled = compile_source(source(adapter.catalog, commit), adapter.catalog).plan
    if compiled.encoded != plan.encoded:
        raise AssertionError('Git source and SDK plans differ')
    store = Store(folder/'runtime.sqlite')
    who = Principal('git-publication', 'host')
    binding = canonical({'repository': str(Path(repository).absolute()), 'remote': remote,
                         'plan': plan.fingerprint, 'commit': commit})
    # Serialize local host provisioning. A crash before this commit may leave
    # an unused runtime run, but cannot have dispatched a publication.
    with closing(sqlite3.connect(folder/'host.sqlite')) as db, db:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE IF NOT EXISTS binding(id INTEGER PRIMARY KEY,binding TEXT,run TEXT,grants TEXT)')
        db.execute('BEGIN IMMEDIATE')
        saved = db.execute('SELECT binding,run,grants FROM binding WHERE id=1').fetchone()
        if saved:
            if saved[0] != binding:
                raise ReplayMismatch('state directory belongs to another publication contract')
            run_id, grants = saved[1], json.loads(saved[2])
        else:
            run_id = store.create_run(who, plan.fingerprint, {'git_dispatches': 10})
            grants = {name: store.grant(run_id, name, spec.scope, expires=store.clock()+3600)
                      for name, spec in adapter.catalog.tools.items()}
            db.execute('INSERT INTO binding VALUES(1,?,?,?)', (binding, run_id, canonical(grants)))
    broker = Broker(store, adapter.catalog, adapter.callbacks(store, who))
    initial = Interpreter(broker).run(plan, run_id, who, grants)
    operation = next(e['data']['operation'] for e in store.events(run_id, who) if e['kind'] == 'Admitted')
    target = store.operation_info(operation, who)
    # A new query_key explicitly spends another read dispatch; the same key
    # replays its observation. Neither path resends the publication attempt.
    status = broker.reconcile(run_id, who, query_key, operation, QUERY, grants[QUERY])
    store.finish_run(run_id, who)
    final = Interpreter(broker).run(plan, run_id, who, grants)
    before = len(store.events(run_id, who))
    replay = Interpreter(broker).run(compiled, run_id, who, grants)
    from al0.data import Intent
    record = target['intent']
    intent = Intent(record['tool'], record['version'], record['scope'], record['args'], record['receipts'])
    return {'initial': initial.data, 'final': final.data, 'business': status['business'],
            'run': run_id, 'operation': operation, 'commit': commit,
            'reference': adapter.reference(operation, intent.digest),
            'source_sdk_ir_equal': True, 'replay_equal': replay == final,
            'replay_added_events': len(store.events(run_id, who))-before,
            'budget': store.budget(run_id, who), 'status': store.status(run_id, who)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True, type=Path)
    parser.add_argument('--remote', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--state-dir', required=True, type=Path)
    parser.add_argument('--recipient', action='append', required=True)
    parser.add_argument('--query-key', default='query-0')
    args = parser.parse_args()
    print(json.dumps(run(args.repository, args.remote, args.commit, args.state_dir,
        recipients=args.recipient, query_key=args.query_key), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
