import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from al0.data import Intent, Principal, Slot
from al0.errors import AuthorizationDenied, StateConflict
from al0.store import Store


class AttemptFundingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'runtime.sqlite'
        self.store = Store(self.path, clock=lambda: 10)
        self.principal = Principal('tenant', 'subject')
        self.run = self.store.create_run(self.principal, 'funding', {'cost': 10, 'calls': 5})
        self.root = self.store.root_allocation(self.run, self.principal)
        self.a = self.store.split_budget(self.run, self.principal, 'a', {'cost': 6, 'calls': 2})
        self.b = self.store.split_budget(self.run, self.principal, 'b', {'cost': 3, 'calls': 2})
        self.store.register_contract('work', '1', {'cost': 3, 'calls': 1}, retry_window=100)
        grant = self.store.grant(self.run, 'work', 'scope', expires=1000, max_attempts=2)
        self.intent = Intent('work', '1', 'scope', {'x': 1})
        self.permit = self.store.authorize(self.run, self.principal, self.intent, grant, authorization_key='permit')

    def admit(self):
        return self.store.admit(self.run, self.principal, Slot('work'), self.intent, self.permit, allocation=self.a)

    def attempt(self, operation):
        return self.store.snapshot(operation, self.principal)['attempts'][-1]['id']

    def start(self):
        op = self.admit()
        attempt = self.attempt(op)
        self.store.start_attempt(op, attempt, self.principal)
        return op, attempt

    def settle(self, attempt):
        self.store.settle(attempt, self.principal, provider='work', evidence_id='bill:'+attempt,
                          actual={'cost': 1, 'calls': 1})

    def tree(self):
        return {r['id']: r['budget'] for r in self.store.allocation_tree(self.run, self.principal)}

    def legacy(self):
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TABLE attempt_allocations')
            db.execute('PRAGMA user_version=15')

    def crash(self, action, point, **extra):
        config = {'database': str(self.path), 'run': self.run, 'permit': self.permit,
                  'allocation': self.a, 'action': action, 'point': point, **extra}
        path = Path(self.temp.name)/'config.json'
        path.write_text(json.dumps(config), encoding='utf-8')
        result = subprocess.run([sys.executable, '-m', 'tests.attempt_funding_worker', str(path)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 77, result.stdout+result.stderr)

    def test_admission_and_retry_persist_funding_once(self):
        op, first = self.start()
        second = self.store.retry(op, self.principal)
        self.assertEqual(self.admit(), op)
        for attempt in (first, second):
            self.assertEqual(self.store.attempt_funding(attempt, self.principal),
                {'operation': op, 'allocation': self.a, 'upper_bound': {'cost': 3, 'calls': 1}})
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM attempt_allocations').fetchone()[0], 2)
        with self.assertRaises(AuthorizationDenied):
            self.store.attempt_funding(first, Principal('another', 'subject'))

    def test_funding_record_cannot_be_retargeted(self):
        _, attempt = self.start()
        with sqlite3.connect(self.path) as db:
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable'):
                db.execute('UPDATE attempt_allocations SET allocation=? WHERE attempt=?', (self.b, attempt))
        self.assertEqual(self.store.attempt_funding(attempt, self.principal)['allocation'], self.a)

    def test_settlement_uses_attempt_origin_even_if_old_operation_index_changes(self):
        op, attempt = self.start()
        # Deliberately perturb the old index to distinguish the two algorithms.
        # This is a storage-boundary test, not a supported custody transfer API.
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE operation_allocations SET allocation=? WHERE operation=?', (self.b, op))
        self.settle(attempt)
        first = self.tree()
        self.assertEqual(first[self.a]['cost'], dict(free=5, reserved=0, confirmed=1, retired=0))
        self.assertEqual(first[self.b]['cost'], dict(free=3, reserved=0, confirmed=0, retired=0))
        self.settle(attempt)
        self.assertEqual(self.tree(), first)

    def test_queued_release_uses_attempt_origin(self):
        op = self.admit()
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE operation_allocations SET allocation=? WHERE operation=?', (self.b, op))
        self.store.cancel(op, self.principal)
        self.assertEqual(self.tree()[self.a]['cost']['free'], 6)
        self.assertEqual(self.tree()[self.b]['cost']['free'], 3)

    def test_late_refund_after_join_follows_original_parent_chain(self):
        _, attempt = self.start()
        self.store.join_budget(self.run, self.principal, self.a)
        before = self.tree()[self.root]['cost']['free']
        self.settle(attempt)
        tree = self.tree()
        self.assertEqual(tree[self.root]['cost']['free'], before+2)
        self.assertEqual(tree[self.a]['cost']['free'], 0)
        self.assertEqual(tree[self.a]['cost']['confirmed'], 1)
        self.assertEqual(self.store.attempt_funding(attempt, self.principal)['allocation'], self.a)

    def test_missing_funding_fails_and_rolls_back_settlement(self):
        _, attempt = self.start()
        before = self.store.budget(self.run, self.principal)
        events = self.store.events(self.run, self.principal)
        with sqlite3.connect(self.path) as db:
            db.execute('DELETE FROM attempt_allocations WHERE attempt=?', (attempt,))
        with self.assertRaises(StateConflict):
            self.settle(attempt)
        with self.assertRaises(StateConflict):
            self.tree()
        self.assertEqual(self.store.budget(self.run, self.principal), before)
        self.assertEqual(self.store.events(self.run, self.principal), events)

    def test_v15_migration_preserves_mixed_attempt_states_and_account_balances(self):
        op, first = self.start()
        second = self.store.retry(op, self.principal)
        before = self.store.budget(self.run, self.principal)
        tree = self.tree()
        self.legacy()
        migrated = Store(self.path, clock=lambda: 10)
        self.assertEqual(migrated.budget(self.run, self.principal), before)
        self.assertEqual(migrated.allocation_tree(self.run, self.principal), self.store.allocation_tree(self.run, self.principal))
        self.assertEqual(self.tree(), tree)
        for attempt in (first, second):
            self.assertEqual(migrated.attempt_funding(attempt, self.principal)['allocation'], self.a)
        migrated.cancel(op, self.principal)
        self.settle(first)
        self.assertEqual(self.tree()[self.a]['cost'], dict(free=5, reserved=0, confirmed=1, retired=0))

    def test_process_exit_during_admission_rolls_back_funding_and_reservation(self):
        before = self.store.budget(self.run, self.principal)
        self.crash('admit', 'after_attempt_funding_insert')
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM attempt_allocations').fetchone()[0], 0)
            self.assertIsNone(db.execute('SELECT consumed_by FROM permits WHERE id=?', (self.permit,)).fetchone()[0])
        self.assertEqual(self.store.budget(self.run, self.principal), before)
        self.assertEqual(self.store.attempt_funding(self.attempt(self.admit()), self.principal)['allocation'], self.a)

    def test_process_exit_after_admission_commit_keeps_exactly_one_funding_record(self):
        self.crash('admit', 'after_admit_commit')
        op = self.admit()
        self.assertEqual(self.store.attempt_funding(self.attempt(op), self.principal)['allocation'], self.a)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM attempt_allocations').fetchone()[0], 1)

    def test_process_exit_during_retry_rolls_back_new_funding_only(self):
        op, first = self.start()
        before = self.store.budget(self.run, self.principal)
        self.crash('retry', 'after_attempt_funding_insert', operation=op)
        self.assertEqual(self.store.budget(self.run, self.principal), before)
        self.assertEqual(len(self.store.snapshot(op, self.principal)['attempts']), 1)
        self.assertEqual(self.store.attempt_funding(first, self.principal)['allocation'], self.a)
        second = self.store.retry(op, self.principal)
        self.assertEqual(self.store.attempt_funding(second, self.principal)['allocation'], self.a)

    def test_process_exit_before_migration_commit_rolls_back_schema_and_backfill(self):
        _, attempt = self.start()
        self.legacy()
        self.crash('migrate', 'before_attempt_funding_migration_commit')
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 15)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='attempt_allocations'").fetchone())
        recovered = Store(self.path, clock=lambda: 10)
        self.assertEqual(recovered.attempt_funding(attempt, self.principal)['allocation'], self.a)

    def test_process_exit_after_migration_commit_preserves_mapping_on_reopen(self):
        _, attempt = self.start()
        self.legacy()
        self.crash('migrate', 'after_attempt_funding_migration_commit')
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 21)
        recovered = Store(self.path, clock=lambda: 10)
        self.assertEqual(recovered.attempt_funding(attempt, self.principal)['allocation'], self.a)
        self.settle(attempt)
        self.assertEqual(self.tree()[self.a]['cost']['free'], 5)
