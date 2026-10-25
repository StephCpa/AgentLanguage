from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from al0.custody import ResourceRef
from al0.data import Principal
from al0.errors import AuthorizationDenied, BudgetExceeded, ContractError, ReplayMismatch, StateConflict, TypeCheckError
from al0.execute import Broker, Interpreter, Response, Value
from al0.inference_progress import progress_type
from al0.ir import Type, Tool, check_plan
from al0.flow import Label
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.inference_results import catalog
from examples.inference_progress import branches, build, demo
from tests.progress_worker import plan


class InferenceProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'store.sqlite';self.store=Store(self.path)
        self.who=Principal('tests','developer');self.c=catalog();self.calls=[]
        self.response=Response('candidate',{'u':1})
        def model(ticket):
            self.calls.append(ticket)
            if isinstance(self.response,Exception):raise self.response
            return self.response
        self.broker=Broker(self.store,self.c,{'model':model,'verify':lambda t:Response(True,{'u':1}),
                                            'send':lambda t:Response('sent',{'u':1})})
        self.addCleanup(lambda:self.broker.wait_for_workers(timeout=10))

    def start(self,p=None,amount=10):
        self.p=plan(self.c) if p is None else p
        self.run=self.store.create_run(self.who,self.p.fingerprint,{'u':amount})
        self.grants={n:self.store.grant(self.run,n,s.scope,expires=self.store.clock()+1000) for n,s in self.c.tools.items()}

    def execute(self):return Interpreter(self.broker).run(self.p,self.run,self.who,self.grants)
    def budget(self):return self.store.budget(self.run,self.who)['u']
    def rows(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory=sqlite3.Row
            return [dict(r) for r in db.execute('SELECT * FROM inference_observations ORDER BY rowid')]
    def resume(self,value,node='next',spec=None,grants=None):
        return self.broker.inference_progress(self.run,self.who,node,Value.from_record(value.data['payload']).resource,
            self.c.tools['model'] if spec is None else spec,self.grants if grants is None else grants,control=value.label)
    def fact(self,output='late',outcome='Applied'):
        ticket=self.calls[0]
        self.store.business_result(ticket.operation,self.who,provider='model',evidence_id='late',outcome=outcome,details={'output':output})
    def custody(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT generation,state,owner_kind FROM resource_custody WHERE kind='Operation'").fetchone()

    def test_source_sdk_task_loop_late_bill(self):
        report=demo(Path(self.temp.name)/'demo')
        self.assertTrue(report['identical_ir']);self.assertTrue(report['replay_equal'])
        self.assertEqual(report['observations'],['Waiting','Success'])
        self.assertEqual(report['calls'],1);self.assertEqual(report['endpoint_cost'],1)
        self.assertEqual(report['reserved_before_bill'],1);self.assertEqual(report['replay_added_events'],0)

    def test_success_closes_control_and_preserves_candidate(self):
        self.start();result=self.execute()
        self.assertEqual(dict(result.variant.members)['Success'],Type('Candidate','Text'))
        self.assertEqual(result.data['tag'],'Success');self.assertEqual(self.custody(),(1,'Spent','Runtime'))
        before=len(self.store.events(self.run,self.who));self.assertEqual(self.execute(),result)
        self.assertEqual(len(self.store.events(self.run,self.who)),before);self.assertEqual(len(self.calls),1)

    def test_invalid_output_is_billed_error(self):
        self.response=Response({'secret':'bad'},{'u':1});self.start();result=self.execute()
        self.assertEqual(result.data['payload']['data']['code'],'InvalidOutput')
        self.assertNotIn('secret',json.dumps(result.record()));self.assertEqual(self.budget()['confirmed'],1)
        self.assertEqual(self.custody()[1],'Spent')

    def test_no_effect_is_not_free(self):
        self.response=Response(None,{'u':1},'ProvenNoEffect');self.start();result=self.execute()
        self.assertEqual(result.data['payload']['data']['code'],'NoResult');self.assertEqual(self.budget()['confirmed'],1)

    def test_terminal_late_bill(self):
        self.response=Response('candidate',None);self.start();result=self.execute()
        self.assertEqual(self.budget()['reserved'],1)
        self.store.settle(self.calls[0].attempt,self.who,provider='model',evidence_id='bill',actual={'u':1})
        self.assertEqual(self.execute(),result);self.assertEqual(self.budget()['confirmed'],1)

    def waiting(self,response):
        self.response=response;self.start();result=self.execute()
        self.assertEqual(result.data['tag'],'Waiting');self.assertEqual(self.custody(),(1,'Live','Root'))
        self.assertEqual(self.execute(),result);self.assertEqual(len(self.calls),1)
        self.assertEqual(self.budget()['reserved'],1)
        return result
    def test_timeout_returns_waiting(self):self.waiting(TimeoutError())
    def test_connection_returns_waiting(self):self.waiting(ConnectionError())
    def test_pending_returns_waiting(self):self.waiting(Response(None,None,'Pending'))

    def test_history_fixed_after_late_success(self):
        old=self.waiting(TimeoutError());self.fact()
        self.assertEqual(self.execute(),old)
        new=self.resume(old);self.assertEqual(new.data['tag'],'Success')
        self.assertEqual(new.data['payload']['data'],'late');self.assertEqual(self.custody(),(2,'Spent','Runtime'))
        self.assertEqual(self.resume(old),new);self.assertEqual(self.execute(),old);self.assertEqual(len(self.calls),1)

    def test_next_waiting_rotates_and_rejects_old_alias(self):
        old=self.waiting(TimeoutError());new=self.resume(old)
        self.assertEqual(Value.from_record(new.data['payload']).resource.generation,2)
        self.assertEqual(self.resume(old),new)
        with self.assertRaises(StateConflict):self.resume(old,'another')
        self.assertEqual(len(self.calls),1)

    def test_changed_observation_source_rejected(self):
        old=self.waiting(TimeoutError());new=self.resume(old)
        with self.assertRaises(ReplayMismatch):self.resume(new)

    def test_late_invalid_output(self):
        old=self.waiting(TimeoutError());self.fact(17)
        self.assertEqual(self.resume(old).data['payload']['data']['code'],'InvalidOutput')

    def test_late_no_effect(self):
        old=self.waiting(TimeoutError());self.fact(None,'ProvenNoEffect')
        self.assertEqual(self.resume(old).data['payload']['data']['code'],'NoResult')

    def test_waiting_task_loop_no_new_calls(self):
        self.response=TimeoutError();self.start(build(self.c));result=self.execute()
        self.broker.wait_for_workers(timeout=10)
        self.assertEqual(result.data['tag'],'Waiting');self.assertEqual(len(self.rows()),4)
        self.assertEqual(len(self.calls),1);self.assertEqual(self.execute(),result)

    def test_terminal_empty_affine_loop(self):
        self.start(build(self.c));result=self.execute()
        self.assertEqual(result.data['tag'],'Success');self.assertEqual(len(self.rows()),1)
        self.assertEqual(self.execute(),result)

    def test_no_capability_cannot_observe_new_site(self):
        old=self.waiting(TimeoutError())
        with self.assertRaises(AuthorizationDenied):self.resume(old,grants={})
        self.assertEqual(self.custody()[0],1)

    def test_action_spec_cannot_resume_model(self):
        old=self.waiting(TimeoutError())
        with self.assertRaises(ContractError):self.resume(old,spec=self.c.tools['send'])

    def test_internal_error_is_not_error_value(self):
        self.response=RuntimeError('bug');self.start()
        with self.assertRaises(RuntimeError):self.execute()
        self.assertEqual(self.rows(),[])

    def test_bad_adapter_contract_is_not_error_value(self):
        self.response={};self.start()
        with self.assertRaises(ContractError):self.execute()
        self.assertEqual(self.rows(),[])

    def test_pre_admission_budget_is_not_swallowed(self):
        self.start(amount=0)
        with self.assertRaises(BudgetExceeded):self.execute()
        self.assertEqual(self.rows(),[]);self.assertEqual(self.calls,[])

    def test_pre_admission_authorization_is_not_swallowed(self):
        self.start();self.grants={}
        with self.assertRaises(AuthorizationDenied):self.execute()
        self.assertEqual(self.calls,[])

    def test_model_operation_only(self):
        for kind in ('action','opref','data'):
            b=PlanBuilder('wrong',self.c)
            ref=(b.operation_parameter('arg',b.tool('send')) if kind=='action' else
                 b.opref_parameter('arg',b.tool('model')) if kind=='opref' else b.literal('arg','Text','id'))
            with self.assertRaises(TypeCheckError):b.resume_infer('result',ref)

    def test_model_await_cannot_bypass_resume_effect(self):
        b=PlanBuilder('wrong-await',self.c);op=b.operation_parameter('operation',b.tool('model'))
        snapshot=b.await_result('snapshot',op)
        with self.assertRaises(TypeCheckError):b.finish(snapshot)

    def test_raw_root_operation_is_not_authority(self):
        from al0.errors import InvalidData
        b=PlanBuilder('resume',self.c);op=b.operation_parameter('operation',b.tool('model'))
        self.start(b.finish(b.resume_infer('result',op)))
        with self.assertRaises(InvalidData):
            Interpreter(self.broker).run(self.p,self.run,self.who,self.grants,inputs={'operation':Value('fake')})

    def test_bundle_preserves_model_control(self):
        b=PlanBuilder('packed',self.c)
        initial=b.infer_progress('initial',b.tool('model'),b.literal('prompt','Text','question'))
        packet=b.pack('packet',{'progress':initial,'funds':b.split_budget('funds',{})})
        unpacked=b.unpack('unpacked',packet)
        funds=b.take('funds-back',unpacked,'funds');progress=b.take('progress',unpacked,'progress')
        b.join_budget('released',funds)
        result=b.match_variant('result',progress,branches(self.c,task=False))
        self.response=TimeoutError();self.start(b.finish(result));old=self.execute()
        self.assertEqual(old.data['tag'],'Waiting');self.assertEqual(len(self.calls),1)
        self.fact();self.assertEqual(self.execute(),old)
        self.assertEqual(self.resume(old).data['tag'],'Success')

    def test_restricted_call_cannot_smuggle_model_operation(self):
        child=PlanBuilder('resume',self.c);op=child.operation_parameter('result',child.tool('model'))
        resume=child.finish(child.resume_infer('progress',op))
        b=PlanBuilder('waiting',self.c);op=b.operation_parameter('result',b.tool('model'))
        restricted=b.finish(b.invoke_caps('restricted',resume,caps={},inputs={'result':op}))
        cases=branches(self.c,task=False);cases['Waiting']=restricted
        b=PlanBuilder('boundary',self.c)
        initial=b.infer_progress('initial',b.tool('model'),b.literal('prompt','Text','q'))
        self.start(b.finish(b.match_variant('result',initial,cases)));self.response=TimeoutError()
        with self.assertRaises(AuthorizationDenied):self.execute()
        self.assertEqual(len(self.calls),1)

    def test_fact_racing_snapshot_cannot_mix_waiting_and_terminal(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        self.response=TimeoutError();self.start();started=Event();future=[]
        with ThreadPoolExecutor(max_workers=1) as executor:
            def writer():
                started.set();self.fact()
            def fault(point):
                if point=='after_inference_observation_move' and not future:
                    future.append(executor.submit(writer));self.assertTrue(started.wait(5))
            self.store.fault=fault
            old=self.execute();future[0].result(timeout=10)
        self.store.fault=lambda p:None
        self.assertEqual(old.data['tag'],'Waiting')
        self.assertEqual(json.loads(self.rows()[0]['snapshot'])['business'],'Unknown')
        self.assertEqual(self.resume(old).data['tag'],'Success')

    def test_intent_stays_action_only(self):
        b=PlanBuilder('wrong',self.c)
        with self.assertRaises(TypeCheckError):b.intent_parameter('arg',b.tool('model'))

    def test_affine_result_cannot_be_dropped(self):
        b=PlanBuilder('drop',self.c)
        b.infer_progress('result',b.tool('model'),b.literal('prompt','Text','question'))
        with self.assertRaises(TypeCheckError):b.finish(b.literal('done','Bool',True))

    def test_resume_consumes_operation(self):
        b=PlanBuilder('duplicate',self.c);op=b.operation_parameter('operation',b.tool('model'))
        b.resume_infer('first',op)
        with self.assertRaises(TypeCheckError):b.finish(b.resume_infer('second',op))

    def test_effect_required_and_pure_forbidden(self):
        doc=json.loads(plan(self.c).encoded);doc['uses']=[]
        with self.assertRaises(TypeCheckError):check_plan(doc,self.c)
        b=PlanBuilder('pure',self.c,pure=True)
        value=b.infer_progress('result',b.tool('model'),b.literal('prompt','Text','q'))
        with self.assertRaises(TypeCheckError):b.finish(value)

    def test_observation_rollback_keeps_original_generation(self):
        self.response=TimeoutError();self.start()
        def fault(point):
            if point=='after_inference_observation_move':raise RuntimeError('cut')
        self.store.fault=fault
        with self.assertRaises(RuntimeError):self.execute()
        self.assertEqual(self.rows(),[]);self.assertEqual(self.custody(),(0,'Live','Root'))
        self.store.fault=lambda p:None
        self.assertEqual(self.execute().data['tag'],'Waiting');self.assertEqual(len(self.calls),1)

    def test_cancelled_operation_cannot_publish_but_keeps_bill(self):
        old=self.waiting(TimeoutError());ref=Value.from_record(old.data['payload']).resource
        with self.store.resource_access(self.run,self.who,[ref]):self.store.cancel(ref.resource,self.who)
        self.fact()
        with self.assertRaises(StateConflict):self.resume(old)
        self.assertEqual(self.execute(),old)
        self.store.settle(self.calls[0].attempt,self.who,provider='model',evidence_id='bill',actual={'u':1})
        self.assertEqual(self.budget()['confirmed'],1)

    def test_revocation_does_not_prevent_reading_existing_facts(self):
        old=self.waiting(TimeoutError());self.store.revoke(self.grants['model']);self.fact()
        self.assertEqual(self.resume(old).data['tag'],'Success');self.assertEqual(len(self.calls),1)

    def test_other_model_runtime_identity_rejected(self):
        self.c.register(Tool.define('other','infer','Text','Text','model',{'u':1}))
        old=self.waiting(TimeoutError())
        with self.assertRaises(ContractError):self.resume(old,spec=self.c.tools['other'])

    def test_private_labels_survive_waiting_and_success(self):
        b=PlanBuilder('private',self.c);prompt=b.parameter('prompt','Text')
        self.start(b.finish(b.infer_progress('initial',b.tool('model'),prompt)));self.response=TimeoutError()
        label=Label({'tool:model','developer'})
        old=Interpreter(self.broker).run(self.p,self.run,self.who,self.grants,inputs={'prompt':Value('secret',label=label)})
        self.assertEqual(old.label.readers,label.readers);self.assertIn('model:model',old.label.taints)
        self.fact();result=self.resume(old)
        self.assertEqual(result.label,old.label);self.assertEqual(Value.from_record(result.data['payload']).label,old.label)

    def cancellation(self,point,observations):
        child=plan(self.c);b=PlanBuilder('cancel-parent',self.c);scope=b.scope('group',{'u':1})
        handle=b.spawn('task',scope,child,{'u':1});outcome=b.join_outcome('outcome',handle)
        cases={}
        for kind in ('Completed','Failed','Waiting','Cancelled'):
            branch=PlanBuilder(kind.lower(),self.c)
            if kind=='Completed':
                value=branch.variant_parameter('result',progress_type('model','Text'))
                # Preserve the protected result in all cases using the same type.
                cases[kind]=branch.finish(value)
            else:
                value=branch.literal('reason','InferenceError',{'code':kind,'tool':'model','version':'1'})
                cases[kind]=branch.finish(branch.variant('result',progress_type('model','Text'),'Error',value))
        result=b.match_task('result',outcome,completed=cases['Completed'],failed=cases['Failed'],waiting=cases['Waiting'],cancelled=cases['Cancelled'])
        b.close_scope('closed',scope);self.start(b.finish(result));cancelled=[];self.response=Response(None,None,'Pending')
        def fault(name):
            if name==point and not cancelled:
                from concurrent.futures import ThreadPoolExecutor
                with closing(sqlite3.connect(self.path)) as db:task=db.execute("SELECT id FROM tasks WHERE kind='Task'").fetchone()[0]
                with ThreadPoolExecutor(max_workers=1) as executor:
                    cancelled.append(executor.submit(self.store.take_task_outcome,task,self.who,cancel=True).result()['kind'])
        self.store.fault=fault
        result=self.execute();self.broker.wait_for_workers(timeout=10)
        self.assertEqual(cancelled,['Cancelled']);self.assertEqual(len(self.rows()),observations)
        self.assertEqual(result.data['payload']['data']['code'],'Cancelled')
        self.assertEqual(self.budget()['reserved'],1)
        self.fact();self.store.settle(self.calls[0].attempt,self.who,provider='model',evidence_id='bill',actual={'u':1})
        self.assertEqual(self.budget()['confirmed'],1)

    def test_task_cancel_before_observation(self):self.cancellation('before_inference_observation_transaction',0)
    def test_task_cancel_after_observation(self):self.cancellation('after_inference_observation_commit',1)

    def test_process_cuts_preserve_single_call_and_fixed_receipt(self):
        cuts=['after_admit_commit','after_dispatch_commit','before_call_business_record','after_call_business_record',
              'after_call_billing_record','after_inference_observation_move','before_inference_observation_commit',
              'after_inference_observation_commit','before_task_finish_commit','after_task_finish_commit',
              'before_task_join_commit','after_task_join_commit']
        for number,point in enumerate(cuts):
            with self.subTest(point=point):
                folder=Path(self.temp.name)/str(number);folder.mkdir()
                store=Store(folder/'runtime.sqlite');task='task_' in point;c=catalog();p=plan(c,task)
                broker=Broker(store,c,{n:lambda t:Response('candidate',{'u':1}) for n in c.tools})
                run=store.create_run(self.who,p.fingerprint,{'u':10})
                grants={n:store.grant(run,n,s.scope,expires=store.clock()+1000) for n,s in c.tools.items()}
                log=folder/'endpoint.jsonl';config=folder/'config.json'
                config.write_text(json.dumps({'database':store.path,'run':run,'grants':grants,'log':str(log),
                                               'task':task,'waiting':point=='after_inference_observation_commit'}))
                args=[sys.executable,'-m','tests.progress_worker',str(config)]
                cut=subprocess.run([*args,point],capture_output=True,text=True,timeout=30)
                self.assertEqual(cut.returncode,77,cut.stdout+cut.stderr)
                recover=subprocess.run([*args,'none'],capture_output=True,text=True,timeout=30)
                self.assertEqual(recover.returncode,0,recover.stdout+recover.stderr)
                first=json.loads(recover.stdout)
                again=subprocess.run([*args,'none'],capture_output=True,text=True,timeout=30)
                self.assertEqual(again.returncode,0,again.stderr);self.assertEqual(json.loads(again.stdout),first)
                calls=[] if not log.exists() else [json.loads(line) for line in log.read_text().splitlines()]
                self.assertEqual(len(calls),0 if point=='after_dispatch_commit' else 1)
                if calls:
                    store.settle(calls[0]['attempt'],self.who,provider='model',evidence_id='late-bill',actual={'u':1})
                    self.assertEqual(store.budget(run,self.who)['u']['confirmed'],1)

    def test_v19_migration_process_cuts(self):
        self.start();before=self.store.budget(self.run,self.who)
        for point in ('before_attempt_funding_migration_commit','after_attempt_funding_migration_commit'):
            with closing(sqlite3.connect(self.path)) as db,db:
                db.execute('DROP TABLE IF EXISTS inference_observations');db.execute('PRAGMA user_version=19')
            config=Path(self.temp.name)/'migration.json';config.write_text(json.dumps({'database':str(self.path),'migrate':True}))
            cut=subprocess.run([sys.executable,'-m','tests.progress_worker',str(config),point],capture_output=True,text=True,timeout=30)
            self.assertEqual(cut.returncode,77,cut.stderr)
            with closing(sqlite3.connect(self.path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],19 if point.startswith('before') else 21)
            Store(self.path);self.assertEqual(self.store.budget(self.run,self.who),before)
