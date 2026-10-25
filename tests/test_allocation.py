from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from al0.data import Intent, Principal, Slot
from al0.errors import AuthorizationDenied, BudgetExceeded, ContractBreach, ReplayMismatch, StateConflict
from al0.store import Store


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'runtime.sqlite'
        self.store = Store(self.path, clock=lambda: 10)
        self.principal = Principal('tenant', 'subject')
        self.run = self.store.create_run(self.principal, 'alloc-test', {'cost': 10, 'calls': 5})
        self.root = self.store.root_allocation(self.run, self.principal)
        self.store.register_contract('work', '1', {'cost': 3, 'calls': 1}, retry_window=100)
        self.grant = self.store.grant(self.run, 'work', 'scope', expires=1000, max_attempts=3)
        self.intent = Intent('work', '1', 'scope', {'value': 1})

    def split(self, key='child', amount=None, parent=None):
        return self.store.split_budget(self.run, self.principal, key,
                                       {'cost': 6, 'calls': 2} if amount is None else amount, parent=parent)

    def owner(self, allocation):
        return next(r for r in self.store.allocation_tree(self.run, self.principal) if r['id'] == allocation)

    def admit(self, allocation=None, node='work'):
        permit = self.store.authorize(self.run, self.principal, self.intent, self.grant, authorization_key=node)
        return self.store.admit(self.run, self.principal, Slot(node), self.intent, permit, allocation=allocation)

    def attempt(self, operation):
        return self.store.snapshot(operation, self.principal)['attempts'][-1]['id']

    def start(self, allocation):
        operation = self.admit(allocation)
        attempt = self.attempt(operation)
        self.store.start_attempt(operation, attempt, self.principal)
        return operation, attempt

    def settle(self, attempt, cost=1):
        self.store.settle(attempt, self.principal, provider='work', evidence_id='bill:'+attempt,
                          actual={'cost': cost, 'calls': 1})

    def assert_accounted(self, *, conserved=True):
        tree = self.store.allocation_tree(self.run, self.principal)
        for resource, account in self.store.budget(self.run, self.principal).items():
            for key in ('free', 'reserved', 'confirmed', 'retired'):
                self.assertEqual(account[key], sum(a['budget'][resource][key] for a in tree))
            if conserved:
                self.assertEqual(account['total'], sum(account[k] for k in ('free', 'reserved', 'confirmed', 'retired')))
        for allocation in tree:
            if allocation['state'] == 'Joined':
                self.assertTrue(all(b['free'] == 0 for b in allocation['budget'].values()))

    def test_split_partitions_free_without_duplicating_root_total(self):
        before = self.store.budget(self.run, self.principal)
        child = self.split()
        self.assertEqual(self.owner(self.root)['budget']['cost']['free'], 4)
        self.assertEqual(self.owner(child)['budget']['cost']['free'], 6)
        self.assertEqual(before, self.store.budget(self.run, self.principal))
        self.assert_accounted()

    def test_split_identity_is_durable_and_binds_parent_and_amount(self):
        child = self.split()
        events = self.store.events(self.run, self.principal)
        self.store = Store(self.path)
        self.assertEqual(child, self.split())
        self.assertEqual(events, self.store.events(self.run, self.principal))
        with self.assertRaises(ReplayMismatch):
            self.split(amount={'cost': 5})
        with self.assertRaises(ReplayMismatch):
            self.split(parent=child)

    def test_parent_cannot_spend_child_credit(self):
        self.split(amount={'cost': 8, 'calls': 3})
        with self.assertRaises(BudgetExceeded):
            self.admit()
        self.assert_accounted()

    def test_child_cannot_spend_sibling_or_parent_credit(self):
        child = self.split(amount={'cost': 2, 'calls': 1})
        with self.assertRaises(BudgetExceeded):
            self.admit(child)
        self.assertEqual(self.owner(child)['budget']['cost']['free'], 2)
        self.assert_accounted()

    def test_split_failure_is_atomic_across_dimensions(self):
        before = self.store.allocation_tree(self.run, self.principal)
        with self.assertRaises(BudgetExceeded):
            self.split(amount={'cost': 5, 'calls': 6})
        self.assertEqual(before, self.store.allocation_tree(self.run, self.principal))
        with self.assertRaises(BudgetExceeded):
            self.split(amount={'unknown': 0})

    def test_concurrent_splits_cannot_double_allocate(self):
        def split(key):
            try:
                return self.split(key, {'cost': 6})
            except BudgetExceeded:
                return None
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(split, ['a', 'b']))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assert_accounted()

    def test_concurrent_same_key_is_one_split(self):
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.split(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.store.allocation_tree(self.run, self.principal)), 2)
        self.assert_accounted()

    def test_same_slot_cannot_switch_its_funding_allocation(self):
        child = self.split()
        operation = self.admit(child)
        with self.assertRaises(ReplayMismatch):
            self.admit()
        self.store.join_budget(self.run, self.principal, child)
        self.assertEqual(operation, self.admit(child))
        self.assert_accounted()

    def test_join_keeps_exposed_reservation_and_refunds_only_free(self):
        child = self.split()
        operation, attempt = self.start(child)
        self.store.join_budget(self.run, self.principal, child)
        self.assertEqual(self.owner(self.root)['budget']['cost']['free'], 7)
        self.assertEqual(self.owner(child)['budget']['cost']['reserved'], 3)
        self.assertEqual(self.store.snapshot(operation, self.principal)['business'], 'Unknown')
        self.settle(attempt)
        self.assertEqual(self.owner(self.root)['budget']['cost']['free'], 9)
        self.assertEqual(self.owner(child)['budget']['cost']['confirmed'], 1)
        self.assert_accounted()

    def test_join_is_idempotent_and_does_not_reopen_on_split_replay(self):
        child = self.split()
        self.store.join_budget(self.run, self.principal, child)
        events = self.store.events(self.run, self.principal)
        self.store.join_budget(self.run, self.principal, child)
        self.assertEqual(child, self.split())
        self.assertEqual(events, self.store.events(self.run, self.principal))
        with self.assertRaises(StateConflict):
            self.split('grandchild', {'cost': 1}, parent=child)
        with self.assertRaises(StateConflict):
            self.admit(child)
        with self.assertRaises(StateConflict):
            self.store.join_budget(self.run, self.principal, self.root)

    def test_nested_join_late_refund_reaches_nearest_open_ancestor(self):
        child = self.split()
        grandchild = self.split('grandchild', {'cost': 4, 'calls': 1}, parent=child)
        operation, attempt = self.start(grandchild)
        with self.assertRaises(StateConflict):
            self.store.join_budget(self.run, self.principal, child)
        self.store.join_budget(self.run, self.principal, grandchild)
        self.store.join_budget(self.run, self.principal, child)
        self.settle(attempt)
        self.assertEqual(self.owner(self.root)['budget']['cost']['free'], 9)
        self.assert_accounted()

    def test_queued_cancel_after_join_refunds_once_to_parent(self):
        child = self.split()
        operation = self.admit(child)
        self.store.join_budget(self.run, self.principal, child)
        self.store.cancel(operation, self.principal)
        self.store.cancel(operation, self.principal)
        self.assertEqual(self.owner(self.root)['budget']['cost']['free'], 10)
        self.assert_accounted()

    def test_retirement_and_late_bill_do_not_release_child_reservation(self):
        child = self.split()
        _, attempt = self.start(child)
        self.store.retire(attempt, self.principal)
        self.store.join_budget(self.run, self.principal, child)
        self.settle(attempt, 0)
        self.assertEqual(self.owner(self.root)['budget']['cost']['free'], 7)
        self.assertEqual(self.owner(child)['budget']['cost']['retired'], 3)
        self.assert_accounted()

    def test_retry_uses_original_allocation_and_join_stops_new_attempts(self):
        child = self.split(amount={'cost': 3, 'calls': 2})
        operation, attempt = self.start(child)
        with self.assertRaises(BudgetExceeded):
            self.store.retry(operation, self.principal)
        self.settle(attempt, 0)
        retry = self.store.retry(operation, self.principal)
        self.store.start_attempt(operation, retry, self.principal)
        self.store.join_budget(self.run, self.principal, child)
        with self.assertRaises(StateConflict):
            self.store.retry(operation, self.principal)
        self.assert_accounted()

    def test_overcharge_preserves_actual_without_hiding_accounting_breach(self):
        child = self.split()
        _, attempt = self.start(child)
        self.store.join_budget(self.run, self.principal, child)
        with self.assertRaises(ContractBreach):
            self.settle(attempt, 4)
        self.assertEqual(self.owner(child)['budget']['cost']['confirmed'], 4)
        self.assertEqual(self.owner(self.root)['budget']['cost']['free'], 7)
        self.assert_accounted(conserved=False)

    def test_foreign_principal_and_foreign_run_allocation_rejected(self):
        child = self.split()
        bad = Principal('tenant', 'other')
        for action in (lambda: self.store.allocation_tree(self.run, bad),
                       lambda: self.store.split_budget(self.run, bad, 'x', {}),
                       lambda: self.store.join_budget(self.run, bad, child)):
            with self.assertRaises(AuthorizationDenied):
                action()
        other = self.store.create_run(self.principal, 'other', {'cost': 10})
        with self.assertRaises(AuthorizationDenied):
            self.store.split_budget(other, self.principal, 'x', {}, parent=child)
        self.assert_accounted()

    def test_join_settlement_race_returns_refund_exactly_once(self):
        child = self.split()
        _, attempt = self.start(child)
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(self.store.join_budget, self.run, self.principal, child),
                       pool.submit(self.settle, attempt)]
            for future in futures:
                future.result()
        self.settle(attempt)
        self.assertEqual(self.owner(self.root)['budget']['cost']['free'], 9)
        self.assert_accounted()

    def test_join_admission_race_either_reserves_or_rejects(self):
        child = self.split()
        with ThreadPoolExecutor(2) as pool:
            joining = pool.submit(self.store.join_budget, self.run, self.principal, child)
            admission = pool.submit(self.admit, child)
            joining.result()
            try:
                operation = admission.result()
            except StateConflict:
                operation = None
        self.assertEqual(self.owner(child)['budget']['cost']['reserved'], 3 if operation else 0)
        self.assert_accounted()

    def crash(self, action, point, allocation=None):
        config = {'database': str(self.path), 'run': self.run, 'action': action, 'allocation': allocation}
        path = Path(self.temp.name)/'crash.json'
        path.write_text(json.dumps(config), encoding='utf-8')
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('allocation_worker.py')), str(path), point],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 77, result.stdout+result.stderr)
        self.store = Store(self.path, clock=lambda: 10)
        self.assert_accounted()

    def test_crash_before_split_commit_rolls_back_transfer(self):
        self.crash('split', 'before_split_commit')
        self.assertEqual(len(self.store.allocation_tree(self.run, self.principal)), 1)
        self.split('worker', {'cost': 6})
        self.assert_accounted()

    def test_crash_after_split_commit_recovers_same_transfer(self):
        self.crash('split', 'after_split_commit')
        events = self.store.events(self.run, self.principal)
        self.split('worker', {'cost': 6})
        self.assertEqual(events, self.store.events(self.run, self.principal))
        self.assert_accounted()

    def test_crash_before_join_commit_keeps_child_ownership(self):
        child = self.split()
        self.crash('join', 'before_join_commit', child)
        self.assertEqual(self.owner(child)['state'], 'Open')
        self.store.join_budget(self.run, self.principal, child)
        self.assert_accounted()

    def test_crash_after_join_commit_does_not_return_credit_twice(self):
        child = self.split()
        self.crash('join', 'after_join_commit', child)
        events = self.store.events(self.run, self.principal)
        self.store.join_budget(self.run, self.principal, child)
        self.assertEqual(events, self.store.events(self.run, self.principal))
        self.assert_accounted()

    def test_v3_migration_preserves_unknown_operation_and_reservation(self):
        operation, attempt = self.start(None)
        before = self.store.budget(self.run, self.principal)
        db = sqlite3.connect(self.path)
        db.executescript('DROP TABLE attempt_allocations; DROP TABLE operation_allocations; DROP TABLE allocation_balances; DROP TABLE allocations; PRAGMA user_version=3;')
        db.close()
        self.store = Store(self.path, clock=lambda: 10)
        self.root = self.store.root_allocation(self.run, self.principal)
        self.assertEqual(before, self.store.budget(self.run, self.principal))
        self.assertEqual(operation, self.admit())
        self.assertEqual(self.store.snapshot(operation, self.principal)['business'], 'Unknown')
        self.settle(attempt)
        self.assert_accounted()


if __name__ == '__main__':
    unittest.main()
