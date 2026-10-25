from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from threading import Barrier
import unittest

from al0.data import Principal
from al0.errors import AuthorizationDenied, ContractError, InvalidData, ReplayMismatch, StateConflict, TypeCheckError
from al0.execute import Broker, Interpreter, Response, Value
from al0.flow import Label
from al0.inference_admission import call_type
from al0.inference_progress import progress_type
from al0.ir import Type, check_plan
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.inference_admission import build, demo
from examples.inference_results import catalog
from tests.inference_admission_worker import plan


class InferenceAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'store.sqlite';self.store=Store(self.path)
        self.c=catalog();self.who=Principal('tests','developer');self.calls=[];self.response=Response('candidate',{'u':1})
        def model(ticket):
            self.calls.append(ticket)
            if isinstance(self.response,Exception):raise self.response
            return self.response
        self.broker=Broker(self.store,self.c,{'model':model,'verify':lambda t:Response(True,{'u':1}),'send':lambda t:Response('sent',{'u':1})})
        self.addCleanup(lambda:self.broker.wait_for_workers(timeout=10))
    def start(self,p=None,amount=10):
        self.p=build(self.c) if p is None else p
        self.run=self.store.create_run(self.who,self.p.fingerprint,{'u':amount})
        self.grants={'model':self.store.grant(self.run,'model','model',expires=self.store.clock()+1000)}
    def execute(self,**kwargs):return Interpreter(self.broker).run(self.p,self.run,self.who,self.grants,**kwargs)
    def query(self,sql):
        with closing(sqlite3.connect(self.path)) as db:return db.execute(sql).fetchall()
    def counts(self):return tuple(self.query('SELECT COUNT(*) FROM '+table)[0][0] for table in ('inference_admissions','permits','operations','attempts','resource_custody'))
    def direct(self,node='other',source=None,allocation=None):
        with self.store.resource_access(self.run,self.who):
            return self.broker.try_infer(self.run,self.who,node,source or Value('question'),self.c.tools['model'],self.grants,allocation=allocation)
    def rejected(self,code):
        result=self.execute();self.assertEqual(result.data['tag'],'Rejected')
        self.assertEqual(result.data['payload']['data']['code'],code)
        self.assertEqual(self.counts(),(1,0,0,0,0));self.assertEqual(self.calls,[])
        before=len(self.store.events(self.run,self.who));self.assertEqual(self.execute(),result)
        self.assertEqual(len(self.store.events(self.run,self.who)),before)
        return result

    def test_example_source_sdk_and_independent_endpoint(self):
        report=demo(Path(self.temp.name)/'demo');self.assertTrue(report['identical_ir'])
        for mode,result in report['cases'].items():
            self.assertTrue(result['replay_equal']);self.assertEqual(result['replay_added_events'],0)
            self.assertEqual(result['calls'],1 if mode in ('Success','Pending','InvalidOutput') else 0)
            self.assertEqual(result['permits'],result['calls'])

    def test_success_nested_candidate(self):
        self.start();result=self.execute()
        self.assertEqual(result.variant,call_type('model','Text'))
        nested=Value.from_record(result.data['payload'])
        self.assertEqual(nested.data['tag'],'Success');self.assertEqual(dict(nested.variant.members)['Success'],Type('Candidate','Text'))
        self.assertEqual(self.counts(),(1,1,1,1,2));self.assertEqual(self.execute(),result);self.assertEqual(len(self.calls),1)

    def test_missing_authority_is_fixed_rejection(self):
        self.start();self.grants={};self.rejected('AuthorizationUnavailable')

    def test_revoked_authority_is_fixed_rejection(self):
        self.start();self.store.revoke(self.grants['model']);self.rejected('AuthorizationUnavailable')

    def test_expired_authority_is_fixed_rejection(self):
        self.start();now=self.store.clock();self.store.clock=lambda:now+2000;self.rejected('AuthorizationUnavailable')

    def test_budget_rejection_has_no_orphan_permit(self):
        self.start(amount=0);self.rejected('BudgetExceeded')

    def test_unknown_grant_is_rejected_without_details(self):
        self.start();self.grants={'model':'missing-private-id'};result=self.rejected('AuthorizationUnavailable')
        self.assertNotIn('missing-private-id',json.dumps(result.record()))

    def test_changed_absent_grant_is_not_automatic_retry(self):
        self.start();grant=self.grants['model'];self.grants={};self.rejected('AuthorizationUnavailable')
        self.grants={'model':grant}
        with self.assertRaises(ReplayMismatch):self.execute()
        self.assertEqual(self.direct().data['tag'],'Started');self.assertEqual(len(self.calls),1)

    def test_budget_refund_does_not_reopen_rejected_position(self):
        self.start(amount=1);funds=self.store.split_budget(self.run,self.who,'held',{'u':1})
        old=self.rejected('BudgetExceeded');self.store.join_budget(self.run,self.who,funds)
        self.assertEqual(self.execute(),old);self.assertEqual(self.direct().data['tag'],'Started')

    def test_request_and_funding_bindings_cannot_change(self):
        self.start(amount=0);self.execute()
        with self.assertRaises(ReplayMismatch):self.direct('result',Value('different'))
        funds=self.store.split_budget(self.run,self.who,'other',{})
        with self.assertRaises(ReplayMismatch):self.direct('result',allocation=funds)

    def test_two_slots_compete_for_last_unit(self):
        self.start(amount=1);self.response=Response('candidate',None);barrier=Barrier(2)
        def call(node):barrier.wait();return self.direct(node)
        with ThreadPoolExecutor(max_workers=2) as executor:results=list(executor.map(call,['one','two']))
        self.assertEqual(sorted(v.data['tag'] for v in results),['Rejected','Started'])
        self.assertEqual(self.counts(),(2,1,1,1,2));self.assertEqual(len(self.calls),1)
        self.store.settle(self.calls[0].attempt,self.who,provider='model',evidence_id='refund',actual={'u':0})
        self.assertEqual([self.direct(n) for n in ['one','two']],results)

    def test_timeout_is_started_waiting(self):
        self.response=TimeoutError();self.start();result=self.execute()
        self.assertEqual(result.data['tag'],'Started');self.assertEqual(result.data['payload']['data']['tag'],'Waiting')
        self.assertEqual(self.execute(),result);self.assertEqual(len(self.calls),1)

    def test_invalid_output_is_started_error_and_billed(self):
        self.response=Response(17,{'u':1});self.start();result=self.execute()
        self.assertEqual(result.data['tag'],'Started');self.assertEqual(result.data['payload']['data']['payload']['data']['code'],'InvalidOutput')
        self.assertEqual(self.store.budget(self.run,self.who)['u']['confirmed'],1)

    def test_late_success_does_not_replace_waiting_observation(self):
        self.response=TimeoutError();self.start();old=self.execute();ticket=self.calls[0]
        self.store.business_result(ticket.operation,self.who,provider='model',evidence_id='late',outcome='Applied',details={'output':'late'})
        self.assertEqual(self.execute(),old)
        progress=Value.from_record(old.data['payload']);ref=Value.from_record(progress.data['payload']).resource
        result=self.broker.inference_progress(self.run,self.who,'next',ref,self.c.tools['model'],self.grants,control=old.label)
        self.assertEqual(result.data['tag'],'Success');self.assertEqual(len(self.calls),1)

    def test_internal_adapter_error_is_not_rejection(self):
        self.response=RuntimeError('bug');self.start()
        with self.assertRaises(RuntimeError):self.execute()
        self.assertEqual(json.loads(self.query('SELECT result FROM inference_admissions')[0][0])['kind'],'Started')

    def test_unauthenticated_principal_is_not_rejection(self):
        self.start()
        with self.assertRaises(AuthorizationDenied):Interpreter(self.broker).run(self.p,self.run,Principal('other','user'),self.grants)
        self.assertEqual(self.counts(),(0,0,0,0,0))

    def test_closed_funding_is_not_rejection(self):
        self.start();funds=self.store.split_budget(self.run,self.who,'closed',{'u':1});self.store.join_budget(self.run,self.who,funds)
        with self.assertRaises(StateConflict):self.direct(allocation=funds)
        self.assertEqual(self.counts(),(0,0,0,0,0))

    def test_private_flow_rejection_preserves_label(self):
        b=PlanBuilder('private',self.c);source=b.parameter('prompt','Text')
        self.start(b.finish(b.try_infer('result',b.tool('model'),source)))
        label=Label({'owner'});result=self.execute(inputs={'prompt':Value('private',label=label)})
        self.assertEqual(result.data['tag'],'Rejected');self.assertEqual(result.data['payload']['data']['code'],'FlowDenied')
        self.assertEqual(result.label,label);self.assertEqual(Value.from_record(result.data['payload']).label,label)
        self.assertEqual(self.counts(),(1,0,0,0,0))

    def test_derived_authority_label_is_retained(self):
        self.start();label=Label({'owner','tool:model'})
        derived=self.store.attenuate_capability(self.run,self.who,'restricted',self.grants['model'],control=label)
        self.grants={'model':derived['grant']};result=self.execute()
        self.assertEqual(result.data['tag'],'Started');self.assertEqual(result.label.readers,label.readers)
        self.assertEqual(self.execute(),result)

    def test_rejected_task_result_preserves_empty_affine_obligation(self):
        self.start(plan(self.c,True));self.grants={};result=self.execute();self.broker.wait_for_workers(timeout=10)
        self.assertEqual(result.data['tag'],'Rejected');self.assertEqual(self.calls,[])
        self.assertEqual(self.execute(),result)

    def test_outer_matching_can_handle_rejection_and_return_progress(self):
        accepted=PlanBuilder('accepted',self.c)
        started=accepted.finish(accepted.variant_parameter('result',progress_type('model','Text')))
        rejected=PlanBuilder('rejected',self.c);reason=rejected.parameter('result','InferenceRejection')
        code=rejected.field('code',reason,'code')
        error=rejected.record('error','InferenceError',{'code':code,'tool':rejected.literal('tool','Text','model'),
                                                        'version':rejected.literal('version','Text','1')})
        denied=rejected.finish(rejected.variant('progress',progress_type('model','Text'),'Error',error))
        b=PlanBuilder('handled',self.c);attempt=b.try_infer('attempt',b.tool('model'),b.literal('q','Text','q'))
        self.start(b.finish(b.match_variant('result',attempt,{'Started':started,'Rejected':denied})),amount=0)
        result=self.execute();self.assertEqual(result.data['tag'],'Error')
        self.assertEqual(result.data['payload']['data']['code'],'BudgetExceeded')

    def cancellation(self,point,decisions):
        b=PlanBuilder('cancel-parent',self.c);scope=b.scope('group',{'u':1})
        handle=b.spawn('task',scope,build(self.c),{'u':1});outcome=b.join_outcome('outcome',handle)
        cases={}
        for kind in ('Completed','Failed','Waiting','Cancelled'):
            case=PlanBuilder(kind,self.c)
            if kind=='Completed':value=case.variant_parameter('result',call_type('model','Text'))
            else:
                reason=case.literal('reason','InferenceRejection',{'code':kind,'tool':'model','version':'1'})
                value=case.variant('result',call_type('model','Text'),'Rejected',reason)
            cases[kind]=case.finish(value)
        result=b.match_task('result',outcome,completed=cases['Completed'],failed=cases['Failed'],waiting=cases['Waiting'],cancelled=cases['Cancelled'])
        b.close_scope('closed',scope);self.start(b.finish(result));cancelled=[]
        def fault(name):
            if name==point and not cancelled:
                task=self.query("SELECT id FROM tasks WHERE kind='Task'")[0][0]
                with ThreadPoolExecutor(max_workers=1) as executor:
                    cancelled.append(executor.submit(self.store.take_task_outcome,task,self.who,cancel=True).result()['kind'])
        self.store.fault=fault;result=self.execute();self.broker.wait_for_workers(timeout=10)
        self.assertEqual(cancelled,['Cancelled']);self.assertEqual(result.data['payload']['data']['code'],'Cancelled')
        self.assertEqual(self.query('SELECT COUNT(*) FROM inference_admissions')[0][0],decisions)
        self.assertEqual(self.calls,[]);self.assertEqual(self.store.budget(self.run,self.who)['u']['reserved'],0)
        if decisions:self.assertEqual(json.loads(self.query('SELECT result FROM inference_admissions')[0][0])['kind'],'Started')

    def test_cancel_before_decision_fences_publication(self):self.cancellation('before_inference_admission_transaction',0)
    def test_cancel_after_decision_retains_started_and_cancels_queue(self):self.cancellation('after_inference_admission_commit',1)

    def test_task_returns_started_or_rejected_without_leaks(self):
        self.start(plan(self.c,True));result=self.execute();self.broker.wait_for_workers(timeout=10)
        self.assertEqual(result.data['tag'],'Started');self.assertEqual(self.execute(),result)

    def test_rejected_nested_variant_cannot_be_dropped(self):
        b=PlanBuilder('wrong',self.c);b.try_infer('result',b.tool('model'),b.literal('q','Text','q'))
        with self.assertRaises(TypeCheckError):b.finish(b.literal('done','Bool',True))

    def test_rejection_schema_is_exact_and_reserved(self):
        self.assertEqual(self.c.record_fields('InferenceRejection'),{'code':'Text','tool':'Text','version':'Text'})
        with self.assertRaises(InvalidData):self.c.schema('InferenceRejection',{'raw':'Text'})
        with self.assertRaises(InvalidData):self.c.validate('InferenceRejection',{'code':'Denied'})

    def test_wrong_tool_missing_effect_and_pure_rejected(self):
        b=PlanBuilder('wrong',self.c)
        with self.assertRaises(TypeCheckError):b.try_infer('result',b.tool('send'),b.literal('q','Text','q'))
        doc=json.loads(build(self.c).encoded);doc['uses']=[]
        with self.assertRaises(TypeCheckError):check_plan(doc,self.c)
        b=PlanBuilder('pure',self.c,pure=True);result=b.try_infer('result',b.tool('model'),b.literal('q','Text','q'))
        with self.assertRaises(TypeCheckError):b.finish(result)

    def test_rollback_restores_presented_references(self):
        self.start(amount=0)
        with self.store.resource_access(self.run,self.who) as presented:
            result=self.broker.try_infer(self.run,self.who,'result',Value('q'),self.c.tools['model'],self.grants)
            self.assertEqual(result.data['tag'],'Rejected');self.assertEqual(presented,{})

    def test_exception_after_authorization_rolls_back_everything(self):
        self.start()
        def fault(point):
            if point=='after_inference_authorization':raise RuntimeError('cut')
        self.store.fault=fault
        with self.assertRaises(RuntimeError):self.execute()
        self.assertEqual(self.counts(),(0,0,0,0,0));self.store.fault=lambda p:None
        self.assertEqual(self.execute().data['tag'],'Started')

    def test_revocation_after_admission_is_not_preflight_rejection(self):
        self.start()
        def fault(point):
            if point=='after_inference_admission_commit':self.store.revoke(self.grants['model'])
        self.store.fault=fault
        result=self.execute()
        self.assertEqual(result.data['tag'],'Started')
        self.assertEqual(result.data['payload']['data']['tag'],'Waiting')
        self.assertEqual(self.calls,[])
        self.assertEqual(json.loads(self.query('SELECT result FROM inference_admissions')[0][0])['kind'],'Started')
        self.assertEqual(self.store.budget(self.run,self.who)['u']['reserved'],1)

    def test_process_cuts_atomic_decision_and_observation(self):
        points=['after_inference_authorization','after_reserve','after_permit_consumption','after_operation_insert',
                'after_attempt_funding_insert','before_inference_admission_commit','after_inference_admission_commit',
                'after_dispatch_commit','before_call_business_record','after_call_business_record',
                'before_inference_observation_commit','after_inference_observation_commit',
                'before_task_finish_commit','after_task_finish_commit','before_task_join_commit','after_task_join_commit']
        for i,point in enumerate(points):
            with self.subTest(point=point):
                folder=Path(self.temp.name)/str(i);folder.mkdir();store=Store(folder/'runtime.sqlite');c=catalog();task='task_' in point
                broker=Broker(store,c,{n:lambda t:Response('candidate',{'u':1}) for n in c.tools});p=plan(c,task)
                run=store.create_run(self.who,p.fingerprint,{'u':10});grants={'model':store.grant(run,'model','model',expires=store.clock()+1000)}
                log=folder/'endpoint.jsonl';config=folder/'config.json'
                config.write_text(json.dumps({'database':store.path,'run':run,'grants':grants,'log':str(log),'task':task}))
                args=[sys.executable,'-m','tests.inference_admission_worker',str(config)]
                cut=subprocess.run([*args,point],capture_output=True,text=True,timeout=30)
                self.assertEqual(cut.returncode,77,cut.stdout+cut.stderr)
                recovered=subprocess.run([*args,'none'],capture_output=True,text=True,timeout=30)
                self.assertEqual(recovered.returncode,0,recovered.stdout+recovered.stderr)
                result=json.loads(recovered.stdout);self.assertEqual(result['data']['tag'],'Started')
                repeated=subprocess.run([*args,'none'],capture_output=True,text=True,timeout=30)
                self.assertEqual(repeated.returncode,0,repeated.stderr);self.assertEqual(json.loads(repeated.stdout),result)
                calls=[] if not log.exists() else [json.loads(line) for line in log.read_text().splitlines()]
                self.assertEqual(len(calls),0 if point=='after_dispatch_commit' else 1)
                if calls:store.settle(calls[0]['attempt'],self.who,provider='model',evidence_id='bill',actual={'u':1})
                with closing(sqlite3.connect(store.path)) as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM permits').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM inference_admissions').fetchone()[0],1)

    def test_rejection_process_cuts_do_not_create_resources(self):
        self.start(amount=0);config=Path(self.temp.name)/'reject.json'
        config.write_text(json.dumps({'database':str(self.path),'run':self.run,'grants':self.grants,'log':str(Path(self.temp.name)/'endpoint')}))
        args=[sys.executable,'-m','tests.inference_admission_worker',str(config)]
        cut=subprocess.run([*args,'before_inference_admission_commit'],capture_output=True,text=True,timeout=30)
        self.assertEqual(cut.returncode,77,cut.stderr);self.assertEqual(self.counts(),(0,0,0,0,0))
        cut=subprocess.run([*args,'after_inference_admission_commit'],capture_output=True,text=True,timeout=30)
        self.assertEqual(cut.returncode,77,cut.stderr);self.assertEqual(self.counts(),(1,0,0,0,0));self.rejected('BudgetExceeded')

    def test_v20_migration_process_cuts(self):
        self.start()
        for point in ('before_attempt_funding_migration_commit','after_attempt_funding_migration_commit'):
            with closing(sqlite3.connect(self.path)) as db,db:
                db.execute('DROP TABLE IF EXISTS inference_admissions');db.execute('PRAGMA user_version=20')
            config=Path(self.temp.name)/'migration.json';config.write_text(json.dumps({'database':str(self.path),'migrate':True}))
            cut=subprocess.run([sys.executable,'-m','tests.inference_admission_worker',str(config),point],capture_output=True,text=True,timeout=30)
            self.assertEqual(cut.returncode,77,cut.stderr)
            with closing(sqlite3.connect(self.path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],20 if point.startswith('before') else 21)
            Store(self.path)
