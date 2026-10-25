"""Bounded ContextView design model; no production runtime correspondence yet.

Counts below are synthetic units, not provider tokens. The model has one
persisted binding, one view, at most one dispatch, and three text materials.
"""
from collections import deque
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path


@dataclass(frozen=True)
class Binding:
    source: int
    counter: int
    limit: int
    policy: str
    private: bool


@dataclass(frozen=True)
class View:
    binding: Binding
    kind: str
    selected: tuple[str, ...]
    count: int
    private: bool


@dataclass(frozen=True)
class State:
    source: int = 0
    counter: int = 0
    limit: int = 6
    policy: str = 'Filter'
    private: bool = True
    authorized: bool = True
    cancelled: bool = False
    binding: Binding | None = None
    prepared: View | None = None
    fixed: View | None = None
    observed: View | None = None
    admitted: bool = False
    sent: View | None = None
    dispatch_authorized: bool = True
    dispatch_cancelled: bool = False


MUTANTS = {
    'drop_mandatory': 'mandatory_preserved',
    'drop_excluded_label': 'selection_label_preserved',
    'clear_failure_label': 'failure_label_preserved',
    'replay_reads_latest_source': 'fixed_observation',
    'dispatch_uses_latest_counter': 'sent_fixed_view',
    'dispatch_after_revoke': 'dispatch_authorized',
    'reopen_failure': 'fixed_observation',
}


def current_binding(state):
    return Binding(state.source, state.counter, state.limit, state.policy, state.private)


def build(binding, mutant=''):
    """Pure synthetic packing: 2 wrapper units and 1 reserved output unit."""
    scale = 1 + binding.counter
    materials = (
        ('mandatory', 2 * scale, False),
        ('source-v' + str(binding.source), (3 + binding.source) * scale, binding.private),
        ('public', scale, False),
    )
    selected = []
    used = 2
    overflow = False
    for name, count, private in materials:
        if used + count + 1 <= binding.limit:
            selected.append((name, count, private))
            used += count
        elif name == 'mandatory' and mutant != 'drop_mandatory' or binding.policy == 'Fail':
            overflow = True
            break
    label = binding.private
    if mutant == 'drop_excluded_label':
        label = any(item[2] for item in selected)
    if overflow:
        return View(binding, 'TooLarge', (), used, False if mutant == 'clear_failure_label' else label)
    return View(binding, 'Ready', tuple(item[0] for item in selected), used, label)


def successors(state, mutant=''):
    # Source/tokenizer/policy changes are outside the already persisted binding.
    if not state.source:
        yield 'source_changes', replace(state, source=1)
    if not state.counter:
        yield 'counter_changes', replace(state, counter=1)
    if state.limit != 10:
        yield 'capacity_changes', replace(state, limit=10)
    if state.authorized:
        yield 'revoke', replace(state, authorized=False)
    if not state.cancelled:
        yield 'cancel', replace(state, cancelled=True, prepared=None)
    if state.binding is None and not state.cancelled:
        yield 'bind_inputs', replace(state, binding=current_binding(state))
    if state.binding is not None and state.fixed is None and not state.cancelled:
        if state.prepared is None:
            yield 'prepare', replace(state, prepared=build(state.binding, mutant))
        else:
            yield 'commit', replace(state, fixed=state.prepared, prepared=None)
    if state.prepared is not None:
        yield 'crash_before_commit', replace(state, prepared=None)
    if state.fixed is not None:
        value = state.fixed
        if mutant == 'replay_reads_latest_source':
            value = build(replace(value.binding, source=state.source))
        elif mutant == 'reopen_failure' and value.kind == 'TooLarge':
            value = build(replace(value.binding, limit=state.limit))
        yield 'read_or_recover', replace(state, observed=value)
        if (value.kind == 'Ready' and not state.admitted and state.authorized
                and not state.cancelled):
            yield 'admit', replace(state, admitted=True)
    if (state.admitted and state.sent is None and not state.cancelled
            and (state.authorized or mutant == 'dispatch_after_revoke')):
        value = state.fixed
        if mutant == 'dispatch_uses_latest_counter':
            value = build(replace(value.binding, counter=state.counter))
        yield 'dispatch', replace(state, sent=value,
                                  dispatch_authorized=state.authorized,
                                  dispatch_cancelled=state.cancelled)


def violations(state):
    for view in (state.prepared, state.fixed):
        if view is None:
            continue
        if view.kind == 'Ready':
            if 'mandatory' not in view.selected:
                yield 'mandatory_preserved'
            if view.count + 1 > view.binding.limit:
                yield 'synthetic_capacity_bound'
            if view.private != view.binding.private:
                yield 'selection_label_preserved'
        elif view.private != view.binding.private:
            yield 'failure_label_preserved'
        if view.binding != state.binding:
            yield 'bound_inputs_preserved'
    if state.observed is not None and state.observed != state.fixed:
        yield 'fixed_observation'
    if state.admitted and (state.fixed is None or state.fixed.kind != 'Ready'):
        yield 'only_ready_admitted'
    if state.sent is not None:
        if state.sent != state.fixed:
            yield 'sent_fixed_view'
        if not state.dispatch_authorized:
            yield 'dispatch_authorized'
        if state.dispatch_cancelled:
            yield 'dispatch_before_cancel'


def roots():
    return [State(limit=limit, policy=policy, private=private)
            for limit in (4, 6, 10) for policy in ('Fail', 'Filter') for private in (False, True)]


def explore(mutant='', target=None):
    parents = {root: None for root in roots()}
    queue = deque(parents)
    edges = 0
    reachable = set()
    while queue:
        state = queue.popleft()
        errors = tuple(violations(state))
        if errors:
            if target is None or target in errors:
                trace = []
                cursor = state
                while parents[cursor] is not None:
                    previous, action = parents[cursor]
                    trace.append(action)
                    cursor = previous
                return {'passed': False, 'states_examined': len(parents),
                        'violations': list(errors), 'initial': asdict(cursor),
                        'trace': list(reversed(trace)), 'final': asdict(state)}
        if state.fixed is not None:
            reachable.add(state.fixed.kind)
            if state.fixed.kind == 'Ready' and len(state.fixed.selected) < 3:
                reachable.add('FilteredReady')
        if state.sent is not None:
            reachable.add('Dispatched')
        if state.observed is not None and state.cancelled:
            reachable.add('HistoricalReadAfterCancel')
        if state.fixed is not None and (state.source, state.counter, state.limit) != (
                state.fixed.binding.source, state.fixed.binding.counter, state.fixed.binding.limit):
            reachable.add('FixedAcrossEnvironmentChange')
        for action, following in successors(state, mutant):
            edges += 1
            if following not in parents:
                parents[following] = (state, action)
                queue.append(following)
    return {'passed': True, 'states': len(parents), 'transitions': edges,
            'reachable_cases': sorted(reachable)}


def audit():
    baseline = explore()
    assert baseline['passed'], baseline
    assert set(baseline['reachable_cases']) == {
        'Ready', 'TooLarge', 'FilteredReady', 'Dispatched',
        'HistoricalReadAfterCancel', 'FixedAcrossEnvironmentChange'}, baseline
    mutants = {}
    for mutant, target in MUTANTS.items():
        result = explore(mutant, target)
        assert not result['passed'] and target in result['violations'], (mutant, result)
        mutants[mutant] = result
    return {'passed': True, 'runtime_integrated': False,
            'count_unit': 'synthetic; not provider tokens', 'baseline': baseline,
            'mutants': mutants, 'scope': 'one binding/view/request; three text materials; two source/counter versions; Fail or whole-material Filter',
            'not_covered': ['production Store correspondence', 'real tokenizers or providers',
                            'summary workflows', 'multiple views/requests',
                            'full label lattice, role partitioning and protected value provenance',
                            'task custody, leases and complete finance protocol',
                            'process or SQLite crash tests']}


if __name__ == '__main__':
    report = audit()
    Path(__file__).with_suffix('.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'mutants'}, indent=2))
    print('targeted_mutants', len(report['mutants']))
