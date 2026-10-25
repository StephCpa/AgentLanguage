"""Two-slot admission model with one permit, refund and revocation.

Admission is one atomic transition. SQLite transaction refinement, task leases,
information flow and uncertain remote commits remain separate proof obligations.
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
    held: bool = True
    granted: bool = True
    consumed: int = -1
    decisions: tuple = ('','')
    ever_rejected: tuple = (False,False)

    @property
    def free(self):
        return 1-int(self.held)-int(self.consumed>=0)


def successors(state,fixed):
    if state.held:
        yield 'refund earlier queued operation',replace(state,held=False)
    if state.granted:
        yield 'revoke authority',replace(state,granted=False)
    for slot,old in enumerate(state.decisions):
        if old and (fixed or old!='Returned'):
            continue
        if not state.granted or state.consumed>=0:
            result='NoPermit'
        elif state.free==0:
            result='Returned'
        else:
            result='Admitted'
        decisions=list(state.decisions)
        decisions[slot]=result
        rejected=list(state.ever_rejected)
        rejected[slot] |= result!='Admitted'
        following=replace(state,decisions=tuple(decisions),ever_rejected=tuple(rejected),
                          consumed=slot if result=='Admitted' else state.consumed)
        if following!=state:
            yield f'admit slot {slot}: {result}',following


def violations(state):
    if any(rejected and result=='Admitted' for rejected,result in zip(state.ever_rejected,state.decisions)):
        yield 'rejected_slot_reopened'
    accepted=[i for i,result in enumerate(state.decisions) if result=='Admitted']
    if len(accepted)>1:
        yield 'permit_consumed_twice'
    if accepted!=([state.consumed] if state.consumed>=0 else []):
        yield 'operation_without_consumption'
    if state.free<0:
        yield 'budget_overreserved'


def run():
    report={}
    for fixed in (False,True):
        report['durable_rejection' if fixed else 'forgotten_rejection']=explore(
            State(),lambda state:successors(state,fixed),violations)
    assert not report['durable_rejection']['violation_states']
    witness=report['forgotten_rejection']['shortest_counterexamples']['rejected_slot_reopened']
    assert len(witness)==3
    # Check non-vacuity: all three alternatives and a later use of a returned
    # token at another slot occur in the corrected reachable state space.
    seen=set()
    todo=[State()]
    while todo:
        state=todo.pop()
        if state in seen:
            continue
        seen.add(state)
        todo.extend(next_state for _,next_state in successors(state,True))
    assert {'Admitted','Returned','NoPermit'} <= {choice for state in seen for choice in state.decisions}
    assert any('Returned' in state.decisions and 'Admitted' in state.decisions for state in seen)
    return report


if __name__=='__main__':
    report=run()
    Path(__file__).with_suffix('.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    for name,result in report.items():
        print(f"{name}: states={result['states']} transitions={result['transitions']} violations={result['violation_states']}")
