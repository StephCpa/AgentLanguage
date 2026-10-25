"""Finite-state design audit, not an implementation benchmark or language proof.

Budget model assumptions:
* Two branches; each can dispatch at most one remote call.
* Each dispatched call costs either zero or exactly one; its upper bound is one.
* Total budget is one. Check-and-reserve is one atomic ledger transition.
* Cancellation requests do not force an already dispatched call to be free.
* A remote no-charge decision is authoritative: a later charge is impossible.
* Settlement atomically moves a held reservation into confirmed spend, or frees it.
* No retries, crashes, ledger loss, extra callers, or hidden service charges.

Local cancellation may happen after a remote outcome but before the outcome is
settled locally. Remote outcome and local settlement are deliberately distinct.
"""

from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
import json


@dataclass(frozen=True)
class Branch:
    # stage: idle, reserved, sent, cancelled_pending, done
    stage: str = "idle"
    # remote: not_sent, pending, charged, free
    remote: str = "not_sent"
    held: bool = False


INITIAL = (Branch(), Branch())
BUDGET = 1


def budget_view(state):
    confirmed = sum(b.stage == "done" and b.remote == "charged" for b in state)
    held = sum(b.held for b in state)
    actual = sum(b.remote == "charged" for b in state)
    return {"confirmed_spend": confirmed, "held": held, "actual_charge": actual}


def successors(state, release_on_cancel):
    view = budget_view(state)
    for i, branch in enumerate(state):
        name = "AB"[i]

        def moved(action, new_branch):
            result = list(state)
            result[i] = new_branch
            return f"{name}.{action}", tuple(result)

        if branch.stage == "idle" and view["confirmed_spend"] + view["held"] < BUDGET:
            yield moved("reserve", Branch("reserved", "not_sent", True))
        if branch.stage == "reserved":
            yield moved("send", Branch("sent", "pending", True))
            # No request was dispatched, so this cancellation can safely release.
            yield moved("cancel_before_send", Branch("done", "free", False))
        if branch.stage == "sent":
            yield moved("cancel_in_flight", replace(
                branch, stage="cancelled_pending",
                held=False if release_on_cancel else branch.held,
            ))
        if branch.stage in {"sent", "cancelled_pending"}:
            if branch.remote == "pending":
                yield moved("remote_charge", replace(branch, remote="charged"))
                yield moved("remote_confirm_no_charge", replace(branch, remote="free"))
            elif branch.remote in {"charged", "free"}:
                yield moved("settle", replace(branch, stage="done", held=False))


def bfs(initial, next_states, is_bad, describe):
    queue = deque([initial])
    parents = {initial: None}
    edges = 0
    first_bad = None
    bad_count = 0
    terminal_count = 0
    while queue:
        state = queue.popleft()
        if is_bad(state):
            bad_count += 1
            if first_bad is None:
                first_bad = state
        outgoing = list(next_states(state))
        edges += len(outgoing)
        terminal_count += not outgoing
        for action, following in outgoing:
            if following not in parents:
                parents[following] = (state, action)
                queue.append(following)
    trace = []
    if first_bad is not None:
        cursor = first_bad
        while parents[cursor] is not None:
            previous, action = parents[cursor]
            trace.append({"action": action, "after": describe(cursor)})
            cursor = previous
        trace.reverse()
    return {
        "reachable_states": len(parents),
        "transitions": edges,
        "terminal_states": terminal_count,
        "bad_states": bad_count,
        "shortest_bad_trace_length": len(trace) if first_bad is not None else None,
        "shortest_bad_trace": trace,
    }, parents


def audit_budget(release_on_cancel):
    result, states = bfs(
        INITIAL,
        lambda s: successors(s, release_on_cancel),
        lambda s: budget_view(s)["actual_charge"] > BUDGET,
        budget_view,
    )
    result["max_actual_charge"] = max(budget_view(s)["actual_charge"] for s in states)
    result["max_confirmed_plus_held"] = max(
        budget_view(s)["confirmed_spend"] + budget_view(s)["held"] for s in states
    )
    result["actual_le_confirmed_plus_held_in_all_states"] = all(
        budget_view(s)["actual_charge"] <=
        budget_view(s)["confirmed_spend"] + budget_view(s)["held"] for s in states
    )
    return result


@dataclass(frozen=True)
class VersionState:
    # Authorization binds the external object's expected version to zero.
    version: int = 0
    phase: str = "unchecked"  # unchecked, checked, committed, denied
    interfered: bool = False  # At most one external concurrent update.
    stale_commit: bool = False


def version_successors(state, endpoint_cas):
    if state.phase == "unchecked":
        yield "client.check_version", replace(
            state, phase="checked" if state.version == 0 else "denied"
        )
    if not state.interfered:
        yield "other_actor.update_version", replace(state, version=1, interfered=True)
    if state.phase == "checked":
        if endpoint_cas and state.version != 0:
            yield "endpoint.CAS_reject", replace(state, phase="denied")
        else:
            yield "endpoint.commit", replace(
                state, phase="committed", stale_commit=state.version != 0
            )


def audit_version(endpoint_cas):
    result, _ = bfs(
        VersionState(), lambda s: version_successors(s, endpoint_cas),
        lambda s: s.stale_commit,
        lambda s: {"version": s.version, "phase": s.phase, "stale_commit": s.stale_commit},
    )
    return result


if __name__ == "__main__":
    report = {
        "budget_naive_release": audit_budget(True),
        "budget_retain_until_settlement": audit_budget(False),
        "version_client_check_only": audit_version(False),
        "version_endpoint_atomic_cas": audit_version(True),
        "scope": "Exhaustive finite-state design audit only; no real services or language implementation tested.",
    }
    assert report["budget_naive_release"]["shortest_bad_trace_length"] == 7
    assert report["budget_retain_until_settlement"]["bad_states"] == 0
    assert report["budget_retain_until_settlement"]["actual_le_confirmed_plus_held_in_all_states"]
    assert report["budget_retain_until_settlement"]["max_confirmed_plus_held"] == 1
    assert report["version_client_check_only"]["shortest_bad_trace_length"] == 3
    assert report["version_endpoint_atomic_cas"]["bad_states"] == 0
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    Path(__file__).with_suffix(".json").write_text(rendered + "\n", encoding="utf-8")
    print(rendered)

