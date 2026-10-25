"""Cross-module regressions from the review of fixed commit 08a32b0."""
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from al0.concurrency import TaskScope
from al0.data import Intent, Principal, Slot, digest
from al0.errors import ContractError, FlowDenied, StateConflict
from al0.execute import Broker, Interpreter, Response
from al0.flow import Label, PUBLIC, plan_label
from al0.ir import Catalog, Tool
from al0.sdk import PlanBuilder
from al0.store import Store
from scripts.check_installed_typing import audit


PRIVATE = Label({'owner'}, {'private-status'})


class AuditRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'runtime.sqlite'
        self.now = 10
        self.store = Store(self.path, clock=lambda: self.now)
        self.who = Principal('audit', 'owner')

    def operation(self):
        self.store.register_contract('work', '1', {'u': 1}, retry_window=100)
        self.run = self.store.create_run(self.who, 'host', {'u': 4})
        intent = Intent('work', '1', 'work', {})
        grant = self.store.grant(self.run, 'work', 'work', expires=1000, max_attempts=3)
        permit = self.store.authorize(self.run, self.who, intent, grant)
        op = self.store.admit(self.run, self.who, Slot('work'), intent, permit)
        return op, self.store.snapshot(op, self.who)['attempts'][0]['id']

    def test_f1_operation_parameter_contributes_private_output_bound(self):
        catalog = Catalog()
        catalog.register(Tool.define('work', 'action', 'Json', 'Text', 'work', {'u': 1}, output_label=PRIVATE))
        b = PlanBuilder('child', catalog)
        op = b.operation_parameter('operation', b.tool('work'))
        status = b.await_result('status', op)
        value = b.match_business('answer', status, 'Text', applied='Applied',
            proven_no_effect='NoEffect', pending='Pending', unknown='Unknown', not_dispatched='Queued')
        self.assertEqual(plan_label(b.finish(value), catalog), PRIVATE)

    def test_f2_retry_cannot_dispatch_at_expiry_after_restart(self):
        op, first = self.operation()
        self.store.start_attempt(op, first, self.who)
        self.now = 109
        second = self.store.retry(op, self.who)
        self.now = 110
        self.store = Store(self.path, clock=lambda: self.now)
        with self.assertRaises(ContractError):
            self.store.start_attempt(op, second, self.who)
        self.assertEqual(self.store.snapshot(op, self.who)['attempts'][1]['phase'], 'Queued')
        self.assertEqual(self.store.budget(self.run, self.who)['u']['reserved'], 2)

    def stale_scope(self):
        broker = Broker(self.store, Catalog(), {})
        run = self.store.create_run(self.who, 'parent', {'u': 4})
        outer = self.store.open_scope(run, self.who, 'outer', {'u': 4})
        parent = self.store.spawn_task(outer, self.who, 'parent', 'parent', {'u': 4})
        old = self.store.claim_task(parent, self.who)
        scope = TaskScope(broker, run, self.who, 'inner', {'u': 2}, parent_lease=old)
        scope.__enter__()
        self.addCleanup(lambda: scope.pool.shutdown(wait=True) if scope.pool is not None else None)
        child = self.store.spawn_task(scope.scope, self.who, 'child', 'child', {'u': 1}, parent_lease=old)
        self.store.claim_task(child, self.who)
        self.new_lease = self.store.claim_task(parent, self.who, recover=True)
        return scope, child

    def test_f3_stale_parent_handoff_cannot_cancel_child(self):
        scope, child = self.stale_scope()
        with self.assertRaises(StateConflict):
            scope.handoff(child)
        self.assertEqual(self.store.task_snapshot(child, self.who)['state'], 'Running')

    def test_f3_stale_parent_exception_cleanup_cannot_cancel_child(self):
        scope, child = self.stale_scope()
        with self.assertRaises(StateConflict):
            scope.__exit__(RuntimeError, RuntimeError('old worker'), None)
        self.assertEqual(self.store.task_snapshot(child, self.who)['state'], 'Running')

    def test_f3_stale_parent_cannot_reopen_or_respawn_existing_child(self):
        scope, child = self.stale_scope()
        before = self.store.events(scope.run, self.who)
        with self.assertRaises(StateConflict):
            self.store.open_scope(scope.run, self.who, 'inner', {'u': 2}, parent_lease=scope.parent_lease)
        with self.assertRaises(StateConflict):
            self.store.spawn_task(scope.scope, self.who, 'child', 'child', {'u': 1}, parent_lease=scope.parent_lease)
        self.assertEqual(self.store.events(scope.run, self.who), before)
        self.assertEqual(self.store.task_snapshot(child, self.who)['state'], 'Running')

    def test_f3_current_parent_and_explicit_supervisor_can_cleanup(self):
        scope, child = self.stale_scope()
        with self.assertRaises(StateConflict):
            self.store.handoff_task(child, self.who)
        self.store.handoff_task(child, self.who, parent_lease=self.new_lease)
        self.assertEqual(self.store.task_snapshot(child, self.who)['state'], 'Cancelled')
        self.store.close_scope(scope.scope, self.who, parent_lease=self.new_lease)
        self.store.supervise_task(self.new_lease.task, self.who)

    def test_f3_supervision_cannot_be_used_inside_stale_worker_context(self):
        scope, child = self.stale_scope()
        with self.store.resource_access(scope.run, self.who, task_lease=scope.parent_lease):
            with self.assertRaises(StateConflict):
                self.store.supervise_task(child, self.who)
            with self.assertRaises(StateConflict):
                self.store.supervise_scope(scope.scope, self.who)
        self.store.supervise_task(child, self.who)
        self.assertEqual(self.store.task_snapshot(child, self.who)['state'], 'Cancelled')

    def test_f4_root_name_cannot_alias_nested_observation(self):
        catalog = Catalog()
        catalog.schema('Pair', {'first': 'Int', 'second': 'Int'})
        catalog.register(Tool.define('counter', 'observe', 'Text', 'Int', 'read', {'u': 1}, output_label=PUBLIC))
        child = PlanBuilder('child', catalog)
        question = child.literal('question', 'Text', 'same')
        child = child.finish(child.observe('inside', child.tool('counter'), question))
        parent = PlanBuilder('parent', catalog)
        question = parent.literal('question', 'Text', 'same')
        root_name = 'branch:' + digest([['wrapped', 'invoke'], 'inside'])
        first = parent.observe(root_name, parent.tool('counter'), question)
        second = parent.invoke('wrapped', child)
        plan = parent.finish(parent.record('answer', 'Pair', {'first': first, 'second': second}))
        calls = []
        def observe(ticket):
            calls.append(ticket.attempt)
            return Response(len(calls), {'u': 1})
        broker = Broker(self.store, catalog, {'counter': observe})
        run = self.store.create_run(self.who, plan.fingerprint, {'u': 4})
        grants = {'counter': self.store.grant(run, 'counter', 'read', expires=1000)}
        result = Interpreter(broker).run(plan, run, self.who, grants)
        self.assertEqual(result.data, {'first': 1, 'second': 2})
        self.assertEqual(Interpreter(broker).run(plan, run, self.who, grants), result)
        self.assertEqual(len(calls), 2)

    def test_f5_finish_allows_existing_queue(self):
        op, attempt = self.operation()
        self.store.finish_run(self.run, self.who)
        self.store.start_attempt(op, attempt, self.who)
        self.assertEqual(self.store.snapshot(op, self.who)['business'], 'Unknown')

    def test_f6_bundle_cap_can_be_attenuated_twice(self):
        catalog = Catalog()
        catalog.register(Tool.define('work', 'action', 'Json', 'Text', 'work', {'u': 1}))
        b = PlanBuilder('bundle-cap', catalog)
        cap = b.capability('cap', b.tool('work'))
        funds = b.split_budget('funds', {'u': 1})
        parts = b.unpack('parts', b.pack('packet', {'cap': cap, 'funds': funds}))
        cap = b.take('taken', parts, 'cap')
        b.join_budget('returned', b.take('taken-funds', parts, 'funds'))
        cap = b.attenuate('first', cap, max_attempts=3)
        plan = b.finish(b.attenuate('second', cap, max_attempts=2))
        self.assertEqual(plan.result_type.kind, 'Cap')

    def test_f7_wheel_gate_does_not_resolve_interpreter_symlink(self):
        wheel = Path(self.temp.name) / 'package.whl'
        with zipfile.ZipFile(wheel, 'w') as archive:
            archive.writestr('al0/py.typed', '')
        requested = Path(self.temp.name) / 'venv' / 'bin' / 'python'
        base = Path(self.temp.name) / 'base-python'
        original_resolve = Path.resolve
        def resolve(path, *args, **kwargs):
            return base if path == requested else original_resolve(path, *args, **kwargs)
        invoked = []
        class StopProbe(Exception):
            pass
        def launch(arguments, **kwargs):
            invoked.append(arguments)
            raise StopProbe()
        with patch.object(Path, 'resolve', resolve), patch('scripts.check_installed_typing.subprocess.run', launch):
            with self.assertRaises(StopProbe):
                audit(requested, wheel)
        self.assertEqual(Path(invoked[0][0]), requested.absolute())
