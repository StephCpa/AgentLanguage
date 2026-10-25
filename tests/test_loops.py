from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from al0.data import Principal
from al0.errors import BudgetExceeded, EvaluationError, ExecutionLimitExceeded, FlowDenied, InvalidData, StateConflict, TypeCheckError
from al0.execute import Broker, Interpreter, Response, Suspended, Value
from al0.flow import Label, plan_label
from al0.frontend import compile_source
from al0.ir import CheckedPlan, check_plan
from al0.limits import ExecutionBudget
from al0.sdk import PlanBuilder
from al0.source import SourceError
from al0.store import Store
from examples.bounded_batches import build, catalog, demo


PURE_SUM='''fn step(index: Int, state: Int) -> Int { let next=add(state,index); return next; }
fn sum(count: Int) -> Int { let zero: Int=0; let result=repeat(count,1000,zero,step,{}); return result; }
workflow main(count: Int) -> Int uses {} { let result=call(sum,{count:count}); return result; }'''


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'state.sqlite'
        self.store=Store(self.path)
        self.who=Principal('tenant','user')
        self.specs=catalog()
        self.calls=[]
        def emit(ticket):
            self.calls.append(ticket.intent.value['args'])
            return Response(ticket.intent.value['args'],{'u':1})
        self.emit=emit
        self.broker=Broker(self.store,self.specs,{'emit':emit})

    def tearDown(self):
        self.broker.wait_for_workers(timeout=10)

    def start(self,plan,budget=10):
        self.run=self.store.create_run(self.who,plan.fingerprint,{'u':budget})
        self.grants={'emit':self.store.grant(self.run,'emit','output',expires=self.store.clock()+1000)}

    def execute(self,plan,inputs=None,execution=None):
        return Interpreter(self.broker).run(plan,self.run,self.who,self.grants,inputs=inputs,execution=execution)

    def frame(self):
        with closing(sqlite3.connect(self.path)) as db:
            row=db.execute('SELECT next_index,current_value,frame FROM loop_frames WHERE run=?',(self.run,)).fetchone()
        return None if row is None else (row[0],json.loads(row[1]),json.loads(row[2]))

    def test_pure_functions_and_runtime_loops_have_no_internal_history(self):
        plan=compile_source(PURE_SUM,self.specs).plan
        self.start(plan)
        result=self.execute(plan,{'count':Value(5)})
        self.assertEqual(result.data,10)
        kinds=[event['kind'] for event in self.store.events(self.run,self.who)]
        self.assertNotIn('LoopStarted',kinds)
        self.assertNotIn('LoopAdvanced',kinds)
        self.assertNotIn('BranchChosen',kinds)
        self.assertEqual(self.calls,[])
        self.assertIsNone(self.frame())

    def test_pure_if_inside_function_does_not_freeze_unrelated_calls(self):
        source='''fn identity(value: Int) -> Int { return value; }
fn select(flag: Bool, value: Int) -> Int { let result=if(flag,identity,identity,{value:value}); return result; }
workflow main() uses {} {
 let yes: Bool=true; let no: Bool=false; let a: Int=1; let b: Int=2;
 let left=call(select,{flag:yes,value:a}); let right=call(select,{flag:no,value:b});
 let result=add(left,right); return result;
}'''
        plan=compile_source(source,self.specs).plan
        self.start(plan)
        self.assertEqual(self.execute(plan).data,3)
        self.assertFalse(any(e['kind']=='BranchChosen' for e in self.store.events(self.run,self.who)))

    def test_effectful_loop_checkpoints_and_skips_completed_body_on_replay(self):
        plan=build(self.specs)
        self.start(plan)
        first_budget=ExecutionBudget(1000)
        first=self.execute(plan,execution=first_budget)
        self.assertEqual(first.data,6)
        self.assertEqual(self.calls,[0,1,3,6])
        self.assertEqual(self.frame()[0],4)
        before=self.store.events(self.run,self.who)
        replay_budget=ExecutionBudget(3)
        self.assertEqual(self.execute(plan,execution=replay_budget),first)
        self.assertEqual(replay_budget.used,3)
        self.assertEqual(first_budget.used,35)
        self.assertEqual(self.store.events(self.run,self.who),before)

    def test_zero_iterations_returns_initial_without_body_effects(self):
        document=json.loads(build(self.specs).encoded)
        document['nodes'][0]['value']=0
        document['nodes'][1]['value']=13
        plan=check_plan(document,self.specs)
        self.start(plan)
        self.assertEqual(self.execute(plan).data,13)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.frame()[0],0)

    def test_count_bound_is_checked_before_first_effect_or_checkpoint(self):
        for count in (-1,11):
            document=json.loads(build(self.specs).encoded)
            document['nodes'][0]['value']=count
            plan=check_plan(document,self.specs)
            self.start(plan)
            with self.assertRaises(EvaluationError):
                self.execute(plan)
            self.assertIsNone(self.frame())
        self.assertEqual(self.calls,[])

    def test_pure_loop_checks_its_declared_bound(self):
        plan=compile_source(PURE_SUM,self.specs).plan
        self.start(plan)
        with self.assertRaises(EvaluationError):
            self.execute(plan,{'count':Value(1001)})

    def test_fuel_exhaustion_retains_progress_for_fresh_invocation(self):
        plan=build(self.specs)
        self.start(plan)
        execution=ExecutionBudget(18)
        with self.assertRaises(ExecutionLimitExceeded):
            self.execute(plan,execution=execution)
        self.assertEqual(execution.used,18)
        self.assertEqual(self.calls,[0])
        self.assertEqual(self.frame()[0],1)
        self.assertEqual(self.execute(plan).data,6)
        self.assertEqual(self.calls,[0,1,3,6])
        self.assertEqual(self.store.budget(self.run,self.who)['u']['confirmed'],4)

    def test_nested_pure_calls_cannot_reset_fuel(self):
        plan=compile_source(PURE_SUM,self.specs).plan
        self.start(plan)
        execution=ExecutionBudget(50)
        with self.assertRaises(ExecutionLimitExceeded):
            self.execute(plan,{'count':Value(1000)},execution)
        self.assertEqual(execution.used,50)
        self.assertEqual(self.calls,[])

    def test_concurrent_children_share_one_step_budget(self):
        body=PlanBuilder('step',self.specs,pure=True)
        index=body.parameter('index','Int')
        state=body.parameter('state','Int')
        step=body.finish(body.compute('sum','add',state,index))
        child=PlanBuilder('child',self.specs,pure=True)
        n=child.literal('count','Int',1)
        zero=child.literal('initial','Int',0)
        child_plan=child.finish(child.repeat('result',n,1,zero,step))
        parent=PlanBuilder('parent',self.specs)
        scope=parent.scope('scope',{})
        a=parent.spawn('a',scope,child_plan,{})
        b=parent.spawn('b',scope,child_plan,{})
        left=parent.join('left',a)
        right=parent.join('right',b)
        parent.close_scope('closed',scope)
        plan=parent.finish(parent.compute('sum','add',left,right))
        self.start(plan)
        execution=ExecutionBudget(18)
        with self.assertRaises(ExecutionLimitExceeded):
            self.execute(plan,execution=execution)
        self.broker.wait_for_workers(timeout=10)
        self.assertEqual(execution.used,18)
        self.assertEqual(self.calls,[])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks WHERE run=? AND disposition='Attached'",(self.run,)).fetchone()[0],0)

    def test_cooperative_deadline_preserves_already_completed_external_cost(self):
        now=[0]
        def slow(ticket):
            result=self.emit(ticket)
            now[0]=5
            return result
        self.broker=Broker(self.store,self.specs,{'emit':slow})
        plan=build(self.specs)
        self.start(plan)
        with self.assertRaises(ExecutionLimitExceeded):
            self.execute(plan,execution=ExecutionBudget(timeout=1,clock=lambda:now[0]))
        self.assertEqual(self.calls,[0])
        self.assertEqual(self.store.budget(self.run,self.who)['u']['confirmed'],1)
        self.assertEqual(self.frame()[0],0)
        self.assertEqual(self.execute(plan).data,6)
        self.assertEqual(self.calls,[0,1,3,6])

    def test_unresolved_iteration_keeps_liability_and_does_not_resend(self):
        first=[True]
        def lost(ticket):
            result=self.emit(ticket)
            if first[0]:
                first[0]=False
                raise TimeoutError()
            return result
        self.broker=Broker(self.store,self.specs,{'emit':lost})
        plan=build(self.specs)
        self.start(plan)
        with self.assertRaises(Suspended) as caught:
            self.execute(plan)
        operation=caught.exception.operation
        self.assertEqual(self.frame()[0],0)
        self.assertEqual(self.store.budget(self.run,self.who)['u']['reserved'],1)
        with self.assertRaises(Suspended):
            self.execute(plan)
        self.assertEqual(self.calls,[0])
        attempt=self.store.snapshot(operation,self.who)['attempts'][0]['id']
        self.store.business_result(operation,self.who,provider='emit',evidence_id='late',outcome='Applied',details={'output':0})
        self.store.settle(attempt,self.who,provider='emit',evidence_id='bill',actual={'u':1})
        self.assertEqual(self.execute(plan).data,6)
        self.assertEqual(self.calls,[0,1,3,6])

    def test_loop_cannot_exceed_external_cost_budget(self):
        plan=build(self.specs)
        self.start(plan,budget=2)
        with self.assertRaises(BudgetExceeded):
            self.execute(plan)
        self.assertEqual(self.calls,[0,1])
        self.assertEqual(self.frame()[0],2)
        self.assertEqual(self.store.budget(self.run,self.who)['u']['confirmed'],2)

    def test_nested_loops_have_distinct_frames_and_replay(self):
        source='''fn step(index: Int,state: Int) -> Int { let one: Int=1; let next=add(state,one); return next; }
workflow outer(index: Int,state: Int) uses {} {
 let n: Int=2; let next=repeat(n,2,state,step,{}); return next;
}
workflow main() uses {} { let n: Int=3; let zero: Int=0; let result=repeat(n,3,zero,outer,{}); return result; }'''
        plan=compile_source(source,self.specs).plan
        self.start(plan)
        self.assertEqual(self.execute(plan).data,6)
        before=self.store.events(self.run,self.who)
        self.assertEqual(self.execute(plan).data,6)
        self.assertEqual(self.store.events(self.run,self.who),before)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM loop_frames WHERE run=?',(self.run,)).fetchone()[0],4)

    def test_loop_entry_freezes_count_state_and_captures(self):
        plan=build(self.specs)
        self.start(plan)
        original=self.store.begin_loop(self.run,self.who,'manual',2,3,Value(7).record(),{'x':Value(1).record()})
        replay=self.store.begin_loop(self.run,self.who,'manual',999,3,Value(999).record(),{'x':Value(999).record()})
        self.assertEqual(replay,original)
        with self.assertRaises(StateConflict):
            self.store.advance_loop(self.run,self.who,'manual',1,Value(8).record())

    def test_function_and_loop_labels_protect_later_sink(self):
        source=PURE_SUM.replace('let result=call(sum,{count:count}); return result;',
            'let result=call(sum,{count:count}); let sent=observe("emit",result); return result;').replace('uses {}','uses {"observe:emit"}')
        plan=compile_source(source,self.specs).plan
        self.start(plan)
        with self.assertRaises(FlowDenied):
            self.execute(plan,{'count':Value(5,label=Label({'owner'}))})
        self.assertEqual(self.calls,[])

    def test_labels_survive_loop_checkpoint_and_replay(self):
        source='''fn step(index: Int,state: Int) -> Int { let next=add(state,index); return next; }
workflow main(n: Int) uses {} { let zero: Int=0; let result=repeat(n,10,zero,step,{}); return result; }'''
        plan=compile_source(source,self.specs).plan
        self.start(plan)
        label=Label({'owner'},{'private-count'})
        value=self.execute(plan,{'n':Value(3,label=label)})
        self.assertEqual(value.data,3)
        self.assertEqual(value.label,label)
        self.assertEqual(Value.from_record(self.frame()[1]).label,label)
        self.assertEqual(self.execute(plan,{'n':Value(3,label=label)}),value)

    def test_crash_boundaries_do_not_repeat_adapter_calls(self):
        config=Path(self.temp.name)/'worker.json'
        logfile=Path(self.temp.name)/'calls.txt'
        for point in ('before_loop_begin_commit','after_loop_begin_commit','before_loop_advance_commit','after_loop_advance_commit'):
            with self.subTest(point=point):
                plan=build(self.specs)
                self.start(plan)
                logfile.write_text('',encoding='utf-8')
                config.write_text(json.dumps({'database':str(self.path),'run':self.run,'grants':self.grants,'log':str(logfile)}),encoding='utf-8')
                command=[sys.executable,'-m','tests.loop_worker',str(config)]
                crash=subprocess.run(command+[point],capture_output=True,text=True,timeout=20)
                self.assertEqual(crash.returncode,77,crash.stdout+crash.stderr)
                resumed=subprocess.run(command+['recover'],capture_output=True,text=True,timeout=20)
                self.assertEqual(resumed.returncode,0,resumed.stdout+resumed.stderr)
                value=Value.from_record(json.loads(resumed.stdout))
                self.assertEqual(value.data,6)
                self.assertIn('external:emit',value.label.taints)
                self.assertEqual(logfile.read_text(encoding='utf-8').splitlines(),['0','1','3','6'])
                self.assertEqual(self.store.budget(self.run,self.who)['u']['confirmed'],4)


class FunctionStaticTests(unittest.TestCase):
    def setUp(self):
        self.specs=catalog()

    def reject(self,source):
        with self.assertRaises(SourceError):
            compile_source(source,self.specs)

    def test_functions_cannot_call_tools_or_hide_them_in_branches(self):
        self.reject('fn bad(x: Int) uses {"observe:emit"} { let sent=observe("emit",x); return x; }')
        self.reject('''workflow effect(x:Int) uses {"observe:emit"} { let sent=observe("emit",x); return x; }
fn bad(x:Int) { let flag:Bool=true; let result=if(flag,effect,effect,{x:x}); return result; }''')
        self.reject('fn bad() { let scope=scope({},false); let closed=close_scope(scope); return closed; }')

    def test_call_requires_a_declared_pure_function(self):
        self.reject('''workflow identity(x:Int) uses {} { return x; }
workflow main() uses {} { let x:Int=1; let result=call(identity,{x:x}); return result; }''')
        b=PlanBuilder('identity',self.specs)
        plan=b.finish(b.parameter('x','Int'))
        forged=CheckedPlan(plan.encoded,plan.catalog_digest,pure=True)
        parent=PlanBuilder('main',self.specs)
        value=parent.literal('x','Int',1)
        with self.assertRaises(TypeCheckError):
            parent.finish(parent.call('result',forged,inputs={'x':value}))

    def test_function_calls_require_exact_argument_bindings(self):
        self.reject('fn identity(x:Int) { return x; } workflow main() uses {} { let result=call(identity,{}); return result; }')
        self.reject('fn identity(x:Int) { return x; } workflow main() uses {} { let x:Text="x"; let result=call(identity,{x:x}); return result; }')

    def test_recursive_functions_are_rejected(self):
        self.reject('fn recurse(x:Int) { let next=call(recurse,{x:x}); return next; }')

    def test_loop_bounds_and_state_invariant_are_static(self):
        for change in ('1000001','true','-1'):
            self.reject(PURE_SUM.replace('repeat(count,1000','repeat(count,'+change))
        self.reject(PURE_SUM.replace('index: Int','index: Text'))
        self.reject(PURE_SUM.replace('let next=add(state,index)','let next: Text="wrong"'))
        self.reject(PURE_SUM.replace('repeat(count,1000,zero,step,{})','repeat(count,1000,zero,step,{state:zero})'))

    def test_effectful_body_cannot_hide_inside_a_pure_loop(self):
        source='''workflow step(index:Int,state:Int) uses {"observe:emit"} { let sent=observe("emit",state); return state; }
fn main(n:Int) { let zero:Int=0; let result=repeat(n,10,zero,step,{}); return result; }'''
        self.reject(source)

    def test_loop_state_does_not_erase_candidate_provenance(self):
        source='''fn step(index:Int,state:Candidate<Int>) { let next=add(state,index); return next; }
workflow main(state:Candidate<Int>) uses {} { let n:Int=2; let result=repeat(n,2,state,step,{}); return result; }'''
        compiled=compile_source(source,self.specs)
        self.assertEqual(compiled.plan.result_type.kind,'Candidate')

    def test_function_body_labels_are_included_in_task_bound(self):
        source='''fn private(x:Int) { let secret=restrict(x,{readers:["owner"],taints:[]}); return secret; }
workflow main(x:Int) uses {} { let result=call(private,{x:x}); return result; }'''
        plan=compile_source(source,self.specs).plan
        self.assertEqual(plan_label(plan,self.specs).readers,frozenset({'owner'}))

    def test_execution_budget_configuration_is_checked(self):
        for steps in (-1,True,1.5):
            with self.assertRaises(InvalidData):
                ExecutionBudget(steps)
        for timeout in (-1,float('inf'),float('nan'),True):
            with self.assertRaises(InvalidData):
                ExecutionBudget(timeout=timeout)

    def test_v9_migration_preserves_budget_and_installs_loop_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.sqlite'
            store=Store(path)
            who=Principal('t','u')
            run=store.create_run(who,'legacy',{'u':3})
            before=store.budget(run,who)
            with closing(sqlite3.connect(path)) as db:
                db.executescript('DROP TABLE loop_frames; PRAGMA user_version=9;')
            upgraded=Store(path)
            self.assertEqual(upgraded.budget(run,who),before)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],21)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM loop_frames').fetchone()[0],0)

    def test_source_sdk_example_and_resume_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            result=demo(directory)
        self.assertEqual(result['result'],6)
        self.assertTrue(result['identical_ir'])
        self.assertEqual(result['emitted'],[0,1,3,6])
        self.assertEqual(result['iterations'],4)
        self.assertEqual(result['replay_added_events'],0)
        self.assertEqual(result['first_steps'],35)
        self.assertEqual(result['replay_steps'],3)


if __name__=='__main__':
    unittest.main()
