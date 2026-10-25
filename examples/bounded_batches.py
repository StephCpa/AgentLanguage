"""Bounded effectful iterations with pure function calls and durable progress."""
import json
from pathlib import Path
import tempfile

from al0.data import Principal
from al0.execute import Broker, Interpreter, Response
from al0.frontend import compile_source
from al0.ir import Catalog, Tool
from al0.limits import ExecutionBudget
from al0.sdk import PlanBuilder
from al0.store import Store


def catalog():
    specs=Catalog()
    specs.register(Tool.define('emit','observe','Int','Int','output',{'u':1},shared_write=True))
    return specs


def build(specs):
    f=PlanBuilder('accumulate',specs,pure=True)
    value=f.parameter('value','Int')
    delta=f.parameter('delta','Int')
    function=f.finish(f.compute('next','add',value,delta))
    body=PlanBuilder('batch',specs)
    index=body.parameter('index','Int')
    state=body.parameter('state','Int')
    next_=body.call('next',function,inputs={'value':state,'delta':index})
    body.observe('sent',body.tool('emit'),next_)
    step=body.finish(next_)
    parent=PlanBuilder('bounded-batches',specs)
    count=parent.literal('count','Int',4)
    initial=parent.literal('initial','Int',0)
    return parent.finish(parent.repeat('total',count,10,initial,step))


def demo(directory):
    directory=Path(directory)
    specs=catalog()
    source=Path(__file__).with_suffix('.al0').read_text(encoding='utf-8')
    plan=compile_source(source,specs,entry='bounded-batches').plan
    sdk=build(specs)
    if plan.encoded!=sdk.encoded:
        raise AssertionError('source and SDK loop plans must be identical')
    store=Store(directory/'runtime.sqlite')
    who=Principal('example','developer')
    run=store.create_run(who,plan.fingerprint,{'u':10})
    grant=store.grant(run,'emit','output',expires=store.clock()+1000)
    calls=[]
    def emit(ticket):
        calls.append(ticket.intent.value['args'])
        return Response(ticket.intent.value['args'],{'u':1})
    broker=Broker(store,specs,{'emit':emit})
    execution=ExecutionBudget(1000)
    result=Interpreter(broker).run(plan,run,who,{'emit':grant},execution=execution)
    before=store.events(run,who)
    replay_budget=ExecutionBudget(10)
    replay=Interpreter(broker).run(sdk,run,who,{'emit':grant},execution=replay_budget)
    return {'result':result.data,'replay':replay.data,'emitted':calls,'identical_ir':True,
            'first_steps':execution.used,'replay_steps':replay_budget.used,
            'iterations':sum(e['kind']=='LoopAdvanced' for e in before),
            'replay_added_events':len(store.events(run,who))-len(before),'budget':store.budget(run,who)}


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as directory:
        print(json.dumps(demo(directory),ensure_ascii=False,indent=2))
