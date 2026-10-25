"""Affine budget parameters, bounded funding and durable workflow calls."""
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
    specs.register(Tool.define('emit','observe','Text','Text','output',{'u':1}))
    return specs


def emitter(specs):
    b=PlanBuilder('emit-one',specs)
    message=b.parameter('message','Text')
    return b.finish(b.observe('value',b.tool('emit'),message))


def build(specs):
    emit=emitter(specs)
    b=PlanBuilder('funded',specs)
    funds=b.budget_parameter('funds')
    message=b.parameter('message','Text')
    value=b.invoke('value',emit,inputs={'message':message},budget=funds)
    b.join_budget('released',funds)
    funded=b.finish(value)
    b=PlanBuilder('budget-workflows',specs)
    message=b.literal('message','Text','hello')
    work=b.split_budget('work',{'u':3})
    reserve=b.split_budget('reserve',{'u':2})
    result=b.invoke('result',funded,inputs={'funds':work,'message':message})
    b.join_budget('released',reserve)
    return b.finish(result)


def demo(directory):
    specs=catalog()
    plan=compile_source(Path(__file__).with_suffix('.al0').read_text(encoding='utf-8'),specs,entry='budget-workflows').plan
    sdk=build(specs)
    assert plan.encoded==sdk.encoded
    store=Store(Path(directory)/'runtime.sqlite')
    who=Principal('example','developer')
    run=store.create_run(who,plan.fingerprint,{'u':10})
    grant=store.grant(run,'emit','output',expires=store.clock()+1000)
    calls=[]
    def emit(ticket):
        calls.append(ticket.intent.value['args'])
        return Response(ticket.intent.value['args'],{'u':1})
    runner=Interpreter(Broker(store,specs,{'emit':emit}))
    result=runner.run(plan,run,who,{'emit':grant})
    events=store.events(run,who)
    replay=runner.run(sdk,run,who,{'emit':grant})
    return {'result':result.data,'replay':replay.data,'calls':calls,'identical_ir':True,
            'invocations':sum(e['kind']=='InvocationBound' for e in events),
            'splits':sum(e['kind']=='BudgetSplit' for e in events),'joins':sum(e['kind']=='BudgetJoined' for e in events),
            'replay_added_events':len(store.events(run,who))-len(events),'budget':store.budget(run,who),
            'allocations':store.allocation_tree(run,who)}


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as directory:
        print(json.dumps(demo(directory),ensure_ascii=False,indent=2))
