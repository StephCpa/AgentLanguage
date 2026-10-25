"""Durable affine custody for the trusted Store task interface.

ResourceAccess carries presented references, not authority by itself. Every new
mediated action checks them in the action's database transaction. Historical
spawn/return receipts never reinstall custody. The checked IR adapter is separate.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json

from .data import canonical, identifier
from .errors import AuthorizationDenied, ReplayMismatch, StateConflict


CUSTODY_SCHEMA = """
CREATE TABLE IF NOT EXISTS permit_origins(
 permit TEXT PRIMARY KEY REFERENCES permits(id), task TEXT REFERENCES tasks(id));
CREATE TABLE IF NOT EXISTS resource_custody(
 kind TEXT NOT NULL CHECK(kind IN ('Permit','Operation','Budget')), resource TEXT NOT NULL,
 run TEXT NOT NULL REFERENCES runs(id), owner_kind TEXT NOT NULL CHECK(owner_kind IN ('Root','Task','Escrow','Runtime')), owner TEXT NOT NULL,
 generation INTEGER NOT NULL CHECK(generation>=0), state TEXT NOT NULL CHECK(state IN ('Live','Spent','Retired')),
 origin_kind TEXT NOT NULL CHECK(origin_kind IN ('Root','Task')), origin TEXT NOT NULL, PRIMARY KEY(kind,resource));
CREATE TABLE IF NOT EXISTS task_resource_inputs(
 task TEXT PRIMARY KEY REFERENCES tasks(id), descriptor TEXT NOT NULL, bindings TEXT NOT NULL,
 sender_kind TEXT NOT NULL, sender TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_resource_returns(
 task TEXT PRIMARY KEY REFERENCES tasks(id), descriptor TEXT NOT NULL, bindings TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('Published','Accepted','Retired')), received TEXT);
"""


@dataclass(frozen=True)
class ResourceRef:
    kind: str
    resource: str
    generation: int

    def __post_init__(self):
        if self.kind not in ('Permit', 'Operation', 'Budget'):
            raise StateConflict('unsupported affine resource kind')
        identifier(self.resource)
        if type(self.generation) is not int or self.generation < 0:
            raise StateConflict('resource generation must be a nonnegative integer')

    def record(self):
        return {'kind': self.kind, 'resource': self.resource, 'generation': self.generation}


def manifest(refs):
    if refs is None:
        refs = {}
    if type(refs) is not dict or any(not isinstance(r, ResourceRef) for r in refs.values()):
        raise StateConflict('resource bundle requires named ResourceRef values')
    for key in refs:
        identifier(key)
    if len({(r.kind, r.resource) for r in refs.values()}) != len(refs):
        raise StateConflict('affine resource cannot appear twice in a bundle')
    return {key: ref.record() for key, ref in refs.items()}


class CustodyMixin:
    def _init_custody_context(self):
        self._resource_access = ContextVar('al0_resource_access', default=None)

    @contextmanager
    def resource_access(self, run, principal, refs=(), *, task_lease=None):
        """Present references for this thread/context; all checks remain in SQL transactions."""
        refs = tuple(refs)
        if any(not isinstance(ref, ResourceRef) for ref in refs):
            raise StateConflict('resource access requires ResourceRef values')
        keyed = {(ref.kind, ref.resource): ref for ref in refs}
        if len(keyed) != len(refs):
            raise StateConflict('duplicate resource access reference')
        token = self._resource_access.set((run, principal, keyed, task_lease))
        try:
            yield keyed
        finally:
            self._resource_access.reset(token)

    def _custody_actor(self, db, run, principal, lease):
        self._run(db, run, principal)
        if lease is None:
            return 'Root', run
        task = self._task(db, lease.task, principal)
        if task['run'] != run:
            raise AuthorizationDenied('resource actor belongs to another run')
        self._lease(task, lease)
        return 'Task', task['id']

    def _check_access_actor(self, db, run, lease):
        access = self._resource_access.get()
        if access is not None:
            if access[0] != run or access[3] != lease:
                raise StateConflict('resource access context does not match the operation actor')
            self._custody_actor(db,run,access[1],lease)

    @staticmethod
    def _custody(db, kind, resource):
        return db.execute('SELECT * FROM resource_custody WHERE kind=? AND resource=?', (kind, resource)).fetchone()

    def _custody_guard(self, db, kind, resource, lease=None):
        row = self._custody(db, kind, resource)
        if row is None:
            return False
        access = self._resource_access.get()
        if access is None or access[0] != row['run'] or access[3] != lease:
            raise StateConflict('managed resource requires its current access context')
        actor = self._custody_actor(db, access[0], access[1], lease)
        ref = access[2].get((kind, resource))
        if (ref is None or ref.generation != row['generation'] or row['state'] != 'Live'
                or actor != (row['owner_kind'], row['owner'])):
            raise StateConflict('resource custody or generation is stale')
        return True

    def _custody_register(self, db, run, kind, resource, actor, principal):
        if actor[0] == 'Task' and kind in ('Permit','Operation'):
            caps = self._task_capabilities(db,actor[1])
            if caps is None:
                raise StateConflict('managed action authority requires a fixed task capability table')
            # Identity and boundary checks happen before any custody is minted.
            self._check_capability_resources(db,run,principal,caps,[(kind,resource)])
        db.execute('INSERT INTO resource_custody VALUES(?,?,?,?,?,0,?,?,?)',
                   (kind, resource, run, *actor, 'Live', *actor))
        self._event(db, run, 'ResourceManaged', resource_kind=kind, resource=resource, owner=list(actor))

    def manage_resource(self, run, principal, kind, resource, *, task_lease=None):
        """Enroll an existing locally owned resource; return its historical birth token.

        Pre-custody permits without a recorded origin require explicit migration,
        not an inference from the caller's current task or string identifier.
        """
        ResourceRef(kind, resource, 0)
        with self._tx() as db:
            actor = self._custody_actor(db, run, principal, task_lease)
            old = self._custody(db, kind, resource)
            if old is not None:
                if old['run'] != run or (old['origin_kind'], old['origin']) != actor:
                    raise StateConflict('only the recorded origin can read the enrollment receipt')
                return ResourceRef(kind, resource, 0)
            if kind == 'Permit':
                row = db.execute('''SELECT p.run,p.consumed_by,o.task FROM permits p
                    JOIN permit_origins o ON o.permit=p.id WHERE p.id=?''', (resource,)).fetchone()
                if row is None or row['run'] != run or row['consumed_by'] is not None:
                    raise StateConflict('permit has no usable recorded origin')
                origin = ('Root', run) if row['task'] is None else ('Task', row['task'])
                if origin != actor:
                    raise StateConflict('permit belongs to another actor')
            else:
                if kind == 'Operation':
                    op = self._operation(db, resource, principal)
                    if op['run'] != run:
                        raise AuthorizationDenied('operation belongs to another run')
                    allocation = self._operation_allocation(db, resource)
                else:
                    budget = self._allocation(db, run, resource, open_only=True)
                    if budget['parent'] is None:
                        raise StateConflict('split the root budget before exporting a budget interface')
                    if db.execute('SELECT 1 FROM tasks WHERE allocation=?', (resource,)).fetchone():
                        raise StateConflict('task ambient budget cannot be exported as a resource')
                    allocation = resource
                self._legacy_task_funding(db, allocation, task_lease)
            self._custody_register(db, run, kind, resource, actor, principal)
            self.fault('before_resource_manage_commit')
        self.fault('after_resource_manage_commit')
        return ResourceRef(kind, resource, 0)

    def resource_birth(self, kind, resource, principal):
        """Read generation zero, never refresh an old alias to current custody."""
        with self._tx() as db:
            row = self._custody(db, kind, resource)
            if row is None:
                raise StateConflict('resource is not managed')
            self._run(db, row['run'], principal)
            return ResourceRef(kind, resource, 0)

    def _present_resource_birth(self, kind, resource, principal):
        """Restore a trusted call's creation receipt; never refresh a moved alias."""
        access=self._resource_access.get()
        if access is not None:
            with self._tx() as db:
                row=self._custody(db,kind,resource)
                if row is not None:
                    self._run(db,row['run'],principal)
                    access[2].setdefault((kind,resource),ResourceRef(kind,resource,0))

    def _custody_created(self, db, run, kind, resource, lease):
        access = self._resource_access.get()
        if access is not None:
            if access[0] != run or access[3] != lease:
                raise StateConflict('resource creation context does not match its actor')
            actor = self._custody_actor(db, run, access[1], lease)
            self._custody_register(db, run, kind, resource, actor, access[1])
            # A creation receipt is available to the same mediated call (e.g.
            # authorize then admit). It never reads or refreshes current custody.
            # If this transaction rolls back the reference names no live object.
            access[2][kind,resource] = ResourceRef(kind,resource,0)

    def _custody_consume(self, db, kind, resource):
        db.execute("UPDATE resource_custody SET state='Spent',generation=generation+1 WHERE kind=? AND resource=?",
                   (kind, resource))

    def _custody_move(self, db, ref, destination, *, state='Live'):
        updated = db.execute('''UPDATE resource_custody SET owner_kind=?,owner=?,generation=generation+1,state=?
            WHERE kind=? AND resource=? AND generation=? AND state='Live' ''',
            (*destination, state, ref.kind, ref.resource, ref.generation)).rowcount
        if updated != 1:
            raise StateConflict('resource changed during transfer')
        return ResourceRef(ref.kind, ref.resource, ref.generation+1)

    def _scope_sender(self, db, parent):
        return ('Task', parent['id']) if parent['kind'] == 'Task' else (
            ('Root', parent['run']) if parent['parent'] is None else ('Task', parent['parent']))

    def _spawn_resources(self, db, parent, task, principal, moves, lease):
        descriptor = manifest(moves)
        if self._resource_access.get() is not None:
            self._check_access_actor(db,parent['run'],lease)
            if self._custody_actor(db,parent['run'],principal,lease) != self._scope_sender(db,parent):
                raise StateConflict('resource context is outside the receiving task scope')
        if not descriptor:
            return
        actor = self._custody_actor(db, parent['run'], principal, lease)
        if actor != self._scope_sender(db, parent):
            raise StateConflict('resource sender is outside the receiving task scope')
        for ref in moves.values():
            if not self._custody_guard(db, ref.kind, ref.resource, lease):
                raise StateConflict('only managed resources can move')
            if self._custody(db, ref.kind, ref.resource)['generation'] != ref.generation:
                raise StateConflict('bundle contains a stale resource reference')
        protected = [(ref.kind,ref.resource) for ref in moves.values() if ref.kind in ('Permit','Operation')]
        if protected:
            caps = self._task_capabilities(db,task)
            if caps is None:
                raise StateConflict('moving action authority requires a fixed child capability table')
            self._check_capability_resources(db,parent['run'],principal,caps,protected)
        bindings = {name: self._custody_move(db, ref, ('Task', task)).record() for name, ref in moves.items()}
        db.execute('INSERT INTO task_resource_inputs VALUES(?,?,?,?,?)',
                   (task, canonical(descriptor), canonical(bindings), *actor))
        self._event(db, parent['run'], 'TaskResourcesMoved', task=task, resources=bindings)
        self.fault('after_task_resources_move')

    def _replay_resource_inputs(self, db, task, moves):
        old = db.execute('SELECT descriptor FROM task_resource_inputs WHERE task=?', (task,)).fetchone()
        if (json.loads(old['descriptor']) if old else {}) != manifest(moves):
            raise ReplayMismatch('spawn resource manifest changed')

    def task_resource_inputs(self, task, principal):
        with self._tx() as db:
            self._task(db, task, principal)
            row = db.execute('SELECT bindings FROM task_resource_inputs WHERE task=?', (task,)).fetchone()
            return {} if row is None else {k: ResourceRef(**v) for k, v in json.loads(row['bindings']).items()}

    def _task_execution_inputs(self, db, task):
        from .flow import value_record
        saved=db.execute('SELECT bindings FROM task_inputs WHERE task=?',(task,)).fetchone()
        bindings={} if saved is None else json.loads(saved['bindings'])
        moved=db.execute('SELECT bindings FROM task_resource_inputs WHERE task=?',(task,)).fetchone()
        if moved is not None:
            from .bundles import record_refs, replace_record_refs
            received=json.loads(moved['bindings'])
            refs={(v['kind'],v['resource']):ResourceRef(**v) for v in received.values()}
            used=set()
            for name,record in list(bindings.items()):
                if 'bundle' in record or 'variant' in record:
                    used.update((ref.kind,ref.resource) for ref in record_refs(record).values())
                    bindings[name]=replace_record_refs(record,refs)
            control=self._task_flow(db,task)[0]
            for name,record in received.items():
                if (record['kind'],record['resource']) in used:
                    continue
                if name in bindings:
                    raise StateConflict('resource input overlaps a data input')
                bindings[name]=value_record(record['resource'],label=control)
                bindings[name]['resource']=record
        return bindings

    def task_execution_inputs(self, task, principal):
        """Reconstruct fixed spawn inputs, with original moved generations."""
        with self._tx() as db:
            self._task(db,task,principal)
            return self._task_execution_inputs(db,task)

    def _publish_resources(self, db, task, principal, resources, lease, *, replay=False):
        descriptor = manifest(resources)
        old = db.execute('SELECT descriptor FROM task_resource_returns WHERE task=?', (task['id'],)).fetchone()
        if replay:
            if (json.loads(old['descriptor']) if old else {}) != descriptor:
                raise ReplayMismatch('task resource return changed')
            return
        if not descriptor:
            return
        for ref in resources.values():
            if (not self._custody_guard(db, ref.kind, ref.resource, lease)
                    or self._custody(db, ref.kind, ref.resource)['generation'] != ref.generation):
                raise StateConflict('return requires current managed resources')
        bindings = {name: self._custody_move(db, ref, ('Escrow', task['id'])).record()
                    for name, ref in resources.items()}
        db.execute("INSERT INTO task_resource_returns VALUES(?,?,?,'Published',NULL)",
                   (task['id'], canonical(descriptor), canonical(bindings)))
        self._event(db, task['run'], 'TaskResourcesPublished', task=task['id'], resources=bindings)
        self.fault('after_task_resources_publish')

    def _receive_resources(self, db, task, principal, lease):
        row = db.execute('SELECT * FROM task_resource_returns WHERE task=?', (task['id'],)).fetchone()
        if row is None:
            return {}
        if row['state'] == 'Accepted':
            return {k:ResourceRef(**v) for k,v in json.loads(row['received']).items()}
        if row['state'] != 'Published':
            raise StateConflict('task resource return has been retired')
        parent = self._task(db, task['parent'], principal)
        actor = self._custody_actor(db, task['run'], principal, lease)
        if actor != self._scope_sender(db, parent):
            raise StateConflict('resource receiver is outside the task scope')
        access = self._resource_access.get()
        if access is None or access[0:2] != (task['run'], principal) or access[3] != lease:
            raise StateConflict('receiving resources requires an explicit access context')
        if parent['kind'] == 'Scope' and (parent['state'] != 'Open' or parent['disposition'] != 'Attached'):
            raise StateConflict('return destination scope is closed')
        received = {}
        if actor[0] == 'Task':
            caps = self._task_capabilities(db,actor[1])
            if caps is None:
                raise StateConflict('resource receiver requires a fixed capability table')
            self._check_capability_resources(db,task['run'],principal,caps,
                [(v['kind'],v['resource']) for v in json.loads(row['bindings']).values()])
        for name, value in json.loads(row['bindings']).items():
            ref = ResourceRef(**value)
            held = self._custody(db, ref.kind, ref.resource)
            if held is None or (held['owner_kind'], held['owner']) != ('Escrow', task['id']):
                raise StateConflict('return escrow no longer holds the resource')
            received[name] = self._custody_move(db, ref, actor).record()
        db.execute("UPDATE task_resource_returns SET state='Accepted',received=? WHERE task=?",
                   (canonical(received), task['id']))
        self._event(db, task['run'], 'TaskResourcesReceived', task=task['id'], resources=received)
        self.fault('after_task_resources_receive')
        return {k:ResourceRef(**v) for k,v in received.items()}

    def task_resource_result(self, task, principal):
        with self._tx() as db:
            self._task(db, task, principal)
            row = db.execute('SELECT * FROM task_resource_returns WHERE task=?', (task,)).fetchone()
            if row is None:
                return {}
            if row['state'] != 'Accepted':
                raise StateConflict('resource result has not been received')
            return {k: ResourceRef(**v) for k, v in json.loads(row['received']).items()}

    def _retire_task_resources(self, db, task):
        rows = db.execute("SELECT * FROM resource_custody WHERE owner=? AND owner_kind IN ('Task','Escrow') AND state='Live'",
                          (task['id'],)).fetchall()
        for row in rows:
            ref = ResourceRef(row['kind'], row['resource'], row['generation'])
            self._custody_move(db, ref, ('Runtime', task['id']), state='Retired')
            if row['kind'] == 'Operation':
                op = db.execute('SELECT * FROM operations WHERE id=?', (row['resource'],)).fetchone()
                self._cancel_operation(db, op)
        # Close explicit budgets leaf-first after moving all local custody.
        budgets = {r['resource'] for r in rows if r['kind'] == 'Budget'}
        for allocation in self._allocation_subtree(db, self._allocation(db, task['run'])['id']):
            if allocation['id'] in budgets:
                self._join_budget(db, task['run'], allocation['id'], allow_exported=True)
        db.execute("UPDATE task_resource_returns SET state='Retired' WHERE task=? AND state='Published'", (task['id'],))
        if rows:
            self._event(db, task['run'], 'TaskResourceResponsibilityTaken', task=task['id'], resources=[r['resource'] for r in rows])
