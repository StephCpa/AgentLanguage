import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from al0.data import Intent, Principal, Slot
from al0.errors import BudgetExceeded, ExecutionLimitExceeded, InvalidData, ReplayMismatch, StateConflict, TypeCheckError
from al0.execute import Broker, Interpreter, Response, Suspended, Value
from al0.flow import Label, plan_label
from al0.frontend import compile_source
from al0.ir import Tool, check_plan
from al0.limits import ExecutionBudget
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.budget_workflows import build, catalog, demo, emitter


class BudgetWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'runtime.sqlite'
        self.store=Store(self.path)
        self.who=Principal('tenant','user')
        self.specs=catalog()
        self.calls=[]
        def emit(ticket):
            self.calls.append(ticket.intent.value['args'])
            return Response(ticket.intent.value['args'],{'u':1})
        self.emit=emit
        self.broker=Broker(self.store,self.specs,{'emit':emit})

    def start(self,plan,budget=10):
        self.run=self.store.create_run(self.who,plan.fingerprint,{'u':budget})
        self.grants={name:self.store.grant(self.run,name,spec.scope,expires=self.store.clock()+1000) for name,spec in self.specs.tools.items()}

    def execute(self,plan,**kwargs):
        return Interpreter(self.broker).run(plan,self.run,self.who,self.grants,**kwargs)

    def root(self,amount=2):
        b=PlanBuilder('parent',self.specs)
        text=b.literal('text','Text','hello')
        funds=b.split_budget('funds',{'u':amount})
        return b,text,funds

    def task_plan(self,child,*,outcome=False):
        b=PlanBuilder('parent-task',self.specs)
        scope=b.scope('scope',{'u':5})
        task=b.spawn('task',scope,child,{'u':5})
        if outcome:
            observation=b.join_outcome('outcome',task)
            cases=[]
            for state in ('Completed','Failed','Waiting','Cancelled'):
                branch=PlanBuilder(state,self.specs)
                cases.append(branch.finish(branch.literal('value','Text',state)))
            result=b.match_task('result',observation,completed=cases[0],failed=cases[1],waiting=cases[2],cancelled=cases[3])
        else:
            result=b.join('result',task)
        b.close_scope('closed',scope)
        return b.finish(result)

    def task_owner(self,*,speculative=False):
        scope=self.store.open_scope(self.run,self.who,'scope',{'u':5},speculative=speculative)
        task=self.store.spawn_task(scope,self.who,'task','program',{'u':5})
        lease=self.store.claim_task(task,self.who)
        allocation=self.store.task_context(lease,self.who,'program')['allocation']
        return task,lease,allocation

    def test_source_sdk_demo_and_replay(self):
        result=demo(self.temp.name)
        self.assertEqual(result['result'],'hello')
        self.assertEqual(result['replay'],'hello')
        self.assertEqual(result['calls'],['hello'])
        self.assertEqual((result['splits'],result['joins'],result['invocations']),(2,2,2))
        self.assertEqual(result['replay_added_events'],0)
        self.assertEqual(result['budget']['u']['confirmed'],1)
        self.assertTrue(all(row['state']=='Joined' for row in result['allocations'][1:]))

    def test_empty_child_cannot_spend_root_credit(self):
        b,text,funds=self.root(0)
        result=b.invoke('result',emitter(self.specs),inputs={'message':text},budget=funds)
        b.join_budget('closed',funds)
        plan=b.finish(result)
        self.start(plan)
        with self.assertRaises(BudgetExceeded):
            self.execute(plan)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.store.budget(self.run,self.who)['u']['free'],10)

    def test_budget_parameter_can_return_to_caller_once(self):
        child=PlanBuilder('identity',self.specs)
        identity=child.finish(child.budget_parameter('funds'))
        b,text,funds=self.root()
        moved=b.invoke('moved',identity,inputs={'funds':funds})
        result=b.invoke('result',emitter(self.specs),inputs={'message':text},budget=moved)
        b.join_budget('closed',moved)
        plan=b.finish(result)
        self.start(plan)
        self.assertEqual(self.execute(plan).data,'hello')
        self.assertEqual(self.execute(plan).data,'hello')
        self.assertEqual(self.calls,['hello'])

    def test_workflow_can_return_a_new_budget(self):
        child=PlanBuilder('factory',self.specs)
        factory=child.finish(child.split_budget('funds',{'u':2}))
        b=PlanBuilder('parent',self.specs)
        funds=b.invoke('funds',factory)
        result=b.join_budget('closed',funds)
        plan=b.finish(result)
        self.start(plan)
        self.assertTrue(self.execute(plan).data)
        self.assertTrue(self.execute(plan).data)
        self.assertEqual(len(self.store.allocation_tree(self.run,self.who)),2)

    def test_split_from_parent_and_join_bottom_up(self):
        b,text,parent=self.root(4)
        child=b.split_budget('child',{'u':2},parent=parent)
        result=b.invoke('result',emitter(self.specs),inputs={'message':text},budget=child)
        b.join_budget('child-closed',child)
        b.join_budget('parent-closed',parent)
        plan=b.finish(result)
        self.start(plan)
        self.assertEqual(self.execute(plan).data,'hello')
        tree=self.store.allocation_tree(self.run,self.who)
        self.assertEqual(tree[2]['parent'],tree[1]['id'])
        self.assertEqual(tree[0]['budget']['u']['free'],9)
        self.assertEqual(tree[2]['budget']['u']['confirmed'],1)

    def test_join_parent_with_open_child_is_a_dynamic_error(self):
        b,_,parent=self.root(4)
        child=b.split_budget('child',{'u':2},parent=parent)
        b.join_budget('parent-closed',parent)
        plan=b.finish(b.join_budget('child-closed',child))
        self.start(plan)
        with self.assertRaisesRegex(StateConflict,'open child'):
            self.execute(plan)
        self.assertTrue(all(row['state']=='Open' for row in self.store.allocation_tree(self.run,self.who)))

    def test_budget_can_move_through_both_branch_alternatives(self):
        b=PlanBuilder('branch',self.specs)
        branch=b.finish(b.budget_parameter('funds'))
        b,_,funds=self.root()
        condition=b.literal('condition','Bool',False)
        moved=b.if_else('moved',condition,then=branch,otherwise=branch,inputs={'funds':funds})
        plan=b.finish(b.join_budget('closed',moved))
        self.start(plan)
        self.assertTrue(self.execute(plan).data)
        self.assertTrue(self.execute(plan).data)

    def test_budget_must_be_joined_moved_or_returned(self):
        b,text,_=self.root()
        with self.assertRaisesRegex(TypeCheckError,'every budget'):
            b.finish(text)

    def test_budget_cannot_be_used_after_join(self):
        b,text,funds=self.root()
        b.join_budget('closed',funds)
        result=b.invoke('result',emitter(self.specs),inputs={'message':text},budget=funds)
        with self.assertRaisesRegex(TypeCheckError,'already moved'):
            b.finish(result)

    def test_budget_cannot_be_duplicated_in_call_parameters(self):
        child=PlanBuilder('two',self.specs)
        a=child.budget_parameter('a')
        c=child.budget_parameter('b')
        child.join_budget('a-closed',a)
        workflow=child.finish(child.join_budget('b-closed',c))
        b,_,funds=self.root()
        value=b.invoke('result',workflow,inputs={'a':funds,'b':funds})
        with self.assertRaisesRegex(TypeCheckError,'multiple names'):
            b.finish(value)

    def test_funding_budget_cannot_also_be_moved_to_callee(self):
        child=PlanBuilder('close',self.specs)
        workflow=child.finish(child.join_budget('closed',child.budget_parameter('funds')))
        b,_,funds=self.root()
        value=b.invoke('result',workflow,inputs={'funds':funds},budget=funds)
        with self.assertRaisesRegex(TypeCheckError,'same budget'):
            b.finish(value)

    def test_budget_parameters_cannot_be_forged_at_root_entry(self):
        b=PlanBuilder('close',self.specs)
        plan=b.finish(b.join_budget('closed',b.budget_parameter('funds')))
        self.start(plan)
        with self.assertRaisesRegex(InvalidData,'checked branch captures'):
            self.execute(plan,inputs={'funds':Value(self.store.root_allocation(self.run,self.who))})

    def test_budget_task_result_requires_its_received_reference(self):
        child=PlanBuilder('factory',self.specs)
        factory=child.finish(child.split_budget('funds',{'u':1}))
        plan=self.task_plan(factory)
        self.start(plan)
        result=self.execute(plan)
        self.assertEqual((result.resource.kind,result.resource.generation),('Budget',2))
        with self.assertRaises(StateConflict):
            self.store.join_budget(self.run,self.who,result.data)
        with self.store.resource_access(self.run,self.who,[result.resource]):
            self.store.join_budget(self.run,self.who,result.data)
        count=len(self.store.events(self.run,self.who))
        self.assertEqual(self.execute(plan),result)
        self.assertEqual(len(self.store.events(self.run,self.who)),count)

    def action_call(self):
        self.specs.register(Tool.define('send','action','Text','Json','output',{'u':1}))
        self.broker=Broker(self.store,self.specs,{'emit':self.emit,'send':self.emit})
        child=PlanBuilder('submit',self.specs)
        request=child.intent_parameter('request',child.tool('send'))
        permit=child.permit_parameter('permit',request)
        workflow=child.finish(child.admit('op',request,permit))
        b=PlanBuilder('parent',self.specs)
        text=b.literal('text','Text','hello')
        intent=b.prepare('request',b.tool('send'),text)
        permit=b.authorize('permit',intent)
        return b,intent,permit,workflow

    def test_invoke_moves_permit_and_returns_operation_responsibility(self):
        b,intent,permit,workflow=self.action_call()
        op=b.invoke('op',workflow,inputs={'request':intent,'permit':permit})
        from examples.resource_branches import handled
        plan=b.finish(handled(b,op))
        self.start(plan)
        self.assertEqual(self.execute(plan).data,'Done')
        self.assertEqual(self.execute(plan).data,'Done')
        self.assertEqual(self.calls,['hello'])

    def test_invoke_does_not_rebind_or_duplicate_a_permit(self):
        b,intent,permit,workflow=self.action_call()
        other=b.prepare('other',b.tool('send'),b.literal('other-text','Text','different'))
        result=b.invoke('op',workflow,inputs={'request':other,'permit':permit})
        with self.assertRaisesRegex(TypeCheckError,'another intent'):
            b.finish(result)
        raw=json.loads(workflow.encoded)
        raw['nodes'].insert(2,{'id':'extra','op':'parameter','kind':'Permit','type':'request','predicates':[]})
        two=check_plan(raw,self.specs)
        b=PlanBuilder('duplicate',self.specs)
        intent=b.prepare('request',b.tool('send'),b.literal('text','Text','hello'))
        permit=b.authorize('permit',intent)
        result=b.invoke('op',two,inputs={'request':intent,'permit':permit,'extra':permit})
        with self.assertRaisesRegex(TypeCheckError,'multiple names'):
            b.finish(result)

    def test_caller_cannot_reuse_moved_permit(self):
        b,intent,permit,workflow=self.action_call()
        result=b.invoke('op',workflow,inputs={'request':intent,'permit':permit})
        b.admit('again',intent,permit)
        with self.assertRaisesRegex(TypeCheckError,'already moved'):
            b.finish(result)

    def test_source_nested_split_and_budget_return(self):
        source='''workflow identity(funds: Budget) -> Budget uses {} { return funds; }
workflow main() -> Bool uses {"budget"} {
 let parent=split_budget({u:4}); let child=split_budget_from(parent,{u:2});
 let moved=invoke(identity,{funds:child}); let c=join_budget(moved);
 let p=join_budget(parent); return p;
}'''
        plan=compile_source(source,self.specs).plan
        self.start(plan)
        self.assertTrue(self.execute(plan).data)
        self.assertTrue(self.execute(plan).data)

    def test_invoke_requires_exact_inputs_and_parent_effects(self):
        b=PlanBuilder('parent',self.specs,uses=[])
        text=b.literal('text','Text','hello')
        result=b.invoke('result',emitter(self.specs),inputs={'message':text})
        with self.assertRaisesRegex(TypeCheckError,'effects'):
            b.finish(result)
        raw=json.loads(build(self.specs).encoded)
        raw['nodes'][3]['inputs'].pop('message')
        with self.assertRaisesRegex(TypeCheckError,'exactly'):
            check_plan(raw,self.specs)

    def test_invoke_rejects_pure_function_and_resource_use_in_pure_code(self):
        f=PlanBuilder('fn',self.specs,pure=True)
        fn=f.finish(f.literal('value','Bool',True))
        b=PlanBuilder('parent',self.specs)
        with self.assertRaisesRegex(TypeCheckError,'use call'):
            b.finish(b.invoke('result',fn))
        f=PlanBuilder('fn',self.specs,pure=True)
        with self.assertRaises(TypeCheckError):
            f.finish(f.split_budget('funds',{'u':1}))

    def test_invocation_frame_freezes_inputs_and_funding(self):
        self.start(build(self.specs))
        first=self.store.begin_invocation(self.run,self.who,'call',{'x':Value('first').record()})
        replay=self.store.begin_invocation(self.run,self.who,'call',{'x':Value('later').record()})
        self.assertEqual(first,replay)
        child=self.store.split_budget(self.run,self.who,'child',{'u':1})
        with self.assertRaises(ReplayMismatch):
            self.store.begin_invocation(self.run,self.who,'call',{'x':Value('first').record()},allocation=child)

    def test_shared_fuel_exhaustion_retains_allocations_for_resume(self):
        plan=build(self.specs)
        self.start(plan)
        with self.assertRaises(ExecutionLimitExceeded):
            self.execute(plan,execution=ExecutionBudget(4))
        self.assertEqual(self.calls,[])
        self.assertEqual(self.store.allocation_tree(self.run,self.who)[0]['budget']['u']['free'],5)
        self.assertEqual(self.execute(plan).data,'hello')
        self.assertEqual(self.calls,['hello'])
        self.assertEqual(self.store.allocation_tree(self.run,self.who)[0]['budget']['u']['free'],9)

    def test_suspension_inside_budget_retains_its_reservation(self):
        def timeout(ticket):
            self.calls.append(ticket.intent.value['args'])
            raise TimeoutError()
        self.broker.adapters['emit']=timeout
        plan=build(self.specs)
        self.start(plan)
        with self.assertRaises(Suspended) as caught:
            self.execute(plan)
        operation=caught.exception.operation
        state=self.store.snapshot(operation,self.who)
        self.assertEqual(self.store.budget(self.run,self.who)['u']['reserved'],1)
        with self.assertRaises(Suspended):
            self.execute(plan)
        self.assertEqual(self.calls,['hello'])
        self.store.business_result(operation,self.who,provider='emit',evidence_id='late-business',outcome='Applied',details={'output':'hello'})
        self.store.settle(state['attempts'][0]['id'],self.who,provider='emit',evidence_id='late-bill',actual={'u':1})
        self.assertEqual(self.execute(plan).data,'hello')
        self.assertTrue(all(row['state']=='Joined' for row in self.store.allocation_tree(self.run,self.who)[1:]))

    def test_task_owned_budgets_charge_descendant_and_replay(self):
        plan=self.task_plan(build(self.specs))
        self.start(plan)
        try:
            self.assertEqual(self.execute(plan).data,'hello')
            before=len(self.store.events(self.run,self.who))
            self.assertEqual(self.execute(plan).data,'hello')
            self.assertEqual(len(self.store.events(self.run,self.who)),before)
            self.assertEqual(self.calls,['hello'])
            tree=self.store.allocation_tree(self.run,self.who)
            self.assertTrue(all(row['state']=='Joined' for row in tree[1:]))
            self.assertEqual(sum(row['budget']['u']['confirmed'] for row in tree),1)
        finally:
            self.broker.wait_for_workers(timeout=10)

    def test_task_wait_handoff_joins_free_descendants_but_retains_exposed_cost(self):
        def timeout(ticket):
            self.calls.append(ticket.intent.value['args'])
            raise TimeoutError()
        self.broker.adapters['emit']=timeout
        plan=self.task_plan(build(self.specs),outcome=True)
        self.start(plan)
        try:
            self.assertEqual(self.execute(plan).data,'Waiting')
            tree=self.store.allocation_tree(self.run,self.who)
            self.assertTrue(all(row['state']=='Joined' for row in tree[1:]))
            self.assertEqual(tree[0]['budget']['u']['free'],9)
            self.assertEqual(self.store.budget(self.run,self.who)['u']['reserved'],1)
            with self.store._tx() as db:
                op=db.execute('SELECT id FROM operations WHERE run=?',(self.run,)).fetchone()[0]
            state=self.store.snapshot(op,self.who)
            self.store.settle(state['attempts'][0]['id'],self.who,provider='emit',evidence_id='late',actual={'u':0})
            self.assertEqual(self.store.allocation_tree(self.run,self.who)[0]['budget']['u']['free'],10)
            self.assertEqual(self.execute(plan).data,'Waiting')
            self.assertEqual(self.calls,['hello'])
        finally:
            self.broker.wait_for_workers(timeout=10)

    def test_descendant_allocation_requires_current_task_lease(self):
        self.start(build(self.specs))
        task,old,parent=self.task_owner()
        child=self.store.split_budget(self.run,self.who,'owned',{'u':2},parent=parent,task_lease=old)
        current=self.store.claim_task(task,self.who,recover=True)
        for lease in (None,old):
            with self.assertRaises(StateConflict):
                self.store.split_budget(self.run,self.who,'escape',{'u':1},parent=child,task_lease=lease)
            with self.assertRaises(StateConflict):
                self.store.join_budget(self.run,self.who,child,task_lease=lease)
        self.store.join_budget(self.run,self.who,child,task_lease=current)
        with self.assertRaisesRegex(StateConflict,'task root'):
            self.store.join_budget(self.run,self.who,parent,task_lease=current)

    def test_descendant_does_not_bypass_speculative_write_policy(self):
        self.specs.register(Tool.define('send','action','Text','Json','output',{'u':1}))
        self.broker=Broker(self.store,self.specs,{'emit':self.emit,'send':self.emit})
        self.start(build(self.specs))
        task,lease,parent=self.task_owner(speculative=True)
        child=self.store.split_budget(self.run,self.who,'owned',{'u':2},parent=parent,task_lease=lease)
        intent=Intent('send','1','output','write')
        permit=self.store.authorize(self.run,self.who,intent,self.grants['send'])
        with self.assertRaisesRegex(StateConflict,'speculative'):
            self.store.admit(self.run,self.who,Slot('send',task),intent,permit,allocation=child,task_lease=lease)

    def test_late_fee_after_task_cancel_credits_root_without_new_dispatch(self):
        self.start(build(self.specs))
        task,lease,parent=self.task_owner()
        child=self.store.split_budget(self.run,self.who,'owned',{'u':2},parent=parent,task_lease=lease)
        intent=Intent('emit','1','output','pending')
        permit=self.store.authorize(self.run,self.who,intent,self.grants['emit'])
        op=self.store.admit(self.run,self.who,Slot('emit',task),intent,permit,allocation=child,task_lease=lease)
        attempt=self.store.snapshot(op,self.who)['attempts'][0]['id']
        self.store.start_attempt(op,attempt,self.who,task_lease=lease)
        self.store.handoff_task(task,self.who)
        self.assertEqual(self.store.budget(self.run,self.who)['u']['reserved'],1)
        self.store.settle(attempt,self.who,provider='emit',evidence_id='late',actual={'u':0})
        with self.assertRaises(StateConflict):
            self.store.split_budget(self.run,self.who,'after',{'u':1},parent=child,task_lease=lease)
        self.assertEqual(self.store.budget(self.run,self.who)['u']['free'],10)

    def test_nested_scope_can_use_task_owned_subbudget(self):
        inner=PlanBuilder('inner',self.specs)
        scope=inner.scope('scope',{'u':1})
        child=PlanBuilder('constant',self.specs)
        constant=child.finish(child.literal('value','Text','done'))
        task=inner.spawn('task',scope,constant,{'u':1})
        result=inner.join('result',task)
        inner.close_scope('closed',scope)
        inner_plan=inner.finish(result)
        b,_,funds=self.root(2)
        result=b.invoke('result',inner_plan,budget=funds)
        b.join_budget('closed',funds)
        plan=self.task_plan(b.finish(result))
        self.start(plan)
        try:
            self.assertEqual(self.execute(plan).data,'done')
            self.assertTrue(all(row['state']=='Joined' for row in self.store.allocation_tree(self.run,self.who)[1:]))
        finally:
            self.broker.wait_for_workers(timeout=10)

    def test_invocation_keeps_private_labels_and_static_upper_bound(self):
        b,_,funds=self.root()
        message=b.restrict('secret',b.literal('message','Text','secret'),Label({'owner','tool:emit'}))
        result=b.invoke('result',emitter(self.specs),inputs={'message':message},budget=funds)
        b.join_budget('closed',funds)
        plan=b.finish(result)
        self.start(plan)
        value=self.execute(plan)
        self.assertEqual(value.label.readers,frozenset({'owner','tool:emit'}))
        self.assertEqual(self.execute(plan).label,value.label)
        self.assertEqual(plan_label(plan,self.specs).taints,frozenset({'external:emit'}))

    def test_v11_migration_preserves_old_allocation(self):
        plan=build(self.specs)
        self.start(plan)
        old=self.store.split_budget(self.run,self.who,'old',{'u':0})
        with self.store._tx() as db:
            db.execute('DROP TABLE invocation_frames')
            db.execute('PRAGMA user_version=11')
        store=Store(self.path)
        broker=Broker(store,self.specs,{'emit':self.emit})
        self.assertEqual(Interpreter(broker).run(plan,self.run,self.who,self.grants).data,'hello')
        self.assertIn(old,{row['id'] for row in store.allocation_tree(self.run,self.who)})
        with store._tx() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],21)

    def test_process_crashes_do_not_duplicate_split_call_or_join(self):
        for index,point in enumerate(('before_split_commit','after_split_commit','before_invocation_commit','after_invocation_commit','before_join_commit','after_join_commit')):
            with self.subTest(point=point):
                path=Path(self.temp.name)/('crash'+str(index)+'.sqlite')
                store=Store(path)
                plan=build(self.specs)
                run=store.create_run(self.who,plan.fingerprint,{'u':10})
                grant=store.grant(run,'emit','output',expires=store.clock()+1000)
                config=path.with_suffix('.json')
                log=path.with_suffix('.log')
                config.write_text(json.dumps({'database':str(path),'run':run,'grants':{'emit':grant},'log':str(log)}),encoding='utf-8')
                crashed=subprocess.run([sys.executable,'-m','tests.budget_workflow_worker',str(config),point],capture_output=True,text=True,timeout=30)
                self.assertEqual(crashed.returncode,77,crashed.stderr)
                recovered=subprocess.run([sys.executable,'-m','tests.budget_workflow_worker',str(config),'none'],capture_output=True,text=True,timeout=30)
                self.assertEqual(recovered.returncode,0,recovered.stderr)
                self.assertEqual(json.loads(recovered.stdout)['data'],'hello')
                self.assertEqual(log.read_text(encoding='utf-8').splitlines(),['hello'])
                events=store.events(run,self.who)
                self.assertEqual(sum(e['kind']=='BudgetSplit' for e in events),2)
                self.assertEqual(sum(e['kind']=='BudgetJoined' for e in events),2)
                self.assertEqual(store.budget(run,self.who)['u']['confirmed'],1)
