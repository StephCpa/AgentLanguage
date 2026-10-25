"""Durable ownership of free credit within one run's root budget.

Root totals live in Store.budgets. Allocation balances partition its free
column; splitting never creates a second root budget. Each attempt preserves
its funding identity, so late refunds survive joining its original allocation.
"""
import json
import uuid

from .data import canonical, identifier, resource_vector
from .errors import AuthorizationDenied, BudgetExceeded, ReplayMismatch, StateConflict


ALLOCATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS allocations(
 id TEXT PRIMARY KEY, run TEXT NOT NULL REFERENCES runs(id),
 parent TEXT REFERENCES allocations(id), split_key TEXT,
 amount TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'Open' CHECK(state IN ('Open','Joined')),
 UNIQUE(run,split_key));
CREATE UNIQUE INDEX IF NOT EXISTS allocation_roots ON allocations(run) WHERE parent IS NULL;
CREATE TABLE IF NOT EXISTS allocation_balances(
 allocation TEXT NOT NULL REFERENCES allocations(id), resource TEXT NOT NULL,
 free INTEGER NOT NULL CHECK(free>=0), PRIMARY KEY(allocation,resource));
CREATE TABLE IF NOT EXISTS operation_allocations(
 operation TEXT PRIMARY KEY REFERENCES operations(id),
 allocation TEXT NOT NULL REFERENCES allocations(id));
CREATE TABLE IF NOT EXISTS attempt_allocations(
 attempt TEXT PRIMARY KEY REFERENCES attempts(id),
 allocation TEXT NOT NULL REFERENCES allocations(id));
CREATE TRIGGER IF NOT EXISTS attempt_allocation_immutable
 BEFORE UPDATE ON attempt_allocations BEGIN
 SELECT RAISE(ABORT, 'attempt funding is immutable');
 END;
"""


class AllocationMixin:
    def _create_root_allocation(self, db, run, budget):
        root = str(uuid.uuid4())
        db.execute('INSERT INTO allocations(id,run,amount) VALUES(?,?,?)', (root, run, canonical(budget)))
        db.executemany('INSERT INTO allocation_balances VALUES(?,?,?)', [(root, k, n) for k, n in budget.items()])
        return root

    def _migrate_allocations(self, db):
        # Called in the schema migration transaction. Existing aggregate free
        # credit initially belongs to the root; past spend is not made free.
        for run in db.execute('SELECT id FROM runs').fetchall():
            existing = db.execute('SELECT id FROM allocations WHERE run=? AND parent IS NULL', (run['id'],)).fetchone()
            if existing:
                root = existing['id']
            else:
                rows = db.execute('SELECT * FROM budgets WHERE run=?', (run['id'],)).fetchall()
                root = self._create_root_allocation(db, run['id'], {r['resource']: r['free'] for r in rows})
                db.execute('UPDATE allocations SET amount=? WHERE id=?',
                           (canonical({r['resource']: r['total'] for r in rows}), root))
            db.execute('INSERT OR IGNORE INTO operation_allocations SELECT id,? FROM operations WHERE run=?', (root, run['id']))

    def _allocation(self, db, run, allocation=None, *, open_only=False):
        if allocation is None:
            row = db.execute('SELECT * FROM allocations WHERE run=? AND parent IS NULL', (run,)).fetchone()
        else:
            row = db.execute('SELECT * FROM allocations WHERE id=? AND run=?', (allocation, run)).fetchone()
        if row is None:
            raise AuthorizationDenied('allocation is unavailable in this run')
        if open_only and row['state'] != 'Open':
            raise StateConflict('joined allocation cannot fund new work')
        return row

    def root_allocation(self, run, principal):
        with self._tx() as db:
            self._run(db, run, principal)
            return self._allocation(db, run)['id']

    def _operation_allocation(self, db, operation):
        return db.execute('SELECT allocation FROM operation_allocations WHERE operation=?', (operation,)).fetchone()['allocation']

    def _migrate_attempt_allocations(self, db):
        # Before v16, every attempt was funded from its operation's immutable
        # allocation. Import that fact once; never infer it on a receipt path.
        db.execute('''INSERT OR IGNORE INTO attempt_allocations
            SELECT a.id,o.allocation FROM attempts a
            JOIN operation_allocations o ON o.operation=a.operation''')
        if db.execute('''SELECT 1 FROM attempts a LEFT JOIN attempt_allocations f ON f.attempt=a.id
                         WHERE f.attempt IS NULL LIMIT 1''').fetchone():
            raise StateConflict('attempt funding migration is incomplete')

    def _attempt_allocation(self, db, attempt):
        row = db.execute('''SELECT f.allocation FROM attempt_allocations f
            JOIN attempts a ON a.id=f.attempt JOIN operations o ON o.id=a.operation
            JOIN allocations l ON l.id=f.allocation AND l.run=o.run WHERE f.attempt=?''', (attempt,)).fetchone()
        if row is None:
            raise StateConflict('attempt has no durable funding record')
        return row['allocation']

    def attempt_funding(self, attempt, principal):
        with self._tx() as db:
            row = db.execute('SELECT * FROM attempts WHERE id=?', (attempt,)).fetchone()
            if row is None:
                raise AuthorizationDenied('attempt is unavailable')
            operation = self._operation(db, row['operation'], principal)
            allocation = self._attempt_allocation(db, attempt)
            self._allocation(db, operation['run'], allocation)
            return {'operation': row['operation'], 'allocation': allocation,
                    'upper_bound': json.loads(row['upper_bound'])}

    def _credit_allocation(self, db, run, allocation, vector):
        # Joined ancestors never reopen. Late settlement refunds travel to the
        # nearest still-open owner in the same immutable parent chain.
        owner = self._allocation(db, run, allocation)
        while owner['state'] == 'Joined':
            owner = self._allocation(db, run, owner['parent'])
        for resource, amount in vector.items():
            db.execute('UPDATE allocation_balances SET free=free+? WHERE allocation=? AND resource=?',
                       (amount, owner['id'], resource))
        return owner['id']

    def split_budget(self, run, principal, key, amount, *, parent=None, task_lease=None):
        """Transfer free credit once under a durable caller-supplied split key."""
        identifier(key)
        amount = resource_vector(amount)
        with self._tx() as db:
            self._run(db, run, principal)
            source = self._allocation(db, run, parent)
            existed = db.execute('SELECT 1 FROM allocations WHERE run=? AND split_key=?', (run, key)).fetchone()
            if not existed:
                self._task_funding(db,source['id'],task_lease)
            child = self._split_budget(db, run, principal, key, amount, source['id'])
            if not existed:
                self._custody_created(db, run, 'Budget', child, task_lease)
            self.fault('before_split_commit')
        self.fault('after_split_commit')
        return child

    def _split_budget(self, db, run, principal, key, amount, parent):
        source = self._allocation(db, run, parent)
        old = db.execute('SELECT * FROM allocations WHERE run=? AND split_key=?', (run, key)).fetchone()
        if old:
            if old['parent'] != source['id'] or old['amount'] != canonical(amount):
                raise ReplayMismatch('split key already binds another allocation request')
            return old['id']
        self._run(db, run, principal, mutable=True)
        self._allocation(db, run, source['id'], open_only=True)
        balances = {r['resource']: r['free'] for r in db.execute('SELECT * FROM allocation_balances WHERE allocation=?', (source['id'],))}
        for resource, quantity in amount.items():
            if resource not in balances or balances[resource] < quantity:
                raise BudgetExceeded('parent allocation lacks the requested free credit')
        child = str(uuid.uuid4())
        db.execute('INSERT INTO allocations(id,run,parent,split_key,amount) VALUES(?,?,?,?,?)',
                   (child, run, source['id'], key, canonical(amount)))
        db.executemany('INSERT INTO allocation_balances VALUES(?,?,?)',
                       [(child, resource, amount.get(resource, 0)) for resource in balances])
        for resource, quantity in amount.items():
            db.execute('UPDATE allocation_balances SET free=free-? WHERE allocation=? AND resource=?',
                       (quantity, source['id'], resource))
        self._event(db, run, 'BudgetSplit', parent=source['id'], allocation=child, key=key, amount=amount)
        return child

    def join_budget(self, run, principal, allocation, *, task_lease=None):
        """Close a child allocation and return only its currently free credit.

        Queued/exposed attempts remain attached to their original allocation.
        This is budget ownership transfer, not task cancellation or a remote
        completion proof. A caller managing tasks must separately join/handoff.
        """
        with self._tx() as db:
            self._run(db, run, principal)
            if db.execute('SELECT 1 FROM tasks WHERE allocation=?',(allocation,)).fetchone():
                raise StateConflict('task root allocation must be released through its task lifecycle')
            if self._allocation(db,run,allocation)['state']=='Joined':
                return
            self._task_funding(db,allocation,task_lease)
            self._join_budget(db, run, allocation)
            self._custody_consume(db, 'Budget', allocation)
            self.fault('before_join_commit')
        self.fault('after_join_commit')

    def _join_budget(self, db, run, allocation, *, allow_exported=False):
        child = self._allocation(db, run, allocation)
        if child['parent'] is None:
            raise StateConflict('the root allocation cannot be joined')
        if child['state'] == 'Joined':
            return
        active = db.execute("SELECT id FROM allocations WHERE parent=? AND state='Open'", (allocation,)).fetchall()
        # Lifecycle cleanup may close an account while independently owned
        # descendants remain open; late refunds still traverse immutable links.
        if active and not allow_exported:
            raise StateConflict('join open child allocations before joining their parent')
        free = {r['resource']: r['free'] for r in db.execute('SELECT * FROM allocation_balances WHERE allocation=?', (allocation,))}
        destination = self._credit_allocation(db, run, child['parent'], free)
        db.execute('UPDATE allocation_balances SET free=0 WHERE allocation=?', (allocation,))
        db.execute("UPDATE allocations SET state='Joined' WHERE id=?", (allocation,))
        self._event(db, run, 'BudgetJoined', allocation=allocation, destination=destination, returned=free)

    def _allocation_subtree(self,db,allocation):
        return db.execute('''WITH RECURSIVE descendants(id,depth) AS (
            SELECT id,0 FROM allocations WHERE id=? UNION ALL
            SELECT a.id,d.depth+1 FROM allocations a JOIN descendants d ON a.parent=d.id)
            SELECT a.* FROM allocations a JOIN descendants d ON a.id=d.id ORDER BY d.depth DESC,a.rowid''',
            (allocation,)).fetchall()

    def _join_allocation_subtree(self,db,run,allocation):
        for child in self._allocation_subtree(db,allocation):
            held = self._custody(db, 'Budget', child['id'])
            if held is None or held['state'] != 'Live':
                self._join_budget(db,run,child['id'],allow_exported=True)

    def allocation_tree(self, run, principal):
        """Return disjoint local accounting, never recursive double counting.

        initial_transfer is provenance, not a currently spendable total. Summing
        each node's free/reserved/confirmed/retired matches the root ledger.
        """
        with self._tx() as db:
            self._run(db, run, principal)
            if db.execute('''SELECT 1 FROM attempts a JOIN operations o ON o.id=a.operation
                LEFT JOIN attempt_allocations f ON f.attempt=a.id
                LEFT JOIN allocations l ON l.id=f.allocation
                WHERE o.run=? AND (l.id IS NULL OR l.run!=o.run) LIMIT 1''', (run,)).fetchone():
                raise StateConflict('allocation accounting requires complete attempt funding records')
            result = []
            for owner in db.execute('SELECT * FROM allocations WHERE run=? ORDER BY rowid', (run,)).fetchall():
                budget = {r['resource']: dict(free=r['free'], reserved=0, confirmed=0, retired=0)
                          for r in db.execute('SELECT * FROM allocation_balances WHERE allocation=?', (owner['id'],))}
                attempts = db.execute('SELECT a.* FROM attempts a JOIN attempt_allocations f ON a.id=f.attempt WHERE f.allocation=?', (owner['id'],)).fetchall()
                for attempt in attempts:
                    key = {'Reserved': 'reserved', 'Settled': 'confirmed', 'Retired': 'retired'}[attempt['billing']]
                    values = json.loads(attempt['actual'] if key == 'confirmed' else attempt['upper_bound'])
                    for resource, quantity in values.items():
                        budget[resource][key] += quantity
                result.append({'id': owner['id'], 'parent': owner['parent'], 'key': owner['split_key'],
                               'state': owner['state'], 'initial_transfer': json.loads(owner['amount']), 'budget': budget})
            return result
