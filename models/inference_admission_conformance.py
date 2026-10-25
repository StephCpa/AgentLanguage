"""Compare a small observation/fee model with 120 real Store schedules.

Scope: one accepted pending call, one subsequent observation, one each of
business receipt, bill, cancellation and revocation. This is not a model of
all language execution or task custody. Endpoint records are independent SQL.
"""
from contextlib import closing
from dataclasses import dataclass, replace
from itertools import permutations
import json
from pathlib import Path
import sqlite3
import tempfile

from al0.data import Principal
from al0.errors import ReplayMismatch, StateConflict
from al0.execute import Broker, Interpreter, Response, InferenceResponse, Value
from al0.store import Store
from examples.inference_admission import build
from examples.inference_results import catalog


@dataclass(frozen=True)
class Expected:
    applied: bool=False
    paid: bool=False
    cancelled: bool=False
    observed: bool=False
    terminal: bool=False

    def step(self, action):
        if action=='fact':return replace(self,applied=True),False
        if action=='bill':return replace(self,paid=True),False
        if action=='revoke':return self,False
        if action=='cancel':return (self,True) if self.terminal else (replace(self,cancelled=True),False)
        if action=='observe':return (self,True) if self.cancelled else (replace(self,observed=True,terminal=self.applied),False)
        raise AssertionError(action)


def run_trace(folder, schedule, *, refused_output=False):
    folder.mkdir();endpoint=folder/'endpoint.sqlite'
    with closing(sqlite3.connect(endpoint)) as db,db:
        db.execute('CREATE TABLE calls(attempt TEXT PRIMARY KEY,operation TEXT,cost INTEGER)')
        db.execute('CREATE TABLE receipts(kind TEXT PRIMARY KEY)')
    def model(ticket):
        with closing(sqlite3.connect(endpoint)) as db,db:db.execute('INSERT INTO calls VALUES(?,?,1)',(ticket.attempt,ticket.operation))
        return Response(None,None,'Pending')
    c=catalog();p=build(c);store=Store(folder/'runtime.sqlite');who=Principal('conformance','driver')
    broker=Broker(store,c,{'model':model,'verify':lambda t:Response(True,{'u':1}),'send':lambda t:Response('sent',{'u':1})})
    run=store.create_run(who,p.fingerprint,{'u':1});grants={'model':store.grant(run,'model','model',expires=store.clock()+1000)}
    original=Interpreter(broker).run(p,run,who,grants)
    progress=Value.from_record(original.data['payload']);source=Value.from_record(progress.data['payload']).resource
    held=source;observation=None;expected=Expected();checks=0
    with closing(sqlite3.connect(endpoint)) as db:attempt,operation,cost=db.execute('SELECT * FROM calls').fetchone()
    assert cost==1 and original.data['tag']=='Started' and progress.data['tag']=='Waiting'
    for action in schedule:
        expected,denied=expected.step(action);error=False
        try:
            if action=='fact':
                with closing(sqlite3.connect(endpoint)) as db,db:db.execute("INSERT INTO receipts VALUES('business')")
                details=InferenceResponse('Refused').details() if refused_output else {'output':'candidate'}
                store.business_result(operation,who,provider='model',evidence_id='late-output',outcome='Applied',details=details)
            elif action=='bill':
                with closing(sqlite3.connect(endpoint)) as db,db:db.execute("INSERT INTO receipts VALUES('billing')")
                store.settle(attempt,who,provider='model',evidence_id='late-bill',actual={'u':1})
            elif action=='revoke':store.revoke(grants['model'])
            elif action=='cancel':
                with store.resource_access(run,who,[held]):store.cancel(operation,who)
            elif action=='observe':
                observation=broker.inference_progress(run,who,'next',source,c.tools['model'],grants,control=progress.label)
                if observation.data['tag']=='Waiting':held=Value.from_record(observation.data['payload']).resource
        except StateConflict:error=True
        assert error==denied,(schedule,action,'unexpected control outcome')
        snapshot=store.snapshot(operation,who)
        assert snapshot['business']==('Applied' if expected.applied else 'Pending'),(schedule,action,'business')
        assert snapshot['cancel_requested']==expected.cancelled,(schedule,action,'cancellation')
        budget=store.budget(run,who)['u']
        assert (budget['free'],budget['reserved'],budget['confirmed'])==(0,1-int(expected.paid),int(expected.paid)),(schedule,action,'fees')
        with closing(sqlite3.connect(store.path)) as db:
            generation,state=db.execute("SELECT generation,state FROM resource_custody WHERE kind='Operation'").fetchone()
            decision=json.loads(db.execute('SELECT result FROM inference_admissions').fetchone()[0])
            records=db.execute('SELECT COUNT(*) FROM inference_observations').fetchone()[0]
        assert (generation,state)==(1+int(expected.observed),'Spent' if expected.terminal else 'Live'),(schedule,action,'custody')
        assert decision['kind']=='Started' and records==1+int(expected.observed),(schedule,action,'fixed records')
        before=len(store.events(run,who));assert Interpreter(broker).run(p,run,who,grants)==original
        if observation is not None:
            assert observation.data['tag']==(('Error' if refused_output else 'Success') if expected.terminal else 'Waiting')
            if expected.terminal and refused_output:assert observation.data['payload']['data']['code']=='Refused'
            assert broker.inference_progress(run,who,'next',source,c.tools['model'],grants,control=progress.label)==observation
        assert len(store.events(run,who))==before,(schedule,action,'replay events')
        with closing(sqlite3.connect(endpoint)) as db:assert db.execute('SELECT COUNT(*) FROM calls').fetchone()[0]==1
        checks+=9+int(observation is not None)
    try:broker.inference_progress(run,who,'stale-alias',source,c.tools['model'],grants,control=progress.label)
    except StateConflict:checks+=1
    else:raise AssertionError((schedule,'old control alias was accepted'))
    return checks


def rejected_traces(folder):
    cases=[]
    for authorized,funded in ((False,False),(False,True),(True,False)):
        c=catalog();p=build(c);store=Store(folder/f'reject-{authorized}-{funded}.sqlite');who=Principal('conformance','driver');calls=[]
        def model(ticket):calls.append(ticket.attempt);return Response('candidate',{'u':1})
        broker=Broker(store,c,{'model':model,'verify':lambda t:Response(True,{'u':1}),'send':lambda t:Response('sent',{'u':1})})
        run=store.create_run(who,p.fingerprint,{'u':1})
        held=None if funded else store.split_budget(run,who,'held',{'u':1})
        grants={'model':store.grant(run,'model','model',expires=store.clock()+1000)} if authorized else {}
        original=Interpreter(broker).run(p,run,who,grants)
        assert original.data['tag']=='Rejected' and not calls
        if held is not None:store.join_budget(run,who,held)
        assert Interpreter(broker).run(p,run,who,grants)==original
        if not authorized:
            grants={'model':store.grant(run,'model','model',expires=store.clock()+1000)}
            try:Interpreter(broker).run(p,run,who,grants)
            except ReplayMismatch:pass
            else:raise AssertionError('new authorization silently reopened rejection')
        with store.resource_access(run,who):
            fresh=broker.try_infer(run,who,'explicit-new-position',Value('question'),c.tools['model'],grants)
        assert fresh.data['tag']=='Started' and len(calls)==1
        with closing(sqlite3.connect(store.path)) as db:
            assert db.execute('SELECT COUNT(*) FROM permits').fetchone()[0]==1
            assert db.execute('SELECT COUNT(*) FROM inference_admissions').fetchone()[0]==2
        cases.append({'initial_authorized':authorized,'initial_funded':funded,'first':'Rejected','new_position':'Started'})
    return cases


def audit():
    checks=0;schedules=list(permutations(('fact','bill','cancel','revoke','observe')))
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp)
        for i,schedule in enumerate(schedules):checks+=run_trace(root/str(i),schedule)
        rejected=rejected_traces(root)
    return {'passed':True,'runtime_integrated':True,'schedules':len(schedules),'steps':5*len(schedules),
            'state_and_replay_checks':checks,'rejection_traces':rejected,
            'scope':'one pending admitted call; permutations of one late business fact, bill, revoke, cancel and observation; fixed replay after each step',
            'not_covered':['arbitrary programs','task transfer and race','provider refusal','multiple attempts','all SQLite execution interleavings']}


if __name__=='__main__':
    report=audit();Path(__file__).with_suffix('.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))
