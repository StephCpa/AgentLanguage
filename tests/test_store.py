from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from al0.data import MAX_INTEGER, Intent, Principal, Slot, canonical, load_json
from al0.errors import (AuthorizationDenied, BudgetExceeded, ContractBreach,
                        ContractError, EvidenceMismatch, InvalidData,
                        PermitConsumed, ReplayMismatch, StateConflict)
from al0.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'runtime.sqlite'
        self.now = 10
        self.store = Store(self.path, clock=lambda: self.now)
        self.principal = Principal('tenant', 'subject')
        self.store.register_contract('merge', '1', {'cost': 1}, predicates=('tested',), retry_window=100)
        self.run = self.store.create_run(self.principal, 'program-v1', {'cost': 2})
        self.grant = self.store.grant(self.run, 'merge', 'repo', expires=1000, max_attempts=3)
        self.args = {'patch': 'fix', 'base_version': 7}
        receipt = self.store.record_check(self.run, self.principal, 'tested', '1', self.args, {'base_version': 7})
        self.intent = Intent('merge', '1', 'repo', self.args, [receipt])
        self.permit = self.store.authorize(self.run, self.principal, self.intent, self.grant)

    def admit(self, node='merge', permit=None):
        return self.store.admit(self.run, self.principal, Slot(node), self.intent, permit or self.permit)

    def attempt(self, op):
        return self.store.snapshot(op, self.principal)['attempts'][-1]['id']

    def started(self):
        op = self.admit()
        attempt = self.attempt(op)
        self.store.start_attempt(op, attempt, self.principal)
        return op, attempt

    def assert_conserved(self):
        for b in self.store.budget(self.run, self.principal).values():
            self.assertEqual(b['total'], sum(b[k] for k in ('free', 'reserved', 'confirmed', 'retired')))

    def test_immutable_intent_detaches_mutable_args(self):
        self.args['patch'] = 'modified'
        value = self.intent.value
        value['args']['patch'] = 'also modified'
        self.assertEqual(self.intent.value['args']['patch'], 'fix')

    def test_replay_returns_same_operation_after_restart_and_revocation(self):
        op = self.admit()
        self.store.revoke(self.grant)
        self.store = Store(self.path, clock=lambda: self.now)
        self.assertEqual(op, self.admit())
        with self.assertRaises(AuthorizationDenied):
            self.store.start_attempt(op, self.attempt(op), self.principal)
        self.assertEqual(self.store.budget(self.run, self.principal)['cost']['reserved'], 1)

    def test_same_slot_changed_intent_is_rejected(self):
        self.admit()
        other = Intent('merge', '1', 'repo', {'patch': 'changed', 'base_version': 7})
        with self.assertRaises(ReplayMismatch):
            self.store.admit(self.run, self.principal, Slot('merge'), other, self.permit)

    def test_different_slot_cannot_reuse_permit(self):
        self.admit()
        with self.assertRaises(PermitConsumed):
            self.admit('second')
        self.assert_conserved()

    def test_foreign_principal_cannot_observe_or_replay(self):
        op = self.admit()
        for bad in (Principal('other', 'subject'), Principal('tenant', 'other')):
            with self.assertRaises(AuthorizationDenied):
                self.store.snapshot(op, bad)
            with self.assertRaises(AuthorizationDenied):
                self.store.admit(self.run, bad, Slot('merge'), self.intent, self.permit)

    def test_receipt_cannot_be_reused_for_different_artifact(self):
        other = Intent('merge', '1', 'repo', {'patch': 'bad'}, self.intent.value['receipts'])
        with self.assertRaises(EvidenceMismatch):
            self.store.authorize(self.run, self.principal, other, self.grant)

    def test_missing_receipt_and_forged_receipt_rejected(self):
        for receipts in ([], ['approved-true']):
            with self.assertRaises(EvidenceMismatch):
                self.store.authorize(self.run, self.principal, Intent('merge', '1', 'repo', self.args, receipts), self.grant)

    def test_scope_not_just_tool_name(self):
        other = Intent('merge', '1', 'other-repo', self.args, self.intent.value['receipts'])
        with self.assertRaises(AuthorizationDenied):
            self.store.authorize(self.run, self.principal, other, self.grant)

    def test_queued_cancel_returns_budget_and_excludes_dispatch(self):
        op = self.admit()
        attempt = self.attempt(op)
        self.store.cancel(op, self.principal)
        self.store.cancel(op, self.principal)
        state = self.store.snapshot(op, self.principal)
        self.assertEqual(state['business'], 'ProvenNoEffect')
        self.assertTrue(state['billing']['attempts_closed'])
        self.assertEqual(self.store.budget(self.run, self.principal)['cost']['free'], 2)
        with self.assertRaises(StateConflict):
            self.store.start_attempt(op, attempt, self.principal)

    def test_cancel_inflight_keeps_reservation_and_accepts_late_charge(self):
        op, attempt = self.started()
        self.store.cancel(op, self.principal)
        self.assertEqual(self.store.budget(self.run, self.principal)['cost']['reserved'], 1)
        self.store.business_result(op, self.principal, provider='merge', evidence_id='success', outcome='Applied')
        self.store.settle(attempt, self.principal, provider='merge', evidence_id='bill', actual={'cost': 1})
        self.assertEqual(self.store.snapshot(op, self.principal)['business'], 'Applied')
        self.assert_conserved()

    def test_business_no_effect_does_not_refund_cost(self):
        op, attempt = self.started()
        self.store.business_result(op, self.principal, provider='merge', evidence_id='no-effect', outcome='ProvenNoEffect')
        self.assertEqual(self.store.budget(self.run, self.principal)['cost']['reserved'], 1)
        self.store.settle(attempt, self.principal, provider='merge', evidence_id='bill', actual={'cost': 1})
        self.assertEqual(self.store.budget(self.run, self.principal)['cost']['confirmed'], 1)

    def test_retry_reserves_new_attempt_and_preserves_operation(self):
        op, first = self.started()
        second = self.store.retry(op, self.principal)
        self.assertNotEqual(first, second)
        self.store.start_attempt(op, second, self.principal)
        with self.assertRaises(BudgetExceeded):
            self.store.retry(op, self.principal)
        self.store.settle(first, self.principal, provider='merge', evidence_id='bill1', actual={'cost': 1})
        summary = self.store.snapshot(op, self.principal)['billing']
        self.assertEqual(summary['confirmed']['cost'], 1)
        self.assertEqual(summary['reserved']['cost'], 1)
        self.store.settle(second, self.principal, provider='merge', evidence_id='bill2', actual={'cost': 1})
        self.assert_conserved()

    def test_retry_limit_and_contract_window(self):
        op, _ = self.started()
        self.now = 110
        with self.assertRaises(ContractError):
            self.store.retry(op, self.principal)

    def test_expiry_checked_at_actual_gateway_acceptance(self):
        op = self.admit()
        self.now = 1000
        with self.assertRaises(AuthorizationDenied):
            self.store.start_attempt(op, self.attempt(op), self.principal)

    def test_attempt_dispatch_right_cannot_be_obtained_twice(self):
        op, attempt = self.started()
        with self.assertRaises(StateConflict):
            self.store.start_attempt(op, attempt, self.principal)

    def test_duplicate_bills_settle_once(self):
        op, attempt = self.started()
        for evidence in ('bill1', 'bill1', 'bill2'):
            self.store.settle(attempt, self.principal, provider='merge', evidence_id=evidence, actual={'cost': 1})
        self.assertEqual(self.store.budget(self.run, self.principal)['cost']['confirmed'], 1)
        self.assert_conserved()

    def test_terminal_conflict_halts_but_preserves_first_decision(self):
        op, _ = self.started()
        self.store.business_result(op, self.principal, provider='merge', evidence_id='ok', outcome='Applied')
        with self.assertRaises(ContractBreach):
            self.store.business_result(op, self.principal, provider='merge', evidence_id='bad', outcome='ProvenNoEffect')
        self.assertEqual(self.store.snapshot(op, self.principal)['business'], 'Applied')
        with self.assertRaises(ContractBreach):
            self.store.authorize(self.run, self.principal, self.intent, self.grant)

    def test_stale_pending_does_not_undo_terminal(self):
        op, _ = self.started()
        self.store.business_result(op, self.principal, provider='merge', evidence_id='ok', outcome='Applied')
        self.store.business_result(op, self.principal, provider='merge', evidence_id='old', outcome='Pending')
        self.assertEqual(self.store.snapshot(op, self.principal)['business'], 'Applied')

    def test_retired_budget_is_not_returned_by_late_free_bill(self):
        op, attempt = self.started()
        self.store.retire(attempt, self.principal)
        self.store.settle(attempt, self.principal, provider='merge', evidence_id='free', actual={'cost': 0})
        b = self.store.budget(self.run, self.principal)['cost']
        self.assertEqual((b['free'], b['retired']), (1, 1))
        self.assert_conserved()

    def test_upper_bound_breach_records_actual_and_halts(self):
        op, attempt = self.started()
        with self.assertRaises(ContractBreach):
            self.store.settle(attempt, self.principal, provider='merge', evidence_id='overcharge', actual={'cost': 3})
        b = self.store.budget(self.run, self.principal)['cost']
        self.assertEqual(b['confirmed'], 3)
        self.assertEqual(json.loads(self.store.snapshot(op, self.principal)['attempts'][0]['actual']), {'cost': 3})
        with self.assertRaises(ContractBreach):
            self.store.authorize(self.run, self.principal, self.intent, self.grant)

    def test_late_overcharges_are_never_rounded_by_sqlite(self):
        op, first = self.started()
        second = self.store.retry(op, self.principal)
        self.store.start_attempt(op, second, self.principal)
        for index, attempt in enumerate((first, second)):
            with self.assertRaises(ContractBreach):
                self.store.settle(attempt, self.principal, provider='merge', evidence_id=f'large-{index}', actual={'cost': MAX_INTEGER})
        self.assertEqual(self.store.budget(self.run, self.principal)['cost']['confirmed'], 2 * MAX_INTEGER)

    def test_concurrent_admission_cannot_double_spend(self):
        permits = [self.store.authorize(self.run, self.principal, self.intent, self.grant) for _ in range(8)]
        def admit_one(index):
            try:
                return self.admit(str(index), permits[index])
            except BudgetExceeded:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            operations = list(pool.map(admit_one, range(8)))
        self.assertEqual(sum(x is not None for x in operations), 2)
        self.assert_conserved()

    def test_concurrent_same_slot_is_one_operation(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            operations = list(pool.map(lambda _: self.admit(), range(8)))
        self.assertEqual(len(set(operations)), 1)
        self.assertEqual(self.store.budget(self.run, self.principal)['cost']['reserved'], 1)

    def test_resource_vector_reservation_is_atomic(self):
        self.store.register_contract('multi', '1', {'cost': 1, 'tokens': 1})
        grant = self.store.grant(self.run, 'multi', 'repo', expires=1000)
        intent = Intent('multi', '1', 'repo', {})
        permit = self.store.authorize(self.run, self.principal, intent, grant)
        with self.assertRaises(BudgetExceeded):
            self.store.admit(self.run, self.principal, Slot('multi'), intent, permit)
        self.assertEqual(self.store.budget(self.run, self.principal)['cost']['free'], 2)

    def test_cancel_dispatch_race_has_one_linearized_outcome(self):
        for index in range(5):
            permit = self.store.authorize(self.run, self.principal, self.intent, self.grant)
            op = self.admit(f'race{index}', permit)
            attempt = self.attempt(op)
            def start():
                try:
                    self.store.start_attempt(op, attempt, self.principal)
                    return True
                except StateConflict:
                    return False
            with ThreadPoolExecutor(max_workers=2) as pool:
                future = pool.submit(start)
                pool.submit(self.store.cancel, op, self.principal).result()
                accepted = future.result()
            snapshot = self.store.snapshot(op, self.principal)
            self.assertEqual(snapshot['business'], 'Unknown' if accepted else 'ProvenNoEffect')
            self.assertEqual(snapshot['billing']['reserved'].get('cost', 0), int(accepted))
            if accepted:
                self.store.settle(attempt, self.principal, provider='merge', evidence_id=f'race-free-{index}', actual={'cost': 0})
            self.assert_conserved()

    def test_revoke_dispatch_race_event_order_matches_authorization(self):
        op = self.admit()
        attempt = self.attempt(op)
        def start():
            try:
                self.store.start_attempt(op, attempt, self.principal)
                return True
            except AuthorizationDenied:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(start)
            pool.submit(self.store.revoke, self.grant).result()
            accepted = future.result()
        events = self.store.events(self.run, self.principal)
        revoked = next(e['sequence'] for e in events if e['kind'] == 'GrantRevoked')
        dispatched = [e['sequence'] for e in events if e['kind'] == 'DispatchStarted']
        if accepted:
            self.assertLess(dispatched[0], revoked)
        else:
            self.assertEqual(dispatched, [])

    def test_authorization_replay_does_not_mint_another_permit(self):
        first = self.store.authorize(self.run, self.principal, self.intent, self.grant, authorization_key='site')
        second = self.store.authorize(self.run, self.principal, self.intent, self.grant, authorization_key='site')
        self.assertEqual(first, second)

    def test_final_billing_dimensions_and_provider_are_checked(self):
        _, attempt = self.started()
        for provider, actual in (('other', {'cost': 1}), ('merge', {}), ('merge', {'cost': 1, 'tokens': 0})):
            with self.assertRaises(EvidenceMismatch):
                self.store.settle(attempt, self.principal, provider=provider, evidence_id='bad', actual=actual)
        self.assertEqual(self.store.budget(self.run, self.principal)['cost']['reserved'], 1)

    def test_unversioned_database_is_not_silently_reinterpreted(self):
        path = Path(self.temp.name)/'old.sqlite'
        db = sqlite3.connect(path)
        db.execute('CREATE TABLE old_schema(x INTEGER)')
        db.commit()
        db.close()
        with self.assertRaises(ContractError):
            Store(path)

    def crash(self, point, **spec):
        path = Path(self.temp.name) / 'crash.json'
        path.write_text(json.dumps({'database': str(self.path), 'run': self.run, 'permit': self.permit,
                                    'intent': self.intent.value, **spec}), encoding='utf-8')
        proc = subprocess.run([sys.executable, str(Path(__file__).with_name('crash_worker.py')), str(path), point], capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 77, proc.stdout + proc.stderr)
        self.store = Store(self.path, clock=lambda: self.now)

    def test_process_crashes_before_admission_commit_rollback_everything(self):
        for point in ('after_reserve', 'after_permit_consumption', 'after_operation_insert', 'before_admit_commit'):
            with self.subTest(point=point):
                self.crash(point, action='admit')
                self.assertEqual(self.store.budget(self.run, self.principal)['cost']['free'], 2)
                self.assertFalse(any(e['kind'] == 'Admitted' for e in self.store.events(self.run, self.principal)))
        self.admit()
        self.assert_conserved()

    def test_process_crash_after_admission_commit_reuses_operation(self):
        self.crash('after_admit_commit', action='admit')
        op = self.admit()
        events = [e for e in self.store.events(self.run, self.principal) if e['kind'] == 'Admitted']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['data']['operation'], op)

    def test_process_crash_after_dispatch_retains_unknown_and_reservation(self):
        op = self.admit()
        attempt = self.attempt(op)
        self.crash('after_dispatch_commit', action='start', operation=op, attempt=attempt)
        self.assertEqual(self.store.snapshot(op, self.principal)['business'], 'Unknown')
        self.assertEqual(self.store.budget(self.run, self.principal)['cost']['reserved'], 1)
        with self.assertRaises(StateConflict):
            self.store.start_attempt(op, attempt, self.principal)

    def test_process_crashes_around_settlement_are_idempotent(self):
        op, attempt = self.started()
        self.crash('before_settlement_commit', action='settle', attempt=attempt)
        self.assertEqual(self.store.budget(self.run, self.principal)['cost']['reserved'], 1)
        self.crash('after_settlement_commit', action='settle', attempt=attempt)
        self.store.settle(attempt, self.principal, provider='merge', evidence_id='bill', actual={'cost': 1})
        self.assertEqual(self.store.budget(self.run, self.principal)['cost']['confirmed'], 1)
        self.assert_conserved()


class DataTests(unittest.TestCase):
    def test_reject_noncanonical_values(self):
        for value in (1.1, float('nan'), {1: 'x'}, object(), '\ud800'):
            with self.assertRaises(InvalidData):
                canonical(value)
        with self.assertRaises(InvalidData):
            load_json('{"x":1,"x":2}')


if __name__ == '__main__':
    unittest.main()
