from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from threading import Event
import unittest

from al0.concurrency import TaskScope
from al0.data import Intent, Principal, Slot
from al0.demo import catalog
from al0.errors import EvidenceMismatch, InvalidData, ReplayMismatch, StateConflict, TypeCheckError
from al0.execute import Broker, Interpreter, Response, Value
from al0.ir import check_plan
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.task_outcomes import build, demo, text_branch


class InputStaticTests(unittest.TestCase):
    def setUp(self):
        self.specs=catalog()
        self.doc=json.loads(build(self.specs).encoded)

    def reject(self):
        with self.assertRaises(TypeCheckError):
            check_plan(self.doc,self.specs)

    def test_missing_parameter_binding_rejected(self):
        self.doc['nodes'][3]['inputs']={}
        self.reject()

    def test_extra_parameter_binding_rejected(self):
        self.doc['nodes'][3]['inputs']['extra']='draft'
        self.reject()

    def test_candidate_cannot_be_laundered_to_data(self):
        child=self.doc['nodes'][3]['plan']
        child['nodes'][0]['kind']='Data'
        self.reject()

    def test_candidate_cannot_claim_checked_parameter(self):
        parameter=self.doc['nodes'][3]['plan']['nodes'][0]
        parameter.update(kind='Checked',predicates=['TestsPassed'])
        self.reject()

    def test_schema_mismatch_rejected(self):
        self.doc['nodes'][3]['inputs']['patch']='request'
        self.reject()

    def test_scope_handle_cannot_be_captured_as_data(self):
        self.doc['nodes'][3]['inputs']['patch']='checks'
        self.reject()

    def test_unknown_parameter_predicate_rejected(self):
        parameter=self.doc['nodes'][3]['plan']['nodes'][0]
        parameter.update(kind='Checked',predicates=['Invented'])
        self.reject()

    def test_parameter_cannot_request_permit_kind(self):
        self.doc['nodes'][3]['plan']['nodes'][0]['kind']='Permit'
        self.reject()

    def test_outcome_must_be_matched(self):
        self.doc['nodes'].pop()
        self.doc['return']='closed'
        self.reject()

    def test_waiting_and_cancelled_cases_cannot_be_omitted(self):
        self.doc['nodes'][-1]['cases'].pop('Waiting')
        self.reject()

    def test_outcome_cannot_be_forged_from_plain_json(self):
        self.doc['nodes'][-1]['input']='request'
        self.reject()

    def test_failure_case_cannot_assume_successful_checked_result(self):
        self.doc['nodes'][-1]['cases']['Failed']=deepcopy(self.doc['nodes'][-1]['cases']['Completed'])
        self.reject()

    def test_branch_effect_must_be_in_parent_boundary(self):
        self.doc['uses'].remove('admit:repo.merge@repo')
        self.reject()

    def test_case_capture_cannot_replace_reserved_result(self):
        self.doc['nodes'][-1]['inputs']['result']='draft'
        self.reject()

    def test_case_result_types_must_match(self):
        self.doc['nodes'][-1]['cases']['Failed']['nodes'][0].update(type='Int',value=1)
        self.reject()

    def test_outcome_consumes_the_task_control_handle(self):
        self.doc['nodes'].insert(5,{'id':'again','op':'join_outcome','task':'checker'})
        self.reject()


class InputRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'runtime.sqlite')
        self.principal=Principal('tenant','subject')
        self.specs=catalog()
        self.broker=Broker(self.store,self.specs,{name:lambda t:Response(True,{'units':1}) for name in self.specs.tools})

    def tearDown(self):
        self.broker.wait_for_workers(timeout=5)

    def identity(self, kind='Data', schema='Json', predicates=()):
        builder=PlanBuilder('identity',self.specs)
        return builder.finish(builder.parameter('value',schema,kind=kind,predicates=predicates))

    def test_root_input_is_fixed_even_for_pure_programs(self):
        plan=self.identity()
        run=self.store.create_run(self.principal,plan.fingerprint,{'units':1})
        executor=Interpreter(self.broker)
        self.assertEqual(executor.run(plan,run,self.principal,{},inputs={'value':Value({'x':1})}).data,{'x':1})
        with self.assertRaises(ReplayMismatch):
            executor.run(plan,run,self.principal,{},inputs={'value':Value({'x':2})})

    def test_runtime_missing_and_extra_inputs_are_rejected(self):
        plan=self.identity()
        run=self.store.create_run(self.principal,plan.fingerprint,{'units':1})
        for inputs in ({},{'value':Value({}),'extra':Value({})}):
            with self.assertRaises(InvalidData):
                Interpreter(self.broker).run(plan,run,self.principal,{},inputs=inputs)

    def test_checked_input_requires_real_bound_evidence(self):
        plan=self.identity('Checked','Patch',['TestsPassed'])
        run=self.store.create_run(self.principal,plan.fingerprint,{'units':1})
        patch={'patch':'fix','base_version':7}
        for receipts in ((),('forged',)):
            with self.assertRaises(EvidenceMismatch):
                Interpreter(self.broker).run(plan,run,self.principal,{},inputs={'value':Value(patch,receipts)})
        receipt=self.store.record_check(run,self.principal,'TestsPassed','1',patch,{})
        value=Interpreter(self.broker).run(plan,run,self.principal,{},inputs={'value':Value(patch,(receipt,))})
        self.assertEqual(value.receipts,(receipt,))

    def test_foreign_and_modified_artifact_receipts_are_rejected(self):
        plan=self.identity('Checked','Patch',['TestsPassed'])
        run=self.store.create_run(self.principal,plan.fingerprint,{'units':1})
        other=self.store.create_run(self.principal,'other',{'units':1})
        patch={'patch':'fix','base_version':7}
        foreign=self.store.record_check(other,self.principal,'TestsPassed','1',patch,{})
        original=self.store.record_check(run,self.principal,'TestsPassed','1',patch,{})
        for data,receipt in [(patch,foreign),({**patch,'patch':'modified'},original)]:
            with self.assertRaises(EvidenceMismatch):
                Interpreter(self.broker).run(plan,run,self.principal,{},inputs={'value':Value(data,(receipt,))})

    def test_spawn_inputs_are_detached_and_key_is_bound_to_payload(self):
        plan=self.identity()
        run=self.store.create_run(self.principal,'host',{'units':2})
        value={'x':1}
        with TaskScope(self.broker,run,self.principal,'scope',{'units':2}) as scope:
            task,future=scope.spawn('child',plan,{'units':1},{},inputs={'value':Value(value)})
            value['x']=2
            with self.assertRaises(ReplayMismatch):
                scope.spawn('child',plan,{'units':1},{},inputs={'value':Value(value)})
            self.assertEqual(future.result().data,{'x':1})
        self.assertEqual(self.store.task_inputs(task,self.principal)['value']['data'],{'x':1})

    def test_explicit_cancellation_is_an_outcome_not_no_effect_proof(self):
        entered,release=Event(),Event()
        def model(ticket):
            entered.set()
            release.wait(timeout=5)
            return Response({'patch':'fix','base_version':7},{'units':2})
        self.broker=Broker(self.store,self.specs,{name:model for name in self.specs.tools})
        builder=PlanBuilder('child',self.specs)
        request=builder.literal('request','Text','work')
        plan=builder.finish(builder.infer('call',builder.tool('code-model'),request))
        run=self.store.create_run(self.principal,'host',{'units':4})
        grant=self.store.grant(run,'code-model','model',expires=self.store.clock()+1000)
        try:
            with TaskScope(self.broker,run,self.principal,'s',{'units':2}) as scope:
                task,future=scope.spawn('child',plan,{'units':2},{'code-model':grant})
                self.assertTrue(entered.wait(timeout=5))
                outcome=scope.cancel_outcome(task)
                self.assertEqual(outcome['kind'],'Cancelled')
                self.assertEqual(self.store.budget(run,self.principal)['units']['reserved'],2)
        finally:
            release.set()
        self.broker.wait_for_workers(timeout=5)
        self.assertEqual(self.store.task_outcome(task,self.principal),outcome)
        self.assertEqual(self.store.budget(run,self.principal)['units']['confirmed'],2)

    def test_success_failed_and_waiting_use_distinct_branches_without_retry(self):
        expected={'success':('Done',1,0),'failed':('NeedsRevision',0,0),'waiting':('NeedsReconciliation',0,1)}
        for mode,(message,operations,reserved) in expected.items():
            with self.subTest(mode=mode):
                result=demo(Path(self.temp.name)/mode,mode=mode)
                self.assertEqual(result['result'],message)
                self.assertEqual(result['replayed_result'],message)
                self.assertEqual(result['endpoint']['business_operations'],operations)
                self.assertEqual(result['budget']['units']['reserved'],reserved)
                self.assertEqual(result['replay_added_events'],0)

    def test_branch_capture_is_explicit_and_branch_sites_do_not_alias_parent(self):
        child=self.identity('Data','Text')
        branch=PlanBuilder('branch',self.specs)
        captured=branch.parameter('message','Text')
        branch.infer('same',branch.tool('code-model'),captured)
        message=branch.literal('return','Text','branch-done')
        case=branch.finish(message)
        parent=PlanBuilder('parent',self.specs)
        prompt=parent.literal('prompt','Text','parent')
        parent.infer('same',parent.tool('code-model'),prompt)
        text=parent.literal('captured','Text','branch')
        scope=parent.scope('scope',{'units':1})
        task=parent.spawn('child',scope,child,{},inputs={'value':prompt})
        outcome=parent.join_outcome('outcome',task)
        parent.close_scope('closed',scope)
        result=parent.match_task('selected',outcome,completed=case,failed=case,waiting=case,cancelled=case,inputs={'message':text})
        plan=parent.finish(result)
        calls=[]
        def model(ticket):
            calls.append(ticket.intent.value['args'])
            return Response({'patch':'fix','base_version':7},{'units':2})
        self.broker=Broker(self.store,self.specs,{name:model for name in self.specs.tools})
        run=self.store.create_run(self.principal,plan.fingerprint,{'units':6})
        grant=self.store.grant(run,'code-model','model',expires=self.store.clock()+1000)
        for _ in range(2):
            self.assertEqual(Interpreter(self.broker).run(plan,run,self.principal,{'code-model':grant}).data,'branch-done')
        self.assertEqual(calls,['parent','branch'])
        self.assertEqual(self.store.budget(run,self.principal)['units']['confirmed'],4)


class OutcomeLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'runtime.sqlite'
        self.store=Store(self.path)
        self.principal=Principal('tenant','subject')
        self.run=self.store.create_run(self.principal,'host',{'units':4})
        self.scope=self.store.open_scope(self.run,self.principal,'scope',{'units':4})

    def waiting(self):
        task=self.store.spawn_task(self.scope,self.principal,'child','program',{'units':2})
        lease=self.store.claim_task(task,self.principal)
        self.store.register_contract('model','1',{'units':1})
        grant=self.store.grant(self.run,'model','model',expires=self.store.clock()+1000)
        intent=Intent('model','1','model',{})
        permit=self.store.authorize(self.run,self.principal,intent,grant)
        allocation=self.store.task_snapshot(task,self.principal)['allocation']
        operation=self.store.admit(self.run,self.principal,Slot('call',task),intent,permit,allocation=allocation,task_lease=lease)
        attempt=self.store.snapshot(operation,self.principal)['attempts'][0]['id']
        self.store.start_attempt(operation,attempt,self.principal,task_lease=lease)
        self.store.wait_task(lease,self.principal,operation)
        return task,operation,attempt

    def crash(self, config, point):
        path=Path(self.temp.name)/'crash.json'
        path.write_text(json.dumps({'database':str(self.path),**config}),encoding='utf-8')
        result=subprocess.run([sys.executable,str(Path(__file__).with_name('outcome_worker.py')),str(path),point],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,77,result.stdout+result.stderr)
        self.store=Store(self.path)

    def test_outcome_and_handoff_roll_back_together_before_commit(self):
        task,_,_=self.waiting()
        self.crash({'action':'take','task':task},'before_task_outcome_commit')
        self.assertIsNone(self.store.task_outcome(task,self.principal))
        state=self.store.task_snapshot(task,self.principal)
        self.assertEqual((state['state'],state['disposition']),('Waiting','Attached'))
        self.assertEqual(self.store.take_task_outcome(task,self.principal)['kind'],'Waiting')

    def test_committed_waiting_outcome_survives_handoff_and_late_success(self):
        task,operation,attempt=self.waiting()
        self.crash({'action':'take','task':task},'after_task_outcome_commit')
        original=self.store.task_outcome(task,self.principal)
        self.assertEqual(original['kind'],'Waiting')
        self.assertEqual(self.store.task_snapshot(task,self.principal)['state'],'Cancelled')
        self.store.business_result(operation,self.principal,provider='model',evidence_id='late',outcome='Applied',details={'output':'late'})
        self.store.settle(attempt,self.principal,provider='model',evidence_id='bill',actual={'units':1})
        self.assertEqual(original,self.store.take_task_outcome(task,self.principal))
        self.assertEqual(self.store.task_snapshot(task,self.principal)['obligations'],[])

    def test_crashed_spawn_keeps_input_and_budget_in_same_commit(self):
        inputs={'value':{'data':{'x':1},'receipts':[]}}
        self.crash({'action':'spawn','scope':self.scope,'inputs':inputs},'after_spawn_commit')
        task=self.store.spawn_task(self.scope,self.principal,'with-input','program',{'units':1},inputs=inputs)
        self.assertEqual(self.store.task_inputs(task,self.principal),inputs)
        with self.assertRaises(ReplayMismatch):
            self.store.spawn_task(self.scope,self.principal,'with-input','program',{'units':1},inputs={'value':{'data':{'x':2},'receipts':[]}})

    def test_v6_migration_treats_old_tasks_as_empty_inputs_without_new_outcomes(self):
        task,_,_=self.waiting()
        before=self.store.budget(self.run,self.principal)
        db=sqlite3.connect(self.path)
        db.executescript('DROP TABLE task_inputs; DROP TABLE run_inputs; DROP TABLE task_outcomes; PRAGMA user_version=6;')
        db.close()
        self.store=Store(self.path)
        self.assertEqual(self.store.task_inputs(task,self.principal),{})
        self.assertIsNone(self.store.task_outcome(task,self.principal))
        self.assertEqual(self.store.budget(self.run,self.principal),before)
        self.assertEqual(self.store.take_task_outcome(task,self.principal)['kind'],'Waiting')


if __name__=='__main__':
    unittest.main()
