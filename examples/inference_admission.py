"""Durable local rejection versus an admitted model's progress."""
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
from examples.inference_results import catalog


def build(c):
    b=PlanBuilder('inference-admission',c)
    prompt=b.literal('prompt','Text','question')
    return b.finish(b.try_infer('result',b.tool('model'),prompt))


def demo(directory=None):
    if directory is None:
        with tempfile.TemporaryDirectory() as temp:return demo(temp)
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    c=catalog();plan=build(c)
    source=compile_source(Path(__file__).with_suffix('.al0').read_text(encoding='utf-8'),c,entry='inference-admission').plan
    report={'identical_ir':plan.encoded==source.encoded,'cases':{}}
    for mode in ('Success','InvalidOutput','Pending','BudgetExceeded','NoGrant','Revoked'):
        endpoint=directory/(mode+'-endpoint.sqlite')
        with closing(sqlite3.connect(endpoint)) as db,db:
            db.execute('CREATE TABLE IF NOT EXISTS calls(attempt TEXT PRIMARY KEY,cost INTEGER)')
        def model(ticket):
            with closing(sqlite3.connect(endpoint)) as db,db:db.execute('INSERT INTO calls VALUES(?,1)',(ticket.attempt,))
            return Response(None,None,'Pending') if mode=='Pending' else Response(17 if mode=='InvalidOutput' else 'candidate',{'u':1})
        store=Store(directory/(mode+'-runtime.sqlite'));who=Principal('example','developer')
        broker=Broker(store,c,{'model':model,'verify':lambda t:Response(True,{'u':1}),'send':lambda t:Response('sent',{'u':1})})
        run=store.create_run(who,plan.fingerprint,{'u':0 if mode=='BudgetExceeded' else 10})
        grants={} if mode=='NoGrant' else {'model':store.grant(run,'model','model',expires=store.clock()+1000)}
        if mode=='Revoked':store.revoke(grants['model'])
        result=Interpreter(broker).run(plan,run,who,grants)
        before=len(store.events(run,who));again=Interpreter(broker).run(source,run,who,grants)
        with closing(sqlite3.connect(endpoint)) as db:count,cost=db.execute('SELECT COUNT(*),COALESCE(SUM(cost),0) FROM calls').fetchone()
        with closing(sqlite3.connect(store.path)) as db:permits=db.execute('SELECT COUNT(*) FROM permits').fetchone()[0]
        payload=result.data['payload']['data']
        report['cases'][mode]={'case':result.data['tag'],'detail':payload.get('tag',payload.get('code')),
            'calls':count,'endpoint_cost':cost,'permits':permits,'budget':store.budget(run,who),
            'replay_equal':again==result,'replay_added_events':len(store.events(run,who))-before}
    return report


if __name__=='__main__':print(json.dumps(demo(),indent=2))
