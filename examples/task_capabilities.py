"""A child task narrows an explicit Cap and returns it for a parent action."""
import json
from pathlib import Path
import tempfile

from al0.data import Principal
from al0.execute import Broker, Interpreter, Response
from al0.frontend import compile_source
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.capability_calls import catalog, deliver


def narrower(specs):
    b=PlanBuilder('narrow',specs)
    permission=b.cap_parameter('permission',b.tool('send'))
    return b.finish(b.attenuate('reports',permission,max_attempts=1,constraints={'channel':'reports'}))


def build(specs):
    b=PlanBuilder('task-capabilities',specs)
    base=b.capability('base',b.tool('send'))
    group=b.scope('group',{})
    task=b.spawn_caps('task',group,narrower(specs),{},caps={'send':base},inputs={'permission':base})
    reports=b.join('reports',task)
    b.close_scope('closed',group)
    message=b.literal('message','Message',{'channel':'reports','text':'hello'})
    return b.finish(b.invoke_caps('result',deliver(specs),caps={'send':reports},inputs={'permission':reports,'message':message}))


def demo(directory):
    specs=catalog()
    plan=compile_source(Path(__file__).with_suffix('.al0').read_text(encoding='utf-8'),specs,entry='task-capabilities').plan
    sdk=build(specs)
    assert plan.encoded==sdk.encoded
    store=Store(Path(directory)/'runtime.sqlite')
    who=Principal('example','developer')
    run=store.create_run(who,plan.fingerprint,{'u':10})
    grants={name:store.grant(run,name,spec.scope,expires=store.clock()+1000,max_attempts=3) for name,spec in specs.tools.items()}
    calls=[]
    def send(ticket):
        calls.append(ticket.intent.value['args'])
        return Response({'sent':True},{'u':1})
    broker=Broker(store,specs,{name:send for name in specs.tools})
    runner=Interpreter(broker)
    result=runner.run(plan,run,who,grants)
    broker.wait_for_workers(timeout=10)
    events=store.events(run,who)
    replay=runner.run(sdk,run,who,grants)
    broker.wait_for_workers(timeout=10)
    with store._tx() as db:
        task=db.execute("SELECT id FROM tasks WHERE run=? AND kind='Task'",(run,)).fetchone()[0]
    cap=store.task_snapshot(task,who)['result']['data']
    return {'result':result.data,'replay':replay.data,'calls':calls,'identical_ir':True,
            'child_tools':sorted(store.bind_task_capabilities(task,who)),
            'returned_cap_attempts':store.capability_info(run,who,cap)['max_attempts'],
            'replay_added_events':len(store.events(run,who))-len(events),'budget':store.budget(run,who)}


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as directory:
        print(json.dumps(demo(directory),ensure_ascii=False,indent=2))
