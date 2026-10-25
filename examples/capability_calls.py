"""Attenuated tool capabilities and a closed workflow authority boundary."""
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
    specs.schema('Message',{'channel':'Text','text':'Text'})
    specs.register(Tool.define('send','action','Message','Json','outbox',{'u':1},retry_window=60))
    specs.register(Tool.define('admin','action','Message','Json','admin',{'u':1}))
    return specs


def handled(b,operation):
    state=b.await_result('state',operation)
    return b.match_business('result',state,'Text',applied='Done',proven_no_effect='Rejected',not_dispatched='Waiting',
                            pending='Waiting',unknown='NeedsReconciliation')


def deliver(specs):
    b=PlanBuilder('deliver',specs)
    cap=b.cap_parameter('permission',b.tool('send'))
    message=b.parameter('message','Message')
    intent=b.prepare('request',b.tool('send'),message)
    permit=b.authorize_with('permit',intent,cap)
    return b.finish(handled(b,b.admit('operation',intent,permit)))


def build(specs):
    child=deliver(specs)
    b=PlanBuilder('capability-calls',specs)
    base=b.capability('base',b.tool('send'))
    reports=b.attenuate('reports',base,max_attempts=1,constraints={'channel':'reports'})
    message=b.literal('message','Message',{'channel':'reports','text':'hello'})
    return b.finish(b.invoke_caps('result',child,caps={'send':reports},inputs={'permission':reports,'message':message}))


def demo(directory):
    specs=catalog()
    plan=compile_source(Path(__file__).with_suffix('.al0').read_text(encoding='utf-8'),specs,entry='capability-calls').plan
    sdk=build(specs)
    assert plan.encoded==sdk.encoded
    store=Store(Path(directory)/'runtime.sqlite')
    who=Principal('example','developer')
    run=store.create_run(who,plan.fingerprint,{'u':10})
    grants={name:store.grant(run,name,spec.scope,expires=store.clock()+1000,max_attempts=3) for name,spec in specs.tools.items()}
    calls=[]
    def send(ticket):
        calls.append({'tool':ticket.intent.value['tool'],'message':ticket.intent.value['args']})
        return Response({'sent':True},{'u':1})
    runner=Interpreter(Broker(store,specs,{name:send for name in specs.tools}))
    result=runner.run(plan,run,who,grants)
    before=store.events(run,who)
    replay=runner.run(sdk,run,who,grants)
    with store._tx() as db:
        child=db.execute('SELECT g.* FROM grants g JOIN grant_derivations d ON g.id=d.child WHERE d.run=?',(run,)).fetchone()
    return {'result':result.data,'replay':replay.data,'calls':calls,'identical_ir':True,
            'derived_max_attempts':child['max_attempts'],'capability_bindings':sum(e['kind']=='CapabilityBound' for e in before),
            'derivations':sum(e['kind']=='CapabilityAttenuated' for e in before),
            'replay_added_events':len(store.events(run,who))-len(before),'budget':store.budget(run,who)}


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as directory:
        print(json.dumps(demo(directory),ensure_ascii=False,indent=2))
