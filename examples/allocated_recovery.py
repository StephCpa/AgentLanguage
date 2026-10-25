"""Separate worker and reconciliation funds under a single root budget."""
import json
from pathlib import Path
import tempfile

from al0.data import Principal
from al0.demo import Endpoint, adapters, catalog
from al0.execute import Broker, Interpreter
from al0.store import Store
from examples.repair_sdk import build


def demo(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    store = Store(directory/'runtime.sqlite')
    endpoint = Endpoint(directory/'endpoint.sqlite')
    specs = catalog(reconciliation=True)
    plan = build(specs)
    principal = Principal('demo', 'developer')
    run = store.create_run(principal, plan.fingerprint, {'units': 10})
    worker = store.split_budget(run, principal, 'worker', {'units': 4})
    recovery = store.split_budget(run, principal, 'recovery', {'units': 2})
    grants = {name: store.grant(run, name, spec.scope, expires=store.clock()+1000)
              for name, spec in specs.tools.items()}
    callbacks = adapters(endpoint, reconciliation=True)

    def lost_reply(ticket):
        endpoint.merge(ticket)
        raise ConnectionError('endpoint committed; response lost')

    callbacks['repo.merge'] = lost_reply
    broker = Broker(store, specs, callbacks)
    executor = Interpreter(broker)
    first = executor.run(plan, run, principal, grants, allocation=worker)
    store.join_budget(run, principal, worker)
    store.finish_run(run, principal)
    before = {'status': store.status(run, principal)['status'], 'budget': store.budget(run, principal)}
    operation = next(o['operation'] for o in store.obligations(run, principal) if o['tool'] == 'repo.merge')
    broker.reconcile(run, principal, 'recover', operation, 'repo.query', grants['repo.query'], allocation=recovery)
    store.join_budget(run, principal, recovery)
    count = len(store.events(run, principal))
    replayed = executor.run(plan, run, principal, grants, allocation=worker)
    broker.reconcile(run, principal, 'recover', operation, 'repo.query', grants['repo.query'], allocation=recovery)
    return {'first_result': first.data, 'before_reconciliation': before,
            'replayed_result': replayed.data, 'status': store.status(run, principal)['status'],
            'budget': store.budget(run, principal), 'allocations': store.allocation_tree(run, principal),
            'replay_added_events': len(store.events(run, principal))-count, 'endpoint': endpoint.state()}


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='al0-allocation-') as folder:
        print(json.dumps(demo(folder), ensure_ascii=False, indent=2))
