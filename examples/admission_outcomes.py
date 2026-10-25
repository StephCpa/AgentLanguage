"""Accepted, budget-rejected and revoked admissions with stable replay."""
import json
from pathlib import Path
import tempfile

from al0.data import Principal
from al0.errors import ExecutionLimitExceeded
from al0.execute import Broker, Interpreter, Response
from al0.frontend import compile_source
from al0.limits import ExecutionBudget
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.resource_branches import catalog


def alternatives(specs,*,retry=False):
    b=PlanBuilder('accepted',specs)
    operation=b.operation_parameter('result',b.tool('send'))
    state=b.await_result('state',operation)
    accepted=b.finish(b.match_business('text',state,'Text',applied='Done',not_dispatched='Waiting',pending='Waiting',
                                      unknown='NeedsReconciliation',proven_no_effect='Rejected'))
    b=PlanBuilder('returned',specs)
    intent=b.intent_parameter('request',b.tool('send'))
    permit=b.permit_parameter('result',intent)
    b.parameter('reason','Json')
    if retry:
        operation=b.admit('retry',intent,permit)
        state=b.await_result('state',operation)
        returned=b.finish(b.match_business('text',state,'Text',applied='Done',not_dispatched='Waiting',pending='Waiting',
                                          unknown='NeedsReconciliation',proven_no_effect='Rejected'))
    else:
        returned=b.finish(b.literal('text','Text','Deferred'))
    b=PlanBuilder('denied',specs)
    b.parameter('reason','Json')
    denied=b.finish(b.literal('text','Text','Denied'))
    return accepted,returned,denied


def build(specs,*,retry=False):
    accepted,returned,denied=alternatives(specs,retry=retry)
    b=PlanBuilder('admission-outcomes',specs)
    message=b.literal('message','Text','hello')
    intent=b.prepare('request',b.tool('send'),message)
    permit=b.authorize('permission',intent)
    admission=b.try_admit('admission',intent,permit)
    return b.finish(b.match_admission('result',admission,admitted=accepted,returned=returned,no_permit=denied,inputs={'request':intent}))


def demo(directory):
    specs=catalog()
    plan=compile_source(Path(__file__).with_suffix('.al0').read_text(encoding='utf-8'),specs,entry='admission-outcomes').plan
    sdk=build(specs)
    assert plan.encoded==sdk.encoded
    results={}
    for scenario,amount in [('accepted',1),('budget',0),('revoked',1)]:
        store=Store(Path(directory)/(scenario+'.sqlite'))
        who=Principal('example','developer')
        run=store.create_run(who,plan.fingerprint,{'u':amount})
        grants={'send':store.grant(run,'send','output',expires=store.clock()+1000)}
        calls=[]
        def send(ticket):
            calls.append(ticket.intent.value['args'])
            return Response({'sent':True},{'u':1})
        runner=Interpreter(Broker(store,specs,{'send':send}))
        if scenario=='revoked':
            try:
                runner.run(plan,run,who,grants,execution=ExecutionBudget(3))
            except ExecutionLimitExceeded:
                pass
            else:
                raise AssertionError('expected to stop after authorization')
            store.revoke(grants['send'])
        result=runner.run(plan,run,who,grants)
        before=len(store.events(run,who))
        replay=runner.run(sdk,run,who,grants)
        results[scenario]={'result':result.data,'replay':replay.data,'calls':calls,
                           'replay_added_events':len(store.events(run,who))-before,'budget':store.budget(run,who)}
    return {'identical_ir':True,'scenarios':results}


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as directory:
        print(json.dumps(demo(directory),ensure_ascii=False,indent=2))
