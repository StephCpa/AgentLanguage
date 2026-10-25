"""One delayed model request, observed through task and loop transfers."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile

from al0.data import Principal
from al0.execute import Broker, Interpreter, Response
from al0.frontend import compile_source
from al0.inference_progress import progress_type
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.inference_results import catalog


def branches(c, *, task=True):
    interface=progress_type('model','Text');cases={}
    for tag,kind,schema in [('Success','Candidate','Text'),('Error','Data','InferenceError')]:
        b=PlanBuilder(tag.lower(),c)
        value=b.parameter('result',schema,kind=kind)
        cases[tag]=b.finish(b.variant('preserved',interface,tag,value))
    b=PlanBuilder('resume',c)
    operation=b.operation_parameter('result',b.tool('model'))
    resume=b.finish(b.resume_infer('progress',operation))
    if task:
        b=PlanBuilder('waiting',c)
        operation=b.operation_parameter('result',b.tool('model'))
        scope=b.scope('group',{})
        handle=b.spawn('worker',scope,resume,{},inputs={'result':operation})
        value=b.join('progress',handle);b.close_scope('closed',scope)
        cases['Waiting']=b.finish(value)
    else:cases['Waiting']=resume
    return cases


def build(c, count=3, *, task=True):
    b=PlanBuilder('step',c)
    b.parameter('index','Int');state=b.variant_parameter('state',progress_type('model','Text'))
    step=b.finish(b.match_variant('next',state,branches(c,task=task)))
    b=PlanBuilder('inference-progress',c)
    prompt=b.literal('prompt','Text','question')
    initial=b.infer_progress('initial',b.tool('model'),prompt)
    count_ref=b.literal('count','Int',count)
    return b.finish(b.repeat('loop',count_ref,5,initial,step))


def demo(directory=None):
    if directory is None:
        with tempfile.TemporaryDirectory() as temp:return demo(temp)
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    c=catalog();plan=build(c)
    source=compile_source(Path(__file__).with_suffix('.al0').read_text(encoding='utf-8'),c,entry='inference-progress').plan
    endpoint=directory/'endpoint.sqlite'
    with closing(sqlite3.connect(endpoint)) as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS calls(attempt TEXT PRIMARY KEY, operation TEXT, cost INTEGER)')
    def model(ticket):
        with closing(sqlite3.connect(endpoint)) as db,db:
            db.execute('INSERT INTO calls VALUES(?,?,?)',(ticket.attempt,ticket.operation,1))
        return Response(None,None,'Pending')
    store=Store(directory/'runtime.sqlite');who=Principal('example','developer')
    broker=Broker(store,c,{'model':model,'verify':lambda t:Response(True,{'u':1}),
                          'send':lambda t:Response('sent',{'u':1})})
    run=store.create_run(who,plan.fingerprint,{'u':10})
    grants={'model':store.grant(run,'model','model',expires=store.clock()+1000)}
    delivered=False
    def fault(point):
        nonlocal delivered
        if point=='after_inference_observation_commit' and not delivered:
            delivered=True
            with closing(sqlite3.connect(endpoint)) as db:attempt,operation,_=db.execute('SELECT * FROM calls').fetchone()
            store.business_result(operation,who,provider='model',evidence_id='late-output',outcome='Applied',details={'output':'candidate'})
    store.fault=fault
    result=Interpreter(broker).run(plan,run,who,grants);broker.wait_for_workers(timeout=10)
    reserved=store.budget(run,who)['u']['reserved']
    with closing(sqlite3.connect(endpoint)) as db:calls=db.execute('SELECT attempt,operation,cost FROM calls').fetchall()
    store.settle(calls[0][0],who,provider='model',evidence_id='late-bill',actual={'u':1})
    before=len(store.events(run,who))
    again=Interpreter(broker).run(source,run,who,grants);broker.wait_for_workers(timeout=10)
    with closing(sqlite3.connect(store.path)) as db:
        observations=[json.loads(row[0])['data']['tag'] for row in db.execute('SELECT result FROM inference_observations ORDER BY rowid')]
    return {'identical_ir':source.encoded==plan.encoded,'case':result.data['tag'],'replay_equal':again==result,
            'calls':len(calls),'endpoint_cost':sum(row[2] for row in calls),'reserved_before_bill':reserved,
            'budget':store.budget(run,who),'observations':observations,
            'replay_added_events':len(store.events(run,who))-before}


if __name__=='__main__':print(json.dumps(demo(),indent=2))
