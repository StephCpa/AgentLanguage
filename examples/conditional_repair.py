"""Data-dependent source and SDK branches using the same durable decision."""
import json
from pathlib import Path
import tempfile

from al0.data import Principal
from al0.demo import Endpoint, adapters, catalog
from al0.execute import Broker, Interpreter
from al0.frontend import compile_source
from al0.sdk import PlanBuilder
from al0.store import Store


def build(specs):
    apply=PlanBuilder('apply',specs)
    patch=apply.parameter('patch','Patch',kind='Checked',predicates=['TestsPassed'])
    intent=apply.prepare('intent',apply.tool('repo.merge'),patch)
    permit=apply.authorize('permit',intent)
    op=apply.admit('operation',intent,permit)
    state=apply.await_result('state',op)
    result=apply.match_business('answer',state,'Text',applied='Done',proven_no_effect='NeedsRevision',
                                not_dispatched='Waiting',pending='Waiting',unknown='NeedsReconciliation')
    yes=apply.finish(result)
    reject=PlanBuilder('reject',specs)
    no=reject.finish(reject.literal('answer','Text','UnexpectedBaseVersion'))
    parent=PlanBuilder('conditional-repair',specs)
    request=parent.literal('request','Text','Fix the example at version 7.')
    draft=parent.infer('draft',parent.tool('code-model'),request)
    tested=parent.check('tested',parent.tool('patch-tests'),draft)
    base=parent.field('base',tested,'base_version')
    expected=parent.literal('expected','Int',7)
    condition=parent.compute('allowed','eq',base,expected)
    return parent.finish(parent.if_else('answer',condition,then=yes,otherwise=no,inputs={'patch':tested}))


def demo(directory):
    directory=Path(directory)
    specs=catalog()
    source=Path(__file__).with_suffix('.al0').read_text(encoding='utf-8')
    plan=compile_source(source,specs,entry='conditional-repair').plan
    sdk=build(specs)
    if plan.encoded!=sdk.encoded:
        raise AssertionError('source and SDK must have identical branch IR')
    store=Store(directory/'runtime.sqlite')
    endpoint=Endpoint(directory/'endpoint.sqlite')
    who=Principal('example','developer')
    run=store.create_run(who,plan.fingerprint,{'units':10})
    grants={n:store.grant(run,n,s.scope,expires=store.clock()+1000) for n,s in specs.tools.items()}
    executor=Interpreter(Broker(store,specs,adapters(endpoint)))
    result=executor.run(plan,run,who,grants)
    before=store.events(run,who)
    replay=executor.run(sdk,run,who,grants)
    return {'result':result.data,'replayed':replay.data,'identical_ir':True,
            'branch_decisions':sum(e['kind']=='BranchChosen' for e in before),
            'replay_added_events':len(store.events(run,who))-len(before),
            'budget':store.budget(run,who),'endpoint':endpoint.state()}


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as folder:
        print(json.dumps(demo(folder),ensure_ascii=False,indent=2))
