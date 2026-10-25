from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from al0.data import Principal
from al0.errors import AuthorizationDenied, ContractBreach, ContractError, FlowDenied, InferenceRefused, StateConflict
from al0.execute import Broker, Interpreter, Response, InferenceResponse, Value
from al0.flow import Label
from al0.inference_admission import call_type
from al0.inference_response import classify_inference
from al0.sdk import PlanBuilder
from al0.store import Store, DispatchBlocked
from examples.inference_results import catalog
from tests.inference_provider_worker import plan
from examples.inference_provider import demo


class InferenceProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'store.sqlite';self.store=Store(self.path)
        self.c=catalog();self.who=Principal('tests','developer');self.calls=[]
        self.response=InferenceResponse('Refused',actual={'u':1})
        def model(ticket):
            self.calls.append(ticket)
            if isinstance(self.response,Exception):raise self.response
            return self.response
        self.broker=Broker(self.store,self.c,{'model':model,'verify':lambda t:Response(True,{'u':1}),'send':lambda t:Response('sent',{'u':1})})
        self.addCleanup(lambda:self.broker.wait_for_workers(timeout=10))
    def start(self,method='try_infer',task=False,p=None):
        self.method=method;self.p=plan(self.c,method,task) if p is None else p
        self.run=self.store.create_run(self.who,self.p.fingerprint,{'u':10})
        self.grants={n:self.store.grant(self.run,n,s.scope,expires=self.store.clock()+1000) for n,s in self.c.tools.items()}
    def execute(self,**kwargs):return Interpreter(self.broker).run(self.p,self.run,self.who,self.grants,**kwargs)
    def query(self,sql):
        with closing(sqlite3.connect(self.path)) as db:return db.execute(sql).fetchall()
    def budget(self):return self.store.budget(self.run,self.who)['u']
    def progress(self,result):return Value.from_record(result.data['payload']) if self.method=='try_infer' else result
    def repeated(self):
        result=self.execute();before=len(self.store.events(self.run,self.who))
        self.assertEqual(self.execute(),result);self.assertEqual(len(self.store.events(self.run,self.who)),before)
        self.assertEqual(len(self.calls),1)
        return self.progress(result)
    def refused(self,method):
        self.start(method);result=self.repeated()
        self.assertEqual(result.data['tag'],'Error');self.assertEqual(result.data['payload']['data'],{'code':'Refused','tool':'model','version':'1'})
        self.assertEqual(self.budget()['confirmed'],1)
        self.assertEqual(self.store.snapshot(self.calls[0].operation,self.who)['business'],'Applied')
    def test_infer_result_refusal(self):self.refused('infer_result')
    def test_infer_progress_refusal(self):self.refused('infer_progress')
    def test_try_infer_started_refusal(self):self.refused('try_infer')

    def test_source_sdk_example_and_endpoint_costs(self):
        report=demo(Path(self.temp.name)/'demo');self.assertTrue(report['identical_ir'])
        for mode,result in report['cases'].items():
            self.assertEqual(result['outer'],'Started');self.assertTrue(result['replay_equal'])
            self.assertEqual(result['replay_added_events'],0);self.assertEqual(result['calls'],0 if mode=='Blocked' else 1)
        self.assertEqual(report['cases']['Refused']['code'],'Refused')
        self.assertEqual(report['cases']['Blocked']['budget']['u']['free'],10)

    def test_legacy_infer_has_specific_refusal_exception(self):
        self.start('infer')
        for _ in range(2):
            with self.assertRaises(InferenceRefused):self.execute()
        self.assertEqual(len(self.calls),1);self.assertEqual(self.budget()['confirmed'],1)

    def test_explicit_output_preserves_candidate(self):
        self.response=InferenceResponse('Output','candidate',{'u':1});self.start()
        result=self.repeated();self.assertEqual(result.data['tag'],'Success');self.assertEqual(result.data['payload']['data'],'candidate')

    def test_explicit_output_invalid_schema_is_not_refusal(self):
        self.response=InferenceResponse('Output',17,{'u':1});self.start()
        self.assertEqual(self.repeated().data['payload']['data']['code'],'InvalidOutput')

    def test_model_json_cannot_forge_refusal(self):
        self.c.tools['model']=replace(self.c.tools['model'],output_type='Json')
        fake={'inference_outcome':{'version':1,'kind':'Refused'},'output':None}
        self.response=Response(fake,{'u':1});self.start();result=self.repeated()
        self.assertEqual(result.data['tag'],'Success');self.assertEqual(result.data['payload']['data'],fake)

    def test_explicit_json_output_can_contain_refusal_like_fields(self):
        self.c.tools['model']=replace(self.c.tools['model'],output_type='Json')
        fake={'kind':'Refused'};self.response=InferenceResponse('Output',fake,{'u':1});self.start()
        self.assertEqual(self.repeated().data['payload']['data'],fake)

    def test_refusal_without_bill_retains_reservation(self):
        self.response=InferenceResponse('Refused');self.start();self.repeated()
        self.assertEqual(self.budget()['reserved'],1)
        self.store.settle(self.calls[0].attempt,self.who,provider='model',evidence_id='late',actual={'u':1})
        self.assertEqual(self.budget()['confirmed'],1);self.assertEqual(self.repeated().data['payload']['data']['code'],'Refused')

    def test_refusal_fee_overrun_is_not_swallowed(self):
        self.response=InferenceResponse('Refused',actual={'u':2});self.start()
        with self.assertRaises(ContractBreach):self.execute()
        state=self.store.snapshot(self.calls[0].operation,self.who)
        self.assertEqual(state['business_evidence']['details']['inference_outcome']['kind'],'Refused')
        self.assertEqual(self.budget()['confirmed'],2)

    def test_invalid_envelope_rejected(self):
        for kind,output in [('Unknown',None),('Refused','private refusal text')]:
            with self.assertRaises(ContractError):InferenceResponse(kind,output)

    def test_non_model_adapter_cannot_use_inference_envelope(self):
        b=PlanBuilder('check',self.c);q=b.literal('q','Text','q');p=b.finish(b.check('checked',b.tool('verify'),q))
        self.broker.adapters['verify']=lambda t:InferenceResponse('Refused',actual={'u':1});self.start(p=p)
        with self.assertRaises(ContractError):self.execute()

    def test_malformed_persisted_marker_is_contract_error(self):
        for marker in (None,{}, {'kind':'Refused','version':True},{'kind':'Refused','version':2},{'kind':'Pending','version':1}):
            state={'business':'Applied','business_evidence':{'details':{'output':None,'inference_outcome':marker}}}
            with self.assertRaises(ContractError):classify_inference(self.c,self.c.tools['model'],state)

    def test_refusal_marker_does_not_prove_no_effect(self):
        state={'business':'ProvenNoEffect','business_evidence':{'details':InferenceResponse('Refused').details()}}
        with self.assertRaises(ContractError):classify_inference(self.c,self.c.tools['model'],state)

    def test_late_refusal_after_unknown_uses_next_observation(self):
        self.response=TimeoutError();self.start();old=self.execute();progress=self.progress(old);ticket=self.calls[0]
        self.store.business_result(ticket.operation,self.who,provider='model',evidence_id='late',outcome='Applied',details=InferenceResponse('Refused').details())
        self.assertEqual(self.execute(),old)
        ref=Value.from_record(progress.data['payload']).resource
        new=self.broker.inference_progress(self.run,self.who,'next',ref,self.c.tools['model'],self.grants,control=old.label)
        self.assertEqual(new.data['payload']['data']['code'],'Refused');self.assertEqual(len(self.calls),1)

    def test_private_refusal_labels_survive(self):
        b=PlanBuilder('private',self.c);source=b.parameter('prompt','Text')
        self.start(p=b.finish(b.try_infer('result',b.tool('model'),source)))
        label=Label({'owner','tool:model'});result=self.execute(inputs={'prompt':Value('private',label=label)})
        progress=self.progress(result);self.assertEqual(result.label.readers,label.readers)
        self.assertEqual(progress.label,result.label);self.assertEqual(Value.from_record(progress.data['payload']).label,result.label)

    def blocked(self,task=False):
        self.start(task=task)
        def fault(point):
            if point=='after_inference_admission_commit':self.store.revoke(self.grants['model'])
        self.store.fault=fault;result=self.execute();self.store.fault=lambda p:None
        progress=self.progress(result);self.assertEqual(progress.data['tag'],'Waiting')
        self.assertEqual(self.calls,[]);self.assertEqual(self.budget()['reserved'],1)
        ref=Value.from_record(progress.data['payload']).resource
        self.assertEqual(self.store.snapshot(ref.resource,self.who)['business'],'NotDispatched')
        self.assertEqual(self.query('SELECT phase,billing FROM attempts'),[('Queued','Reserved')])
        self.assertEqual(self.query('SELECT accepted FROM outbox'),[(0,)])
        self.assertEqual(self.execute(),result)
        return result,ref
    def test_revoked_dispatch_returns_waiting_without_rejection(self):self.blocked()

    def test_blocked_observation_rotates_but_does_not_retry(self):
        old,ref=self.blocked()
        new=self.broker.inference_progress(self.run,self.who,'next',ref,self.c.tools['model'],self.grants,control=old.label)
        self.assertEqual(Value.from_record(new.data['payload']).resource.generation,ref.generation+1)
        self.assertEqual(self.query('SELECT COUNT(*) FROM attempts'),[(1,)])
        self.assertEqual(self.execute(),old)
        with self.assertRaises(StateConflict):self.broker.inference_progress(self.run,self.who,'stale',ref,self.c.tools['model'],self.grants)

    def test_cancel_blocked_queue_releases_unexposed_reservation(self):
        old,ref=self.blocked()
        with self.store.resource_access(self.run,self.who,[ref]):self.store.cancel(ref.resource,self.who)
        self.assertEqual(self.budget()['reserved'],0);self.assertEqual(self.budget()['free'],10)
        self.assertEqual(self.execute(),old);self.assertEqual(self.calls,[])

    def test_blocked_task_returns_control_and_original_funding_can_refund(self):
        old,ref=self.blocked(task=True);self.broker.wait_for_workers(timeout=10)
        with self.store.resource_access(self.run,self.who,[ref]):self.store.cancel(ref.resource,self.who)
        self.assertEqual(self.budget()['free'],10);self.assertEqual(self.budget()['reserved'],0)
        self.assertEqual(self.execute(),old)

    def test_old_start_attempt_still_raises_on_revocation(self):
        _,ref=self.blocked();attempt=self.query('SELECT id FROM attempts')[0][0]
        with self.store.resource_access(self.run,self.who,[ref]):
            with self.assertRaises(AuthorizationDenied):self.store.start_attempt(ref.resource,attempt,self.who)
            self.assertIsInstance(self.store.try_start_attempt(ref.resource,attempt,self.who),DispatchBlocked)

    def test_attempt_and_principal_errors_are_not_blocked(self):
        _,ref=self.blocked()
        with self.store.resource_access(self.run,self.who,[ref]):
            with self.assertRaises(StateConflict):self.store.try_start_attempt(ref.resource,'missing',self.who)
            with self.assertRaises(AuthorizationDenied):self.store.try_start_attempt(ref.resource,'missing',Principal('other','user'))

    def test_stale_custody_is_not_blocked(self):
        old,ref=self.blocked();attempt=self.query('SELECT id FROM attempts')[0][0]
        self.broker.inference_progress(self.run,self.who,'next',ref,self.c.tools['model'],self.grants,control=old.label)
        with self.store.resource_access(self.run,self.who,[ref]):
            with self.assertRaises(StateConflict):self.store.try_start_attempt(ref.resource,attempt,self.who)

    def test_stronger_dispatch_control_is_blocked_at_transaction(self):
        self.start();stopped=[]
        def fault(point):
            if point=='after_inference_admission_commit' and not stopped:
                stopped.append(True);raise RuntimeError('cut')
        self.store.fault=fault
        with self.assertRaises(RuntimeError):self.execute()
        self.store.fault=lambda p:None
        operation=self.query('SELECT id FROM operations')[0][0];ref=self.store.resource_birth('Operation',operation,self.who)
        label=Label({'owner'})
        result=self.broker.inference_progress(self.run,self.who,'private-observation',ref,self.c.tools['model'],self.grants,control=label)
        self.assertEqual(result.data['tag'],'Waiting');self.assertEqual(result.label.readers,label.readers)
        self.assertEqual(self.calls,[]);self.assertEqual(self.budget()['reserved'],1)

    def cancellation(self,point,blocked=False):
        b=PlanBuilder('cancel-parent',self.c);scope=b.scope('group',{'u':1})
        handle=b.spawn('task',scope,plan(self.c),{'u':1});outcome=b.join_outcome('outcome',handle);cases={}
        for kind in ('Completed','Failed','Waiting','Cancelled'):
            case=PlanBuilder(kind,self.c)
            if kind=='Completed':value=case.variant_parameter('result',call_type('model','Text'))
            else:
                reason=case.literal('reason','InferenceRejection',{'code':kind,'tool':'model','version':'1'})
                value=case.variant('result',call_type('model','Text'),'Rejected',reason)
            cases[kind]=case.finish(value)
        result=b.match_task('result',outcome,completed=cases['Completed'],failed=cases['Failed'],waiting=cases['Waiting'],cancelled=cases['Cancelled'])
        b.close_scope('closed',scope);self.start(p=b.finish(result));cancelled=[]
        def fault(name):
            if blocked and name=='after_inference_admission_commit':self.store.revoke(self.grants['model'])
            if name==point and not cancelled:
                task=self.query("SELECT id FROM tasks WHERE kind='Task'")[0][0]
                with ThreadPoolExecutor(max_workers=1) as executor:
                    cancelled.append(executor.submit(self.store.take_task_outcome,task,self.who,cancel=True).result()['kind'])
        self.store.fault=fault;result=self.execute();self.broker.wait_for_workers(timeout=10)
        self.assertEqual(cancelled,['Cancelled']);self.assertEqual(result.data['payload']['data']['code'],'Cancelled')
        self.assertEqual(self.query('SELECT COUNT(*) FROM inference_observations'),[(0,)])
        self.assertEqual(self.budget()['confirmed'],0 if blocked else 1)
        self.assertEqual(len(self.calls),0 if blocked else 1)
    def test_cancellation_between_refusal_and_bill(self):self.cancellation('after_call_business_record')
    def test_cancellation_after_block_before_observation(self):self.cancellation('after_dispatch_blocked',True)

    def test_refusal_task_result_and_late_bill(self):
        self.response=InferenceResponse('Refused');self.start(task=True);self.repeated();self.broker.wait_for_workers(timeout=10)
        self.assertEqual(self.budget()['reserved'],1)
        self.store.settle(self.calls[0].attempt,self.who,provider='model',evidence_id='bill',actual={'u':1})
        self.assertEqual(self.budget()['confirmed'],1)

    def test_process_cuts_preserve_refusal_and_fee(self):
        points=['before_call_business_record','after_call_business_record','after_call_billing_record',
                'before_inference_observation_commit','after_inference_observation_commit',
                'before_inference_result_commit','after_inference_result_commit',
                'before_task_finish_commit','after_task_finish_commit','before_task_join_commit','after_task_join_commit']
        for i,point in enumerate(points):
            with self.subTest(point=point):
                folder=Path(self.temp.name)/str(i);folder.mkdir();store=Store(folder/'runtime.sqlite');c=catalog()
                method='infer_result' if 'inference_result' in point else 'try_infer';task='task_' in point;p=plan(c,method,task)
                broker=Broker(store,c,{n:lambda t:Response('candidate',{'u':1}) for n in c.tools})
                run=store.create_run(self.who,p.fingerprint,{'u':10});grants={'model':store.grant(run,'model','model',expires=store.clock()+1000)}
                log=folder/'endpoint.jsonl';config=folder/'config.json'
                config.write_text(json.dumps({'database':store.path,'run':run,'grants':grants,'log':str(log),'task':task,'method':method}))
                args=[sys.executable,'-m','tests.inference_provider_worker',str(config)]
                cut=subprocess.run([*args,point],capture_output=True,text=True,timeout=30)
                self.assertEqual(cut.returncode,77,cut.stdout+cut.stderr);calls=[json.loads(line) for line in log.read_text().splitlines()];self.assertEqual(len(calls),1)
                if point=='before_call_business_record':
                    store.business_result(calls[0]['operation'],self.who,provider='model',evidence_id='recovered',outcome='Applied',details=InferenceResponse('Refused').details())
                recovered=subprocess.run([*args,'none'],capture_output=True,text=True,timeout=30)
                self.assertEqual(recovered.returncode,0,recovered.stdout+recovered.stderr)
                result=Value.from_record(json.loads(recovered.stdout));progress=Value.from_record(result.data['payload']) if method=='try_infer' else result
                self.assertEqual(progress.data['payload']['data']['code'],'Refused')
                store.settle(calls[0]['attempt'],self.who,provider='model',evidence_id='bill',actual={'u':1})
                repeated=subprocess.run([*args,'none'],capture_output=True,text=True,timeout=30)
                self.assertEqual(repeated.returncode,0,repeated.stderr);self.assertEqual(json.loads(repeated.stdout),result.record())
                self.assertEqual(len(log.read_text().splitlines()),1);self.assertEqual(store.budget(run,self.who)['u']['confirmed'],1)

    def test_process_cuts_preserve_blocked_waiting_without_dispatch(self):
        for i,point in enumerate(('after_dispatch_blocked','before_inference_observation_commit','after_inference_observation_commit')):
            with self.subTest(point=point):
                folder=Path(self.temp.name)/('blocked'+str(i));folder.mkdir();store=Store(folder/'runtime.sqlite');c=catalog();p=plan(c)
                broker=Broker(store,c,{n:lambda t:Response('candidate',{'u':1}) for n in c.tools})
                run=store.create_run(self.who,p.fingerprint,{'u':10});grants={'model':store.grant(run,'model','model',expires=store.clock()+1000)}
                log=folder/'endpoint.jsonl';config=folder/'config.json'
                config.write_text(json.dumps({'database':store.path,'run':run,'grants':grants,'log':str(log),'task':False,'method':'try_infer','blocked':True}))
                args=[sys.executable,'-m','tests.inference_provider_worker',str(config)]
                cut=subprocess.run([*args,point],capture_output=True,text=True,timeout=30);self.assertEqual(cut.returncode,77,cut.stderr)
                recovered=subprocess.run([*args,'none'],capture_output=True,text=True,timeout=30)
                self.assertEqual(recovered.returncode,0,recovered.stdout+recovered.stderr)
                self.assertEqual(json.loads(recovered.stdout)['data']['payload']['data']['tag'],'Waiting')
                self.assertFalse(log.exists());self.assertEqual(store.budget(run,self.who)['u']['reserved'],1)
