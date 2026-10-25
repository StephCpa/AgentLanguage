"""Trusted refusal, ordinary output and blocked queued dispatch share one IR."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile

from al0.data import Principal
from al0.execute import Broker, Interpreter, Response, InferenceResponse, Value
from al0.frontend import compile_source
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.inference_results import catalog


def build(c):
    b=PlanBuilder('inference-provider',c);prompt=b.literal('prompt','Text','question')
    return b.finish(b.try_infer('result',b.tool('model'),prompt))


def demo(directory=None):
    if directory is None:
        with tempfile.TemporaryDirectory() as temp:return demo(temp)
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    c=catalog();plan=build(c)
    source=compile_source(Path(__file__).with_suffix('.al0').read_text(encoding='utf-8'),c,entry='inference-provider').plan
    report={'identical_ir':source.encoded==plan.encoded,'cases':{}}
    for mode in ('Output','Refused','Blocked'):
        endpoint=directory/(mode+'-endpoint.sqlite')
        with closing(sqlite3.connect(endpoint)) as db,db:db.execute('CREATE TABLE IF NOT EXISTS calls(attempt TEXT PRIMARY KEY,cost INTEGER)')
        def model(ticket):
            with closing(sqlite3.connect(endpoint)) as db,db:db.execute('INSERT INTO calls VALUES(?,1)',(ticket.attempt,))
            return InferenceResponse('Refused') if mode=='Refused' else InferenceResponse('Output','candidate',{'u':1})
        store=Store(directory/(mode+'-runtime.sqlite'));who=Principal('example','developer')
        broker=Broker(store,c,{'model':model,'verify':lambda t:Response(True,{'u':1}),'send':lambda t:Response('sent',{'u':1})})
        run=store.create_run(who,plan.fingerprint,{'u':10});grants={'model':store.grant(run,'model','model',expires=store.clock()+1000)}
        def fault(point):
            if mode=='Blocked' and point=='after_inference_admission_commit':store.revoke(grants['model'])
        store.fault=fault;result=Interpreter(broker).run(plan,run,who,grants)
        progress=Value.from_record(result.data['payload']);before_resolution=store.budget(run,who)
        with closing(sqlite3.connect(endpoint)) as db:calls=db.execute('SELECT attempt,cost FROM calls').fetchall()
        if mode=='Refused':store.settle(calls[0][0],who,provider='model',evidence_id='late-bill',actual={'u':1})
        if mode=='Blocked':
            operation=Value.from_record(progress.data['payload']).resource
            with store.resource_access(run,who,[operation]):store.cancel(operation.resource,who)
        before=len(store.events(run,who));again=Interpreter(broker).run(source,run,who,grants)
        report['cases'][mode]={'outer':result.data['tag'],'case':progress.data['tag'],
            'code':progress.data['payload']['data']['code'] if progress.data['tag']=='Error' else None,
            'calls':len(calls),'endpoint_cost':sum(r[1] for r in calls),'before_resolution':before_resolution,
            'budget':store.budget(run,who),'replay_equal':again==result,'replay_added_events':len(store.events(run,who))-before}
    return report


if __name__=='__main__':print(json.dumps(demo(),indent=2))
