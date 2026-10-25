"""Transfer a model candidate to a checker task and branch on its outcome."""
import json
from pathlib import Path
import tempfile

from al0.data import Principal
from al0.demo import Endpoint, adapters, catalog
from al0.execute import Broker, Interpreter, Response
from al0.sdk import PlanBuilder
from al0.store import Store


def text_branch(specs,name,text):
    builder=PlanBuilder(name,specs)
    return builder.finish(builder.literal('message','Text',text))


def build(specs=None):
    specs=specs or catalog()
    checker=PlanBuilder('check-child',specs)
    patch=checker.parameter('patch','Patch',kind='Candidate')
    checked=checker.check('tested',checker.tool('patch-tests'),patch)
    child=checker.finish(checked)

    success=PlanBuilder('apply-checked-result',specs)
    result=success.parameter('result','Patch',kind='Checked',predicates=['TestsPassed'])
    intent=success.prepare('intent',success.tool('repo.merge'),result)
    permit=success.authorize('permit',intent)
    operation=success.admit('operation',intent,permit)
    status=success.await_result('status',operation)
    message=success.match_business('message',status,'Text',applied='Done',proven_no_effect='NeedsRevision',
                                   pending='Waiting',unknown='NeedsReconciliation',not_dispatched='Waiting')
    completed=success.finish(message)

    parent=PlanBuilder('task-outcomes',specs)
    request=parent.literal('request','Text','Fix the example at version 7.')
    draft=parent.infer('draft',parent.tool('code-model'),request)
    scope=parent.scope('checks',{'units':2})
    task=parent.spawn('checker',scope,child,{'units':1},inputs={'patch':draft})
    outcome=parent.join_outcome('outcome',task)
    parent.close_scope('closed',scope)
    answer=parent.match_task('answer',outcome,completed=completed,
                            failed=text_branch(specs,'failed','NeedsRevision'),
                            waiting=text_branch(specs,'waiting','NeedsReconciliation'),
                            cancelled=text_branch(specs,'cancelled','Cancelled'))
    return parent.finish(answer)


def demo(directory, *, mode='success'):
    folder=Path(directory)
    folder.mkdir(parents=True,exist_ok=True)
    store=Store(folder/'runtime.sqlite')
    endpoint=Endpoint(folder/'endpoint.sqlite')
    specs=catalog()
    plan=build(specs)
    principal=Principal('demo','developer')
    run=store.create_run(principal,plan.fingerprint,{'units':10})
    grants={name:store.grant(run,name,spec.scope,expires=store.clock()+1000) for name,spec in specs.tools.items()}
    callbacks=adapters(endpoint)
    if mode=='failed':
        callbacks['patch-tests']=lambda t:Response(False,{'units':1})
    elif mode=='waiting':
        def timeout(ticket):
            raise TimeoutError('checker reply lost')
        callbacks['patch-tests']=timeout
    elif mode!='success':
        raise ValueError('unknown demo mode')
    broker=Broker(store,specs,callbacks)
    executor=Interpreter(broker)
    result=executor.run(plan,run,principal,grants)
    broker.wait_for_workers(timeout=5)
    store.finish_run(run,principal)
    count=len(store.events(run,principal))
    replayed=executor.run(plan,run,principal,grants)
    broker.wait_for_workers(timeout=5)
    return {'mode':mode,'result':result.data,'replayed_result':replayed.data,
            'replay_added_events':len(store.events(run,principal))-count,
            'status':store.status(run,principal)['status'],'budget':store.budget(run,principal),
            'endpoint':endpoint.state()}


if __name__=='__main__':
    with tempfile.TemporaryDirectory(prefix='al0-outcomes-') as folder:
        print(json.dumps([demo(Path(folder)/mode,mode=mode) for mode in ('success','failed','waiting')],ensure_ascii=False,indent=2))
