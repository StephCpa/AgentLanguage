"""Boundary compositions: task egress, expiring endpoints and recovery identity."""
from contextlib import closing
from pathlib import Path
from threading import Event
import json
import sqlite3
import tempfile
import unittest

from al0.concurrency import TaskRunner
from al0.data import Intent, Principal, Slot, digest
from al0.errors import AuthorizationDenied, ContractError, FlowDenied, ReplayMismatch, StateConflict
from al0.execute import Broker, Interpreter, Response
from al0.flow import Label, PUBLIC, plan_label
from al0.ir import Catalog, Tool, Type, check_plan
from al0.sdk import PlanBuilder
from al0.semantics import execution_site
from al0.store import DispatchBlocked, Store


PRIVATE = Label({'owner'}, {'private-status'})


class AuditCombinations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.who = Principal('audit', 'owner')

    def flow_case(self, mode, recipient):
        c = Catalog()
        c.register(Tool.define('work', 'action', 'Json', 'Text', 'work', {'u': 1},
                               shared_write=False, output_label=PRIVATE))
        c.register(Tool.define('gate', 'observe', 'Text', 'Text', 'gate', {'u': 1}, output_label=PUBLIC))
        c.register(Tool.define('model', 'infer', 'Text', 'Text', 'model', {'u': 1},
                               recipients=['owner'], output_label=PUBLIC))
        c.register(Tool.define('publish', 'observe', 'Json' if mode == 'handoff' else 'Text',
                               'Text', 'publish', {'u': 1}, recipients=[recipient], output_label=PUBLIC))
        child = PlanBuilder('private-child', c)
        op = child.operation_parameter('operation', child.tool('work'))
        status = child.await_result('status', op)
        value = child.match_business('value', status, 'Text', applied='classified-applied',
            proven_no_effect='classified-none', pending='classified-pending',
            unknown='classified-unknown', not_dispatched='classified-queued')
        if mode == 'Waiting':
            value = child.infer('unresolved', child.tool('model'), value)
        child = child.finish(value)
        parent = PlanBuilder('parent', c)
        request = parent.literal('request', 'Json', {})
        intent = parent.prepare('intent', parent.tool('work'), request)
        permit = parent.authorize('permit', intent)
        op = parent.admit('operation', intent, permit)
        scope = parent.scope('scope', {'u': 2}, speculative=mode == 'race')
        task = parent.spawn('child', scope, child, {'u': 1}, inputs={'operation': op})
        if mode == 'race':
            public = PlanBuilder('public-child', c)
            public = public.finish(public.literal('constant', 'Text', 'public-winner'))
            other = parent.spawn('public', scope, public, {})
        parent.observe('synchronize', parent.tool('gate'), parent.literal('wait', 'Text', 'wait'))
        if mode == 'handoff':
            result = parent.handoff('result', task)
        elif mode == 'Cancelled':
            result = parent.cancel_outcome('result', task)
        elif mode == 'race':
            result = parent.race('result', [task, other])
        else:
            result = parent.join_outcome('result', task)
        if mode not in ('handoff', 'race'):
            branches = {}
            for kind in ('Completed', 'Failed', 'Waiting', 'Cancelled'):
                branch = PlanBuilder(kind, c)
                branches[kind.lower()] = branch.finish(branch.literal('value', 'Text', kind))
            result = parent.match_task('matched', result, **branches)
        parent.close_scope('closed', scope)
        plan = parent.finish(parent.observe('published', parent.tool('publish'), result))
        store = Store(Path(self.temp.name) / (mode + '-' + recipient + '.sqlite'), clock=lambda: 10)
        ready, release = Event(), Event()
        published = []
        def work(ticket):
            if mode in ('Failed', 'race'):
                raise RuntimeError('synthetic private failure')
            if mode == 'Cancelled':
                ready.set()
                if not release.wait(10):
                    raise RuntimeError('test cleanup did not release endpoint')
            return Response('private-response', {'u': 1})
        def gate(ticket):
            if not ready.wait(10):
                raise RuntimeError('child did not reach its durable outcome')
            return Response('ready', {'u': 1})
        def publish(ticket):
            published.append(ticket.intent.value)
            return Response('published', {'u': 1})
        original_wait = store.wait_task
        def wait_task(*args, **kwargs):
            original_wait(*args, **kwargs)
            ready.set()
        store.wait_task = wait_task
        store.fault = lambda point: ready.set() if point == 'after_task_finish_commit' else None
        broker = Broker(store, c, {'work': work, 'gate': gate, 'publish': publish,
                                  'model': lambda ticket: Response(None, None, 'Pending')})
        run = store.create_run(self.who, plan.fingerprint, {'u': 20})
        grants = {n: store.grant(run, n, spec.scope, expires=1000) for n, spec in c.tools.items()}
        try:
            if recipient == 'public':
                with self.assertRaises(FlowDenied):
                    Interpreter(broker).run(plan, run, self.who, grants)
                self.assertEqual(published, [])
            else:
                self.assertEqual(Interpreter(broker).run(plan, run, self.who, grants).data, 'published')
                self.assertEqual(len(published), 1)
                self.assertEqual(Label.from_data(published[0]['flow']), PRIVATE)
                payload = published[0]['args']
                if mode == 'handoff':
                    self.assertEqual(set(payload), {'task', 'state', 'disposition'})
                elif mode == 'race':
                    self.assertEqual(payload, 'public-winner')
                else:
                    self.assertEqual(payload, mode)
                    with closing(sqlite3.connect(store.path)) as db:
                        outcome = json.loads(db.execute('SELECT outcome FROM task_outcomes').fetchone()[0])
                    self.assertEqual(Label.from_data(outcome['label']), PRIVATE)
        finally:
            release.set()
            broker.wait_for_workers(timeout=15)

    def test_f1_task_boundaries_reject_public_and_allow_owner(self):
        for mode in ('Completed', 'Failed', 'Waiting', 'Cancelled', 'handoff', 'race'):
            for recipient in ('public', 'owner'):
                with self.subTest(mode=mode, recipient=recipient):
                    self.flow_case(mode, recipient)

    def test_f1_nested_resource_interfaces_and_opref_sources(self):
        c = Catalog()
        c.register(Tool.define('work', 'action', 'Json', 'Text', 'work', {'u': 1}, output_label=PRIVATE))
        bundle = Type('Bundle', members=(('nested', Type('Bundle', members=(('operation', Type('Operation', 'work')),))),))
        variant = Type('Variant', members=(('Owned', bundle), ('Empty', Type('Data', 'Text'))))
        for typ in (bundle, variant):
            b = PlanBuilder('resource-identity', c)
            value = b.bundle_parameter('input', typ) if typ.kind == 'Bundle' else b.variant_parameter('input', typ)
            self.assertEqual(plan_label(b.finish(value), c), PRIVATE)
        b = PlanBuilder('borrow-and-return', c)
        op = b.operation_parameter('operation', b.tool('work'))
        snapshot = b.inspect('snapshot', b.borrow_operation('reference', op))
        b.match_business('handled', snapshot, 'Text', applied='yes', proven_no_effect='no',
                         pending='pending', unknown='unknown', not_dispatched='queued')
        self.assertEqual(plan_label(b.finish(op), c), PRIVATE)

    def test_f2_expiring_endpoint_and_both_dispatch_apis(self):
        for checked in (False, True):
            for when in (109, 110, 111):
                for restart in (False, True):
                    with self.subTest(checked=checked, when=when, restart=restart):
                        name = f'{checked}-{when}-{restart}'
                        now = [10]
                        path = Path(self.temp.name) / (name + '.sqlite')
                        endpoint = Path(self.temp.name) / (name + '-endpoint.sqlite')
                        store = Store(path, clock=lambda: now[0])
                        store.register_contract('work', '1', {'u': 1}, retry_window=100)
                        run = store.create_run(self.who, 'host', {'u': 4})
                        grant = store.grant(run, 'work', 'work', expires=1000, max_attempts=3)
                        intent = Intent('work', '1', 'work', {})
                        permit = store.authorize(run, self.who, intent, grant)
                        op = store.admit(run, self.who, Slot('work'), intent, permit)
                        first = store.snapshot(op, self.who)['attempts'][0]['id']
                        store.start_attempt(op, first, self.who)
                        with closing(sqlite3.connect(endpoint)) as db, db:
                            db.execute('CREATE TABLE keys(operation TEXT PRIMARY KEY,expires INTEGER)')
                            db.execute('CREATE TABLE effects(operation TEXT)')
                            db.execute('INSERT INTO keys VALUES(?,110)', (op,))
                            db.execute('INSERT INTO effects VALUES(?)', (op,))
                        now[0] = 109
                        second = store.retry(op, self.who)
                        now[0] = when
                        if restart:
                            store = Store(path, clock=lambda: now[0])
                        with closing(sqlite3.connect(endpoint)) as db, db:
                            db.execute('DELETE FROM keys WHERE expires<=?', (now[0],))
                            self.assertEqual(db.execute('SELECT COUNT(*) FROM keys').fetchone()[0], int(when < 110))
                        dispatch = store.try_start_attempt if checked else store.start_attempt
                        if when >= 110:
                            with self.assertRaises(ContractError):
                                dispatch(op, second, self.who)
                            self.assertEqual(store.snapshot(op, self.who)['attempts'][1]['phase'], 'Queued')
                            store.cancel(op, self.who)
                            self.assertEqual(store.budget(run, self.who)['u']['reserved'], 1)
                        else:
                            dispatch(op, second, self.who)
                            with closing(sqlite3.connect(endpoint)) as db, db:
                                if db.execute('SELECT 1 FROM keys WHERE operation=?', (op,)).fetchone() is None:
                                    db.execute('INSERT INTO effects VALUES(?)', (op,))
                        with closing(sqlite3.connect(endpoint)) as db:
                            self.assertEqual(db.execute('SELECT COUNT(*) FROM effects').fetchone()[0], 1)
                        store.settle(first, self.who, provider='work', evidence_id='late-first', actual={'u': 1})
                        self.assertEqual(store.budget(run, self.who)['u']['confirmed'], 1)

    def test_f4_root_names_cannot_enter_the_site_namespace(self):
        nested = execution_site('inside', ('wrapped', 'invoke'))
        names = ('inside', 'branch:' + digest([['wrapped', 'invoke'], 'inside']), nested,
                 'site:["site",[],"inside"]', 'wrapped:invoke:inside')
        sites = [execution_site(name) for name in names]
        self.assertEqual(len(set(sites)), len(names))
        self.assertNotIn(nested, sites)

    def test_f5_finish_existing_dispatch_new_work_cancel_and_late_fee(self):
        for checked in (False, True):
            for outcome in ('Completed', 'Failed'):
                for fence in ('none', 'cancel', 'revoke'):
                    with self.subTest(checked=checked, outcome=outcome, fence=fence):
                        store = Store(Path(self.temp.name) / f'finish-{checked}-{outcome}-{fence}.sqlite', clock=lambda: 10)
                        store.register_contract('work', '1', {'u': 1}, retry_window=100)
                        run = store.create_run(self.who, 'host', {'u': 4})
                        grant = store.grant(run, 'work', 'work', expires=1000, max_attempts=3)
                        intent = Intent('work', '1', 'work', {})
                        permit = store.authorize(run, self.who, intent, grant)
                        unused = store.authorize(run, self.who, intent, grant)
                        op = store.admit(run, self.who, Slot('existing'), intent, permit)
                        attempt = store.snapshot(op, self.who)['attempts'][0]['id']
                        store.finish_run(run, self.who, outcome=outcome)
                        with self.assertRaises(StateConflict):
                            store.admit(run, self.who, Slot('new'), intent, unused)
                        dispatch = store.try_start_attempt if checked else store.start_attempt
                        if fence == 'cancel':
                            store.cancel(op, self.who)
                            with self.assertRaises(StateConflict):
                                dispatch(op, attempt, self.who)
                            self.assertEqual(store.budget(run, self.who)['u']['reserved'], 0)
                        elif fence == 'revoke':
                            store.revoke(grant)
                            if checked:
                                self.assertIsInstance(dispatch(op, attempt, self.who), DispatchBlocked)
                            else:
                                with self.assertRaises(AuthorizationDenied):
                                    dispatch(op, attempt, self.who)
                            self.assertEqual(store.snapshot(op, self.who)['attempts'][0]['phase'], 'Queued')
                        else:
                            dispatch(op, attempt, self.who)
                            with self.assertRaises(StateConflict):
                                store.retry(op, self.who)
                            store.cancel(op, self.who)
                            self.assertEqual(store.budget(run, self.who)['u']['reserved'], 1)
                            store.settle(attempt, self.who, provider='work', evidence_id='late', actual={'u': 1})
                            self.assertEqual(store.budget(run, self.who)['u']['confirmed'], 1)

    def test_f6_cap_sources_share_ir_and_runtime_attenuation(self):
        c = Catalog()
        c.register(Tool.define('work', 'action', 'Json', 'Text', 'work', {'u': 1}))
        identity = PlanBuilder('identity', c)
        identity = identity.finish(identity.cap_parameter('result', identity.tool('work')))
        for source in ('Cap', 'parameter', 'Bundle', 'Variant', 'branch', 'join'):
            with self.subTest(source=source):
                b = PlanBuilder('cap-' + source, c)
                cap = b.capability('base', b.tool('work'))
                if source == 'parameter':
                    cap = b.invoke_caps('returned', identity, caps={'work': cap}, inputs={'result': cap})
                elif source == 'Bundle':
                    funds = b.split_budget('funds', {'u': 1})
                    parts = b.unpack('parts', b.pack('bundle', {'permission': cap, 'funds': funds}))
                    cap = b.take('permission', parts, 'permission')
                    b.join_budget('returned-funds', b.take('taken-funds', parts, 'funds'))
                elif source == 'Variant':
                    interface = Type('Variant', members=(('First', Type('Cap', 'work')), ('Second', Type('Cap', 'work'))))
                    choice = b.variant('choice', interface, 'First', cap)
                    cap = b.match_variant('selected', choice, {'First': identity, 'Second': identity})
                elif source == 'branch':
                    yes = b.literal('yes', 'Bool', True)
                    cap = b.if_else('selected', yes, then=identity, otherwise=identity, inputs={'result': cap})
                elif source == 'join':
                    scope = b.scope('scope', {})
                    task = b.spawn_caps('task', scope, identity, {}, caps={'work': cap}, inputs={'result': cap})
                    cap = b.join('joined', task)
                    b.close_scope('closed', scope)
                document = json.loads(b.finish(cap).encoded)
                document['nodes'].extend([
                    {'id': 'first', 'op': 'attenuate', 'cap': document['return'], 'expires': None,
                     'max_attempts': 3, 'constraints': {'channel': 'reports'}},
                    {'id': 'second', 'op': 'attenuate', 'cap': 'first', 'expires': None,
                     'max_attempts': 2, 'constraints': {}},
                ])
                document['return'] = 'second'
                ir_plan = check_plan(document, c)
                cap = b.attenuate('first', cap, max_attempts=3, constraints={'channel': 'reports'})
                plan = b.finish(b.attenuate('second', cap, max_attempts=2))
                self.assertEqual(plan.encoded, ir_plan.encoded)
                store = Store(Path(self.temp.name) / ('cap-' + source + '.sqlite'), clock=lambda: 10)
                broker = Broker(store, c, {'work': lambda ticket: self.fail('cap construction must not dispatch')})
                run = store.create_run(self.who, plan.fingerprint, {'u': 4})
                grants = {'work': store.grant(run, 'work', 'work', expires=1000, max_attempts=4)}
                try:
                    result = Interpreter(broker).run(plan, run, self.who, grants)
                    self.assertEqual(store.capability_info(run, self.who, result.data)['max_attempts'], 2)
                    self.assertEqual(Interpreter(broker).run(ir_plan, run, self.who, grants), result)
                    store.authorize(run, self.who, Intent('work', '1', 'work', {'channel': 'reports'}), result.data)
                    with self.assertRaises(AuthorizationDenied):
                        store.authorize(run, self.who, Intent('work', '1', 'work', {'channel': 'other'}), result.data)
                finally:
                    broker.wait_for_workers(timeout=15)

    def test_old_semantics_rejected_before_root_or_completed_task_replay(self):
        c = Catalog()
        b = PlanBuilder('old-program', c)
        plan = b.finish(b.literal('value', 'Text', 'old'))
        # Exact pre-audit Catalog encoding: flow version 1, no execution field.
        legacy_catalog = digest({'flow_semantics': 1, 'schemas': {}, 'tools': {}})
        legacy_plan = digest({'ir': json.loads(plan.encoded), 'catalog': legacy_catalog})
        store = Store(Path(self.temp.name) / 'old.sqlite')
        run = store.create_run(self.who, legacy_plan, {'u': 1})
        scope = store.open_scope(run, self.who, 'scope', {'u': 1})
        task = store.spawn_task(scope, self.who, 'task', legacy_plan, {})
        lease = store.claim_task(task, self.who)
        store.finish_task(lease, self.who, {'data': 'old', 'receipts': []})
        broker = Broker(store, c, {})
        before = store.events(run, self.who)
        with self.assertRaises(ReplayMismatch):
            Interpreter(broker).run(plan, run, self.who, {})
        with self.assertRaises(ReplayMismatch):
            TaskRunner(broker).run(task, self.who, plan, {})
        self.assertEqual(store.events(run, self.who), before)
        store.supervise_scope(scope, self.who)
