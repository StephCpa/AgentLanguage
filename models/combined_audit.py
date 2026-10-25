"""A bounded joint protocol model, with explicit provider and receipt events.

One admitted operation, an already-derived grant, two attempts, budget two,
one coordinator crash/recovery, fixed clock and an honest deduplicating provider.
Provider execution is independent of coordinator liveness. Transactions are
atomic; this is neither a power-loss model nor a whole-language proof.
"""
from collections import deque
from dataclasses import dataclass, replace
import json
from pathlib import Path

if __package__:
    from .extended_audit import explore
else:
    from extended_audit import explore


@dataclass(frozen=True)
class Attempt:
    phase: str = 'Queued'
    ticket: bool = False
    remote: str = 'absent'
    held: bool = True
    billed: bool = False


@dataclass(frozen=True)
class State:
    attempts: tuple = (Attempt(),)
    business: str = 'NotDispatched'
    cancelled: bool = False
    revoked: bool = False
    recovery: int = 0  # 0: before crash; 1: down; 2: recovered
    unauthorized: bool = False


def ledger(s):
    reserved = sum(a.held for a in s.attempts)
    confirmed = sum(a.billed for a in s.attempts)
    return {'total': 2, 'free': 2-reserved-confirmed,
            'reserved': reserved, 'confirmed': confirmed, 'retired': 0}


def closed(s):
    return s.cancelled or s.business in ('Applied', 'ProvenNoEffect')


def release_queued(attempts):
    return tuple(replace(a, phase='Closed', held=False) if a.phase == 'Queued'
                 else a for a in attempts)


def dispatch_error(s, index):
    if closed(s):
        return 'StateConflict'
    if s.revoked:
        return 'AuthorizationDenied'
    a = s.attempts[index]
    if a.phase != 'Queued':
        return 'StateConflict'
    return None


def retry_error(s):
    if closed(s) or s.business != 'Unknown':
        return 'StateConflict'
    if s.revoked:
        return 'AuthorizationDenied'
    if any(a.phase == 'Queued' for a in s.attempts):
        return 'StateConflict'
    if len(s.attempts) == 2:
        return 'AuthorizationDenied'
    if ledger(s)['free'] < 1:
        return 'BudgetExceeded'
    return None


def successors(s, *, mode='apply', mutant=None):
    if mode not in ('apply', 'reject'):
        raise ValueError(mode)
    # The provider is a different process: accepted requests can finish while
    # the coordinator is down, revoked or cancelled.
    for i, a in enumerate(s.attempts):
        if a.remote == 'inflight':
            aa = list(s.attempts)
            aa[i] = replace(a, remote='received')
            yield f'endpoint:{i}', replace(s, attempts=tuple(aa))
    if s.recovery == 1:
        yield 'recover', replace(s, recovery=2)
        return
    if not s.revoked:
        yield 'revoke', replace(s, revoked=True)
    if not s.cancelled:
        attempts = release_queued(s.attempts)
        if mutant == 'cancel_refunds_exposed':
            attempts = tuple(replace(a, held=False) for a in attempts)
        business = s.business
        if not any(a.phase == 'DispatchStarted' for a in s.attempts) and not closed(s):
            business = 'ProvenNoEffect'
        yield 'cancel', replace(s, cancelled=True, attempts=attempts, business=business)
    if s.recovery == 0:
        # A dispatch ticket not yet handed to the network is lost. An inflight
        # request is independent and survives. Recovery never recreates tickets.
        yield 'crash', replace(s, recovery=1, attempts=tuple(
            replace(a, ticket=False) for a in s.attempts))
    gate = replace(s, revoked=False) if mutant == 'leaf_only' else s
    if retry_error(gate) is None:
        yield 'retry', replace(s, attempts=s.attempts + (
            Attempt(held=mutant != 'retry_without_reservation'),),
            unauthorized=s.unauthorized or s.revoked)
    for i, a in enumerate(s.attempts):
        def change(action, value, **kwargs):
            aa = list(s.attempts)
            aa[i] = value
            return f'{action}:{i}', replace(s, attempts=tuple(aa), **kwargs)
        if dispatch_error(gate, i) is None:
            yield change('dispatch', replace(a, phase='DispatchStarted', ticket=True),
                         business='Unknown', unauthorized=s.unauthorized or s.revoked)
        if a.ticket:
            # The permission decision linearized at dispatch, not at arrival.
            yield change('launch', replace(a, ticket=False, remote='inflight'))
        if a.remote == 'received' and not a.billed:
            yield change('bill', replace(a, held=False, billed=True))
    if any(a.remote == 'received' for a in s.attempts) and s.business not in ('Applied', 'ProvenNoEffect'):
        yield 'business', replace(s, business='Applied' if mode == 'apply' else 'ProvenNoEffect',
                                  attempts=release_queued(s.attempts))


def violations(s):
    b = ledger(s)
    charged = sum(a.remote == 'received' for a in s.attempts)
    if s.unauthorized:
        yield 'new_work_after_ancestor_revocation'
    if b['free'] < 0:
        yield 'budget_exceeded'
    if charged > b['reserved'] + b['confirmed']:
        yield 'external_fees_not_covered'
    if any(a.phase in ('Queued', 'DispatchStarted') and not a.billed and not a.held
           for a in s.attempts):
        yield 'unsettled_attempt_not_reserved'
    if any(a.billed and a.remote != 'received' for a in s.attempts):
        yield 'billing_without_provider_record'
    if any(a.remote != 'absent' and a.phase != 'DispatchStarted' for a in s.attempts):
        yield 'remote_request_without_dispatch'


def context_keys(s, action):
    """Declared, deliberately finite coverage partition (not all graph edges)."""
    b = ledger(s)
    yield ('security', action, s.revoked, s.cancelled, s.recovery)
    yield ('control', action, s.business, len(s.attempts))
    yield ('cost', action, b['reserved'], b['confirmed'],
           sum(a.remote == 'received' for a in s.attempts))


def graph(mode):
    initial = State()
    parents, edges = {initial: None}, {}
    queue = deque([initial])
    while queue:
        s = queue.popleft()
        edges[s] = tuple(successors(s, mode=mode))
        for action, n in edges[s]:
            if n not in parents:
                parents[n] = s, action
                queue.append(n)
    return parents, edges


def path_to(state, parents):
    actions = []
    while parents[state] is not None:
        state, action = parents[state]
        actions.append(action)
    return tuple(reversed(actions))


def corpus(mode):
    """Deterministic witnesses for context partitions and feasible action pairs.

    Every reachable model state is enumerated, but only selected traces are run
    against SQLite. A shortest BFS prefix plus one/two edges supplies candidates;
    greedy set cover removes redundant candidates without losing obligations.
    """
    parents, edges = graph(mode)
    witnesses = {}
    for s, outgoing in edges.items():
        prefix = None
        for action, n in outgoing:
            for key in context_keys(s, action):
                if key not in witnesses:
                    prefix = path_to(s, parents) if prefix is None else prefix
                    witnesses[key] = prefix + (action,)
            for following, _ in edges[n]:
                key = ('pair', action, following)
                if key not in witnesses:
                    prefix = path_to(s, parents) if prefix is None else prefix
                    witnesses[key] = prefix + (action, following)
    candidates = {}
    for trace in dict.fromkeys(witnesses.values()):
        s, keys, previous = State(), set(), None
        for action in trace:
            keys.update(context_keys(s, action))
            if previous is not None:
                keys.add(('pair', previous, action))
            s = dict(edges[s])[action]
            previous = action
        candidates[trace] = keys
    remaining, chosen = set(witnesses), []
    while remaining:
        trace = max(candidates, key=lambda t: (len(candidates[t] & remaining), -len(t), t))
        covered = candidates.pop(trace) & remaining
        if not covered:
            raise AssertionError('coverage construction stalled')
        chosen.append(trace)
        remaining -= covered
    replayed_states, replayed_edges = {State()}, set()
    for trace in chosen:
        s = State()
        for action in trace:
            replayed_edges.add((s, action))
            s = dict(edges[s])[action]
            replayed_states.add(s)
    return chosen, {'states': len(parents), 'transitions': sum(map(len, edges.values())),
                    'coverage_obligations': len(witnesses), 'selected_traces': len(chosen),
                    'selected_steps': sum(map(len, chosen)),
                    'replayed_distinct_states': len(replayed_states),
                    'replayed_distinct_edges': len(replayed_edges),
                    'coverage_definition': 'action x security/control/cost partition; all feasible adjacent action pairs'}


def run():
    report = {'bounds': 'one admitted operation; derived grant; two attempts; two cost units; one crash/recovery; fixed time'}
    for mode in ('apply', 'reject'):
        report[mode] = {}
        for name in ('correct', 'cancel_refunds_exposed', 'retry_without_reservation', 'leaf_only'):
            result = explore(State(), lambda s: successors(s, mode=mode, mutant=None if name == 'correct' else name), violations)
            if bool(result['violation_states']) != (name != 'correct'):
                raise AssertionError((mode, name, result))
            report[mode][name] = result
        traces, coverage = corpus(mode)
        report[mode]['corpus'] = {**coverage, 'traces': traces}
    return report


if __name__ == '__main__':
    result = run()
    Path(__file__).with_suffix('.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    for mode in ('apply', 'reject'):
        for name, value in result[mode].items():
            print(mode, name, {k: v for k, v in value.items() if k not in ('shortest_counterexamples', 'traces')})
