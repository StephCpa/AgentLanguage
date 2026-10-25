"""Computed resource vectors fund a scope, child task and local allocation."""
import json
from pathlib import Path
import tempfile

from al0.data import Principal
from al0.execute import Broker, Interpreter, Response, Value
from al0.frontend import compile_source
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.budget_workflows import catalog, emitter


def build(specs):
    b=PlanBuilder('allocation',specs,pure=True)
    units=b.parameter('units','Int')
    one=b.literal('one','Int',1)
    total=b.compute('total','add',units,one)
    allocation=b.finish(b.resources('amount',{'u':total}))
    b=PlanBuilder('worker',specs)
    message=b.parameter('message','Text')
    amount=b.parameter('amount','Json')
    funds=b.split_budget('funds',amount)
    value=b.invoke('value',emitter(specs),inputs={'message':message},budget=funds)
    b.join_budget('released',funds)
    worker=b.finish(value)
    b=PlanBuilder('dynamic-budgets',specs)
    units=b.parameter('units','Int')
    amount=b.call('amount',allocation,inputs={'units':units})
    message=b.literal('message','Text','hello')
    group=b.scope('group',amount)
    task=b.spawn('task',group,worker,amount,inputs={'message':message,'amount':amount})
    result=b.join('result',task)
    b.close_scope('closed',group)
    return b.finish(result)


def demo(directory):
    specs=catalog()
    plan=compile_source(Path(__file__).with_suffix('.al0').read_text(encoding='utf-8'),specs,entry='dynamic-budgets').plan
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
    broker=Broker(store,specs,{'emit':emit})
    runner=Interpreter(broker)
    result=runner.run(plan,run,who,{'emit':grant},inputs={'units':Value(2)})
    broker.wait_for_workers(timeout=10)
    events=store.events(run,who)
    replay=runner.run(sdk,run,who,{'emit':grant},inputs={'units':Value(2)})
    broker.wait_for_workers(timeout=10)
    return {'result':result.data,'replay':replay.data,'calls':calls,'identical_ir':True,
            'evaluations':sum(e['kind']=='BudgetAmountBound' for e in events),
            'transfers':[row['initial_transfer'] for row in store.allocation_tree(run,who)[1:]],
            'replay_added_events':len(store.events(run,who))-len(events),'budget':store.budget(run,who)}


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as directory:
        print(json.dumps(demo(directory),ensure_ascii=False,indent=2))
