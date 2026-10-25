"""Bounded capability invocation, revocation and late-receipt model.

One root, one derived reports-only capability, one invocation, one operation,
one recovery and one unit of cost. Transactions are atomic. Epoch changes,
expiry, multi-attempt retries, task leases and information flow are excluded.
This model checks selected rules, not a refinement proof of the implementation.
"""
from dataclasses import dataclass, replace
import json
from pathlib import Path

if __package__:
    from .extended_audit import explore
else:
    from extended_audit import explore


@dataclass(frozen=True)
class State:
    root_active: bool = True
    child_exists: bool = False
    child_active: bool = True
    argument_cap: str = ''
    permit_channel: str = ''
    phase: str = 'idle'
    recovered: bool = False
    reserved: int = 0
    confirmed: int = 0
    unauthorized: bool = False
    boundary_breach: bool = False
    outside_effect: bool = False


def live(state,grant,*,leaf_only=False):
    if grant=='root':
        return state.root_active
    return state.child_exists and state.child_active and (leaf_only or state.root_active)


def successors(state,*,leaf_only=False,ambient_only=False):
    if state.root_active:
        yield 'revoke root',replace(state,root_active=False)
    if state.child_exists and state.child_active:
        yield 'revoke child',replace(state,child_active=False)
    if not state.child_exists and state.root_active:
        yield 'derive reports-only child',replace(state,child_exists=True)
    if state.child_exists and not state.argument_cap:
        # The ambient map is always restricted to child. The mutant forgets
        # that an explicit parameter can still carry the root authority.
        for cap in ('root','child'):
            if cap=='child' or ambient_only:
                yield 'enter child boundary with '+cap,replace(state,argument_cap=cap,boundary_breach=cap=='root')
    grant=state.argument_cap
    if grant and not state.permit_channel and live(state,grant,leaf_only=leaf_only):
        for channel in ('reports','admin'):
            if grant=='root' or channel=='reports':
                yield 'authorize '+channel,replace(state,permit_channel=channel,
                    unauthorized=state.unauthorized or not live(state,grant))
    if state.permit_channel and state.phase=='idle' and live(state,grant,leaf_only=leaf_only):
        yield 'admit and reserve',replace(state,phase='queued',reserved=1,
            unauthorized=state.unauthorized or not live(state,grant))
    if state.phase=='queued':
        yield 'cancel unexposed operation',replace(state,phase='cancelled',reserved=0)
        if live(state,grant,leaf_only=leaf_only):
            yield 'commit dispatch',replace(state,phase='exposed',
                unauthorized=state.unauthorized or not live(state,grant),
                outside_effect=state.permit_channel!='reports')
    if state.phase=='exposed':
        # A receipt can arrive after either grant was revoked. Authority loss
        # does not erase the accounting responsibility for exposed work.
        yield 'accept final fee receipt',replace(state,phase='settled',reserved=0,confirmed=1)
    if not state.recovered:
        # Recovery reads durable capability, permit and operation identities.
        yield 'recover durable identities',replace(state,recovered=True)


def violations(state):
    if state.unauthorized:
        yield 'new_work_without_live_ancestor'
    if state.boundary_breach:
        yield 'captured_cap_exceeds_invocation_boundary'
    if state.outside_effect:
        yield 'dispatch_outside_invocation_constraints'
    if state.reserved+state.confirmed>1:
        yield 'budget_exceeded'
    if state.phase in ('queued','exposed') and state.reserved!=1:
        yield 'outstanding_cost_not_reserved'
    if state.phase=='settled' and (state.reserved,state.confirmed)!=(0,1):
        yield 'final_fee_not_accounted'


def run():
    report={}
    fixed_states=set()
    for name,options in (('transitive_boundary',{}),('leaf_only',{'leaf_only':True}),
                         ('ambient_only',{'ambient_only':True})):
        def inspect(state):
            if not options:
                fixed_states.add(state)
            return violations(state)
        report[name]=explore(State(),lambda state:successors(state,**options),inspect)
    assert not report['transitive_boundary']['violation_states']
    assert 'new_work_without_live_ancestor' in report['leaf_only']['violation_states']
    assert 'dispatch_outside_invocation_constraints' in report['ambient_only']['violation_states']
    witnessed={
        'allowed_child_dispatch':any(s.phase=='exposed' for s in fixed_states),
        'cancel_refunds_unexposed':any(s.phase=='cancelled' and s.reserved==0 for s in fixed_states),
        'revocation_preserves_exposed_reservation':any(s.phase=='exposed' and not s.root_active and s.reserved==1 for s in fixed_states),
        'late_fee_after_revocation':any(not s.root_active and s.phase=='exposed'
            and any(n.phase=='settled' and n.confirmed==1 for _,n in successors(s)) for s in fixed_states),
        'completed_recovery_after_revocation':any(s.phase=='settled' and not s.root_active and not s.recovered
            and any(n.phase=='settled' and n.recovered for _,n in successors(s)) for s in fixed_states),
    }
    assert all(witnessed.values())
    report['witnessed_behaviors']=witnessed
    return report


if __name__=='__main__':
    report=run()
    Path(__file__).with_suffix('.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    for name,result in report.items():
        if 'states' in result:
            print(f"{name}: states={result['states']} transitions={result['transitions']} violations={result['violation_states']}")
        else:
            print(f'{name}: {result}')
