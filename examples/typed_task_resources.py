"""Checked source/SDK task transfer with independent endpoint records."""
import json
from pathlib import Path
import sqlite3
import tempfile

from al0.data import Principal
from al0.execute import Broker, Interpreter, Response
from al0.flow import PUBLIC
from al0.frontend import compile_source
from al0.ir import Catalog, Tool
from al0.sdk import PlanBuilder
from al0.store import Store


def catalog():
    result=Catalog()
    result.register(Tool.define('send','action','Text','Text','output',{'u':1},output_label=PUBLIC))
    return result


def submit(specs):
    b=PlanBuilder('submit',specs)
    request=b.intent_parameter('request',b.tool('send'))
    permission=b.permit_parameter('permission',request)
    return b.finish(b.admit('operation',request,permission))


def funded(specs):
    b=PlanBuilder('funded',specs)
    funds=b.budget_parameter('funds')
    request=b.intent_parameter('request',b.tool('send'))
    permission=b.permit_parameter('permission',request)
    operation=b.invoke('operation',submit(specs),inputs={'request':request,'permission':permission},budget=funds)
    b.join_budget('released',funds)
    return b.finish(operation)


def build(specs):
    b=PlanBuilder('typed-task-resources',specs)
    message=b.literal('message','Text','hello')
    request=b.prepare('request',b.tool('send'),message)
    permission=b.authorize('permission',request)
    funds=b.split_budget('funds',{'u':2})
    group=b.scope('group',{})
    task=b.spawn('task',group,funded(specs),{},inputs={'funds':funds,'request':request,'permission':permission})
    operation=b.join('operation',task)
    b.close_scope('closed',group)
    state=b.await_result('state',operation)
    result=b.match_business('result',state,'Text',applied='Done',proven_no_effect='Rejected',
                            not_dispatched='Waiting',pending='Waiting',unknown='NeedsReconciliation')
    return b.finish(result)


def demo(directory):
    specs=catalog()
    sdk=build(specs)
    plan=compile_source(Path(__file__).with_suffix('.al0').read_text(encoding='utf-8'),specs,
                        entry='typed-task-resources').plan
    assert sdk.encoded==plan.encoded
    store=Store(Path(directory)/'runtime.sqlite')
    who=Principal('example','developer')
    run=store.create_run(who,plan.fingerprint,{'u':10})
    grant=store.grant(run,'send','output',expires=store.clock()+1000)
    endpoint=Path(directory)/'endpoint.sqlite'
    with sqlite3.connect(endpoint) as db:
        db.execute('CREATE TABLE calls(attempt TEXT PRIMARY KEY, body TEXT, fee INTEGER)')

    def send(ticket):
        with sqlite3.connect(endpoint) as db:
            db.execute('INSERT INTO calls VALUES(?,?,1)',(ticket.attempt,ticket.intent.value['args']))
        return Response('sent',{'u':1})

    broker=Broker(store,specs,{'send':send})
    runner=Interpreter(broker)
    try:
        result=runner.run(plan,run,who,{'send':grant})
        events=store.events(run,who)
        replay=runner.run(sdk,run,who,{'send':grant})
    finally:
        broker.wait_for_workers(timeout=10)
    with sqlite3.connect(endpoint) as db:
        calls,fees=db.execute('SELECT count(*),sum(fee) FROM calls').fetchone()
    return {'result':result.data,'replay':replay.data,'identical_ir':True,'provider_calls':calls,
            'provider_fees':fees,'budget':store.budget(run,who),
            'replay_added_events':len(store.events(run,who))-len(events),
            'resource_moves':sum(e['kind']=='TaskResourcesMoved' for e in events),
            'resource_returns':sum(e['kind']=='TaskResourcesReceived' for e in events)}


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as directory:
        print(json.dumps(demo(directory),ensure_ascii=False,indent=2))
