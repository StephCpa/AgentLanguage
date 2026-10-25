from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from al0.data import Intent, Principal, Slot
from al0.errors import AuthorizationDenied, BudgetExceeded, ReplayMismatch, StateConflict, TypeCheckError
from al0.execute import Broker, Interpreter, Response
from al0.flow import Label, PUBLIC, plan_label
from al0.ir import check_plan
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.admission_outcomes import alternatives, build, catalog, demo


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'runtime.sqlite'
        self.now=100
        self.store=Store(self.path,clock=lambda:self.now)
        self.who=Principal('tenant','user')
        self.specs=catalog()
        self.calls=[]
        def send(ticket):
            self.calls.append(ticket.intent.value['args'])
            return Response({'sent':True},{'u':1})
        self.broker=Broker(self.store,self.specs,{'send':send})
        self.plan=build(self.specs)
        self.run=self.store.create_run(self.who,self.plan.fingerprint,{'u':1})
        self.grant=self.store.grant(self.run,'send','output',expires=200)
        self.intent=Intent('send','1','output','hello')
        self.permit=self.store.authorize(self.run,self.who,self.intent,self.grant)

    def attempt(self,node='attempt',**kwargs):
        return self.store.try_admit(self.run,self.who,Slot(node),self.intent,self.permit,**kwargs)

    def hold_budget(self):
        permit=self.store.authorize(self.run,self.who,self.intent,self.grant)
        return self.store.admit(self.run,self.who,Slot('held'),self.intent,permit)

    def decisions(self):
        with self.store._tx() as db:
            return [dict(row) for row in db.execute('SELECT * FROM admission_decisions WHERE run=?',(self.run,))]

    def execute(self,plan=None):
        return Interpreter(self.broker).run(plan or self.plan,self.run,self.who,{'send':self.grant})

    def test_source_sdk_demo_covers_three_alternatives(self):
        results=demo(self.temp.name)['scenarios']
        for scenario,expected,count in [('accepted','Done',1),('budget','Deferred',0),('revoked','Denied',0)]:
            self.assertEqual(results[scenario]['result'],expected)
            self.assertEqual(results[scenario]['replay'],expected)
            self.assertEqual(len(results[scenario]['calls']),count)
            self.assertEqual(results[scenario]['replay_added_events'],0)

    def test_success_is_one_atomic_stable_decision(self):
        result=self.attempt()
        self.assertEqual(result['kind'],'Admitted')
        self.assertNotIn('permit',result)
        self.assertEqual(self.store.budget(self.run,self.who)['u']['reserved'],1)
        before=len(self.store.events(self.run,self.who))
        self.store.revoke(self.grant)
        self.assertEqual(self.attempt(),result)
        self.assertEqual(len(self.store.events(self.run,self.who)),before+1)
        self.assertEqual(len(self.decisions()),1)

    def test_rejection_never_reopens_after_budget_refund(self):
        held=self.hold_budget()
        rejected=self.attempt()
        self.assertEqual(rejected['kind'],'Rejected')
        self.assertEqual(rejected['permit'],self.permit)
        self.assertEqual(rejected['reason'],{'code':'AL1002'})
        self.store.cancel(held,self.who)
        self.assertEqual(self.attempt(),rejected)
        with self.assertRaises(BudgetExceeded):
            self.store.admit(self.run,self.who,Slot('attempt'),self.intent,self.permit)
        accepted=self.attempt('new-slot')
        self.assertEqual(accepted['kind'],'Admitted')
        # Historical returned value remains stable after its token is spent.
        self.assertEqual(self.attempt(),rejected)
        self.assertEqual(self.attempt('third')['permit'],None)

    def test_expired_permit_is_not_returned(self):
        self.now=200
        result=self.attempt()
        self.assertEqual(result['reason']['code'],'AL1000')
        self.assertIsNone(result['permit'])
        self.assertEqual(self.store.budget(self.run,self.who)['u']['reserved'],0)

    def test_revoked_permit_is_not_returned(self):
        self.store.revoke(self.grant)
        result=self.attempt()
        self.assertIsNone(result['permit'])
        self.assertEqual(result['reason']['code'],'AL1000')

    def test_expiry_between_budget_failure_and_return_check(self):
        self.hold_budget()
        times=iter([100,100,200])
        self.store.clock=lambda:next(times,200)
        result=self.attempt()
        self.assertIsNone(result['permit'])
        self.assertEqual(result['reason']['code'],'AL1000')

    def test_other_intent_cannot_recover_this_permit(self):
        other=Intent('send','1','output','different')
        result=self.store.try_admit(self.run,self.who,Slot('wrong'),other,self.permit)
        self.assertIsNone(result['permit'])
        self.assertEqual(result['reason']['code'],'AL1000')

    def test_decision_binds_descriptor_permit_and_allocation(self):
        self.attempt()
        other_permit=self.store.authorize(self.run,self.who,self.intent,self.grant)
        other_intent=Intent('send','1','output','other')
        child=self.store.split_budget(self.run,self.who,'empty',{'u':0})
        for intent,permit,allocation in [(other_intent,self.permit,None),(self.intent,other_permit,None),(self.intent,self.permit,child)]:
            with self.assertRaises(ReplayMismatch):
                self.store.try_admit(self.run,self.who,Slot('attempt'),intent,permit,allocation=allocation)

    def test_authentication_failure_is_not_published_as_a_rejection(self):
        with self.assertRaises(AuthorizationDenied):
            self.store.try_admit(self.run,Principal('tenant','intruder'),Slot('attempt'),self.intent,self.permit)
        self.assertEqual(self.decisions(),[])

    def test_stale_task_cannot_publish_a_new_admission_decision(self):
        scope=self.store.open_scope(self.run,self.who,'scope',{'u':1})
        task=self.store.spawn_task(scope,self.who,'child','program',{'u':1})
        old=self.store.claim_task(task,self.who)
        current=self.store.claim_task(task,self.who,recover=True)
        allocation=self.store.task_context(current,self.who,'program')['allocation']
        with self.assertRaises(StateConflict):
            self.store.try_admit(self.run,self.who,Slot('attempt',task),self.intent,self.permit,allocation=allocation,task_lease=old)
        self.assertEqual(self.decisions(),[])
        self.assertEqual(self.store.try_admit(self.run,self.who,Slot('attempt',task),self.intent,self.permit,
                                            allocation=allocation,task_lease=current)['kind'],'Admitted')

    def test_control_denial_is_labeled_and_does_not_return_permit(self):
        secret=Label({'owner'})
        result=self.attempt(control=secret)
        self.assertEqual(result['reason']['code'],'AL7001')
        self.assertIsNone(result['permit'])
        self.assertEqual(Label.from_data(result['label']),secret)
        self.assertEqual(self.attempt(control=PUBLIC),result)
        self.assertEqual(self.store.budget(self.run,self.who)['u']['free'],1)

    def test_partial_attempt_rolls_back_before_returning_permit(self):
        for point in ('after_reserve','after_permit_consumption'):
            def fault(actual):
                if actual==point:
                    raise BudgetExceeded('injected transaction failure')
            self.store.fault=fault
            result=self.attempt(point)
            self.assertEqual(result['permit'],self.permit)
            self.assertEqual(self.store.budget(self.run,self.who)['u']['free'],1)
            with self.store._tx() as db:
                self.assertIsNone(db.execute('SELECT consumed_by FROM permits WHERE id=?',(self.permit,)).fetchone()[0])
                self.assertEqual(db.execute('SELECT count(*) FROM operations').fetchone()[0],0)
        self.store.fault=lambda _:None
        self.assertEqual(self.attempt('clean')['kind'],'Admitted')

    def test_unexpected_failure_does_not_masquerade_as_rejection(self):
        def fault(point):
            if point=='after_permit_consumption':
                raise RuntimeError('injected')
        self.store.fault=fault
        with self.assertRaises(RuntimeError):
            self.attempt()
        self.assertEqual(self.decisions(),[])
        self.assertEqual(self.store.budget(self.run,self.who)['u']['free'],1)

    def test_parallel_same_slot_returns_identical_result(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.attempt(),range(8)))
        self.assertTrue(all(result==results[0] for result in results))
        self.assertEqual(len(self.decisions()),1)
        self.assertEqual(sum(e['kind']=='Admitted' for e in self.store.events(self.run,self.who)),1)

    def test_parallel_different_slots_cannot_accept_same_permit_twice(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda n:self.attempt('slot'+str(n)),range(4)))
        self.assertEqual(sum(r['kind']=='Admitted' for r in results),1)
        denied=[r for r in results if r['kind']=='Rejected']
        self.assertTrue(all(r['permit'] is None and r['reason']['code']=='AL1001' for r in denied))
        self.assertEqual(self.store.budget(self.run,self.who)['u']['reserved'],1)

    def test_old_successful_admission_can_be_read_through_sum_api(self):
        op=self.store.admit(self.run,self.who,Slot('attempt'),self.intent,self.permit)
        before=len(self.store.events(self.run,self.who))
        self.assertEqual(self.attempt()['operation'],op)
        self.assertEqual(len(self.store.events(self.run,self.who)),before)

    def test_v10_migration_preserves_existing_operation_and_permit(self):
        op=self.store.admit(self.run,self.who,Slot('attempt'),self.intent,self.permit)
        with self.store._tx() as db:
            db.execute('DROP TABLE admission_decisions')
            db.execute('PRAGMA user_version=10')
        self.store=Store(self.path,clock=lambda:self.now)
        self.assertEqual(self.attempt()['operation'],op)
        with self.store._tx() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],21)
            self.assertEqual(db.execute('SELECT consumed_by FROM permits WHERE id=?',(self.permit,)).fetchone()[0],op)

    def test_returned_branch_uses_new_slot_after_refund_and_replays_once(self):
        plan=build(self.specs,retry=True)
        self.run=self.store.create_run(self.who,plan.fingerprint,{'u':1})
        self.grant=self.store.grant(self.run,'send','output',expires=200)
        held=self.hold_budget()
        def fault(point):
            if point=='after_admission_decision_commit':
                raise RuntimeError('stop after committed rejection')
        self.store.fault=fault
        with self.assertRaises(RuntimeError):
            self.execute(plan)
        self.store.fault=lambda _:None
        self.store.cancel(held,self.who)
        self.assertEqual(self.execute(plan).data,'Done')
        before=len(self.store.events(self.run,self.who))
        self.assertEqual(self.execute(plan).data,'Done')
        self.assertEqual(len(self.store.events(self.run,self.who)),before)
        self.assertEqual(self.calls,['hello'])
        self.assertEqual(json.loads(self.decisions()[0]['result'])['kind'],'Rejected')

    def test_admitted_unknown_is_not_a_returned_permit(self):
        def timeout(ticket):
            self.calls.append('timeout')
            raise TimeoutError()
        self.broker.adapters['send']=timeout
        self.assertEqual(self.execute().data,'NeedsReconciliation')
        self.assertEqual(self.execute().data,'NeedsReconciliation')
        self.assertEqual(json.loads(self.decisions()[0]['result'])['kind'],'Admitted')
        self.assertEqual(self.calls,['timeout'])
        self.assertEqual(self.store.budget(self.run,self.who)['u']['reserved'],1)

    def test_business_rejection_does_not_return_consumed_permit_or_refund_cost(self):
        self.broker.adapters['send']=lambda ticket:Response({'rejected':True},{'u':1},outcome='ProvenNoEffect')
        self.assertEqual(self.execute().data,'Rejected')
        decision=json.loads(self.decisions()[0]['result'])
        self.assertEqual(decision['kind'],'Admitted')
        self.assertNotIn('permit',decision)
        self.assertEqual(self.store.budget(self.run,self.who)['u']['confirmed'],1)

    def test_returned_permit_must_pass_current_authority_at_its_next_slot(self):
        held=self.hold_budget()
        returned=self.attempt()
        self.assertEqual(returned['permit'],self.permit)
        self.store.cancel(held,self.who)
        self.store.revoke(self.grant)
        self.assertEqual(self.attempt(),returned)
        denied=self.attempt('later')
        self.assertIsNone(denied['permit'])
        self.assertEqual(denied['reason']['code'],'AL1000')
        self.assertEqual(self.store.budget(self.run,self.who)['u']['free'],1)

    def test_admission_must_be_matched(self):
        raw=json.loads(self.plan.encoded)
        raw['nodes'].pop()
        raw['return']='message'
        with self.assertRaisesRegex(TypeCheckError,'Admission must'):
            check_plan(raw,self.specs)

    def test_admission_match_must_cover_all_cases(self):
        raw=json.loads(self.plan.encoded)
        del raw['nodes'][-1]['cases']['NoPermit']
        with self.assertRaisesRegex(TypeCheckError,'exactly'):
            check_plan(raw,self.specs)

    def test_admission_cannot_be_matched_twice(self):
        raw=json.loads(self.plan.encoded)
        node=json.loads(json.dumps(raw['nodes'][-1]))
        node['id']='again'
        raw['nodes'].append(node)
        with self.assertRaisesRegex(TypeCheckError,'already moved'):
            check_plan(raw,self.specs)

    def test_original_permit_cannot_be_used_after_try_admit(self):
        raw=json.loads(self.plan.encoded)
        raw['nodes'].append({'id':'again','op':'admit','intent':'request','permit':'permission'})
        with self.assertRaisesRegex(TypeCheckError,'already moved'):
            check_plan(raw,self.specs)

    def test_returned_permit_cannot_bind_another_intent(self):
        raw=json.loads(self.plan.encoded)
        raw['nodes'].insert(3,{'id':'other','op':'prepare','tool':'send','input':'message'})
        raw['nodes'][-1]['inputs']['request']='other'
        with self.assertRaisesRegex(TypeCheckError,'another intent'):
            check_plan(raw,self.specs)

    def test_returned_permit_cannot_be_used_twice(self):
        raw=json.loads(build(self.specs,retry=True).encoded)
        branch=raw['nodes'][-1]['cases']['Returned']
        branch['nodes'].append({'id':'again','op':'admit','intent':'request','permit':'result'})
        with self.assertRaisesRegex(TypeCheckError,'already moved'):
            check_plan(raw,self.specs)

    def test_no_permit_branch_cannot_claim_a_permit(self):
        raw=json.loads(self.plan.encoded)
        raw['nodes'][-1]['cases']['NoPermit']=raw['nodes'][-1]['cases']['Returned']
        with self.assertRaisesRegex(TypeCheckError,'different outcome/evidence'):
            check_plan(raw,self.specs)

    def test_admitted_branch_cannot_drop_operation(self):
        raw=json.loads(self.plan.encoded)
        branch=raw['nodes'][-1]['cases']['Admitted']
        branch['nodes']=[branch['nodes'][0],{'id':'text','op':'literal','type':'Text','value':'lost'}]
        branch['return']='text'
        with self.assertRaisesRegex(TypeCheckError,'handled or returned'):
            check_plan(raw,self.specs)

    def test_result_and_reason_cannot_be_overridden_by_captures(self):
        for key in ('result','reason'):
            raw=json.loads(self.plan.encoded)
            raw['nodes'][-1]['inputs'][key]='message'
            with self.assertRaisesRegex(TypeCheckError,'reserved'):
                check_plan(raw,self.specs)

    def test_plan_label_includes_all_admission_alternatives(self):
        raw=json.loads(self.plan.encoded)
        branch=raw['nodes'][-1]['cases']['NoPermit']
        branch['nodes'].append({'id':'secret','op':'restrict','input':'text','label':Label({'owner'}).to_data()})
        branch['return']='secret'
        plan=check_plan(raw,self.specs)
        self.assertEqual(plan_label(plan,self.specs).readers,frozenset({'owner'}))

    def test_admission_plan_runs_inside_a_task(self):
        b=PlanBuilder('parent',self.specs)
        scope=b.scope('scope',{'u':1})
        task=b.spawn('child',scope,self.plan,{'u':1})
        result=b.join('result',task)
        b.close_scope('closed',scope)
        plan=b.finish(result)
        self.run=self.store.create_run(self.who,plan.fingerprint,{'u':1})
        self.grant=self.store.grant(self.run,'send','output',expires=200)
        try:
            self.assertEqual(self.execute(plan).data,'Done')
            self.assertEqual(self.execute(plan).data,'Done')
            self.assertEqual(self.calls,['hello'])
        finally:
            self.broker.wait_for_workers(timeout=10)

    def test_process_crashes_preserve_success_or_rejection(self):
        cases=[(1,point) for point in ('before_admission_decision_commit','after_admission_decision_commit','after_permit_consumption','after_admit_commit')]
        cases += [(0,point) for point in ('before_admission_decision_commit','after_admission_decision_commit')]
        for index,(amount,point) in enumerate(cases):
            with self.subTest(amount=amount,point=point):
                path=Path(self.temp.name)/('crash'+str(index)+'.sqlite')
                store=Store(path)
                run=store.create_run(self.who,self.plan.fingerprint,{'u':amount})
                grant=store.grant(run,'send','output',expires=store.clock()+1000)
                config=path.with_suffix('.json')
                log=path.with_suffix('.log')
                config.write_text(json.dumps({'database':str(path),'run':run,'grants':{'send':grant},'log':str(log)}),encoding='utf-8')
                crashed=subprocess.run([sys.executable,'-m','tests.admission_worker',str(config),point],capture_output=True,text=True,timeout=30)
                self.assertEqual(crashed.returncode,77,crashed.stderr)
                recovered=subprocess.run([sys.executable,'-m','tests.admission_worker',str(config),'none'],capture_output=True,text=True,timeout=30)
                self.assertEqual(recovered.returncode,0,recovered.stderr)
                self.assertEqual(json.loads(recovered.stdout)['data'],'Done' if amount else 'Deferred')
                calls=log.read_text(encoding='utf-8').splitlines() if log.exists() else []
                self.assertEqual(calls,['hello'] if amount else [])
                self.assertEqual(store.budget(run,self.who)['u']['confirmed'],amount)
                self.assertEqual(store.budget(run,self.who)['u']['reserved'],0)
