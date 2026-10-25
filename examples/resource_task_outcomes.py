"""Resource TaskOutcome branches and atomic resource race winners."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile

from al0.data import Principal
from al0.execute import Broker, Interpreter, Response
from al0.frontend import compile_source
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.resource_bundles import catalog, producer, handled
from examples.typed_task_resources import submit


def completed(specs):
    b=PlanBuilder('completed',specs)
    result=b.bundle_parameter('result',producer(specs))
    opened=b.unpack('opened',result)
    funds=b.take('funds',opened,'funds')
    request=b.take('request',opened,'request')
    permission=b.take('permission',opened,'permission')
    operation=b.take('operation',opened,'operation')
    note=b.take('note',opened,'note')
    first=handled(b,'first',operation)
    second=b.invoke('second-operation',submit(specs),inputs={'request':request,'permission':permission},budget=funds)
    b.join_budget('released',funds)
    second=handled(b,'second',second)
    return b.finish(b.record('report','Report',{'first':first,'second':second,'note':note}))


def fallback(specs):
    b=PlanBuilder('fallback',specs)
    return b.finish(b.literal('report','Report',{'first':'Unavailable','second':'Unavailable','note':'Runtime owns remaining responsibility'}))


def build(specs):
    b=PlanBuilder('resource-outcomes',specs)
    request=b.prepare('request',b.tool('send'),b.literal('message','Text','second'))
    permission=b.authorize('permission',request)
    funds=b.split_budget('funds',{'u':3})
    scope=b.scope('scope',{'u':1})
    task=b.spawn('task',scope,producer(specs),{'u':1},inputs={'funds':funds,'request':request,'permission':permission})
    outcome=b.join_outcome('outcome',task)
    b.close_scope('closed',scope)
    other=fallback(specs)
    return b.finish(b.match_task('report',outcome,completed=completed(specs),failed=other,waiting=other,cancelled=other))


def candidate(specs):
    b=PlanBuilder('candidate',specs)
    funds=b.budget_parameter('funds')
    note=b.parameter('note','Text')
    inner=b.pack('inner',{'funds':funds,'note':note})
    return b.finish(b.pack('packet',{'inner':inner}))


def race_plan(specs):
    b=PlanBuilder('resource-race',specs)
    scope=b.scope('scope',{},speculative=True)
    first=b.split_budget('first-funds',{'u':2})
    second=b.split_budget('second-funds',{'u':2})
    a=b.spawn('first',scope,candidate(specs),{},inputs={'funds':first,'note':b.literal('a','Text','first')})
    z=b.spawn('second',scope,candidate(specs),{},inputs={'funds':second,'note':b.literal('z','Text','second')})
    packet=b.race('packet',[a,z])
    b.close_scope('closed',scope)
    outer=b.unpack('outer',packet)
    inner=b.unpack('inner',b.take('member',outer,'inner'))
    funds=b.take('funds',inner,'funds')
    note=b.take('note',inner,'note')
    b.join_budget('released',funds)
    return b.finish(note)


def demo(directory):
    specs=catalog()
    store=Store(Path(directory)/'runtime.sqlite')
    who=Principal('example','developer')
    endpoint=Path(directory)/'endpoint.sqlite'
    with closing(sqlite3.connect(endpoint)) as db, db:
        db.execute('CREATE TABLE calls(attempt TEXT PRIMARY KEY, body TEXT, fee INTEGER)')
    def send(ticket):
        with closing(sqlite3.connect(endpoint)) as db, db:
            db.execute('INSERT INTO calls VALUES(?,?,1)',(ticket.attempt,ticket.intent.value['args']))
        return Response('sent',{'u':1})
    broker=Broker(store,specs,{'send':send})
    reports={}
    try:
        for entry,sdk in [('resource-outcomes',build(specs)),('resource-race',race_plan(specs))]:
            source=compile_source(Path(__file__).with_suffix('.al0').read_text(encoding='utf-8'),specs,entry=entry).plan
            assert source.encoded==sdk.encoded
            run=store.create_run(who,sdk.fingerprint,{'u':10})
            grants={'send':store.grant(run,'send','output',expires=store.clock()+1000)}
            result=Interpreter(broker).run(source,run,who,grants)
            broker.wait_for_workers(timeout=10)
            count=len(store.events(run,who))
            replay=Interpreter(broker).run(sdk,run,who,grants)
            broker.wait_for_workers(timeout=10)
            reports[entry]={'result':result.data,'replay':replay.data,'identical_ir':True,
                            'budget':store.budget(run,who),'replay_added_events':len(store.events(run,who))-count}
    finally:
        broker.wait_for_workers(timeout=10)
    with closing(sqlite3.connect(endpoint)) as db, db:
        reports['provider_calls']=db.execute('SELECT body,fee FROM calls ORDER BY rowid').fetchall()
    return reports


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as directory:
        print(json.dumps(demo(directory),ensure_ascii=False,indent=2))
