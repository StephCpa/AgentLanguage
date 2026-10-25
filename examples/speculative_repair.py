"""Checked speculative candidates; one persisted winner authorizes a write."""
import json
from pathlib import Path
import tempfile
from threading import Barrier

from al0.semantics import execution_site
from al0.data import Principal
from al0.demo import Endpoint, adapters, catalog
from al0.execute import Broker, Interpreter
from al0.sdk import PlanBuilder
from al0.store import Store


def build(specs=None):
    specs=specs or catalog()
    child=PlanBuilder('candidate',specs)
    request=child.literal('request','Text','Repair the local example.')
    draft=child.infer('draft',child.tool('code-model'),request)
    checked=child.check('checked',child.tool('patch-tests'),draft)
    candidate=child.finish(checked)
    parent=PlanBuilder('speculative-repair',specs)
    scope=parent.scope('candidates',{'units':6},speculative=True)
    left=parent.spawn('left',scope,candidate,{'units':3})
    right=parent.spawn('right',scope,candidate,{'units':3})
    selected=parent.race('winner',[left,right])
    parent.close_scope('close_candidates',scope)
    intent=parent.prepare('intent',parent.tool('repo.merge'),selected)
    permit=parent.authorize('permit',intent)
    operation=parent.admit('merge',intent,permit)
    status=parent.await_result('status',operation)
    result=parent.match_business('result',status,'Text',applied='Done',proven_no_effect='NeedsRevision',
                                 pending='Waiting',unknown='NeedsReconciliation',not_dispatched='Waiting')
    return parent.finish(result)


def demo(directory):
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
    checker=callbacks['patch-tests']
    barrier=Barrier(2)
    def check(ticket):
        barrier.wait(timeout=5)
        return checker(ticket)
    callbacks['patch-tests']=check
    broker=Broker(store,specs,callbacks)
    executor=Interpreter(broker)
    result=executor.run(plan,run,principal,grants)
    broker.wait_for_workers(timeout=10)
    events=store.events(run,principal)
    winner=next(e['data']['winner'] for e in events if e['kind']=='RaceSelected')
    replayed=executor.run(plan,run,principal,grants)
    broker.wait_for_workers(timeout=10)
    return {'result':result.data,'replayed_result':replayed.data,'winner':winner,
            'winner_after_replay':next(e['data']['winner'] for e in store.events(run,principal) if e['kind']=='RaceSelected'),
            'budget':store.budget(run,principal),'endpoint':endpoint.state(),
            'write_after_selection':next(e['sequence'] for e in events if e['kind']=='Admitted' and e['data']['node']==execution_site('merge')) > next(e['sequence'] for e in events if e['kind']=='RaceSelected'),
            'race_decisions':sum(e['kind']=='RaceSelected' for e in store.events(run,principal))}


if __name__=='__main__':
    with tempfile.TemporaryDirectory(prefix='al0-speculate-') as folder:
        print(json.dumps(demo(folder),ensure_ascii=False,indent=2))
