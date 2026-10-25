from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from al0.data import Principal
from al0.errors import AuthorizationDenied, BudgetExceeded, ContractError, FlowDenied, InvalidData, ReplayMismatch, TypeCheckError
from al0.execute import Broker, Interpreter, Response, Suspended, Value
from al0.flow import Label
from al0.inference import result_type
from al0.ir import Catalog, Type, check_plan
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.inference_results import catalog, demo
from tests.inference_worker import plan


class InferenceResultTests(unittest.TestCase):
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

    def execute(self,**kwargs):
        return Interpreter(self.broker).run(self.p,self.run,self.who,self.grants,**kwargs)

    def budget(self):return self.store.budget(self.run,self.who)['u']

    def count_results(self):
        with closing(sqlite3.connect(self.path)) as db:return db.execute('SELECT COUNT(*) FROM inference_results').fetchone()[0]

    def repeat(self):
        result=self.execute();before=len(self.store.events(self.run,self.who))
        self.assertEqual(self.execute(),result)
        self.assertEqual(len(self.store.events(self.run,self.who)),before)
        self.assertEqual(len(self.calls),1)
        return result

    def test_example_source_sdk_and_endpoint_costs(self):
        report=demo(Path(self.temp.name)/'example')
        self.assertTrue(report['identical_ir'])
        for mode,result in report['cases'].items():
            self.assertEqual(result['replay_added_events'],0)
            self.assertEqual(result['result'],'Done' if mode=='Success' else mode)
            self.assertEqual(result['budget']['u']['confirmed'],3 if mode=='Success' else 1)
            self.assertEqual(len(result['calls']),3 if mode=='Success' else 1)

    def test_success_retains_candidate_interface(self):
        self.start();value=self.repeat()
        self.assertEqual(dict(value.variant.members)['Success'],Type('Candidate','Text'))
        self.assertEqual(value.data['payload']['data'],'candidate')
        self.assertEqual(self.count_results(),1)

    def test_invalid_output_is_explicit_and_billed(self):
        self.response=Response({'private':'bad output'},{'u':1});self.start()
        value=self.repeat()
        self.assertEqual(value.data['tag'],'Error')
        self.assertEqual(value.data['payload']['data'],{'code':'InvalidOutput','tool':'model','version':'1'})
        self.assertNotIn('private',json.dumps(value.record()))
        self.assertEqual(self.budget()['confirmed'],1)

    def test_proven_no_effect_does_not_imply_free(self):
        self.response=Response(None,{'u':1},'ProvenNoEffect');self.start()
        self.assertEqual(self.repeat().data['payload']['data']['code'],'NoResult')
        self.assertEqual(self.budget()['confirmed'],1)

    def test_error_without_bill_preserves_reservation_and_late_bill(self):
        self.response=Response(17,None);self.start();result=self.repeat()
        self.assertEqual(self.budget()['reserved'],1)
        attempt=self.calls[0].attempt
        self.store.settle(attempt,self.who,provider='model',evidence_id='late-bill',actual={'u':1})
        self.assertEqual(self.execute(),result)
        self.assertEqual(self.budget()['reserved'],0);self.assertEqual(self.budget()['confirmed'],1)

    def test_success_without_bill_keeps_responsibility(self):
        self.response=Response('candidate',None);self.start()
        self.assertEqual(self.repeat().data['tag'],'Success')
        self.assertEqual(self.budget()['reserved'],1)

    def suspended(self,response,business):
        self.response=response;self.start()
        for _ in range(2):
            with self.assertRaises(Suspended) as caught:self.execute()
            self.assertEqual(caught.exception.business,business)
        self.assertEqual(len(self.calls),1);self.assertEqual(self.count_results(),0)
        self.assertEqual(self.budget()['reserved'],1)

    def test_timeout_is_not_terminal_error_or_implicit_retry(self):self.suspended(TimeoutError(),'Unknown')
    def test_connection_loss_is_not_terminal_error_or_implicit_retry(self):self.suspended(ConnectionError(),'Unknown')
    def test_pending_is_not_terminal_error(self):self.suspended(Response(None,None,'Pending'),'Pending')

    def test_unknown_resumes_from_external_fact_without_new_call(self):
        self.suspended(TimeoutError(),'Unknown');ticket=self.calls[0]
        self.store.business_result(ticket.operation,self.who,provider='model',evidence_id='late-output',
                                   outcome='Applied',details={'output':'recovered'})
        self.assertEqual(self.repeat().data['payload']['data'],'recovered')
        self.assertEqual(self.budget()['reserved'],1)

    def test_unknown_to_proven_no_effect_returns_error(self):
        self.suspended(ConnectionError(),'Unknown');ticket=self.calls[0]
        self.store.business_result(ticket.operation,self.who,provider='model',evidence_id='no-effect',outcome='ProvenNoEffect')
        self.assertEqual(self.repeat().data['payload']['data']['code'],'NoResult')

    def test_adapter_contract_failure_is_not_normal_error(self):
        self.response={'not':'Response'};self.start()
        with self.assertRaises(ContractError):self.execute()
        self.assertEqual(self.count_results(),0);self.assertEqual(self.budget()['reserved'],1)

    def test_internal_exception_is_not_normal_error(self):
        self.response=RuntimeError('host bug');self.start()
        with self.assertRaises(RuntimeError):self.execute()
        self.assertEqual(self.count_results(),0)

    def test_no_grant_is_not_model_error(self):
        self.start();self.grants={}
        with self.assertRaises(AuthorizationDenied):self.execute()
        self.assertEqual(self.calls,[]);self.assertEqual(self.count_results(),0)

    def test_budget_failure_is_not_model_error(self):
        self.start(amount=0)
        with self.assertRaises(BudgetExceeded):self.execute()
        self.assertEqual(self.calls,[]);self.assertEqual(self.count_results(),0)

    def test_root_input_validation_is_not_model_output_error(self):
        b=PlanBuilder('input',self.c)
        self.start(b.finish(b.infer_result('result',b.tool('model'),b.parameter('prompt','Text'))))
        with self.assertRaises(InvalidData):self.execute(inputs={'prompt':Value(17)})
        self.assertEqual(self.calls,[]);self.assertEqual(self.count_results(),0)

    def test_pure_function_cannot_infer_result(self):
        b=PlanBuilder('pure',self.c,pure=True)
        value=b.infer_result('result',b.tool('model'),b.literal('prompt','Text','question'))
        with self.assertRaises(TypeCheckError):b.finish(value)

    def test_wrong_tool_kind_rejected(self):
        b=PlanBuilder('wrong',self.c)
        with self.assertRaises(TypeCheckError):b.infer_result('result',b.tool('send'),b.literal('prompt','Text','question'))

    def test_missing_infer_effect_rejected(self):
        document=json.loads(plan(self.c).encoded);document['uses']=[]
        with self.assertRaises(TypeCheckError):check_plan(document,self.c)

    def test_success_payload_cannot_be_declared_checked(self):
        success=PlanBuilder('success',self.c)
        success_plan=success.finish(success.parameter('result','Text',kind='Checked',predicates=['valid']))
        error=PlanBuilder('error',self.c);error.parameter('result','InferenceError')
        fallback=error.literal('fallback','Text','fallback')
        error_plan=error.finish(error.check('checked',error.tool('verify'),fallback))
        b=PlanBuilder('wrong-evidence',self.c)
        value=b.infer_result('value',b.tool('model'),b.literal('prompt','Text','question'))
        output=b.match_variant('result',value,{'Success':success_plan,'Error':error_plan})
        with self.assertRaises(TypeCheckError):b.finish(output)

    def test_error_schema_is_fixed_and_has_field_access(self):
        self.assertEqual(self.c.record_fields('InferenceError'),{'code':'Text','tool':'Text','version':'Text'})
        with self.assertRaises(InvalidData):self.c.schema('InferenceError',{'raw':'Text'})
        with self.assertRaises(InvalidData):self.c.validate('InferenceError',{'code':'InvalidOutput','tool':'model'})

    def test_stored_result_is_used_before_output_revalidation(self):
        self.response=Response(17,{'u':1});self.start();first=self.execute()
        validate=self.c.validate
        self.c.validate=lambda name,value:None if name=='Text' and value==17 else validate(name,value)
        self.assertEqual(self.execute(),first);self.assertEqual(len(self.calls),1)

    def test_changed_contract_is_rejected_for_stored_result(self):
        self.start();self.execute()
        with self.assertRaises(ReplayMismatch):self.store.inference_result(self.calls[0].operation,self.who,{'changed':True})

    def test_error_keeps_private_input_and_model_taint(self):
        b=PlanBuilder('private',self.c)
        prompt=b.restrict('private',b.literal('prompt','Text','secret'),Label({'tool:model','owner'},{'secret'}))
        self.start(b.finish(b.infer_result('result',b.tool('model'),prompt)));self.response=Response(17,{'u':1})
        value=self.repeat()
        self.assertEqual(value.label.readers,frozenset({'tool:model','owner'}))
        self.assertTrue({'secret','model:model'}<=value.label.taints)
        self.assertEqual(Value.from_record(value.data['payload']).label,value.label)

    def test_error_match_control_prevents_public_export(self):
        error=PlanBuilder('export',self.c);error.parameter('result','InferenceError')
        public=error.literal('public','Text','error happened')
        exported=error.finish(error.check('export',error.tool('verify'),public))
        ok=PlanBuilder('ok',self.c);candidate=ok.parameter('result','Text',kind='Candidate')
        success=ok.finish(ok.check('checked',ok.tool('verify'),candidate))
        b=PlanBuilder('export-control',self.c)
        prompt=b.restrict('private',b.literal('prompt','Text','question'),Label({'owner','tool:model'}))
        value=b.infer_result('value',b.tool('model'),prompt)
        self.start(b.finish(b.match_variant('result',value,{'Success':success,'Error':exported})))
        self.response=Response(17,{'u':1})
        with self.assertRaises(FlowDenied):self.execute()
        self.assertEqual(len(self.calls),1)

    def test_direct_task_return_keeps_result_without_local_match(self):
        self.response=Response(17,{'u':1});self.start(plan(self.c,True))
        self.assertEqual(self.repeat().data['payload']['data']['code'],'InvalidOutput')
        self.assertEqual(self.count_results(),1)

    def test_old_infer_still_raises_schema_error(self):
        b=PlanBuilder('old',self.c);self.start(b.finish(b.infer('value',b.tool('model'),b.literal('prompt','Text','question'))))
        self.response=Response(17,{'u':1})
        with self.assertRaises(InvalidData):self.execute()
        self.assertEqual(self.budget()['confirmed'],1);self.assertEqual(self.count_results(),0)

    def crash(self,point,*,task=False,invalid=True):
        self.start(plan(self.c,task));config=Path(self.temp.name)/'config.json';log=Path(self.temp.name)/'endpoint.jsonl'
        config.write_text(json.dumps({'database':str(self.path),'run':self.run,'grants':self.grants,
                                     'task':task,'invalid':invalid,'log':str(log)}),encoding='utf-8')
        args=[sys.executable,'-m','tests.inference_worker',str(config)]
        stopped=subprocess.run([*args,point],capture_output=True,text=True,timeout=30)
        self.assertEqual(stopped.returncode,77,stopped.stdout+stopped.stderr)
        facts=[json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(facts),1);fact=facts[0]
        if point=='before_call_business_record':
            with self.assertRaises(Suspended):self.execute()
            self.store.business_result(fact['operation'],self.who,provider='model',evidence_id='recovered-business',
                                       outcome='Applied',details={'output':fact['output']})
        recovered=subprocess.run([*args,'none'],capture_output=True,text=True,timeout=30)
        self.assertEqual(recovered.returncode,0,recovered.stdout+recovered.stderr)
        result=json.loads(recovered.stdout)
        self.assertEqual(result['data']['tag'],'Error' if invalid else 'Success')
        self.assertEqual(len(log.read_text(encoding='utf-8').splitlines()),1)
        if point in ('before_call_business_record','after_call_business_record'):
            self.assertEqual(self.budget()['reserved'],1)
            self.store.settle(fact['attempt'],self.who,provider='model',evidence_id='recovered-bill',actual={'u':1})
        self.assertEqual(self.budget()['confirmed'],1);self.assertEqual(self.count_results(),1)
        before=len(self.store.events(self.run,self.who));self.assertEqual(self.execute().record(),result)
        self.assertEqual(len(self.store.events(self.run,self.who)),before)

    def test_process_exit_before_business_record(self):self.crash('before_call_business_record')
    def test_process_exit_after_business_record(self):self.crash('after_call_business_record')
    def test_process_exit_after_billing_record(self):self.crash('after_call_billing_record')
    def test_process_exit_before_result_commit(self):self.crash('before_inference_result_commit')
    def test_process_exit_after_result_commit(self):self.crash('after_inference_result_commit')
    def test_process_exit_after_success_result_commit(self):self.crash('after_inference_result_commit',invalid=False)
    def test_process_exit_before_task_result_commit(self):self.crash('before_inference_result_commit',task=True)
    def test_process_exit_before_task_publish(self):self.crash('before_task_finish_commit',task=True)
    def test_process_exit_after_task_publish(self):self.crash('after_task_finish_commit',task=True)
    def test_process_exit_before_task_receive(self):self.crash('before_task_join_commit',task=True)
    def test_process_exit_after_task_receive(self):self.crash('after_task_join_commit',task=True)

    def migrate(self,point):
        self.start();self.execute()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DROP TABLE inference_results');db.execute('PRAGMA user_version=17')
        config=Path(self.temp.name)/'migration.json';config.write_text(json.dumps({'database':str(self.path),'migrate':True}),encoding='utf-8')
        stopped=subprocess.run([sys.executable,'-m','tests.inference_worker',str(config),point],capture_output=True,text=True,timeout=30)
        self.assertEqual(stopped.returncode,77,stopped.stdout+stopped.stderr)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],17 if point.startswith('before') else 21)
        Store(self.path)
        self.assertEqual(self.execute().data['tag'],'Success');self.assertEqual(len(self.calls),1)
        self.assertEqual(self.budget()['confirmed'],1)

    def test_process_exit_before_v17_migration_commit(self):self.migrate('before_attempt_funding_migration_commit')
    def test_process_exit_after_v17_migration_commit(self):self.migrate('after_attempt_funding_migration_commit')
