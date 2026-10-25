"""Atomic mixed task results, including an unused Permit and its Intent."""
import json
from pathlib import Path
import sqlite3
import tempfile

from al0.data import Principal
from al0.execute import Broker, Interpreter, Response
from al0.frontend import compile_source
from al0.ir import Catalog
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.typed_task_resources import catalog as base_catalog, submit


def catalog():
    specs=base_catalog()
    specs.schema('Report',{'first':'Text','second':'Text','note':'Text'})
    return specs


def producer(specs):
    b=PlanBuilder('produce',specs)
    funds=b.budget_parameter('funds')
    request=b.intent_parameter('request',b.tool('send'))
    permission=b.permit_parameter('permission',request)
    first=b.prepare('first',b.tool('send'),b.literal('message','Text','first'))
    approved=b.authorize('approved',first)
    operation=b.admit('operation',first,approved)
    note=b.literal('note','Text','ready')
    return b.finish(b.pack('packet',{'funds':funds,'request':request,'permission':permission,'operation':operation,'note':note}))


def forwarder(specs):
    b=PlanBuilder('forward',specs)
    return b.finish(b.bundle_parameter('packet',producer(specs)))


def handled(b,name,operation):
    state=b.await_result(name+'-state',operation)
    return b.match_business(name,state,'Text',applied='Done',proven_no_effect='Rejected',
                            pending='Waiting',not_dispatched='Waiting',unknown='Unknown')


def build(specs):
    b=PlanBuilder('resource-bundles',specs)
    request=b.prepare('request',b.tool('send'),b.literal('message','Text','second'))
    permission=b.authorize('permission',request)
    funds=b.split_budget('funds',{'u':3})
    scope=b.scope('scope',{'u':1})
    task=b.spawn('task',scope,producer(specs),{'u':1},inputs={'funds':funds,'request':request,'permission':permission})
    packet=b.join('packet',task)
    relay=b.spawn('relay',scope,forwarder(specs),{},inputs={'packet':packet})
    returned=b.join('returned',relay)
    b.close_scope('closed',scope)
    opened=b.unpack('opened',returned)
    funds=b.take('returned-funds',opened,'funds')
    request=b.take('returned-request',opened,'request')
    permission=b.take('returned-permission',opened,'permission')
    operation=b.take('returned-operation',opened,'operation')
    note=b.take('returned-note',opened,'note')
    first=handled(b,'first',operation)
    second_op=b.invoke('second-operation',submit(specs),inputs={'request':request,'permission':permission},budget=funds)
    b.join_budget('released',funds)
    second=handled(b,'second',second_op)
    return b.finish(b.record('report','Report',{'first':first,'second':second,'note':note}))


def demo(directory):
    specs=catalog()
    sdk=build(specs)
    source=compile_source(Path(__file__).with_suffix('.al0').read_text(encoding='utf-8'),specs,entry='resource-bundles').plan
    assert source.encoded==sdk.encoded
    store=Store(Path(directory)/'runtime.sqlite')
    who=Principal('example','developer')
    run=store.create_run(who,source.fingerprint,{'u':10})
    grants={'send':store.grant(run,'send','output',expires=store.clock()+1000)}
    endpoint=Path(directory)/'endpoint.sqlite'
    with sqlite3.connect(endpoint) as db:
        db.execute('CREATE TABLE calls(attempt TEXT PRIMARY KEY, body TEXT, fee INTEGER)')
    def send(ticket):
        with sqlite3.connect(endpoint) as db:
            db.execute('INSERT INTO calls VALUES(?,?,1)',(ticket.attempt,ticket.intent.value['args']))
        return Response('sent',{'u':1})
    broker=Broker(store,specs,{'send':send})
    try:
        result=Interpreter(broker).run(source,run,who,grants)
        count=len(store.events(run,who))
        replay=Interpreter(broker).run(sdk,run,who,grants)
    finally:
        broker.wait_for_workers(timeout=10)
    with sqlite3.connect(endpoint) as db:
        calls=db.execute('SELECT body,fee FROM calls ORDER BY rowid').fetchall()
    return {'result':result.data,'replay':replay.data,'identical_ir':True,'provider_calls':calls,
            'budget':store.budget(run,who),'replay_added_events':len(store.events(run,who))-count}


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as directory:
        print(json.dumps(demo(directory),ensure_ascii=False,indent=2))
