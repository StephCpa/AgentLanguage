import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from al0.concurrency import TaskRunner, TaskScope
from al0.data import Intent, Principal
from al0.errors import AuthorizationDenied, FlowDenied, InvalidData, ReplayMismatch, StateConflict, TypeCheckError
from al0.execute import Broker, Interpreter, Response, Value
from al0.flow import Label
from al0.frontend import compile_source
from al0.ir import check_plan
from al0.sdk import PlanBuilder
from al0.source import SourceError
from al0.store import Store
from examples.capability_calls import deliver, handled
from examples.task_capabilities import build, catalog, demo, narrower


class TaskCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'runtime.sqlite'
        self.store=Store(self.path)
        self.who=Principal('tenant','user')
        self.specs=catalog()
        self.calls=[]
        def send(ticket):
            self.calls.append(ticket.intent.value['args'])
            return Response({'sent':True},{'u':1})
        self.send=send
        self.broker=Broker(self.store,self.specs,{name:send for name in self.specs.tools})
        self.start(build(self.specs))

    def start(self,plan):
        self.run=self.store.create_run(self.who,plan.fingerprint,{'u':10})
        self.grants={name:self.store.grant(self.run,name,spec.scope,expires=self.store.clock()+1000,max_attempts=3)
                     for name,spec in self.specs.tools.items()}

    def execute(self,plan):
        try:
            return Interpreter(self.broker).run(plan,self.run,self.who,self.grants)
        finally:
            self.broker.wait_for_workers(timeout=10)

    def raw_task(self,*,caps=True,plan=None):
        plan=narrower(self.specs) if plan is None else plan
        scope=self.store.open_scope(self.run,self.who,'raw-scope',{})
        inputs={'permission':Value(self.grants['send']).record()} if plan.parameters else {}
        task=self.store.spawn_task(scope,self.who,'raw-task',plan.fingerprint,{},inputs=inputs,
                                   caps={'send':self.grants['send']} if caps else None)
        return task,plan

    def root(self):
        b=PlanBuilder('parent',self.specs)
        base=b.capability('base',b.tool('send'))
        narrow=b.attenuate('limited',base,constraints={'channel':'reports'})
        scope=b.scope('scope',{'u':2})
        return b,base,narrow,scope

    def tasks(self):
        with self.store._tx() as db:
            return [row['id'] for row in db.execute("SELECT id FROM tasks WHERE run=? AND kind='Task' ORDER BY rowid",(self.run,))]

    def test_source_sdk_demo_and_durable_child_map(self):
        result=demo(self.temp.name)
        self.assertEqual((result['result'],result['replay']),('Done','Done'))
        self.assertEqual(result['child_tools'],['send'])
        self.assertEqual(result['returned_cap_attempts'],1)
        self.assertEqual(len(result['calls']),1)
        self.assertEqual(result['replay_added_events'],0)

    def test_plain_spawn_accepts_explicit_cap_and_freezes_inherited_map(self):
        b,base,_,scope=self.root()
        task=b.spawn('task',scope,narrower(self.specs),{},inputs={'permission':base})
        result=b.join('result',task)
        b.close_scope('closed',scope)
        plan=b.finish(result)
        self.start(plan)
        cap=self.execute(plan).data
        self.assertEqual(self.store.bind_task_capabilities(self.tasks()[0],self.who),self.grants)
        self.assertEqual(self.store.capability_info(self.run,self.who,cap)['max_attempts'],1)

    def test_broader_cap_input_is_rejected_before_task_allocation(self):
        b,base,narrow,scope=self.root()
        task=b.spawn_caps('task',scope,narrower(self.specs),{},caps={'send':narrow},inputs={'permission':base})
        result=b.join('result',task)
        b.close_scope('closed',scope)
        plan=b.finish(result)
        self.start(plan)
        with self.assertRaisesRegex(AuthorizationDenied,'boundary'):
            self.execute(plan)
        self.assertEqual(self.tasks(),[])
        self.assertEqual(self.store.budget(self.run,self.who)['u']['free'],10)

    def test_missing_ambient_tool_does_not_fall_back_to_host(self):
        child=PlanBuilder('escape',self.specs)
        message=child.literal('message','Message',{'channel':'admin','text':'bad'})
        request=child.prepare('request',child.tool('admin'),message)
        permit=child.authorize('permit',request)
        child=child.finish(handled(child,child.admit('op',request,permit)))
        b,_,narrow,scope=self.root()
        task=b.spawn_caps('task',scope,child,{'u':1},caps={'send':narrow})
        result=b.join('result',task)
        b.close_scope('closed',scope)
        plan=b.finish(result)
        self.start(plan)
        with self.assertRaises(AuthorizationDenied):
            self.execute(plan)
        self.assertEqual(self.calls,[])

    def test_empty_map_blocks_even_explicit_cap_inputs(self):
        b,base,_,scope=self.root()
        task=b.spawn_caps('task',scope,narrower(self.specs),{},caps={},inputs={'permission':base})
        result=b.join('result',task)
        b.close_scope('closed',scope)
        plan=b.finish(result)
        self.start(plan)
        with self.assertRaises(AuthorizationDenied):
            self.execute(plan)
        self.assertEqual(self.tasks(),[])

    def test_nested_plain_spawn_preserves_restricted_map(self):
        leaf=PlanBuilder('leaf',self.specs)
        leaf=leaf.finish(leaf.capability('admin',leaf.tool('admin')))
        middle=PlanBuilder('middle',self.specs)
        group=middle.scope('group',{})
        task=middle.spawn('task',group,leaf,{})
        result=middle.join('result',task)
        middle.close_scope('closed',group)
        middle=middle.finish(result)
        b,_,narrow,scope=self.root()
        task=b.spawn_caps('task',scope,middle,{},caps={'send':narrow})
        result=b.join('result',task)
        b.close_scope('closed',scope)
        plan=b.finish(result)
        self.start(plan)
        with self.assertRaises(AuthorizationDenied):
            self.execute(plan)
        self.assertEqual(len(self.tasks()),2)
        for task in self.tasks():
            self.assertEqual(set(self.store.bind_task_capabilities(task,self.who)),{'send'})

    def test_dynamic_budget_can_fund_capability_task(self):
        b,base,_,scope=self.root()
        quantity=b.literal('quantity','Int',0)
        amount=b.resources('amount',{'u':quantity})
        task=b.spawn_caps('task',scope,narrower(self.specs),amount,caps={'send':base},inputs={'permission':base})
        result=b.join('result',task)
        b.close_scope('closed',scope)
        plan=b.finish(result)
        self.start(plan)
        cap=self.execute(plan).data
        self.assertEqual(self.execute(plan).data,cap)
        self.assertEqual(sum(e['kind']=='BudgetAmountBound' for e in self.store.events(self.run,self.who)),1)

    def test_completed_branch_cap_can_authorize_parent_action(self):
        b,base,_,scope=self.root()
        task=b.spawn_caps('task',scope,narrower(self.specs),{},caps={'send':base},inputs={'permission':base})
        outcome=b.join_outcome('outcome',task)
        completed=PlanBuilder('completed',self.specs)
        cap=completed.cap_parameter('result',completed.tool('send'))
        message=completed.literal('message','Message',{'channel':'reports','text':'hello'})
        result=completed.invoke_caps('result-value',deliver(self.specs),caps={'send':cap},inputs={'permission':cap,'message':message})
        completed=completed.finish(result)
        fallback=PlanBuilder('fallback',self.specs)
        fallback=fallback.finish(fallback.literal('value','Text','NoCap'))
        result=b.match_task('result',outcome,completed=completed,failed=fallback,waiting=fallback,cancelled=fallback)
        b.close_scope('closed',scope)
        plan=b.finish(result)
        self.start(plan)
        self.assertEqual(self.execute(plan).data,'Done')
        self.assertEqual(self.execute(plan).data,'Done')
        self.assertEqual(len(self.calls),1)

    def test_descendant_cap_input_can_be_returned_and_further_attenuated(self):
        b,_,narrow,scope=self.root()
        deeper=b.attenuate('deeper',narrow,constraints={'text':'hello'})
        task=b.spawn_caps('task',scope,narrower(self.specs),{},caps={'send':narrow},inputs={'permission':deeper})
        result=b.join('result',task)
        b.close_scope('closed',scope)
        final=b.attenuate('final',result,max_attempts=1)
        plan=b.finish(final)
        self.start(plan)
        cap=self.execute(plan).data
        self.store.authorize(self.run,self.who,Intent('send','1','outbox',{'channel':'reports','text':'hello'}),cap)
        with self.assertRaises(AuthorizationDenied):
            self.store.authorize(self.run,self.who,Intent('send','1','outbox',{'channel':'reports','text':'other'}),cap)

    def test_runner_recovery_rejects_map_replacement_before_fencing(self):
        task,plan=self.raw_task()
        old=self.store.claim_task(task,self.who)
        with self.assertRaises(ReplayMismatch):
            TaskRunner(self.broker).run(task,self.who,plan,self.grants,recover=True)
        self.assertEqual(self.store.task_snapshot(task,self.who)['epoch'],old.epoch)
        result=TaskRunner(self.broker).run(task,self.who,plan,recover=True)
        self.assertEqual(self.store.capability_info(self.run,self.who,result.data)['max_attempts'],1)

    def test_direct_interpreter_cannot_replace_task_map(self):
        task,plan=self.raw_task()
        lease=self.store.claim_task(task,self.who)
        with self.assertRaises(ReplayMismatch):
            Interpreter(self.broker).run(plan,self.run,self.who,self.grants,task_lease=lease,
                                         inputs={'permission':Value(self.grants['send'])})

    def test_task_inputs_remain_pinned_to_spawn_record(self):
        task,plan=self.raw_task()
        lease=self.store.claim_task(task,self.who)
        derived=self.store.attenuate_capability(self.run,self.who,'different',self.grants['send'])['grant']
        with self.assertRaises(ReplayMismatch):
            Interpreter(self.broker).run(plan,self.run,self.who,{'send':self.grants['send']},task_lease=lease,
                                         inputs={'permission':Value(derived)})

    def test_root_cannot_forge_cap_input(self):
        plan=narrower(self.specs)
        self.start(plan)
        with self.assertRaises(InvalidData):
            Interpreter(self.broker).run(plan,self.run,self.who,self.grants,inputs={'permission':Value(self.grants['send'])})

    def test_wrong_tool_map_and_parameter_are_rejected_statically(self):
        raw=json.loads(build(self.specs).encoded)
        raw['nodes'][2]['caps']={'admin':'base'}
        with self.assertRaises(TypeCheckError):
            check_plan(raw,self.specs)
        b=PlanBuilder('wrong',self.specs)
        admin=b.capability('admin',b.tool('admin'))
        scope=b.scope('scope',{})
        task=b.spawn_caps('task',scope,narrower(self.specs),{},caps={'admin':admin},inputs={'permission':admin})
        result=b.join('result',task)
        b.close_scope('closed',scope)
        with self.assertRaises(TypeCheckError):
            b.finish(result)

    def test_budget_result_with_empty_caps_uses_custody_protocol(self):
        child=PlanBuilder('budget-child',self.specs)
        child=child.finish(child.split_budget('funds',{}))
        b=PlanBuilder('bad',self.specs)
        scope=b.scope('scope',{})
        task=b.spawn_caps('task',scope,child,{},caps={})
        result=b.join('result',task)
        b.close_scope('closed',scope)
        plan=b.finish(result)
        self.start(plan)
        received=self.execute(plan)
        self.assertEqual((received.resource.kind,received.resource.generation),('Budget',2))
        self.assertEqual(self.store.bind_task_capabilities(self.tasks()[0],self.who),{})
        with self.assertRaises(StateConflict):
            self.store.join_budget(self.run,self.who,received.data)
        with self.store.resource_access(self.run,self.who,[received.resource]):
            self.store.join_budget(self.run,self.who,received.data)

    def test_budget_input_with_empty_caps_moves_and_is_consumed(self):
        child=PlanBuilder('budget-child',self.specs)
        funds=child.budget_parameter('funds')
        child=child.finish(child.join_budget('closed',funds))
        b=PlanBuilder('bad',self.specs)
        scope=b.scope('scope',{})
        funds=b.split_budget('funds',{})
        task=b.spawn_caps('task',scope,child,{},caps={},inputs={'funds':funds})
        result=b.join('result',task)
        b.close_scope('closed',scope)
        plan=b.finish(result)
        self.start(plan)
        self.assertTrue(self.execute(plan).data)
        task=self.tasks()[0]
        self.assertEqual(self.store.bind_task_capabilities(task,self.who),{})
        transferred=self.store.task_resource_inputs(task,self.who)['funds']
        self.assertEqual((transferred.kind,transferred.generation),('Budget',1))
        with self.store._tx() as db:
            row=db.execute('SELECT state FROM resource_custody WHERE resource=?',(transferred.resource,)).fetchone()
        self.assertEqual(row['state'],'Spent')

    def test_parent_revocation_blocks_new_child_attenuation(self):
        task,plan=self.raw_task()
        self.store.revoke(self.grants['send'])
        with self.assertRaises(AuthorizationDenied):
            TaskRunner(self.broker).run(task,self.who,plan)
        self.assertEqual(self.store.task_snapshot(task,self.who)['state'],'Failed')

    def test_completed_cap_replays_after_revocation_without_new_authority(self):
        task,plan=self.raw_task()
        cap=TaskRunner(self.broker).run(task,self.who,plan).data
        self.store.revoke(self.grants['send'])
        events=self.store.events(self.run,self.who)
        self.assertEqual(TaskRunner(self.broker).run(task,self.who,plan).data,cap)
        self.assertEqual(self.store.events(self.run,self.who),events)
        with self.assertRaises(AuthorizationDenied):
            self.store.authorize(self.run,self.who,Intent('send','1','outbox',{'channel':'reports','text':'hello'}),cap)

    def test_completed_root_replays_returned_cap_and_action_after_revocation(self):
        plan=build(self.specs)
        self.assertEqual(self.execute(plan).data,'Done')
        self.store.revoke(self.grants['send'])
        events=self.store.events(self.run,self.who)
        self.assertEqual(self.execute(plan).data,'Done')
        self.assertEqual(self.store.events(self.run,self.who),events)
        self.assertEqual(len(self.calls),1)

    def test_cap_labels_survive_task_return_and_replay(self):
        # A host-bound input label gives the task's explicit Cap its label.
        plan=narrower(self.specs)
        task_scope=self.store.open_scope(self.run,self.who,'label-scope',{})
        secret=Label({'owner'})
        task=self.store.spawn_task(task_scope,self.who,'label-task',plan.fingerprint,{},
            inputs={'permission':Value(self.grants['send'],label=secret).record()},caps={'send':self.grants['send']})
        first=TaskRunner(self.broker).run(task,self.who,plan)
        replay=TaskRunner(self.broker).run(task,self.who,plan)
        self.assertEqual(first,replay)
        self.assertEqual(first.label.readers,secret.readers)
        with self.assertRaises(FlowDenied):
            self.broker.deliver(first,'public')

    def test_outcome_completed_branch_receives_cap(self):
        b,base,_,scope=self.root()
        task=b.spawn_caps('task',scope,narrower(self.specs),{},caps={'send':base},inputs={'permission':base})
        outcome=b.join_outcome('outcome',task)
        completed=PlanBuilder('completed',self.specs)
        result=completed.cap_parameter('result',completed.tool('send'))
        completed=completed.finish(completed.attenuate('limited',result,max_attempts=1))
        fallback=PlanBuilder('fallback',self.specs)
        fallback=fallback.finish(fallback.cap_parameter('fallback',fallback.tool('send')))
        result=b.match_task('result',outcome,completed=completed,failed=fallback,waiting=fallback,cancelled=fallback,inputs={'fallback':base})
        b.close_scope('closed',scope)
        plan=b.finish(result)
        self.start(plan)
        cap=self.execute(plan).data
        self.assertEqual(self.execute(plan).data,cap)
        self.assertEqual(self.store.capability_info(self.run,self.who,cap)['max_attempts'],1)

    def test_race_can_return_cap_with_stable_winner(self):
        b=PlanBuilder('race',self.specs)
        base=b.capability('base',b.tool('send'))
        scope=b.scope('scope',{},speculative=True)
        a=b.spawn_caps('a',scope,narrower(self.specs),{},caps={'send':base},inputs={'permission':base})
        c=b.spawn_caps('b',scope,narrower(self.specs),{},caps={'send':base},inputs={'permission':base})
        selected=b.race('selected',[a,c])
        narrowed=b.attenuate('narrowed',selected,max_attempts=1)
        b.close_scope('closed',scope)
        plan=b.finish(narrowed)
        self.start(plan)
        value=self.execute(plan)
        self.assertEqual(self.execute(plan),value)

    def test_cap_spawn_keeps_speculative_write_checks(self):
        b,base,_,_=self.root()
        scope=b.scope('speculative',{'u':1},speculative=True)
        message=b.literal('message','Message',{'channel':'reports','text':'hello'})
        task=b.spawn_caps('task',scope,deliver(self.specs),{'u':1},caps={'send':base},inputs={'permission':base,'message':message})
        result=b.join('result',task)
        b.close_scope('closed',scope)
        with self.assertRaisesRegex(TypeCheckError,'speculative'):
            b.finish(result)

    def test_host_never_started_task_can_bind_once(self):
        task,plan=self.raw_task(caps=False)
        result=TaskRunner(self.broker).run(task,self.who,plan,{'send':self.grants['send']})
        self.assertEqual(self.store.bind_task_capabilities(task,self.who),{'send':self.grants['send']})
        self.assertEqual(self.store.capability_info(self.run,self.who,result.data)['max_attempts'],1)
        with self.assertRaises(ReplayMismatch):
            self.store.bind_task_capabilities(task,self.who,self.grants)

    def test_missing_legacy_map_is_not_guessed_after_task_started(self):
        task,plan=self.raw_task(caps=False)
        lease=self.store.claim_task(task,self.who)
        with self.assertRaisesRegex(StateConflict,'never-started'):
            TaskRunner(self.broker).run(task,self.who,plan,self.grants,recover=True)
        self.assertEqual(self.store.task_snapshot(task,self.who)['epoch'],lease.epoch)

    def test_task_scope_replay_cannot_change_capability_map(self):
        plan=narrower(self.specs)
        with TaskScope(self.broker,self.run,self.who,'scope',{}) as scope:
            task,_=scope.spawn('child',plan,{},self.grants,inputs={'permission':Value(self.grants['send'])})
            scope.join(task)
        before=self.store.allocation_tree(self.run,self.who)
        with TaskScope(self.broker,self.run,self.who,'scope',{}) as scope:
            with self.assertRaises(ReplayMismatch):
                scope.spawn('child',plan,{}, {'send':self.grants['send']},inputs={'permission':Value(self.grants['send'])})
        self.assertEqual(self.store.allocation_tree(self.run,self.who),before)

    def test_capability_map_rejects_wrong_run_and_tool(self):
        task,_=self.raw_task(caps=False)
        with self.assertRaises(AuthorizationDenied):
            self.store.bind_task_capabilities(task,self.who,{'send':self.grants['admin']})
        other=self.store.create_run(self.who,'other',{})
        cap=self.store.grant(other,'send','outbox',expires=self.store.clock()+1000)
        with self.assertRaises(AuthorizationDenied):
            self.store.bind_task_capabilities(task,self.who,{'send':cap})

    def test_source_reports_undefined_cap_binding(self):
        source=Path('examples/task_capabilities.al0').read_text(encoding='utf-8')
        source=source.replace('{send: base}', '{send: missing}')
        with self.assertRaises(SourceError) as caught:
            compile_source(source,self.specs,entry='task-capabilities')
        span=caught.exception.diagnostic.span
        self.assertEqual(source[span.start:span.end],'missing')

    def test_v14_migration_preserves_tasks_without_inventing_authority(self):
        task,plan=self.raw_task(caps=False)
        self.store.claim_task(task,self.who)
        with self.store._tx() as db:
            db.execute('DROP TABLE task_capabilities')
            db.execute('PRAGMA user_version=14')
        self.store=Store(self.path)
        self.broker=Broker(self.store,self.specs,{name:self.send for name in self.specs.tools})
        self.assertEqual(self.store.task_snapshot(task,self.who)['state'],'Running')
        with self.assertRaises(StateConflict):
            TaskRunner(self.broker).run(task,self.who,plan,self.grants,recover=True)
        with self.store._tx() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],21)
            self.assertEqual(db.execute('SELECT count(*) FROM task_capabilities').fetchone()[0],0)

    def test_process_crashes_preserve_spawn_input_map_and_cap_result(self):
        points=('before_spawn_commit','after_spawn_commit','before_capability_derive_commit','after_capability_derive_commit',
                'before_task_finish_commit','after_task_finish_commit')
        self.crash_cases(points,raw=False)

    def test_host_initial_map_binding_crashes_do_not_rebind(self):
        self.crash_cases(('before_task_capabilities_commit','after_task_capabilities_commit'),raw=True)

    def crash_cases(self,points,*,raw):
        for index,point in enumerate(points):
            with self.subTest(point=point):
                path=Path(self.temp.name)/('crash'+str(index)+'.sqlite')
                store=Store(path)
                plan=build(self.specs)
                run=store.create_run(self.who,plan.fingerprint,{'u':10})
                grants={name:store.grant(run,name,spec.scope,expires=store.clock()+1000,max_attempts=3) for name,spec in self.specs.tools.items()}
                config=path.with_suffix('.json')
                log=path.with_suffix('.log')
                data={'database':str(path),'run':run,'grants':grants,'log':str(log)}
                if raw:
                    scope=store.open_scope(run,self.who,'scope',{})
                    data['grants']={'send':grants['send']}
                    data['task']=store.spawn_task(scope,self.who,'child',narrower(self.specs).fingerprint,{},
                        inputs={'permission':Value(grants['send']).record()})
                config.write_text(json.dumps(data),encoding='utf-8')
                command=[sys.executable,'-m','tests.task_capability_worker',str(config)]
                crashed=subprocess.run(command+[point],capture_output=True,text=True,timeout=30)
                self.assertEqual(crashed.returncode,77,crashed.stderr)
                resumed=subprocess.run(command+['none'],capture_output=True,text=True,timeout=30)
                self.assertEqual(resumed.returncode,0,resumed.stderr)
                events=store.events(run,self.who)
                self.assertEqual(sum(e['kind']=='TaskCapabilitiesBound' for e in events),1)
                if raw:
                    cap=json.loads(resumed.stdout)['data']
                    self.assertEqual(store.capability_info(run,self.who,cap)['max_attempts'],1)
                else:
                    self.assertEqual(json.loads(resumed.stdout)['data'],'Done')
                    self.assertEqual(log.read_text(encoding='utf-8').splitlines(),['hello'])
                    self.assertEqual(store.budget(run,self.who)['u']['confirmed'],1)
