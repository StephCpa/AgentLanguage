"""Trusted Store experiment: move, return, and retry an operation across tasks.

The checked language adapter is not enabled yet. A separate SQLite provider
records physical requests, effects and charges independently of Store.
"""
import json
from pathlib import Path
import tempfile

from al0.data import Intent, Principal, Slot
from al0.store import Store
from models.combined_conformance import Provider


def demo(directory):
    directory = Path(directory)
    store = Store(directory/'runtime.sqlite', clock=lambda: 10)
    provider = Provider(directory/'provider.sqlite', 'apply')
    who = Principal('tenant', 'subject')
    run = store.create_run(who, 'affine-resource-host', {'cost': 10})
    store.register_contract('work', '1', {'cost': 2}, retry_window=100)
    grant = store.grant(run, 'work', 'outbox', expires=1000, max_attempts=2)
    intent = Intent('work', '1', 'outbox', {'message': 'hello'})
    permit = store.authorize(run, who, intent, grant, authorization_key='send')
    account_a = store.split_budget(run, who, 'a', {'cost': 4})
    account_b = store.split_budget(run, who, 'b', {'cost': 2})
    moves = {'permit': store.manage_resource(run, who, 'Permit', permit),
             'funds': store.manage_resource(run, who, 'Budget', account_a)}
    scope = store.open_scope(run, who, 'scope', {'cost': 2})
    with store.resource_access(run, who, moves.values()):
        task = store.spawn_task(scope, who, 'child', 'host-child', {}, caps={'work': grant}, moves=moves)
    lease = store.claim_task(task, who)
    inputs = store.task_resource_inputs(task, who)
    with store.resource_access(run, who, inputs.values(), task_lease=lease):
        operation = store.admit(run, who, Slot('send', task), intent, permit, allocation=account_a, task_lease=lease)
    op_ref = store.resource_birth('Operation', operation, who)
    first = store.snapshot(operation, who)['attempts'][0]['id']
    exports = {'operation': op_ref, 'funds': inputs['funds']}
    with store.resource_access(run, who, exports.values(), task_lease=lease):
        store.start_attempt(operation, first, who, task_lease=lease)
        provider.launch(operation, first)
        store.finish_task(lease, who, {'data': 'returned', 'receipts': []}, resources=exports)
    with store.resource_access(run, who):
        store.join_task(task, who)
    returned = store.task_resource_result(task, who)
    # Source task/scope exit does not cancel resources already received by root.
    store.handoff_task(task, who)
    store.close_scope(scope, who)
    with store.resource_access(run, who, returned.values()):
        second = store.retry(operation, who, allocation=account_b)
        store.start_attempt(operation, second, who)
        provider.launch(operation, second)
    for attempt in (second, first):
        provider.finish(attempt)
        store.settle(attempt, who, provider='work', evidence_id='bill:'+attempt,
                     actual={'cost': provider.bill(attempt)})
    store.business_result(operation, who, provider='work', evidence_id='done', outcome=provider.business(operation))
    before = store.events(run, who)
    # Historical receipts are stable even though the resources have moved on.
    assert store.spawn_task(scope, who, 'child', 'host-child', {}, caps={'work': grant}, moves=moves) == task
    store.join_task(task, who)
    assert before == store.events(run, who)
    return {'budget': store.budget(run, who), 'business': store.snapshot(operation, who)['business'],
            'attempt_funding': [store.attempt_funding(a, who)['allocation'] for a in (first, second)],
            'distinct_funding_accounts': store.attempt_funding(first, who)['allocation'] != store.attempt_funding(second, who)['allocation'],
            'provider_effects': len(provider.snapshot()['effects']), 'provider_fees': provider.snapshot()['fees'],
            'return_generations': {k: ref.generation for k, ref in returned.items()}, 'replay_added_events': 0}


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as directory:
        print(json.dumps(demo(directory), indent=2))
