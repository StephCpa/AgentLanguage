"""Two checked child plans with identical node names and separate task slots."""
import json
from pathlib import Path
import tempfile
from threading import Barrier

from al0.concurrency import TaskScope
from al0.data import Principal
from al0.execute import Broker, Response
from al0.ir import Catalog, Tool
from al0.sdk import PlanBuilder
from al0.store import Store


def demo(directory):
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    store=Store(directory/'runtime.sqlite')
    specs=Catalog()
    specs.register(Tool.define('model','infer','Text','Text','model',{'units':1}))
    builder=PlanBuilder('child-plan',specs)
    request=builder.literal('request','Text','Summarize the local example.')
    plan=builder.finish(builder.infer('answer',builder.tool('model'),request))
    principal=Principal('demo','developer')
    run=store.create_run(principal,'parallel-host-controller',{'units':10})
    grants={'model':store.grant(run,'model','model',expires=store.clock()+1000)}
    barrier=Barrier(2)
    calls=[]

    def model(ticket):
        calls.append(ticket.operation)
        barrier.wait(timeout=5)
        return Response('Local summary',{'units':1})

    broker=Broker(store,specs,{'model':model})
    with TaskScope(broker,run,principal,'summaries',{'units':4},max_workers=2) as scope:
        left,lresult=scope.spawn('left',plan,{'units':2},grants)
        right,rresult=scope.spawn('right',plan,{'units':2},grants)
    store.finish_run(run,principal)
    count=len(store.events(run,principal))
    # Completed task values come from their durable results, even though both
    # plans use the same source node names and the scope has already exited.
    with TaskScope(broker,run,principal,'summaries',{'units':4}) as replay:
        replay.spawn('left',plan,{'units':2},grants)
        replay.spawn('right',plan,{'units':2},grants)
    return {'results':[lresult.result().data,rresult.result().data],
            'distinct_operations':len(set(calls)),
            'tasks':[{'state':store.task_snapshot(t,principal)['state'],
                      'disposition':store.task_snapshot(t,principal)['disposition']} for t in (left,right)],
            'status':store.status(run,principal)['status'],
            'budget':store.budget(run,principal),
            'replay_added_events':len(store.events(run,principal))-count}


if __name__=='__main__':
    with tempfile.TemporaryDirectory(prefix='al0-tasks-') as folder:
        print(json.dumps(demo(folder),ensure_ascii=False,indent=2))
