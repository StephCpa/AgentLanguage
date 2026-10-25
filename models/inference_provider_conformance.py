"""Extend generated Store schedules with refusal and unexposed blocked queues."""
from contextlib import closing
from itertools import permutations
import json
from pathlib import Path
import sqlite3
import tempfile

from al0.data import Principal
from al0.errors import StateConflict
from al0.execute import Broker, Interpreter, Response, InferenceResponse, Value
from al0.store import Store
from examples.inference_admission import build
from examples.inference_results import catalog
from models.inference_admission_conformance import run_trace


def blocked_trace(folder,schedule):
    folder.mkdir();endpoint=folder/'endpoint.sqlite'
    with closing(sqlite3.connect(endpoint)) as db,db:db.execute('CREATE TABLE calls(attempt TEXT PRIMARY KEY)')
    def model(ticket):
        with closing(sqlite3.connect(endpoint)) as db,db:db.execute('INSERT INTO calls VALUES(?)',(ticket.attempt,))
        return InferenceResponse('Refused',actual={'u':1})
    c=catalog();p=build(c);store=Store(folder/'runtime.sqlite');who=Principal('conformance','driver')
    broker=Broker(store,c,{'model':model,'verify':lambda t:Response(True,{'u':1}),'send':lambda t:Response('sent',{'u':1})})
    run=store.create_run(who,p.fingerprint,{'u':1});grants={'model':store.grant(run,'model','model',expires=store.clock()+1000)}
    def fault(point):
        if point=='after_inference_admission_commit':store.revoke(grants['model'])
    store.fault=fault;original=Interpreter(broker).run(p,run,who,grants);store.fault=lambda p:None
    progress=Value.from_record(original.data['payload']);source=Value.from_record(progress.data['payload']).resource
    held=source;cancelled=observed=False;observation=None;checks=0
    for action in schedule:
        denied=action=='observe' and cancelled;error=False
        try:
            if action=='cancel':
                with store.resource_access(run,who,[held]):store.cancel(source.resource,who)
                cancelled=True
            elif action=='observe':
                observation=broker.inference_progress(run,who,'next',source,c.tools['model'],grants,control=progress.label)
                assert observation.data['tag']=='Waiting'
                held=Value.from_record(observation.data['payload']).resource;observed=True
            else:assert Interpreter(broker).run(p,run,who,grants)==original
        except StateConflict:error=True
        assert error==denied,(schedule,action,'unexpected control outcome')
        snapshot=store.snapshot(source.resource,who);budget=store.budget(run,who)['u']
        assert (snapshot['business'],snapshot['cancel_requested'])==('ProvenNoEffect' if cancelled else 'NotDispatched',cancelled)
        assert (budget['free'],budget['reserved'],budget['confirmed'])==(int(cancelled),1-int(cancelled),0)
        with closing(sqlite3.connect(store.path)) as db:
            generation,state=db.execute("SELECT generation,state FROM resource_custody WHERE kind='Operation'").fetchone()
            decision=json.loads(db.execute('SELECT result FROM inference_admissions').fetchone()[0])
            assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==1
        assert (generation,state)==(1+int(observed),'Live') and decision['kind']=='Started'
        before=len(store.events(run,who));assert Interpreter(broker).run(p,run,who,grants)==original
        if observation is not None:
            assert broker.inference_progress(run,who,'next',source,c.tools['model'],grants,control=progress.label)==observation
        assert len(store.events(run,who))==before
        with closing(sqlite3.connect(endpoint)) as db:assert db.execute('SELECT COUNT(*) FROM calls').fetchone()[0]==0
        checks+=7
    return checks


def audit():
    refusal_schedules=list(permutations(('fact','bill','cancel','revoke','observe')))
    blocked_schedules=list(permutations(('observe','cancel','replay')))
    refusal_checks=blocked_checks=0
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp)
        for i,schedule in enumerate(refusal_schedules):refusal_checks+=run_trace(root/str(i),schedule,refused_output=True)
        for i,schedule in enumerate(blocked_schedules):blocked_checks+=blocked_trace(root/('blocked'+str(i)),schedule)
    return {'passed':True,'runtime_integrated':True,'refusal_schedules':len(refusal_schedules),
        'blocked_schedules':len(blocked_schedules),'steps':5*len(refusal_schedules)+3*len(blocked_schedules),
        'refusal_check_groups':refusal_checks,'blocked_check_groups':blocked_checks,
        'scope':'one pending call then late refusal/bill/revoke/cancel/observe, or one unexposed revoked queue then observe/cancel/replay',
        'not_covered':['arbitrary programs','multiple attempts','all task and race interleavings','all SQLite interleavings']}


if __name__=='__main__':
    report=audit();Path(__file__).with_suffix('.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))
