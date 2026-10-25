"""Executable proposal for affine custody across a durable task boundary.

This is a protocol model, not a Store implementation or a refinement proof.
One parent, one child, one permit/intent, one operation, two budget allocations,
at most two attempts and one crash/recovery. Transactions are atomic. Grant,
principal, label and schema validity are assumed, not proved by this model.
"""
from collections import Counter, deque
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
import json
from pathlib import Path


RESOURCES = ('permit', 'operation', 'budget_a', 'budget_b')
UPPER = 2
ACTUAL = 1
TOTAL = 6


class Rejected(Exception):
    pass


@dataclass(frozen=True)
class Custody:
    owner: str
    generation: int = 0


@dataclass(frozen=True)
class Attempt:
    funding: int
    reserved_from: int
    phase: str = 'Queued'
    delivered: bool = False
    held: bool = True
    billed: bool = False


@dataclass(frozen=True)
class State:
    resources: tuple = (Custody('parent'), Custody('absent'), Custody('parent'), Custody('absent'))
    child: str = 'Absent'
    child_cancelled: bool = False
    # Recovery changes worker epoch without recreating custody or funds.
    recovery: int = 0  # 0 before crash, 1 down, 2 recovered
    epoch: int = 0
    # Root, A, B. A is already split from root; spawn allocates B atomically.
    free: tuple = (2, 4, 0)
    opened: tuple = (True, False)
    attempts: tuple = ()
    admitted: int = 0
    operation_open: bool = False
    operation_creator: str = ''
    confirmed: int = 0
    bad_access: str = ''
    wrong_cleanup: bool = False
    # The durable spawn receipt is an observation, never a new grant of custody.
    spawn_receipt: tuple = ()


MUTANTS = {
    'partial_spawn': 'spawn_bundle_not_atomic',
    'publish_without_escrow': 'return_published_before_join',
    'replay_restores_custody': 'custody_resurrected_by_replay',
    'ignore_generation': 'stale_custody_token_accepted',
    'ignore_epoch': 'stale_worker_accepted',
    'move_attempt_funding': 'attempt_funding_changed',
    'refund_to_controller': 'allocation_accounting_mismatch',
    'origin_based_cleanup': 'cleanup_touched_foreign_custody',
    'refund_exposed_on_cancel': 'external_liability_uncovered',
}


def _require(condition):
    if not condition:
        raise Rejected()


def _rights(state, index, actor, generation, epoch, mutant):
    held = state.resources[index]
    _require(actor in ('parent', 'child') and held.owner == actor)
    _require(actor != 'child' or state.child == 'Running')
    _require(mutant == 'ignore_generation' or held.generation == generation)
    _require(mutant == 'ignore_epoch' or state.epoch == epoch)


def _owned(state, index, actor, mutant):
    _rights(state, index, actor, state.resources[index].generation, state.epoch, mutant)


def _move(resource, owner):
    return Custody(owner, resource.generation+1)


def _credit(free, opened, account, amount):
    # The immutable funding lineage is root -> A / B; moving custody does not
    # reparent allocations. Closed accounts forward later refunds to root.
    destination = account if opened[account-1] else 0
    free[destination] += amount


def actions(state):
    for name in ('spawn', 'publish', 'join', 'cancel_child', 'crash', 'recover',
                 'replay_spawn', 'stale_generation', 'stale_epoch'):
        yield name
    for actor in ('parent', 'child'):
        for account in (1, 2):
            yield f'admit:{actor}:{account}'
            yield f'retry:{actor}:{account}'
        for index in range(len(state.attempts)):
            yield f'dispatch:{actor}:{index}'
    for index in range(len(state.attempts)):
        yield f'provider:{index}'
        yield f'bill:{index}'


def step(state, action, mutant=None):
    """Execute one atomic transition or reject it without changing state."""
    _require(mutant is None or mutant in MUTANTS)
    parts = action.split(':')
    kind = parts[0]
    if kind == 'provider':
        index = int(parts[1])
        attempt = state.attempts[index]
        _require(attempt.phase == 'Exposed' and not attempt.delivered)
        attempts = list(state.attempts)
        attempts[index] = replace(attempt, delivered=True)
        return replace(state, attempts=tuple(attempts))
    if kind == 'recover':
        _require(state.recovery == 1)
        return replace(state, recovery=2, epoch=state.epoch+1)
    _require(state.recovery != 1)
    if kind == 'crash':
        _require(state.recovery == 0)
        return replace(state, recovery=1)
    if kind == 'spawn':
        _require(state.child == 'Absent')
        _owned(state, 2, 'parent', mutant)
        resources = list(state.resources)
        for index in (0, 1, 2):
            if resources[index].owner == 'parent':
                if mutant == 'partial_spawn' and index == 2:
                    continue
                resources[index] = _move(resources[index], 'child')
        resources[3] = Custody('child', 1)
        _require(state.free[0] >= 2)
        free = (state.free[0]-2, state.free[1], state.free[2]+2)
        attempts = state.attempts
        if mutant == 'move_attempt_funding':
            attempts = tuple(replace(a, funding=2) for a in attempts)
        return replace(state, child='Running', resources=tuple(resources), free=free,
                       opened=(True, True), spawn_receipt=tuple(resources), attempts=attempts)
    if kind == 'replay_spawn':
        _require(bool(state.spawn_receipt))
        if mutant == 'replay_restores_custody':
            return replace(state, resources=state.spawn_receipt)
        return state
    if kind == 'publish':
        _require(state.child == 'Running')
        destination = 'parent' if mutant == 'publish_without_escrow' else 'escrow'
        resources = tuple(_move(r, destination) if r.owner == 'child' else r for r in state.resources)
        return replace(state, child='Returning', resources=resources)
    if kind == 'join':
        _require(state.child in ('Returning', 'Joined'))
        if state.child == 'Joined':
            return state
        resources = tuple(_move(r, 'parent') if r.owner == 'escrow' else r for r in state.resources)
        return replace(state, child='Joined', resources=resources)
    if kind == 'cancel_child':
        _require(state.child != 'Absent' and not state.child_cancelled)
        resources = list(state.resources)
        affected = {i for i, r in enumerate(resources) if r.owner in ('child', 'escrow')}
        wrong_cleanup = False
        if mutant == 'origin_based_cleanup':
            # Existing allocation-tree based cleanup is unsuitable once a
            # returned resource can outlive its original task's custody.
            origin_set = {3}
            if state.operation_creator == 'child':
                origin_set.add(1)
            wrong_cleanup = bool(origin_set-affected)
            affected |= origin_set
        for index in affected:
            resources[index] = _move(resources[index], 'runtime')
        free, opened, attempts = list(state.free), list(state.opened), list(state.attempts)
        operation_open = state.operation_open
        if 1 in affected:
            operation_open = False
            for index, attempt in enumerate(attempts):
                if attempt.phase == 'Queued' or (mutant == 'refund_exposed_on_cancel' and attempt.held):
                    _credit(free, opened, attempt.funding, UPPER)
                    attempts[index] = replace(attempt, held=False,
                        phase='Closed' if attempt.phase == 'Queued' else attempt.phase)
        for index, account in ((2, 1), (3, 2)):
            if index in affected and opened[account-1]:
                free[0] += free[account]
                free[account] = 0
                opened[account-1] = False
        child = 'Joined' if state.child == 'Joined' else 'Cancelled'
        return replace(state, child=child, child_cancelled=True, resources=tuple(resources),
                       operation_open=operation_open, free=tuple(free), opened=tuple(opened),
                       attempts=tuple(attempts), wrong_cleanup=wrong_cleanup)
    if kind in ('admit', 'retry'):
        actor, account = parts[1], int(parts[2])
        _require(account in (1, 2))
        _owned(state, account+1, actor, mutant)
        _require(state.opened[account-1] and state.free[account] >= UPPER)
        resources = list(state.resources)
        admitted, creator = state.admitted, state.operation_creator
        if kind == 'admit':
            _owned(state, 0, actor, mutant)
            _require(not state.attempts)
            resources[0] = _move(resources[0], 'spent')
            resources[1] = Custody(actor, 1)
            admitted += 1
            creator = actor
        else:
            _owned(state, 1, actor, mutant)
            _require(state.operation_open and len(state.attempts) == 1)
            _require(state.attempts[0].phase == 'Exposed')
        free = list(state.free)
        free[account] -= UPPER
        attempts = state.attempts+(Attempt(account, account),)
        return replace(state, resources=tuple(resources), free=tuple(free), attempts=attempts,
                       admitted=admitted, operation_open=True, operation_creator=creator)
    if kind == 'dispatch':
        actor, index = parts[1], int(parts[2])
        _owned(state, 1, actor, mutant)
        _require(state.operation_open)
        attempt = state.attempts[index]
        _require(attempt.phase == 'Queued')
        attempts = list(state.attempts)
        attempts[index] = replace(attempt, phase='Exposed')
        return replace(state, attempts=tuple(attempts))
    if kind == 'bill':
        index = int(parts[1])
        attempt = state.attempts[index]
        _require(attempt.delivered and not attempt.billed)
        free = list(state.free)
        account = attempt.funding
        if mutant == 'refund_to_controller':
            account = 2 if state.resources[1].owner == 'child' else 1
        _credit(free, state.opened, account, UPPER-ACTUAL)
        attempts = list(state.attempts)
        attempts[index] = replace(attempt, held=False, billed=True)
        return replace(state, free=tuple(free), attempts=tuple(attempts), confirmed=state.confirmed+ACTUAL)
    if kind in ('stale_generation', 'stale_epoch'):
        # Probe a retained old local token. A generation probe uses a current
        # worker after parent->child->parent ABA. An epoch probe uses a current
        # custody generation under a pre-crash worker lease.
        actor = state.resources[2].owner
        _require(actor in ('parent', 'child'))
        if kind == 'stale_generation':
            _require(actor == 'parent' and state.resources[2].generation > 0)
            generation, epoch = 0, state.epoch
        else:
            _require(state.epoch > 0)
            generation, epoch = state.resources[2].generation, 0
        _rights(state, 2, actor, generation, epoch, mutant)
        return replace(state, bad_access=kind)
    raise Rejected()


def violations(state):
    resources = state.resources
    if state.admitted > 1 or (resources[0].owner == 'spent') != bool(state.admitted):
        yield 'custody_resurrected_by_replay'
    if state.child in ('Returning', 'Joined', 'Cancelled') and any(r.owner == 'child' for r in resources):
        yield 'custody_resurrected_by_replay'
    if state.child == 'Running' and any(r.owner == 'parent' for r in resources):
        yield 'spawn_bundle_not_atomic'
    if state.child == 'Returning' and any(r.owner == 'parent' for r in resources):
        yield 'return_published_before_join'
    if state.bad_access == 'stale_generation':
        yield 'stale_custody_token_accepted'
    if state.bad_access == 'stale_epoch':
        yield 'stale_worker_accepted'
    if state.wrong_cleanup:
        yield 'cleanup_touched_foreign_custody'
    if any(a.funding != a.reserved_from for a in state.attempts):
        yield 'attempt_funding_changed'
    if any(a.phase == 'Exposed' and not (a.held or a.billed) for a in state.attempts):
        yield 'external_liability_uncovered'
    if sum(state.free)+UPPER*sum(a.held for a in state.attempts)+state.confirmed != TOTAL or min(state.free) < 0:
        yield 'root_budget_not_conserved'
    if state.confirmed != ACTUAL*sum(a.billed for a in state.attempts):
        yield 'billing_not_exactly_once'
    # Independently reconstruct balances from immutable reservation origins,
    # receipt facts, and account closure, not from mutable custody or free[].
    expected = [0 if state.spawn_receipt else 2, 4, 2 if state.spawn_receipt else 0]
    for attempt in state.attempts:
        expected[attempt.reserved_from] -= UPPER
        if attempt.billed:
            expected[attempt.reserved_from] += UPPER-ACTUAL
        elif attempt.phase == 'Closed':
            expected[attempt.reserved_from] += UPPER
    for account in (1, 2):
        if not state.opened[account-1]:
            expected[0] += expected[account]
            expected[account] = 0
    if tuple(expected) != state.free:
        yield 'allocation_accounting_mismatch'
    for index, account in ((2, 1), (3, 2)):
        if resources[index].owner in ('parent', 'child', 'escrow') and not state.opened[account-1]:
            yield 'live_budget_was_closed'


def path(parents, state):
    result = []
    while parents[state] is not None:
        previous, action = parents[state]
        result.append(action)
        state = previous
    return list(reversed(result))


@lru_cache(maxsize=1)
def graph():
    parents = {State(): None}
    pending = deque(parents)
    transitions = 0
    rejected = Counter()
    while pending:
        state = pending.popleft()
        errors = list(violations(state))
        if errors:
            raise AssertionError((errors, path(parents, state)))
        for action in actions(state):
            try:
                target = step(state, action)
            except Rejected:
                rejected[action.split(':')[0]] += 1
                continue
            transitions += 1
            if target not in parents:
                parents[target] = state, action
                pending.append(target)
    return parents, transitions, dict(rejected)


def counterexample(mutant):
    expected = MUTANTS[mutant]
    parents = {State(): None}
    pending = deque(parents)
    while pending:
        state = pending.popleft()
        errors = list(violations(state))
        if expected in errors:
            return {'invariant': expected, 'actions': path(parents, state),
                    'violations': errors, 'state': asdict(state)}
        # A malformed state's descendants do not add useful evidence and may
        # introduce unbounded replay cycles under deliberately broken rules.
        if errors:
            continue
        for action in actions(state):
            try:
                target = step(state, action, mutant)
            except Rejected:
                continue
            if target not in parents:
                parents[target] = state, action
                pending.append(target)
    raise AssertionError('mutant has no counterexample: '+mutant)


SCENARIOS = {
    'returned_operation_uses_two_funding_origins': (
        'admit:parent:1', 'spawn', 'dispatch:child:0', 'crash', 'provider:0', 'recover',
        'retry:child:2', 'publish', 'join', 'dispatch:parent:1', 'cancel_child',
        'provider:1', 'bill:1', 'bill:0'),
    'cancelled_child_keeps_exposed_liability': (
        'spawn', 'admit:child:2', 'dispatch:child:0', 'cancel_child',
        'crash', 'provider:0', 'recover', 'bill:0'),
    'unused_permit_returns_with_new_generation': (
        'spawn', 'publish', 'join', 'replay_spawn', 'join', 'admit:parent:1',
        'dispatch:parent:0', 'provider:0', 'bill:0'),
    'cancelled_escrow_closes_only_queued_reservation': (
        'admit:parent:1', 'spawn', 'dispatch:child:0', 'retry:child:2', 'publish',
        'cancel_child', 'provider:0', 'bill:0'),
}


def run_scenario(actions_, mutant=None):
    state = State()
    for action in actions_:
        state = step(state, action, mutant)
    return state


def audit():
    parents, transitions, rejected = graph()
    scenarios = {}
    for name, sequence in SCENARIOS.items():
        state = run_scenario(sequence)
        if state not in parents or list(violations(state)):
            raise AssertionError(name)
        scenarios[name] = {'actions': sequence, 'free': state.free, 'confirmed': state.confirmed,
                           'reserved': UPPER*sum(a.held for a in state.attempts)}
    return {'passed': True, 'scope': __doc__, 'states': len(parents), 'transitions': transitions,
            'rejected_requests': rejected, 'scenarios': scenarios,
            'coverage': {
                'child_states': dict(Counter(s.child for s in parents)),
                'recovery_states': dict(Counter(str(s.recovery) for s in parents)),
                'funding_sequences': sorted({','.join(str(a.reserved_from) for a in s.attempts) for s in parents}),
                'returned_exposed_operation': sum(s.child == 'Joined' and s.resources[1].owner == 'parent'
                    and any(a.phase == 'Exposed' for a in s.attempts) for s in parents),
                'cancelled_unsettled_exposure': sum(s.child == 'Cancelled'
                    and any(a.phase == 'Exposed' and a.held for a in s.attempts) for s in parents),
            },
            'mutants': {name: counterexample(name) for name in MUTANTS},
            'runtime_integrated': False, 'whole_language_proof': False}


if __name__ == '__main__':
    report = audit()
    output = Path(__file__).with_name('affine_transfer_results.json')
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('passed', 'states', 'transitions', 'runtime_integrated')}))
