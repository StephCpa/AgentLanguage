from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
from threading import Barrier, Event
import unittest

from al0.data import Intent, Principal, Slot
from al0.errors import ReplayMismatch, StateConflict, TypeCheckError
from al0.execute import Broker, Interpreter, Response
from al0.ir import Catalog, Tool, check_plan
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.speculative_repair import demo as repair_demo


def catalog():
    result=Catalog()
    result.register(Tool.define('model','infer','Text','Text','model',{'units':1}))
    result.register(Tool.define('log','observe','Text','Text','shared-log',{'units':1},shared_write=True))
    return result


def child(specs, text='answer', tool='model'):
    builder=PlanBuilder('child',specs)
    source=builder.literal('source','Text',text)
    result=(builder.infer if tool=='model' else builder.observe)('output',builder.tool(tool),source)
    return builder.finish(result)


def parallel(specs, *, race=False, children=None, speculative=None):
    plans=children or [child(specs,'left'),child(specs,'right')]
    builder=PlanBuilder('parent',specs)
    scope=builder.scope('scope',{'units':2},speculative=race if speculative is None else speculative)
    handles=[builder.spawn(name,scope,plan,{'units':1}) for name,plan in zip(['left','right'],plans)]
    if race:
        result=builder.race('winner',handles)
    else:
        builder.join('first',handles[0])
        result=builder.join('second',handles[1])
    builder.close_scope('closed',scope)
    return builder.finish(result)


class TaskIRStaticTests(unittest.TestCase):
    def setUp(self):
        self.catalog=catalog()
        self.doc=json.loads(parallel(self.catalog).encoded)

    def reject(self, document):
        with self.assertRaises(TypeCheckError):
            check_plan(document,self.catalog)

    def test_task_effect_must_be_declared(self):
        self.doc['uses'].remove('tasks')
        self.reject(self.doc)

    def test_child_effect_must_be_in_parent_boundary(self):
        self.doc['uses'].remove('infer:model')
        self.reject(self.doc)

    def test_double_join_rejected(self):
        self.doc['nodes'][4]['task']='left'
        self.reject(self.doc)

    def test_scope_cannot_close_with_unconsumed_child(self):
        self.doc['nodes'].pop(3)
        self.reject(self.doc)

    def test_scope_cannot_be_left_open_at_return(self):
        self.doc['nodes'].pop()
        self.reject(self.doc)

    def test_spawn_after_close_rejected(self):
        node=deepcopy(self.doc['nodes'][1])
        node['id']='extra'
        self.doc['nodes'].append(node)
        self.reject(self.doc)

    def test_task_handle_cannot_escape_as_result(self):
        self.doc['return']='left'
        self.reject(self.doc)

    def test_child_cannot_capture_parent_reference(self):
        self.doc['nodes'][1]['plan']['nodes'][1]['input']='scope'
        self.reject(self.doc)

    def test_handoff_consumes_handle_without_fabricating_a_value(self):
        self.doc['nodes'][3]['op']='handoff'
        check_plan(self.doc,self.catalog)
        self.doc['nodes'][4]['task']='left'
        self.reject(self.doc)

    def test_race_requires_speculative_scope(self):
        with self.assertRaises(TypeCheckError):
            parallel(self.catalog,race=True,speculative=False)

    def test_race_rejects_duplicate_handles(self):
        doc=json.loads(parallel(self.catalog,race=True).encoded)
        doc['nodes'][3]['tasks']=['left','left']
        self.reject(doc)

    def test_race_types_do_not_promote_candidate_to_checked(self):
        data=PlanBuilder('data-child',self.catalog)
        plan=data.finish(data.literal('data','Text','known'))
        with self.assertRaises(TypeCheckError):
            parallel(self.catalog,race=True,children=[plan,child(self.catalog)])

    def test_shared_write_disguised_as_observation_is_rejected(self):
        with self.assertRaises(TypeCheckError):
            parallel(self.catalog,children=[child(self.catalog,tool='log')]*2,speculative=True)

    def test_nested_non_speculative_scope_cannot_escape_outer_policy(self):
        nested=parallel(self.catalog,children=[child(self.catalog,tool='log')]*2,speculative=False)
        with self.assertRaises(TypeCheckError):
            parallel(self.catalog,children=[nested,nested],speculative=True)

    def test_explicit_sdk_effect_boundary_is_not_widened_by_spawn(self):
        builder=PlanBuilder('parent',self.catalog,uses=['tasks'])
        scope=builder.scope('scope',{'units':1})
        task=builder.spawn('child',scope,child(self.catalog),{'units':1})
        result=builder.join('result',task)
        builder.close_scope('close',scope)
        with self.assertRaises(TypeCheckError):
            builder.finish(result)

    def test_race_cannot_combine_handles_from_different_scopes(self):
        doc=json.loads(parallel(self.catalog,race=True).encoded)
        second=deepcopy(doc['nodes'][0])
        second['id']='other_scope'
        doc['nodes'].insert(1,second)
        doc['nodes'][3]['scope']='other_scope'
        self.reject(doc)


class TaskIRExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'runtime.sqlite')
        self.principal=Principal('tenant','subject')
        self.catalog=catalog()

    def tearDown(self):
        if hasattr(self,'broker'):
            self.broker.wait_for_workers(timeout=5)

    def setup_run(self, plan, callback):
        self.run=self.store.create_run(self.principal,plan.fingerprint,{'units':10})
        self.grants={name:self.store.grant(self.run,name,spec.scope,expires=self.store.clock()+1000) for name,spec in self.catalog.tools.items()}
        self.broker=Broker(self.store,self.catalog,{'model':callback,'log':callback})
        return Interpreter(self.broker)

    def test_parallel_ir_replays_distinct_children_without_new_calls(self):
        plan=parallel(self.catalog)
        barrier=Barrier(2)
        calls=[]
        def model(ticket):
            calls.append(ticket.operation)
            barrier.wait(timeout=5)
            return Response(ticket.intent.value['args'],{'units':1})
        executor=self.setup_run(plan,model)
        self.assertEqual(executor.run(plan,self.run,self.principal,self.grants).data,'right')
        before=self.store.events(self.run,self.principal)
        self.assertEqual(executor.run(plan,self.run,self.principal,self.grants).data,'right')
        self.assertEqual(before,self.store.events(self.run,self.principal))
        self.assertEqual(len(set(calls)),2)
        self.store.finish_run(self.run,self.principal)

    def test_nested_task_ir_uses_parent_funding_and_closes_all_scopes(self):
        nested=parallel(self.catalog)
        builder=PlanBuilder('outer',self.catalog)
        scope=builder.scope('scope',{'units':2})
        task=builder.spawn('child',scope,nested,{'units':2})
        result=builder.join('result',task)
        builder.close_scope('close',scope)
        plan=builder.finish(result)
        executor=self.setup_run(plan,lambda t:Response(t.intent.value['args'],{'units':1}))
        self.assertEqual(executor.run(plan,self.run,self.principal,self.grants).data,'right')
        self.store.finish_run(self.run,self.principal)
        self.assertEqual(self.store.status(self.run,self.principal)['status'],'CompletedClean')
        self.assertEqual(self.store.budget(self.run,self.principal)['units']['confirmed'],2)

    def test_race_does_not_wait_for_loser_or_erase_its_pending_bill(self):
        plan=parallel(self.catalog,race=True)
        entered,release,returned=Event(),Event(),Event()
        def model(ticket):
            if ticket.intent.value['args']=='left':
                entered.set()
                if not release.wait(timeout=5):
                    raise TimeoutError()
                returned.set()
                return Response('late-loser',{'units':1})
            if not entered.wait(timeout=5):
                raise TimeoutError()
            return Response('winner',{'units':1})
        executor=self.setup_run(plan,model)
        try:
            value=executor.run(plan,self.run,self.principal,self.grants)
            self.assertEqual(value.data,'winner')
            self.assertFalse(returned.is_set())
            self.assertEqual(self.store.budget(self.run,self.principal)['units']['reserved'],1)
            self.store.finish_run(self.run,self.principal)
            self.assertEqual(self.store.status(self.run,self.principal)['status'],'CompletedWithObligations')
            again=executor.run(plan,self.run,self.principal,self.grants)
            self.assertEqual(again.data,'winner')
            self.assertEqual(sum(e['kind']=='RaceSelected' for e in self.store.events(self.run,self.principal)),1)
        finally:
            release.set()
            # Callback return alone is not a database-close barrier.
            self.assertTrue(returned.wait(timeout=5))
        self.broker.wait_for_workers(timeout=5)
        self.assertEqual(self.store.status(self.run,self.principal)['status'],'CompletedClean')
        self.assertEqual(self.store.budget(self.run,self.principal)['units']['confirmed'],2)

    def test_failed_candidate_does_not_win_and_keeps_its_exposed_reservation(self):
        plan=parallel(self.catalog,race=True)
        barrier=Barrier(2)
        def model(ticket):
            barrier.wait(timeout=5)
            if ticket.intent.value['args']=='left':
                raise ValueError('failed candidate')
            return Response('success',{'units':1})
        executor=self.setup_run(plan,model)
        self.assertEqual(executor.run(plan,self.run,self.principal,self.grants).data,'success')
        self.assertEqual(self.store.budget(self.run,self.principal)['units']['reserved'],1)

    def test_all_candidates_fail_with_explicit_handoff_and_no_winner(self):
        plan=parallel(self.catalog,race=True)
        def model(ticket):
            raise TimeoutError()
        executor=self.setup_run(plan,model)
        with self.assertRaises(StateConflict):
            executor.run(plan,self.run,self.principal,self.grants)
        self.assertEqual(sum(e['kind']=='RaceSelected' for e in self.store.events(self.run,self.principal)),0)
        self.store.finish_run(self.run,self.principal,outcome='Failed')
        self.assertEqual(self.store.status(self.run,self.principal)['status'],'FailedWithObligations')

    def test_join_unknown_child_fails_without_losing_responsibility(self):
        plan=parallel(self.catalog)
        def model(ticket):
            raise TimeoutError()
        executor=self.setup_run(plan,model)
        with self.assertRaises(StateConflict):
            executor.run(plan,self.run,self.principal,self.grants)
        self.store.finish_run(self.run,self.principal,outcome='Failed')
        self.assertTrue(self.store.obligations(self.run,self.principal))

    def test_runtime_rejects_shared_write_even_without_ir_checker(self):
        plan=parallel(self.catalog,race=True)
        self.setup_run(plan,lambda t:Response('unused',{'units':1}))
        scope=self.store.open_scope(self.run,self.principal,'raw',{'units':1},speculative=True)
        task=self.store.spawn_task(scope,self.principal,'child','raw-program',{'units':1})
        lease=self.store.claim_task(task,self.principal)
        state=self.store.task_snapshot(task,self.principal)
        intent=Intent('log','1','shared-log','value')
        permit=self.store.authorize(self.run,self.principal,intent,self.grants['log'])
        with self.assertRaises(StateConflict):
            self.store.admit(self.run,self.principal,Slot('log',task),intent,permit,allocation=state['allocation'],task_lease=lease)
        self.assertEqual(self.store.budget(self.run,self.principal)['units']['reserved'],0)

    def test_checked_winner_is_used_for_one_parent_write(self):
        with tempfile.TemporaryDirectory() as folder:
            result=repair_demo(folder)
        self.assertEqual(result['result'],'Done')
        self.assertEqual(result['replayed_result'],'Done')
        self.assertEqual(result['winner'],result['winner_after_replay'])
        self.assertEqual(result['race_decisions'],1)
        self.assertEqual(result['endpoint']['business_operations'],1)
        self.assertTrue(result['write_after_selection'])

    def test_nested_ir_process_recovery_reuses_completed_leaf(self):
        def wrap(plan, name):
            builder=PlanBuilder(name,self.catalog)
            scope=builder.scope('scope',{'units':1})
            task=builder.spawn('child',scope,plan,{'units':1})
            result=builder.join('result',task)
            builder.close_scope('closed',scope)
            return builder.finish(result)
        plan=wrap(wrap(child(self.catalog),'inner'),'outer')
        calls=[]
        executor=self.setup_run(plan,lambda t:calls.append(t))
        config=Path(self.temp.name)/'nested.json'
        config.write_text(json.dumps(dict(database=self.store.path,plan=plan.encoded,run=self.run,grants=self.grants)),encoding='utf-8')
        crashed=subprocess.run([sys.executable,str(Path(__file__).with_name('task_ir_worker.py')),str(config),'after_task_finish_commit'],capture_output=True,text=True,timeout=20)
        self.assertEqual(crashed.returncode,77,crashed.stdout+crashed.stderr)
        result=executor.run(plan,self.run,self.principal,self.grants,recover_tasks=True)
        self.assertEqual(result.data,'recorded-child')
        self.assertEqual(calls,[])
        self.store.finish_run(self.run,self.principal)
        self.assertEqual(self.store.budget(self.run,self.principal)['units']['confirmed'],1)

    def test_explicitly_isolated_action_can_run_inside_speculation(self):
        self.catalog.register(Tool.define('sandbox','action','Text','Json','private',{'units':1},shared_write=False))
        builder=PlanBuilder('sandbox-child',self.catalog)
        value=builder.literal('input','Text','private computation')
        intent=builder.prepare('intent',builder.tool('sandbox'),value)
        permit=builder.authorize('permit',intent)
        operation=builder.admit('operation',intent,permit)
        status=builder.await_result('status',operation)
        result=builder.match_business('result',status,'Text',applied='ready',proven_no_effect='no',pending='wait',unknown='unknown',not_dispatched='wait')
        isolated=builder.finish(result)
        plan=parallel(self.catalog,race=True,children=[isolated,isolated])
        self.run=self.store.create_run(self.principal,plan.fingerprint,{'units':4})
        grants={name:self.store.grant(self.run,name,spec.scope,expires=self.store.clock()+1000) for name,spec in self.catalog.tools.items()}
        self.broker=Broker(self.store,self.catalog,{name:lambda t:Response({}, {'units':1}) for name in self.catalog.tools})
        self.assertEqual(Interpreter(self.broker).run(plan,self.run,self.principal,grants).data,'ready')
        self.broker.wait_for_workers(timeout=5)


class RaceLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'runtime.sqlite'
        self.store=Store(self.path)
        self.principal=Principal('tenant','subject')
        self.run=self.store.create_run(self.principal,'program',{'units':4})
        self.scope=self.store.open_scope(self.run,self.principal,'scope',{'units':4},speculative=True)
        self.tasks=[]
        for key in ('left','right'):
            task=self.store.spawn_task(self.scope,self.principal,key,'child',{'units':2})
            lease=self.store.claim_task(task,self.principal)
            self.store.finish_task(lease,self.principal,{'data':key,'receipts':[]})
            self.tasks.append(task)

    def choose(self, winner):
        return self.store.select_race(self.scope,self.principal,'race',self.tasks,winner)

    def test_concurrent_selectors_observe_same_persisted_winner(self):
        with ThreadPoolExecutor(2) as pool:
            winners=list(pool.map(self.choose,self.tasks))
        self.assertEqual(winners[0],winners[1])
        self.assertEqual(sum(e['kind']=='RaceSelected' for e in self.store.events(self.run,self.principal)),1)

    def test_replay_cannot_change_candidate_set(self):
        self.choose(self.tasks[0])
        with self.assertRaises(ReplayMismatch):
            self.store.race_winner(self.scope,self.principal,'race',list(reversed(self.tasks)))

    def test_scope_replay_cannot_weaken_speculation(self):
        with self.assertRaises(ReplayMismatch):
            self.store.open_scope(self.run,self.principal,'scope',{'units':4},speculative=False)

    def test_v5_migration_preserves_task_epoch_and_ordinary_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'v5.sqlite'
            store=Store(path)
            run=store.create_run(self.principal,'old-program',{'units':4})
            scope=store.open_scope(run,self.principal,'old-scope',{'units':4})
            task=store.spawn_task(scope,self.principal,'old-child','old-child-program',{'units':2})
            lease=store.claim_task(task,self.principal)
            before=store.budget(run,self.principal)
            db=sqlite3.connect(path)
            db.executescript('DROP TABLE task_races; DROP TABLE task_policies; DROP TABLE tool_effects; PRAGMA user_version=5;')
            db.close()
            store=Store(path)
            state=store.task_snapshot(task,self.principal)
            self.assertFalse(state['speculative'])
            self.assertEqual(state['state'],'Running')
            self.assertEqual(state['epoch'],lease.epoch)
            self.assertEqual(before,store.budget(run,self.principal))
            store.close_scope(scope,self.principal,cancel=True)

    def crash(self, point):
        config=Path(self.temp.name)/'race.json'
        config.write_text(json.dumps(dict(database=str(self.path),scope=self.scope,tasks=self.tasks)),encoding='utf-8')
        result=subprocess.run([sys.executable,str(Path(__file__).with_name('race_worker.py')),str(config),point],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,77,result.stdout+result.stderr)
        self.store=Store(self.path)

    def test_crash_before_selection_commit_keeps_candidates_attached(self):
        self.crash('before_race_commit')
        self.assertIsNone(self.store.race_winner(self.scope,self.principal,'race',self.tasks))
        self.assertTrue(all(self.store.task_snapshot(t,self.principal)['disposition']=='Attached' for t in self.tasks))
        self.choose(self.tasks[1])

    def test_crash_after_selection_commit_never_reselects(self):
        self.crash('after_race_commit')
        before=self.store.events(self.run,self.principal)
        self.assertEqual(self.choose(self.tasks[1]),self.tasks[0])
        self.assertEqual(before,self.store.events(self.run,self.principal))
        self.store.close_scope(self.scope,self.principal)
        self.assertEqual(self.store.budget(self.run,self.principal)['units']['free'],4)


if __name__=='__main__':
    unittest.main()
