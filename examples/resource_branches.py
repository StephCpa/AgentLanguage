"""Move an intent-bound permit into one branch and its operation into another."""
import json
from pathlib import Path
import tempfile

from al0.data import Principal
from al0.execute import Broker, Interpreter, Response
from al0.frontend import compile_source
from al0.ir import Catalog, Tool
from al0.sdk import PlanBuilder
from al0.store import Store


def catalog():
    specs=Catalog()
    specs.register(Tool.define('send','action','Text','Json','output',{'u':1}))
    return specs


def handled(builder,operation):
    state=builder.await_result('status',operation)
    return builder.match_business('result',state,'Text',not_dispatched='Waiting',pending='Waiting',
                                  unknown='NeedsReconciliation',applied='Done',proven_no_effect='Rejected')


def build(specs):
    b=PlanBuilder('submit',specs)
    intent=b.intent_parameter('request',b.tool('send'))
    permit=b.permit_parameter('permission',intent)
    submit=b.finish(b.admit('operation',intent,permit))
    b=PlanBuilder('complete',specs)
    op=b.operation_parameter('operation',b.tool('send'))
    complete=b.finish(handled(b,op))
    b=PlanBuilder('resource-branches',specs)
    message=b.literal('message','Text','hello')
    intent=b.prepare('request',b.tool('send'),message)
    permit=b.authorize('permission',intent)
    selected=b.literal('selected','Bool',True)
    op=b.if_else('operation',selected,then=submit,otherwise=submit,inputs={'request':intent,'permission':permit})
    return b.finish(b.if_else('result',selected,then=complete,otherwise=complete,inputs={'operation':op}))


def demo(directory):
    specs=catalog()
    plan=compile_source(Path(__file__).with_suffix('.al0').read_text(encoding='utf-8'),specs,entry='resource-branches').plan
    sdk=build(specs)
    assert sdk.encoded==plan.encoded
    who=Principal('example','developer')
    store=Store(Path(directory)/'runtime.sqlite')
    run=store.create_run(who,plan.fingerprint,{'u':10})
    grants={'send':store.grant(run,'send','output',expires=store.clock()+1000)}
    calls=[]
    def send(ticket):
        calls.append(ticket.intent.value['args'])
        return Response({'sent':True},{'u':1})
    broker=Broker(store,specs,{'send':send})
    result=Interpreter(broker).run(plan,run,who,grants)
    before=store.events(run,who)
    replay=Interpreter(broker).run(sdk,run,who,grants)
    return {'result':result.data,'replay':replay.data,'calls':calls,'identical_ir':True,
            'branches':sum(e['kind']=='BranchChosen' for e in before),
            'replay_added_events':len(store.events(run,who))-len(before),'budget':store.budget(run,who)}


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as directory:
        print(json.dumps(demo(directory),ensure_ascii=False,indent=2))
