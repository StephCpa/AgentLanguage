from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from threading import Event, Barrier
import unittest

from al0.concurrency import TaskRunner, TaskScope
from al0.data import Intent, Principal, Slot
from al0.errors import AuthorizationDenied, BudgetExceeded, ReplayMismatch, StateConflict
from al0.execute import Broker, Response, Suspended
from al0.ir import Catalog, Tool
from al0.sdk import PlanBuilder
from al0.store import Store


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'runtime.sqlite'
        self.store = Store(self.path, clock=lambda: 10)
        self.principal = Principal('tenant', 'subject')
        self.run = self.store.create_run(self.principal, 'controller', {'cost': 12})
        self.scope = self.store.open_scope(self.run, self.principal, 'scope', {'cost': 10})
        self.store.register_contract('work', '1', {'cost': 2}, retry_window=100)
        self.grant = self.store.grant(self.run, 'work', 'scope', expires=1000, max_attempts=3)
        self.intent = Intent('work', '1', 'scope', {'input': 1})

    def spawn(self, key='child', amount=6, parent=None, lease=None):
        return self.store.spawn_task(parent or self.scope, self.principal, key, 'child-program', {'cost': amount}, parent_lease=lease)

    def working(self):
        task = self.spawn()
        return task, self.store.claim_task(task, self.principal)

    def admit(self, task, lease, node='work'):
        row = self.store.task_snapshot(task, self.principal)
        permit = self.store.authorize(self.run, self.principal, self.intent, self.grant, authorization_key=task+':'+node)
        return self.store.admit(self.run, self.principal, Slot(node, task), self.intent, permit, allocation=row['allocation'], task_lease=lease)

    def start(self, operation, lease):
        attempt = self.store.snapshot(operation, self.principal)['attempts'][-1]['id']
        self.store.start_attempt(operation, attempt, self.principal, task_lease=lease)
        return attempt

    def assert_accounted(self):
        tree = self.store.allocation_tree(self.run, self.principal)
        budget = self.store.budget(self.run, self.principal)['cost']
        for key in ('free','reserved','confirmed','retired'):
            self.assertEqual(budget[key], sum(a['budget']['cost'][key] for a in tree))
        self.assertEqual(budget['total'], sum(budget[k] for k in ('free','reserved','confirmed','retired')))

    def test_spawn_and_budget_transfer_replay_once(self):
        task = self.spawn()
        before = self.store.events(self.run, self.principal)
        self.assertEqual(task, self.spawn())
        self.assertEqual(before, self.store.events(self.run, self.principal))
        with self.assertRaises(ReplayMismatch):
            self.spawn(amount=5)
        self.assert_accounted()

    def test_failed_spawn_does_not_leave_task_or_allocation(self):
        tree = self.store.allocation_tree(self.run, self.principal)
        with self.assertRaises(BudgetExceeded):
            self.spawn(amount=11)
        self.assertEqual(tree, self.store.allocation_tree(self.run, self.principal))
        self.assertEqual(self.store.task_snapshot(self.scope, self.principal)['children'], [])

    def test_two_spawns_compete_for_one_scope_budget(self):
        def spawn(key):
            try:
                return self.spawn(key)
            except BudgetExceeded:
                return None
        with ThreadPoolExecutor(2) as pool:
            result = list(pool.map(spawn, ['left','right']))
        self.assertEqual(sum(r is not None for r in result), 1)
        self.assert_accounted()

    def test_root_scope_identity_cannot_change_funding(self):
        self.assertEqual(self.scope, self.store.open_scope(self.run,self.principal,'scope',{'cost':10}))
        with self.assertRaises(ReplayMismatch):
            self.store.open_scope(self.run,self.principal,'scope',{'cost':9})

    def test_task_allocation_cannot_be_split_or_joined_outside_lifecycle(self):
        task, lease = self.working()
        allocation = self.store.task_snapshot(task,self.principal)['allocation']
        with self.assertRaises(StateConflict):
            self.store.split_budget(self.run,self.principal,'escape',{'cost':1},parent=allocation)
        with self.assertRaises(StateConflict):
            self.store.join_budget(self.run,self.principal,allocation)
        with self.assertRaises(StateConflict):
            self.admit(task,None)
        self.admit(task,lease)
        self.assert_accounted()

    def test_claim_recovery_fences_old_admission_dispatch_and_completion(self):
        task, first = self.working()
        operation = self.admit(task, first)
        with self.assertRaises(StateConflict):
            self.store.claim_task(task,self.principal)
        second = self.store.claim_task(task,self.principal,recover=True)
        for action in (lambda:self.admit(task,first,'new'), lambda:self.start(operation,first),
                       lambda:self.store.finish_task(first,self.principal,'stale')):
            with self.assertRaises(StateConflict):
                action()
        self.assertEqual(operation,self.admit(task,second))
        self.start(operation,second)
        self.assert_accounted()

    def test_cancel_queued_task_reclaims_reservation(self):
        task, lease = self.working()
        operation = self.admit(task,lease)
        self.store.handoff_task(task,self.principal)
        state=self.store.task_snapshot(task,self.principal)
        self.assertEqual(state['state'],'Cancelled')
        self.assertEqual(state['disposition'],'Joined')
        self.assertEqual(state['obligations'],[])
        self.assertEqual(self.store.snapshot(operation,self.principal)['business'],'ProvenNoEffect')
        with self.assertRaises(StateConflict):
            self.start(operation,lease)
        self.assertEqual(self.store.budget(self.run,self.principal)['cost']['reserved'],0)
        self.assert_accounted()

    def test_inflight_scope_exit_keeps_responsibility_and_accepts_late_receipts(self):
        task, lease = self.working()
        operation = self.admit(task,lease)
        attempt=self.start(operation,lease)
        self.store.close_scope(self.scope,self.principal,cancel=True)
        self.store.finish_run(self.run,self.principal)
        self.assertEqual(self.store.status(self.run,self.principal)['status'],'CompletedWithObligations')
        self.assertEqual(self.store.task_snapshot(task,self.principal)['disposition'],'Runtime')
        self.assertEqual(self.store.budget(self.run,self.principal)['cost']['reserved'],2)
        self.store.business_result(operation,self.principal,provider='work',evidence_id='result',outcome='Applied')
        self.store.settle(attempt,self.principal,provider='work',evidence_id='bill',actual={'cost':1})
        self.assertEqual(self.store.status(self.run,self.principal)['status'],'CompletedClean')
        self.assertEqual(self.store.task_snapshot(task,self.principal)['obligations'],[])
        self.assertEqual(self.store.allocation_tree(self.run,self.principal)[0]['budget']['cost']['free'],11)
        self.assert_accounted()

    def test_normal_scope_exit_requires_consumed_child_control_handles(self):
        task, lease = self.working()
        with self.assertRaises(StateConflict):
            self.store.close_scope(self.scope,self.principal)
        with self.assertRaises(StateConflict):
            self.store.finish_run(self.run,self.principal)
        self.store.finish_task(lease,self.principal,'value')
        with self.assertRaises(StateConflict):
            self.store.close_scope(self.scope,self.principal)
        self.store.join_task(task,self.principal)
        self.store.close_scope(self.scope,self.principal)
        self.store.finish_run(self.run,self.principal)
        self.assertEqual(self.store.status(self.run,self.principal)['status'],'CompletedClean')

    def test_join_running_task_requires_explicit_handoff(self):
        task, _ = self.working()
        with self.assertRaises(StateConflict):
            self.store.join_task(task,self.principal)
        self.store.handoff_task(task,self.principal)
        before=self.store.events(self.run,self.principal)
        self.store.handoff_task(task,self.principal)
        self.assertEqual(before,self.store.events(self.run,self.principal))

    def test_completed_task_join_hands_unresolved_work_to_runtime(self):
        task, lease=self.working()
        operation=self.admit(task,lease)
        self.start(operation,lease)
        self.store.finish_task(lease,self.principal,'local-return')
        joined=self.store.join_task(task,self.principal)
        self.assertEqual(joined['state'],'Completed')
        self.assertEqual(joined['disposition'],'Runtime')
        self.assertEqual(len(joined['obligations']),1)
        self.assert_accounted()

    def test_nested_tasks_must_be_joined_before_parent_returns(self):
        parent, lease=self.working()
        child=self.spawn('nested',3,parent,lease)
        with self.assertRaises(StateConflict):
            self.store.finish_task(lease,self.principal,'parent')
        child_lease=self.store.claim_task(child,self.principal)
        self.store.finish_task(child_lease,self.principal,'child')
        self.store.join_task(child,self.principal,parent_lease=lease)
        self.store.finish_task(lease,self.principal,'parent')
        self.store.join_task(parent,self.principal)
        self.store.close_scope(self.scope,self.principal)
        self.assert_accounted()

    def test_nested_cancellation_closes_all_allocations_bottom_up(self):
        parent, lease=self.working()
        child=self.spawn('nested',3,parent,lease)
        child_lease=self.store.claim_task(child,self.principal)
        operation=self.admit(child,child_lease)
        self.store.close_scope(self.scope,self.principal,cancel=True)
        for task in (parent,child):
            self.assertEqual(self.store.task_snapshot(task,self.principal)['state'],'Cancelled')
        self.assertEqual(self.store.snapshot(operation,self.principal)['business'],'ProvenNoEffect')
        self.assertEqual(self.store.allocation_tree(self.run,self.principal)[0]['budget']['cost']['free'],12)
        self.assert_accounted()

    def test_scope_exit_races_dispatch_at_one_transaction_boundary(self):
        task, lease=self.working()
        operation=self.admit(task,lease)
        def start():
            try:
                return self.start(operation,lease)
            except StateConflict:
                return None
        with ThreadPoolExecutor(2) as pool:
            dispatch=pool.submit(start)
            close=pool.submit(self.store.close_scope,self.scope,self.principal,cancel=True)
            attempt=dispatch.result()
            close.result()
        self.assertEqual(self.store.budget(self.run,self.principal)['cost']['reserved'],2 if attempt else 0)
        self.assertEqual(self.store.snapshot(operation,self.principal)['business'],'Unknown' if attempt else 'ProvenNoEffect')
        self.assert_accounted()

    def test_scope_exit_races_admission_without_orphan_work(self):
        task, lease=self.working()
        def admit():
            try:
                return self.admit(task,lease)
            except StateConflict:
                return None
        with ThreadPoolExecutor(2) as pool:
            future=pool.submit(admit)
            close=pool.submit(self.store.close_scope,self.scope,self.principal,cancel=True)
            operation=future.result()
            close.result()
        if operation:
            self.assertEqual(self.store.snapshot(operation,self.principal)['business'],'ProvenNoEffect')
        self.assertEqual(self.store.budget(self.run,self.principal)['cost']['reserved'],0)
        self.assert_accounted()

    def test_closed_scope_replay_cannot_spawn_new_child_or_reissue_budget(self):
        task=self.spawn()
        self.store.close_scope(self.scope,self.principal,cancel=True)
        events=self.store.events(self.run,self.principal)
        self.assertEqual(task,self.spawn())
        self.assertEqual(self.scope,self.store.open_scope(self.run,self.principal,'scope',{'cost':10}))
        with self.assertRaises(StateConflict):
            self.spawn('new',1)
        self.assertEqual(events,self.store.events(self.run,self.principal))

    def test_foreign_principal_cannot_take_over_task(self):
        task=self.spawn()
        bad=Principal('other','subject')
        for action in (lambda:self.store.claim_task(task,bad,recover=True),
                       lambda:self.store.handoff_task(task,bad), lambda:self.store.close_scope(self.scope,bad,cancel=True)):
            with self.assertRaises(AuthorizationDenied):
                action()

    def test_v4_migration_preserves_allocated_unknown_operation(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'v4.sqlite'
            store=Store(path,clock=lambda:10)
            run=store.create_run(self.principal,'legacy',{'cost':8})
            store.register_contract('legacy','1',{'cost':2})
            child=store.split_budget(run,self.principal,'child',{'cost':4})
            grant=store.grant(run,'legacy','scope',expires=100)
            intent=Intent('legacy','1','scope',{})
            permit=store.authorize(run,self.principal,intent,grant)
            op=store.admit(run,self.principal,Slot('legacy'),intent,permit,allocation=child)
            attempt=store.snapshot(op,self.principal)['attempts'][0]['id']
            store.start_attempt(op,attempt,self.principal)
            before=store.allocation_tree(run,self.principal)
            db=sqlite3.connect(path)
            db.executescript('DROP TABLE tasks; PRAGMA user_version=4;')
            db.close()
            store=Store(path,clock=lambda:10)
            self.assertEqual(before,store.allocation_tree(run,self.principal))
            self.assertEqual(store.snapshot(op,self.principal)['business'],'Unknown')
            store.join_budget(run,self.principal,child)
            self.assertEqual(store.budget(run,self.principal)['cost']['reserved'],2)

    def crash(self, action, point):
        path=Path(self.temp.name)/'crash.json'
        path.write_text(json.dumps(dict(database=str(self.path),scope=self.scope,action=action)),encoding='utf-8')
        result=subprocess.run([sys.executable,str(Path(__file__).with_name('task_worker.py')),str(path),point],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,77,result.stdout+result.stderr)
        self.store=Store(self.path,clock=lambda:10)
        self.assert_accounted()

    def test_spawn_crash_before_commit_rolls_back_task_and_funding(self):
        self.crash('spawn','before_spawn_commit')
        self.assertEqual(self.store.task_snapshot(self.scope,self.principal)['children'],[])
        self.assertEqual(len(self.store.allocation_tree(self.run,self.principal)),2)

    def test_spawn_crash_after_commit_recovers_one_task(self):
        self.crash('spawn','after_spawn_commit')
        events=self.store.events(self.run,self.principal)
        self.spawn()
        self.assertEqual(events,self.store.events(self.run,self.principal))

    def test_exit_crash_before_commit_preserves_active_task_and_queue(self):
        task,lease=self.working()
        operation=self.admit(task,lease)
        self.crash('close','before_scope_close_commit')
        self.assertEqual(self.store.task_snapshot(task,self.principal)['state'],'Running')
        self.assertEqual(self.store.snapshot(operation,self.principal)['attempts'][0]['phase'],'Queued')
        self.store.close_scope(self.scope,self.principal,cancel=True)
        self.assert_accounted()

    def test_exit_crash_after_commit_preserves_handoff_without_double_refund(self):
        task,lease=self.working()
        operation=self.admit(task,lease)
        self.start(operation,lease)
        self.crash('close','after_scope_close_commit')
        events=self.store.events(self.run,self.principal)
        self.store.close_scope(self.scope,self.principal,cancel=True)
        self.assertEqual(events,self.store.events(self.run,self.principal))
        self.assertEqual(self.store.task_snapshot(task,self.principal)['disposition'],'Runtime')
        self.assertEqual(self.store.budget(self.run,self.principal)['cost']['reserved'],2)


class ConcurrentInterpreterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'runtime.sqlite')
        self.principal=Principal('tenant','subject')
        self.run=self.store.create_run(self.principal,'host-controller',{'units':10})
        self.catalog=Catalog()
        self.catalog.register(Tool.define('model','infer','Text','Text','model',{'units':1}))
        builder=PlanBuilder('model-task',self.catalog)
        request=builder.literal('input','Text','hello')
        output=builder.infer('answer',builder.tool('model'),request)
        self.plan=builder.finish(output)
        self.grants={'model':self.store.grant(self.run,'model','model',expires=self.store.clock()+1000)}

    def broker(self, callback):
        return Broker(self.store,self.catalog,{'model':callback})

    def test_parallel_same_node_names_have_distinct_slots_and_cached_results(self):
        barrier=Barrier(2)
        calls=[]
        def model(ticket):
            calls.append(ticket.operation)
            barrier.wait(timeout=5)
            return Response('answer',{'units':1})
        broker=self.broker(model)
        with TaskScope(broker,self.run,self.principal,'scope',{'units':4},max_workers=2) as scope:
            left,f1=scope.spawn('left',self.plan,{'units':2},self.grants)
            right,f2=scope.spawn('right',self.plan,{'units':2},self.grants)
        self.assertEqual(f1.result().data,'answer')
        self.assertEqual(f2.result().data,'answer')
        self.assertEqual(len(set(calls)),2)
        before=self.store.events(self.run,self.principal)
        with TaskScope(broker,self.run,self.principal,'scope',{'units':4}) as replay:
            replay.spawn('left',self.plan,{'units':2},self.grants)
            replay.spawn('right',self.plan,{'units':2},self.grants)
        self.assertEqual(before,self.store.events(self.run,self.principal))
        self.assertEqual(self.store.budget(self.run,self.principal)['units']['confirmed'],2)

    def test_repeated_spawn_in_one_scope_returns_same_future(self):
        with TaskScope(self.broker(lambda t:Response('ok',{'units':1})),self.run,self.principal,'s',{'units':2}) as scope:
            first=scope.spawn('child',self.plan,{'units':2},self.grants)
            second=scope.spawn('child',self.plan,{'units':2},self.grants)
            self.assertEqual(first,second)

    def test_timeout_becomes_waiting_then_runtime_handoff_without_resend(self):
        calls=[]
        def model(ticket):
            calls.append(ticket.attempt)
            raise TimeoutError()
        broker=self.broker(model)
        with TaskScope(broker,self.run,self.principal,'s',{'units':2}) as scope:
            task,future=scope.spawn('child',self.plan,{'units':2},self.grants)
        self.assertIsInstance(future.exception(),Suspended)
        self.assertEqual(len(calls),1)
        state=self.store.task_snapshot(task,self.principal)
        self.assertEqual(state['disposition'],'Runtime')
        self.assertEqual(len(state['obligations']),1)
        self.store.finish_run(self.run,self.principal)
        self.assertEqual(self.store.status(self.run,self.principal)['status'],'CompletedWithObligations')

    def test_worker_failure_cancels_scope_and_preserves_exposed_attempt(self):
        def model(ticket):
            raise ValueError('adapter failed after exposure')
        with self.assertRaises(ValueError):
            with TaskScope(self.broker(model),self.run,self.principal,'s',{'units':2}) as scope:
                task,_=scope.spawn('child',self.plan,{'units':2},self.grants)
        state=self.store.task_snapshot(task,self.principal)
        self.assertEqual(state['state'],'Failed')
        self.assertEqual(state['disposition'],'Runtime')
        self.assertEqual(self.store.budget(self.run,self.principal)['units']['reserved'],1)

    def test_later_spawn_failure_cancels_blocked_first_sibling(self):
        first_started,release=Event(),Event()
        def model(ticket):
            if ticket.intent.value['args']=='hello':
                first_started.set()
                release.wait(timeout=5)
                return Response('late',{'units':1})
            if not first_started.wait(timeout=5):
                raise TimeoutError()
            raise ValueError('second task failed')
        builder=PlanBuilder('failing-child',self.catalog)
        request=builder.literal('input','Text','fail')
        failed=builder.finish(builder.infer('answer',builder.tool('model'),request))
        try:
            with self.assertRaisesRegex(ValueError,'second task'):
                with TaskScope(self.broker(model),self.run,self.principal,'s',{'units':4},max_workers=2) as scope:
                    first,future=scope.spawn('first',self.plan,{'units':2},self.grants)
                    scope.spawn('second',failed,{'units':2},self.grants)
            # Scope cancellation occurs while the first callback remains live.
            self.assertEqual(self.store.task_snapshot(first,self.principal)['state'],'Cancelled')
            self.assertEqual(self.store.budget(self.run,self.principal)['units']['reserved'],2)
        finally:
            release.set()
        with self.assertRaises(StateConflict):
            future.result(timeout=5)

    def crash_running_task(self, point):
        scope=self.store.open_scope(self.run,self.principal,'s',{'units':2})
        task=self.store.spawn_task(scope,self.principal,'child',self.plan.fingerprint,{'units':2})
        path=Path(self.temp.name)/'crash.json'
        path.write_text(json.dumps(dict(database=self.store.path,action='run',task=task,plan=self.plan.encoded,grants=self.grants)),encoding='utf-8')
        result=subprocess.run([sys.executable,str(Path(__file__).with_name('task_worker.py')),str(path),point],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,77,result.stdout+result.stderr)
        self.store=Store(self.store.path)
        return task

    def test_process_crash_after_dispatch_requires_explicit_recovery_and_never_resends(self):
        task=self.crash_running_task('after_dispatch_commit')
        calls=[]
        broker=self.broker(lambda t:calls.append(t))
        runner=TaskRunner(broker)
        with self.assertRaises(StateConflict):
            runner.run(task,self.principal,self.plan,self.grants)
        with self.assertRaises(Suspended):
            runner.run(task,self.principal,self.plan,self.grants,recover=True)
        self.assertEqual(calls,[])
        self.assertEqual(self.store.task_snapshot(task,self.principal)['state'],'Waiting')
        self.assertEqual(self.store.budget(self.run,self.principal)['units']['reserved'],1)

    def test_process_crash_before_task_result_commit_reuses_persisted_call(self):
        task=self.crash_running_task('before_task_finish_commit')
        calls=[]
        result=TaskRunner(self.broker(lambda t:calls.append(t))).run(task,self.principal,self.plan,self.grants,recover=True)
        self.assertEqual(result.data,'recorded')
        self.assertEqual(calls,[])
        self.assertEqual(self.store.budget(self.run,self.principal)['units']['confirmed'],1)

    def test_process_crash_after_task_result_commit_returns_saved_value(self):
        task=self.crash_running_task('after_task_finish_commit')
        before=self.store.events(self.run,self.principal)
        calls=[]
        result=TaskRunner(self.broker(lambda t:calls.append(t))).run(task,self.principal,self.plan,self.grants)
        self.assertEqual(result.data,'recorded')
        self.assertEqual(calls,[])
        self.assertEqual(before,self.store.events(self.run,self.principal))

    def test_exception_exit_fences_live_worker_but_accepts_late_bill(self):
        started, release=Event(),Event()
        def model(ticket):
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError()
            return Response('late',{'units':1})
        broker=self.broker(model)
        try:
            with self.assertRaisesRegex(ValueError,'body'):
                with TaskScope(broker,self.run,self.principal,'s',{'units':2}) as scope:
                    task,future=scope.spawn('child',self.plan,{'units':2},self.grants)
                    self.assertTrue(started.wait(timeout=5))
                    raise ValueError('body')
            self.assertEqual(self.store.task_snapshot(task,self.principal)['state'],'Cancelled')
            self.assertEqual(self.store.budget(self.run,self.principal)['units']['reserved'],1)
        finally:
            release.set()
        with self.assertRaises(StateConflict):
            future.result(timeout=5)
        self.assertEqual(self.store.budget(self.run,self.principal)['units']['confirmed'],1)
        self.assertEqual(self.store.task_snapshot(task,self.principal)['obligations'],[])

    def test_waiting_task_can_resume_after_receipt_without_recalling_model(self):
        calls=[]
        def model(ticket):
            calls.append(ticket)
            raise TimeoutError()
        broker=self.broker(model)
        scope=self.store.open_scope(self.run,self.principal,'s',{'units':2})
        task=self.store.spawn_task(scope,self.principal,'child',self.plan.fingerprint,{'units':2})
        runner=TaskRunner(broker)
        with self.assertRaises(Suspended):
            runner.run(task,self.principal,self.plan,self.grants)
        ticket=calls[0]
        self.store.business_result(ticket.operation,self.principal,provider='model',evidence_id='late',outcome='Applied',details={'output':'late'})
        self.store.settle(ticket.attempt,self.principal,provider='model',evidence_id='bill',actual={'units':1})
        result=runner.run(task,self.principal,self.plan,self.grants)
        self.assertEqual(result.data,'late')
        self.assertEqual(len(calls),1)
        self.store.join_task(task,self.principal)
        self.store.close_scope(scope,self.principal)

    def test_changed_child_program_cannot_resume(self):
        broker=self.broker(lambda t:Response('ok',{'units':1}))
        scope=self.store.open_scope(self.run,self.principal,'s',{'units':2})
        task=self.store.spawn_task(scope,self.principal,'child','different-program',{'units':2})
        with self.assertRaises(ReplayMismatch):
            TaskRunner(broker).run(task,self.principal,self.plan,self.grants)


if __name__ == '__main__':
    unittest.main()
