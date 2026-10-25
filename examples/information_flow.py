"""Local confidentiality/integrity example; no network or real secret data."""
import json
from pathlib import Path
import tempfile

from al0.data import Principal
from al0.errors import FlowDenied
from al0.execute import Broker, Interpreter, Response
from al0.flow import Label
from al0.ir import Catalog, Tool
from al0.sdk import PlanBuilder
from al0.store import Store


def build(specs, *, publish):
    b=PlanBuilder('checked-private-proposal',specs)
    source=b.literal('request','Text','synthetic private request')
    private=b.restrict('private',source,Label({'owner','tool:local-model','tool:local-check'},{'private-input'}))
    candidate=b.infer('draft',b.tool('local-model'),private)
    result=b.check('checked',b.tool('local-check'),candidate)
    if publish:
        intent=b.prepare('intent',b.tool('publish'),result)
        permit=b.authorize('permission',intent)
        result=b.admit('publish-operation',intent,permit)
    return b.finish(result)


def demo(directory):
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    store=Store(directory/'flow.sqlite')
    principal=Principal('example','owner')
    specs=Catalog()
    specs.register(Tool.define('local-model','infer','Text','Text','model',{'units':1}))
    specs.register(Tool.define('local-check','check','Text','Bool','check',{'units':1},predicate='Valid'))
    specs.register(Tool.define('publish','action','Text','Json','public',{'units':1},predicates=['Valid']))
    calls=[]
    def callback(ticket):
        tool=ticket.intent.value['tool']
        calls.append(tool)
        return Response(True if tool=='local-check' else 'validated proposal',{'units':1})
    broker=Broker(store,specs,{name:callback for name in specs.tools})
    output={}
    for publish in (False,True):
        plan=build(specs,publish=publish)
        run=store.create_run(principal,plan.fingerprint,{'units':5})
        grants={name:store.grant(run,name,spec.scope,expires=store.clock()+1000) for name,spec in specs.tools.items()}
        executor=Interpreter(broker)
        try:
            value=executor.run(plan,run,principal,grants)
        except FlowDenied as error:
            if not publish:
                raise
            output['public_action_rejected']=error.code
            output['blocked_run_budget']=store.budget(run,principal)['units']
        else:
            output['owner_delivery']=broker.deliver(value,'owner')
            output['label']=value.label.to_data()
            try:
                broker.deliver(value,'public-feed')
            except FlowDenied as error:
                output['public_export_rejected']=error.code
            before=len(store.events(run,principal))
            replay=executor.run(plan,run,principal,grants)
            output['replay_preserves_label']=value.label==replay.label
            output['replay_added_events']=len(store.events(run,principal))-before
    output['public_adapter_calls']=calls.count('publish')
    return output


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as directory:
        print(json.dumps(demo(directory),ensure_ascii=False,indent=2))
