"""Explicit terminal inference values across a task and an evidence boundary."""
import json
from pathlib import Path
import sqlite3
import tempfile
from contextlib import closing

from al0.data import Principal
from al0.execute import Broker, Interpreter, Response
from al0.frontend import compile_source
from al0.ir import Catalog, Tool
from al0.sdk import PlanBuilder
from al0.store import Store


def catalog():
    c=Catalog()
    c.register(Tool.define('model','infer','Text','Text','model',{'u':1}))
    c.register(Tool.define('verify','check','Text','Bool','check',{'u':1},predicate='valid'))
    c.register(Tool.define('send','action','Text','Text','output',{'u':1},predicates=['valid']))
    return c


def build(c):
    child=PlanBuilder('draft',c)
    prompt=child.parameter('prompt','Text')
    draft=child.finish(child.infer_result('result',child.tool('model'),prompt))
    ok=PlanBuilder('success',c)
    candidate=ok.parameter('result','Text',kind='Candidate')
    checked=ok.check('checked',ok.tool('verify'),candidate)
    intent=ok.prepare('request',ok.tool('send'),checked)
    operation=ok.admit('operation',intent,ok.authorize('permission',intent))
    state=ok.await_result('state',operation)
    success=ok.finish(ok.match_business('result-text',state,'Text',applied='Done',
        proven_no_effect='NoEffect',pending='Pending',unknown='Unknown',not_dispatched='Queued'))
    error=PlanBuilder('error',c)
    detail=error.parameter('result','InferenceError')
    failure=error.finish(error.field('code',detail,'code'))
    b=PlanBuilder('inference-results',c)
    prompt=b.literal('prompt','Text','draft message')
    group=b.scope('group',{'u':1})
    task=b.spawn('draft',group,draft,{'u':1},inputs={'prompt':prompt})
    outcome=b.join('outcome',task)
    b.close_scope('closed',group)
    return b.finish(b.match_variant('result',outcome,{'Success':success,'Error':failure}))


def demo(directory=None):
    if directory is None:
        with tempfile.TemporaryDirectory() as temp:return demo(temp)
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    c=catalog();plan=build(c)
    source=compile_source(Path(__file__).with_suffix('.al0').read_text(encoding='utf-8'),c,entry='inference-results').plan
    report={'identical_ir':plan.encoded==source.encoded,'cases':{}}
    for mode in ('Success','InvalidOutput','NoResult'):
        endpoint=directory/(mode+'-endpoint.sqlite')
        with closing(sqlite3.connect(endpoint)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS calls(attempt TEXT PRIMARY KEY, tool TEXT, cost INTEGER)')
        def adapter(ticket):
            name=ticket.intent.value['tool']
            with closing(sqlite3.connect(endpoint)) as db, db:db.execute('INSERT INTO calls VALUES(?,?,?)',(ticket.attempt,name,1))
            if name=='model':return Response(17 if mode=='InvalidOutput' else 'candidate',{'u':1},
                                             'ProvenNoEffect' if mode=='NoResult' else 'Applied')
            return Response(True if name=='verify' else 'sent',{'u':1})
        store=Store(directory/(mode+'-runtime.sqlite'))
        who=Principal('example','developer');broker=Broker(store,c,{name:adapter for name in c.tools})
        run=store.create_run(who,plan.fingerprint,{'u':10})
        grants={name:store.grant(run,name,spec.scope,expires=store.clock()+1000) for name,spec in c.tools.items()}
        value=Interpreter(broker).run(plan,run,who,grants);broker.wait_for_workers(timeout=10)
        before=len(store.events(run,who));again=Interpreter(broker).run(source,run,who,grants)
        broker.wait_for_workers(timeout=10)
        with closing(sqlite3.connect(endpoint)) as db:calls=db.execute('SELECT tool,SUM(cost) FROM calls GROUP BY tool ORDER BY tool').fetchall()
        report['cases'][mode]={'result':value.data,'replay':again.data,'calls':calls,
            'budget':store.budget(run,who),'replay_added_events':len(store.events(run,who))-before}
    return report


if __name__=='__main__':print(json.dumps(demo(),ensure_ascii=False,indent=2))
