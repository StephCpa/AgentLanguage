"""Bounded semantic experiments for AL-0, Python 3.10+, standard library.

Five separate state machines, not a full language or a combined protocol proof.
Every model has a deliberately weakened variant and a corrected variant.
Network events are nondeterministic; BFS enumerates every reachable state.
The report contains shortest witnesses for each violated invariant.
"""
from collections import deque
from dataclasses import asdict, dataclass, replace
from pathlib import Path
import json


def explore(initial, successors, violations):
    parents = {initial: None}
    queue = deque([initial])
    witnesses = {}
    bad_counts = {}
    edges = terminals = 0
    while queue:
        state = queue.popleft()
        for name in violations(state):
            bad_counts[name] = bad_counts.get(name, 0) + 1
            if name not in witnesses:
                path, cursor = [], state
                while parents[cursor] is not None:
                    prev, action = parents[cursor]
                    path.append({"action": action, "state": asdict(cursor)})
                    cursor = prev
                witnesses[name] = list(reversed(path))
        outgoing = list(successors(state))
        edges += len(outgoing)
        terminals += not outgoing
        for action, following in outgoing:
            if following not in parents:
                parents[following] = state, action
                queue.append(following)
    return {"states": len(parents), "transitions": edges,
            "terminal_states": terminals, "violation_states": bad_counts,
            "shortest_counterexamples": witnesses}


@dataclass(frozen=True)
class Call:
    phase: str = "idle"
    held: int = 0
    charged: bool = False
    settled: bool = False


@dataclass(frozen=True)
class CostState:
    calls: tuple = (Call(), Call())
    confirmed: int = 0
    effect: bool = False


def cost_view(s):
    return s.confirmed + sum(c.held for c in s.calls), sum(c.charged for c in s.calls)


def cost_next(s, fixed, mode, budget=1):
    """Two physical calls, each costs exactly one when provider charges.

    no_effect: two distinct operations; first can be certified no business effect
      before billing arrives. That certificate does NOT exclude a later charge.
    retry: two attempts for one logical effect; provider deduplicates effect,
      but bills every attempt. Attempt 2 starts only after attempt 1 was sent.
    Neither model includes hidden charges, crash, or a dishonest cost upper bound.
    """
    accounted, _ = cost_view(s)
    for i, c in enumerate(s.calls):
        def move(action, new_call, **changes):
            cs = list(s.calls)
            cs[i] = new_call
            return f"call{i + 1}.{action}", replace(s, calls=tuple(cs), **changes)

        if c.phase == "idle":
            if mode == "retry" and i == 1 and s.calls[0].phase not in {"sent", "done"}:
                continue
            # Mutant: business idempotence is incorrectly treated as cost idempotence.
            amount = 0 if mode == "retry" and i == 1 and not fixed else 1
            if accounted + amount <= budget:
                yield move("reserve", replace(c, phase="reserved", held=amount))
        if c.phase == "reserved":
            yield move("send", replace(c, phase="sent"))
        if c.phase == "sent" and mode == "no_effect":
            yield move("business_no_effect", replace(
                c, phase="no_effect", held=c.held if fixed else 0))
        if c.phase in {"sent", "no_effect"} and not c.charged:
            yield move("provider_charge", replace(c, charged=True),
                       effect=s.effect or mode == "retry")
        if c.charged and not c.settled:
            yield move("billing_settle", replace(c, settled=True, held=0, phase="done"),
                       confirmed=s.confirmed + 1)


def cost_violations(s, budget=1):
    accounted, actual = cost_view(s)
    if actual > budget:
        yield "actual_cost_exceeds_budget"
    if accounted > budget:
        yield "ledger_exceeds_budget"
    if actual > accounted:
        yield "actual_cost_not_covered"


@dataclass(frozen=True)
class ReplayState:
    pc: int = 0
    spent: bool = False
    admissions: int = 0
    restored: bool = False


def replay_next(s, fixed):
    # A checkpoint predates permit consumption. At most one restore; two slots.
    # Fresh slot after restore represents replanning or a changed dynamic branch.
    if s.pc == 0 and not s.spent and s.admissions < 2:
        yield "admit_next_slot", replace(s, pc=1, spent=True, admissions=s.admissions + 1)
    if s.pc == 1 and not s.restored:
        yield "restore_old_checkpoint", replace(s, pc=0, restored=True,
                                                spent=s.spent if fixed else False)


@dataclass(frozen=True)
class RevokeState:
    phase: str = "ready"
    revoked: bool = False
    stale_acceptance: bool = False


def revoke_next(s, fixed):
    # The protected event is gateway acceptance. Later endpoint completion is
    # permitted after revocation; this model never claims instantaneous recall.
    if not s.revoked:
        yield "revoke", replace(s, revoked=True)
    if s.phase == "ready":
        yield "client_check", replace(s, phase="denied" if s.revoked else "checked")
    if s.phase == "checked":
        if fixed and s.revoked:
            yield "gateway_reject", replace(s, phase="denied")
        else:
            yield "gateway_accept", replace(s, phase="accepted", stale_acceptance=s.revoked)


@dataclass(frozen=True)
class AdmissionState:
    phase: str = "ready"
    spent: bool = False
    operation: bool = False
    crashed: bool = False


def admission_next(s, fixed):
    # One crash; all declared durable fields survive. Failed transaction rolls
    # back entirely in the fixed model. A consumed permit must name an operation.
    if s.phase == "ready":
        if fixed:
            yield "atomic_admission", replace(s, phase="done", spent=True, operation=True)
        else:
            yield "persist_permit_consumption", replace(s, phase="partial", spent=True)
    if s.phase == "partial":
        yield "persist_operation", replace(s, phase="done", operation=True)
    if not s.crashed:
        yield "crash_and_recover", replace(s, phase="recovered", crashed=True)


def run():
    result = {"scope": "Five independent finite models; no full protocol, compiler, real endpoint, liveness, or unbounded proof."}
    for fixed in (False, True):
        suffix = "fixed" if fixed else "mutant"
        for mode in ("no_effect", "retry"):
            result[f"{mode}_{suffix}"] = explore(
                CostState(), lambda s, f=fixed, m=mode: cost_next(s, f, m), cost_violations)
        result[f"checkpoint_{suffix}"] = explore(
            ReplayState(), lambda s, f=fixed: replay_next(s, f),
            lambda s: ["permit_spent_twice"] if s.admissions > 1 else [])
        result[f"revocation_{suffix}"] = explore(
            RevokeState(), lambda s, f=fixed: revoke_next(s, f),
            lambda s: ["revoked_authority_accepted"] if s.stale_acceptance else [])
        result[f"admission_{suffix}"] = explore(
            AdmissionState(), lambda s, f=fixed: admission_next(s, f),
            lambda s: ["consumption_without_operation_after_crash"]
            if s.crashed and s.spent and not s.operation else [])
    # Sanity control: the safe per-attempt rule allows two billed attempts when
    # the budget is two, rather than achieving safety by forbidding all retries.
    result["retry_fixed_budget_two"] = explore(
        CostState(), lambda s: cost_next(s, True, "retry", 2),
        lambda s: cost_violations(s, 2))
    for name, item in result.items():
        if name == "scope":
            continue
        if name.endswith("_mutant"):
            assert item["violation_states"], name
        else:
            assert not item["violation_states"], (name, item)
    # Explicit reachability witness for the budget-two progress control.
    frontier, seen, reaches_two = [CostState()], set(), False
    while frontier:
        state = frontier.pop()
        if state in seen:
            continue
        seen.add(state)
        reaches_two |= state.confirmed == 2
        frontier.extend(n for _, n in cost_next(state, True, "retry", 2))
    assert reaches_two
    result["retry_fixed_budget_two"]["reaches_two_settled_attempts"] = reaches_two
    return result


if __name__ == "__main__":
    report = run()
    dest = Path(__file__).with_suffix(".json")
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for key, value in report.items():
        if isinstance(value, dict):
            shortest = {k: len(v) for k, v in value["shortest_counterexamples"].items()}
            print(f"{key}: states={value['states']} edges={value['transitions']} violations={value['violation_states']} shortest={shortest}")
