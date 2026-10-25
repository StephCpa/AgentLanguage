from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from al0.custody import ResourceRef
from al0.data import Intent, Principal, Slot
from al0.errors import AuthorizationDenied, ReplayMismatch, StateConflict
from al0.store import Store
from examples.affine_task_resources import demo


class ResourceCustodyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'runtime.sqlite'
        self.store = Store(self.path,clock=lambda:10)
        self.who = Principal('tenant','subject')
        self.run = self.store.create_run(self.who,'custody',{'cost':20})
        self.store.register_contract('work','1',{'cost':2},retry_window=100)
        self.grant = self.store.grant(self.run,'work','outbox',expires=1000,max_attempts=3)
        self.intent = Intent('work','1','outbox',{'channel':'reports'})
        self.permit = self.store.authorize(self.run,self.who,self.intent,self.grant,authorization_key='p')
        self.a = self.store.split_budget(self.run,self.who,'a',{'cost':8})
        self.b = self.store.split_budget(self.run,self.who,'b',{'cost':4})
        self.refs = {'permit':self.store.manage_resource(self.run,self.who,'Permit',self.permit),
                     'budget':self.store.manage_resource(self.run,self.who,'Budget',self.a)}
        self.scope = self.store.open_scope(self.run,self.who,'scope',{'cost':4})

    def access(self, refs=(), lease=None):
        return self.store.resource_access(self.run,self.who,refs.values() if isinstance(refs,dict) else refs,task_lease=lease)

    def spawn(self, *, moves=None, key='child', caps=None, amount=None, scope=None):
        moves = self.refs if moves is None else moves
        with self.access(moves):
            return self.store.spawn_task(self.scope if scope is None else scope,self.who,key,'host-child',
                {} if amount is None else amount,caps={'work':self.grant} if caps is None else caps,moves=moves)

    def child(self):
        task = self.spawn()
        lease = self.store.claim_task(task,self.who)
        return task,lease,self.store.task_resource_inputs(task,self.who)

    def admit(self,task,lease,refs):
        with self.access(refs,lease):
            op = self.store.admit(self.run,self.who,Slot('send',task),self.intent,self.permit,allocation=self.a,task_lease=lease)
        return op, {'operation':self.store.resource_birth('Operation',op,self.who),'budget':refs['budget']}

    def attempt(self, op):
        return self.store.snapshot(op,self.who)['attempts'][-1]['id']

    def publish(self,lease,refs):
        with self.access(refs,lease):
            self.store.finish_task(lease,self.who,{'data':'done','receipts':[]},resources=refs)

    def receive(self,task):
        with self.access():
            self.store.join_task(task,self.who)
        return self.store.task_resource_result(task,self.who)

    def held(self,ref):
        with self.store._tx() as db:
            return dict(self.store._custody(db,ref.kind,ref.resource))

    def test_independent_provider_records_two_charges_one_effect_across_return(self):
        with tempfile.TemporaryDirectory() as folder:
            result = demo(folder)
        self.assertEqual(result['business'],'Applied')
        self.assertEqual((result['provider_effects'],result['provider_fees']),(1,2))
        self.assertTrue(result['distinct_funding_accounts'])
        self.assertEqual(result['budget']['cost'],dict(total=10,free=8,reserved=0,confirmed=2,retired=0))
        self.assertEqual(result['replay_added_events'],0)

    def test_strings_cannot_bypass_enrolled_permit_or_budget(self):
        with self.assertRaises(StateConflict):
            self.store.admit(self.run,self.who,Slot('bad'),self.intent,self.permit,allocation=self.a)
        with self.assertRaises(StateConflict):
            self.store.split_budget(self.run,self.who,'bad',{'cost':1},parent=self.a)
        with self.assertRaises(StateConflict):
            self.store.open_scope(self.run,self.who,'bad-scope',{'cost':1},allocation=self.a)
        with self.access([self.refs['budget']]):
            with self.assertRaises(StateConflict):
                self.store.admit(self.run,self.who,Slot('bad'),self.intent,self.permit,allocation=self.a)
        self.assertEqual(self.store.budget(self.run,self.who)['cost']['reserved'],0)

    def test_move_then_admit_consumes_child_permit_and_creates_child_operation(self):
        task,lease,refs = self.child()
        op,exports = self.admit(task,lease,refs)
        self.assertEqual(self.held(exports['operation'])['owner'],task)
        self.assertEqual(self.held(refs['permit'])['state'],'Spent')
        with self.access(self.refs):
            with self.assertRaises(StateConflict):
                self.store.split_budget(self.run,self.who,'stale',{'cost':1},parent=self.a)
        with self.assertRaises(StateConflict):
            self.store.start_attempt(op,self.attempt(op),self.who,task_lease=lease)
        with self.access(exports,lease):
            self.store.start_attempt(op,self.attempt(op),self.who,task_lease=lease)

    def test_bundle_alias_and_stale_manifest_roll_back_spawn_and_funding(self):
        before=self.store.budget(self.run,self.who)
        for moves in ({'a':self.refs['budget'],'b':self.refs['budget']},
                      {'budget':replace(self.refs['budget'],generation=5)}):
            with self.assertRaises(StateConflict):
                self.spawn(moves=moves,amount={'cost':2})
        self.assertEqual(self.store.budget(self.run,self.who),before)
        self.assertEqual(self.store.task_snapshot(self.scope,self.who)['children'],[])
        self.assertEqual(self.held(self.refs['budget'])['owner_kind'],'Root')

    def test_spawn_replay_reads_old_tokens_without_reinstalling_custody(self):
        task,lease,refs=self.child()
        self.publish(lease,refs)
        returned=self.receive(task)
        before=self.store.events(self.run,self.who)
        self.assertEqual(self.spawn(),task)
        self.assertEqual(self.store.task_resource_inputs(task,self.who),refs)
        self.assertEqual(self.held(returned['budget'])['generation'],3)
        self.assertEqual(self.store.events(self.run,self.who),before)
        with self.assertRaises(ReplayMismatch):
            self.spawn(moves={'budget':self.refs['budget']})

    def test_publish_escrow_prevents_both_parties_from_new_use(self):
        task,lease,refs=self.child()
        self.publish(lease,refs)
        self.assertEqual(self.held(refs['budget'])['owner_kind'],'Escrow')
        with self.assertRaises(StateConflict):
            self.store.task_resource_result(task,self.who)
        for source,worker in ((refs,lease),(self.refs,None)):
            with self.access(source,worker),self.assertRaises(StateConflict):
                self.store.split_budget(self.run,self.who,'escaped',{'cost':1},parent=self.a,task_lease=worker)
        with self.assertRaises(StateConflict):
            self.store.join_task(task,self.who)

    def test_aba_and_enrollment_receipt_never_refresh_old_reference(self):
        task,lease,refs=self.child()
        self.publish(lease,refs)
        received=self.receive(task)
        old=self.store.manage_resource(self.run,self.who,'Budget',self.a)
        self.assertEqual(old,self.refs['budget'])
        self.assertEqual(received['budget'].generation,3)
        with self.access([old]),self.assertRaises(StateConflict):
            self.store.split_budget(self.run,self.who,'stale',{'cost':1},parent=self.a)
        with self.access(received):
            allocation=self.store.split_budget(self.run,self.who,'fresh',{'cost':1},parent=self.a)
        self.assertEqual(self.held(self.store.resource_birth('Budget',allocation,self.who))['owner_kind'],'Root')

    def test_worker_recovery_fences_old_epoch_without_removing_new_custody(self):
        task,first,refs=self.child()
        second=self.store.claim_task(task,self.who,recover=True)
        with self.access(refs,first),self.assertRaises(StateConflict):
            self.store.admit(self.run,self.who,Slot('send',task),self.intent,self.permit,allocation=self.a,task_lease=first)
        self.admit(task,second,refs)

    def test_cancelled_escrow_cannot_be_received_and_unused_permit_cannot_revive(self):
        task,lease,refs=self.child()
        self.publish(lease,refs)
        self.store.handoff_task(task,self.who)
        with self.assertRaises(StateConflict):
            self.receive(task)
        self.assertEqual(self.held(refs['permit'])['state'],'Retired')
        self.spawn()
        self.assertEqual(self.held(refs['permit'])['state'],'Retired')
        self.assertEqual(self.store.budget(self.run,self.who)['cost']['reserved'],0)

    def test_cancellation_preserves_exposed_funding_and_accepts_late_evidence(self):
        task,lease,refs=self.child()
        op,exports=self.admit(task,lease,refs)
        attempt=self.attempt(op)
        with self.access(exports,lease):
            self.store.start_attempt(op,attempt,self.who,task_lease=lease)
        self.store.handoff_task(task,self.who)
        self.assertEqual(self.store.budget(self.run,self.who)['cost']['reserved'],2)
        with self.access(exports,lease),self.assertRaises(StateConflict):
            self.store.retry(op,self.who,task_lease=lease,allocation=self.a)
        self.store.settle(attempt,self.who,provider='work',evidence_id='late',actual={'cost':1})
        self.assertEqual(self.store.budget(self.run,self.who)['cost']['confirmed'],1)
        self.assertEqual(self.store.budget(self.run,self.who)['cost']['reserved'],0)

    def test_two_joiners_get_one_durable_transfer_and_same_historical_result(self):
        task,lease,refs=self.child()
        self.publish(lease,refs)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a,b=list(pool.map(lambda _:self.receive(task),range(2)))
        self.assertEqual(a,b)
        events=[e for e in self.store.events(self.run,self.who) if e['kind']=='TaskResourcesReceived']
        self.assertEqual(len(events),1)

    def test_join_cancel_race_has_exactly_one_receiver_or_runtime_owner(self):
        task,lease,refs=self.child()
        self.publish(lease,refs)
        def join():
            try:return self.receive(task)
            except StateConflict:return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            future=pool.submit(join)
            pool.submit(self.store.handoff_task,task,self.who).result()
            received=future.result()
        row=self.held(refs['budget'])
        self.assertEqual(row['owner_kind'],'Root' if received else 'Runtime')
        if received:
            self.assertEqual(row['state'],'Live')
        else:
            self.assertEqual(row['state'],'Retired')

    def test_authority_cannot_move_into_a_narrower_child_table(self):
        narrow=self.store.attenuate_capability(self.run,self.who,'narrow',self.grant,max_attempts=1)['grant']
        with self.assertRaises(AuthorizationDenied):
            self.spawn(caps={'work':narrow})
        self.assertEqual(self.store.task_snapshot(self.scope,self.who)['children'],[])

    def test_task_cannot_mint_managed_authority_outside_its_fixed_table(self):
        task=self.spawn(moves={'budget':self.refs['budget']},caps={})
        lease=self.store.claim_task(task,self.who)
        with self.access((),lease),self.assertRaises(AuthorizationDenied):
            self.store.authorize(self.run,self.who,self.intent,self.grant,authorization_key='wider',task_lease=lease)

    def test_revocation_still_blocks_dispatch_after_move(self):
        task,lease,refs=self.child()
        op,exports=self.admit(task,lease,refs)
        self.store.revoke(self.grant)
        with self.access(exports,lease),self.assertRaises(AuthorizationDenied):
            self.store.start_attempt(op,self.attempt(op),self.who,task_lease=lease)

    def test_speculative_task_cannot_dispatch_a_previously_admitted_write(self):
        with self.access(self.refs):
            op=self.store.admit(self.run,self.who,Slot('before'),self.intent,self.permit,allocation=self.a)
        opref=self.store.resource_birth('Operation',op,self.who)
        spec=self.store.open_scope(self.run,self.who,'spec',{},speculative=True)
        task=self.spawn(moves={'op':opref},scope=spec)
        lease=self.store.claim_task(task,self.who)
        refs=self.store.task_resource_inputs(task,self.who)
        with self.access(refs,lease),self.assertRaises(StateConflict):
            self.store.start_attempt(op,self.attempt(op),self.who,task_lease=lease)

    def test_returned_child_budget_survives_closed_financial_ancestors(self):
        task=self.spawn(moves={},amount={'cost':4})
        lease=self.store.claim_task(task,self.who)
        ambient=self.store.task_snapshot(task,self.who)['allocation']
        with self.access((),lease):
            allocation=self.store.split_budget(self.run,self.who,'inner',{'cost':3},parent=ambient,task_lease=lease)
        budget=self.store.resource_birth('Budget',allocation,self.who)
        self.publish(lease,{'budget':budget})
        returned=self.receive(task)
        self.store.close_scope(self.scope,self.who)
        with self.access(returned):
            self.store.join_budget(self.run,self.who,allocation)
        self.assertEqual(self.held(budget)['state'],'Spent')
        tree=self.store.allocation_tree(self.run,self.who)
        self.assertTrue(all(row['state']=='Joined' for row in tree if row['id'] in (ambient,allocation)))

    def test_foreign_actor_and_forged_generation_do_not_change_custody(self):
        with self.store.resource_access(self.run,Principal('wrong','subject'),self.refs.values()),self.assertRaises(AuthorizationDenied):
            self.store.split_budget(self.run,self.who,'wrong',{'cost':1},parent=self.a)
        forged=ResourceRef('Budget',self.a,100)
        with self.access([forged]),self.assertRaises(StateConflict):
            self.store.split_budget(self.run,self.who,'wrong',{'cost':1},parent=self.a)

    def test_parent_task_cannot_be_impersonated_with_root_access_context(self):
        task,lease,refs=self.child()
        with self.access(refs),self.assertRaises(StateConflict):
            self.store.admit(self.run,self.who,Slot('send',task),self.intent,self.permit,allocation=self.a,task_lease=lease)

    def test_legacy_permit_without_origin_is_not_guessed_from_current_actor(self):
        raw=self.store.authorize(self.run,self.who,self.intent,self.grant,authorization_key='legacy')
        with self.store._tx() as db:
            db.execute('DELETE FROM permit_origins WHERE permit=?',(raw,))
        with self.assertRaises(StateConflict):
            self.store.manage_resource(self.run,self.who,'Permit',raw)

    def test_nested_task_return_uses_parent_worker_and_preserves_authority(self):
        task,lease,refs=self.child()
        with self.access(refs,lease):
            nested_scope=self.store.open_scope(self.run,self.who,'nested',{'cost':1},allocation=self.a,parent_lease=lease)
            nested=self.store.spawn_task(nested_scope,self.who,'nested','host-child',{},parent_lease=lease,
                caps={'work':self.grant},moves=refs)
        nested_lease=self.store.claim_task(nested,self.who)
        nested_refs=self.store.task_resource_inputs(nested,self.who)
        self.publish(nested_lease,nested_refs)
        with self.access(),self.assertRaises(StateConflict):
            self.store.join_task(nested,self.who)
        with self.access((),lease):
            self.store.join_task(nested,self.who,parent_lease=lease)
        returned=self.store.task_resource_result(nested,self.who)
        self.store.close_scope(nested_scope,self.who,parent_lease=lease)
        self.assertEqual(self.held(returned['permit'])['owner'],task)
        self.admit(task,lease,returned)

    def test_moving_budget_does_not_move_an_unmanaged_operation_by_accident(self):
        raw=self.store.authorize(self.run,self.who,self.intent,self.grant,authorization_key='legacy-op')
        op=self.store.admit(self.run,self.who,Slot('legacy'),self.intent,raw,allocation=self.b)
        budget=self.store.manage_resource(self.run,self.who,'Budget',self.b)
        task=self.spawn(moves={'budget':budget})
        lease=self.store.claim_task(task,self.who)
        refs=self.store.task_resource_inputs(task,self.who)
        with self.access(refs,lease),self.assertRaises(StateConflict):
            self.store.start_attempt(op,self.attempt(op),self.who,task_lease=lease)
        self.store.start_attempt(op,self.attempt(op),self.who)

    def test_returned_resource_can_move_on_without_old_join_reinstalling_it(self):
        task,lease,refs=self.child()
        self.publish(lease,refs)
        returned=self.receive(task)
        second=self.spawn(moves=returned,key='second')
        before=self.store.events(self.run,self.who)
        self.store.join_task(task,self.who)
        self.assertEqual(self.store.task_resource_result(task,self.who),returned)
        self.assertEqual(self.held(returned['budget'])['owner'],second)
        self.assertEqual(self.store.events(self.run,self.who),before)

    def crash(self,action,point,refs,**extra):
        config={'database':str(self.path),'run':self.run,'scope':self.scope,'grant':self.grant,
                'action':action,'point':point,'refs':{k:v.record() for k,v in refs.items()},**extra}
        path=Path(self.temp.name)/'worker.json'
        path.write_text(json.dumps(config),encoding='utf-8')
        result=subprocess.run([sys.executable,'-m','tests.resource_custody_worker',str(path)],
            cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,77,result.stdout+result.stderr)

    def test_process_exit_during_spawn_rolls_back_every_move(self):
        before=self.store.budget(self.run,self.who)
        self.crash('spawn','after_task_resources_move',self.refs)
        self.assertEqual(self.store.task_snapshot(self.scope,self.who)['children'],[])
        self.assertEqual(self.held(self.refs['budget'])['generation'],0)
        self.assertEqual(self.held(self.refs['permit'])['owner_kind'],'Root')
        self.assertEqual(self.store.budget(self.run,self.who),before)
        self.spawn()

    def test_process_exit_after_spawn_commit_replays_without_second_move(self):
        self.crash('spawn','after_spawn_commit',self.refs)
        task=self.spawn()
        refs=self.store.task_resource_inputs(task,self.who)
        self.assertEqual(refs['budget'].generation,1)
        self.assertEqual(len(self.store.task_snapshot(self.scope,self.who)['children']),1)

    def test_process_exit_during_publish_keeps_resources_with_task(self):
        task,lease,refs=self.child()
        self.crash('publish','after_task_resources_publish',refs,task=task,epoch=lease.epoch)
        self.assertEqual(self.store.task_snapshot(task,self.who)['state'],'Running')
        self.assertEqual(self.held(refs['budget'])['owner_kind'],'Task')
        new_lease=self.store.claim_task(task,self.who,recover=True)
        self.publish(new_lease,refs)
        self.receive(task)

    def test_process_exit_after_publish_commit_recovers_escrow(self):
        task,lease,refs=self.child()
        self.crash('publish','after_task_finish_commit',refs,task=task,epoch=lease.epoch)
        self.assertEqual(self.held(refs['budget'])['owner_kind'],'Escrow')
        self.publish(lease,refs)
        returned=self.receive(task)
        self.assertEqual(returned['permit'].generation,3)

    def test_process_exit_during_receive_rolls_back_entire_bundle(self):
        task,lease,refs=self.child()
        self.publish(lease,refs)
        self.crash('join','after_task_resources_receive',{},task=task)
        self.assertEqual(self.held(refs['budget'])['owner_kind'],'Escrow')
        self.assertEqual(self.held(refs['permit'])['owner_kind'],'Escrow')
        returned=self.receive(task)
        self.assertEqual(returned['budget'].generation,3)

    def test_process_exit_after_receive_commit_does_not_reassign_on_replay(self):
        task,lease,refs=self.child()
        self.publish(lease,refs)
        self.crash('join','after_task_join_commit',{},task=task)
        before=self.store.events(self.run,self.who)
        returned=self.receive(task)
        self.assertEqual(returned['budget'].generation,3)
        self.assertEqual(self.store.events(self.run,self.who),before)

    def test_process_exit_before_cancel_commit_retains_task_custody_and_reservation(self):
        task,lease,refs=self.child()
        op,exports=self.admit(task,lease,refs)
        with self.access(exports,lease):
            self.store.start_attempt(op,self.attempt(op),self.who,task_lease=lease)
        self.crash('cancel','before_handoff_commit',{},task=task)
        self.assertEqual(self.held(exports['operation'])['owner_kind'],'Task')
        self.assertFalse(self.store.snapshot(op,self.who)['cancel_requested'])
        self.assertEqual(self.store.budget(self.run,self.who)['cost']['reserved'],2)
        self.store.handoff_task(task,self.who)

    def test_process_exit_after_cancel_commit_keeps_runtime_responsibility(self):
        task,lease,refs=self.child()
        op,exports=self.admit(task,lease,refs)
        attempt=self.attempt(op)
        with self.access(exports,lease):
            self.store.start_attempt(op,attempt,self.who,task_lease=lease)
        self.crash('cancel','after_handoff_commit',{},task=task)
        self.assertEqual(self.held(exports['operation'])['owner_kind'],'Runtime')
        self.assertEqual(self.store.budget(self.run,self.who)['cost']['reserved'],2)
        self.store.handoff_task(task,self.who)
        self.store.settle(attempt,self.who,provider='work',evidence_id='late',actual={'cost':1})
        self.assertEqual(self.store.budget(self.run,self.who)['cost']['confirmed'],1)

    def test_access_context_is_reset_even_after_exception(self):
        with self.assertRaisesRegex(RuntimeError,'abort'):
            with self.access(self.refs):
                raise RuntimeError('abort')
        with self.assertRaises(StateConflict):
            self.store.split_budget(self.run,self.who,'leak',{'cost':1},parent=self.a)

    def test_v16_migration_does_not_guess_old_permit_origins(self):
        before=self.store.budget(self.run,self.who)
        with self.store._tx() as db:
            for table in ('task_resource_returns','task_resource_inputs','resource_custody','permit_origins'):
                db.execute('DROP TABLE '+table)
            db.execute('PRAGMA user_version=16')
        self.store=Store(self.path,clock=lambda:10)
        self.assertEqual(self.store.budget(self.run,self.who),before)
        with self.assertRaises(StateConflict):
            self.store.manage_resource(self.run,self.who,'Permit',self.permit)
        budget=self.store.manage_resource(self.run,self.who,'Budget',self.a)
        self.assertEqual(budget.generation,0)
        # Legacy admission remains subject to its existing authority and ledger.
        with self.access([budget]):
            op=self.store.admit(self.run,self.who,Slot('legacy'),self.intent,self.permit,allocation=self.a)
        self.assertEqual(self.store.attempt_funding(self.attempt(op),self.who)['allocation'],self.a)
