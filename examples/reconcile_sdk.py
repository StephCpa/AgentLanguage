"""Budgeted recovery after an endpoint commits but its reply is lost."""
import json
from pathlib import Path
import tempfile

from al0.data import Principal
from al0.demo import Endpoint, adapters, catalog
from al0.execute import Broker, Interpreter
from al0.sdk import PlanBuilder
from al0.store import Store


def build(specs):
    builder = PlanBuilder('reconcile-example', specs)
    request = builder.literal('request', 'Text', 'Fix the off-by-one error at base version 7.')
    draft = builder.infer('draft', builder.tool('code-model'), request)
    tested = builder.check('tested', builder.tool('patch-tests'), draft)
    intent = builder.prepare('intent', builder.tool('repo.merge'), tested)
    permit = builder.authorize('permit', intent)
    operation = builder.admit('operation', intent, permit)
    status = builder.await_result('status', operation)
    outcomes = dict(applied='Done', proven_no_effect='NeedsRevision', pending='Waiting',
                    unknown='NeedsReconciliation', not_dispatched='Waiting')
    builder.match_business('initial_result', status, 'Text', **outcomes)
    # This sequential example always makes one explicit query. Conditional
    # branching is a later IR extension; match_business currently selects data.
    reconciled = builder.reconcile('query', builder.tool('repo.query'), operation)
    result = builder.match_business('result', reconciled, 'Text', **outcomes)
    return builder.finish(result)


def demo(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    store = Store(directory/'runtime.sqlite')
    endpoint = Endpoint(directory/'endpoint.sqlite')
    specs = catalog(reconciliation=True)
    plan = build(specs)
    principal = Principal('demo', 'developer')
    run = store.create_run(principal, plan.fingerprint, {'units': 10})
    grants = {name: store.grant(run, name, spec.scope, expires=store.clock()+3600)
              for name, spec in specs.tools.items()}
    callbacks = adapters(endpoint, reconciliation=True)

    def lost_reply(ticket):
        endpoint.merge(ticket)
        raise ConnectionError('endpoint committed; response lost')

    callbacks['repo.merge'] = lost_reply
    executor = Interpreter(Broker(store, specs, callbacks))
    result = executor.run(plan, run, principal, grants)
    store.finish_run(run, principal)
    event_count = len(store.events(run, principal))
    replayed = executor.run(plan, run, principal, grants)
    return {'result': result.data, 'replayed_result': replayed.data,
            'replay_added_events': len(store.events(run, principal))-event_count,
            'run_status': store.status(run, principal),
            'budget': store.budget(run, principal), 'endpoint': endpoint.state()}


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='al0-reconcile-') as folder:
        print(json.dumps(demo(folder), ensure_ascii=False, indent=2))
