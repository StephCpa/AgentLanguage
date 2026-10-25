"""A loop transfers its budget through a task on every funded model call."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile

from al0.data import Principal
from al0.execute import Broker, Interpreter, Response
from al0.frontend import compile_source
from al0.ir import Catalog, Tool
from al0.sdk import PlanBuilder
from al0.store import Store


def catalog():
    c=Catalog();c.register(Tool.define('model','infer','Int','Text','model',{'u':1}))
    return c


def build(c,count=3):
    b=PlanBuilder('sample',c)
    index=b.parameter('index','Int')
    sample=b.finish(b.infer_result('candidate',b.tool('model'),index))
    b=PlanBuilder('worker',c)
    funds=b.budget_parameter('funds');index=b.parameter('index','Int')
    b.invoke('sample',sample,inputs={'index':index},budget=funds)
    worker=b.finish(funds)
    b=PlanBuilder('step',c)
    index=b.parameter('index','Int');state=b.budget_parameter('state')
    scope=b.scope('scope',{})
    task=b.spawn('task',scope,worker,{},inputs={'funds':state,'index':index})
    result=b.join('returned',task);b.close_scope('closed',scope)
    step=b.finish(result)
    b=PlanBuilder('affine-loop',c)
    funds=b.split_budget('funds',{'u':5});n=b.literal('count','Int',count)
    returned=b.repeat('loop',n,5,funds,step)
    return b.finish(b.join_budget('released',returned))


def demo(directory=None):
    if directory is None:
        with tempfile.TemporaryDirectory() as temp:return demo(temp)
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    c=catalog();plan=build(c)
    source=compile_source(Path(__file__).with_suffix('.al0').read_text(encoding='utf-8'),c,entry='affine-loop').plan
    endpoint=directory/'endpoint.sqlite'
    with closing(sqlite3.connect(endpoint)) as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS calls(attempt TEXT PRIMARY KEY, iteration INTEGER, cost INTEGER)')
    def model(ticket):
        with closing(sqlite3.connect(endpoint)) as db,db:
            db.execute('INSERT INTO calls VALUES(?,?,?)',(ticket.attempt,ticket.intent.value['args'],1))
        return Response('candidate',{'u':1})
    store=Store(directory/'runtime.sqlite');who=Principal('example','developer');broker=Broker(store,c,{'model':model})
    run=store.create_run(who,plan.fingerprint,{'u':10})
    grants={'model':store.grant(run,'model','model',expires=store.clock()+1000)}
    result=Interpreter(broker).run(plan,run,who,grants);broker.wait_for_workers(timeout=10)
    before=len(store.events(run,who));again=Interpreter(broker).run(source,run,who,grants);broker.wait_for_workers(timeout=10)
    with closing(sqlite3.connect(endpoint)) as db:calls=db.execute('SELECT iteration,cost FROM calls ORDER BY iteration').fetchall()
    with closing(sqlite3.connect(store.path)) as db:
        receipt_steps=[r[0] for r in db.execute('SELECT step FROM loop_resource_receipts ORDER BY step')]
    return {'identical_ir':source.encoded==plan.encoded,'result':result.data,'replay':again.data,
            'calls':calls,'budget':store.budget(run,who),'receipt_steps':receipt_steps,
            'replay_added_events':len(store.events(run,who))-before}


if __name__=='__main__':print(json.dumps(demo(),indent=2))
