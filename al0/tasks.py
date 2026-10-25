"""Persistent task control, fenced workers, and structured responsibility exit.

Task leases fence new mediated work, not already accepted remote effects.
Principal authentication and worker supervision remain trusted host duties.
"""
from dataclasses import dataclass
import json
import uuid

from .data import canonical, identifier, resource_vector
from .bindings import normalize_bindings, validate_record
from .bundles import accept_record
from .custody import ResourceRef
from .errors import AuthorizationDenied, ReplayMismatch, StateConflict
from .flow import Label, PUBLIC, value_record


TASK_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
 id TEXT PRIMARY KEY, run TEXT NOT NULL REFERENCES runs(id),
 parent TEXT REFERENCES tasks(id), task_key TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('Scope','Task')),
 program TEXT NOT NULL, allocation TEXT NOT NULL UNIQUE REFERENCES allocations(id),
 amount TEXT NOT NULL, state TEXT NOT NULL,
 epoch INTEGER NOT NULL DEFAULT 0, disposition TEXT NOT NULL DEFAULT 'Attached'
 CHECK(disposition IN ('Attached','Joined','Runtime')),
 result TEXT, waiting_on TEXT REFERENCES operations(id),
 UNIQUE(parent,task_key));
CREATE UNIQUE INDEX IF NOT EXISTS root_scope_keys ON tasks(run,task_key) WHERE parent IS NULL;
CREATE TABLE IF NOT EXISTS task_policies(
 task TEXT PRIMARY KEY REFERENCES tasks(id), speculative INTEGER NOT NULL CHECK(speculative IN (0,1)));
CREATE TABLE IF NOT EXISTS tool_effects(
 tool TEXT NOT NULL, version TEXT NOT NULL, shared_write INTEGER NOT NULL CHECK(shared_write IN (0,1)),
 PRIMARY KEY(tool,version), FOREIGN KEY(tool,version) REFERENCES contracts(tool,version));
CREATE TABLE IF NOT EXISTS task_races(
 scope TEXT NOT NULL REFERENCES tasks(id), race_key TEXT NOT NULL,
 candidates TEXT NOT NULL, winner TEXT NOT NULL REFERENCES tasks(id), PRIMARY KEY(scope,race_key));
CREATE TABLE IF NOT EXISTS task_inputs(
 task TEXT PRIMARY KEY REFERENCES tasks(id), bindings TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_capabilities(
 task TEXT PRIMARY KEY REFERENCES tasks(id), bindings TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_inputs(
 run TEXT PRIMARY KEY REFERENCES runs(id), bindings TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_outcomes(
 task TEXT PRIMARY KEY REFERENCES tasks(id), outcome TEXT NOT NULL);
"""


@dataclass(frozen=True)
class TaskLease:
    task: str
    epoch: int


class TaskMixin:
    def register_tool_effect(self, tool, version, shared_write):
        if type(shared_write) is not bool:
            raise StateConflict('shared-write classification must be boolean')
        with self._tx() as db:
            old = db.execute('SELECT shared_write FROM tool_effects WHERE tool=? AND version=?', (tool,version)).fetchone()
            if old and bool(old['shared_write']) != shared_write:
                raise ReplayMismatch('tool effect classification is immutable within a version')
            db.execute('INSERT OR IGNORE INTO tool_effects VALUES(?,?,?)', (tool,version,int(shared_write)))

    def _speculative(self, db, task):
        row = db.execute('SELECT speculative FROM task_policies WHERE task=?', (task,)).fetchone()
        return bool(row and row['speculative'])

    def _task_effect(self, db, allocation, intent):
        owner = self._allocation_task(db,allocation)
        access = self._resource_access.get()
        if access is not None:
            owner = None if access[3] is None else self._task(db, access[3].task, access[1])
        if owner and self._speculative(db,owner['id']):
            value = intent.value
            effect = db.execute('SELECT shared_write FROM tool_effects WHERE tool=? AND version=?', (value['tool'],value['version'])).fetchone()
            if effect is None or effect['shared_write']:
                raise StateConflict('speculative task cannot request an unclassified or shared-write tool')

    def _task(self, db, task, principal):
        row = db.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()
        if row is None:
            raise AuthorizationDenied('task is unavailable')
        self._run(db, row['run'], principal)
        return row

    def _unmanaged_allocation(self, db, allocation):
        if self._allocation_task(db,allocation) is not None:
            raise StateConflict('task-owned allocation must be managed through task lifecycle APIs')

    def _allocation_task(self,db,allocation):
        # The nearest task boundary owns every manually split descendant.
        return db.execute('''WITH RECURSIVE ancestors(id,parent,depth) AS (
            SELECT id,parent,0 FROM allocations WHERE id=? UNION ALL
            SELECT a.id,a.parent,p.depth+1 FROM allocations a JOIN ancestors p ON a.id=p.parent)
            SELECT t.* FROM tasks t JOIN ancestors a ON t.allocation=a.id ORDER BY a.depth LIMIT 1''',
            (allocation,)).fetchone()

    @staticmethod
    def _lease(row, lease):
        if (not isinstance(lease, TaskLease) or lease.task != row['id'] or lease.epoch != row['epoch']
                or row['state'] != 'Running' or row['disposition'] != 'Attached'):
            raise StateConflict('worker lease is stale or task no longer accepts new work')

    def _task_funding(self, db, allocation, lease):
        funding = db.execute('SELECT run FROM allocations WHERE id=?',(allocation,)).fetchone()
        if funding is None:
            raise StateConflict('funding allocation is unavailable')
        self._check_access_actor(db,funding['run'],lease)
        if self._custody_guard(db, 'Budget', allocation, lease):
            return
        self._legacy_task_funding(db, allocation, lease)

    def _legacy_task_funding(self, db, allocation, lease):
        row = self._allocation_task(db,allocation)
        if row is not None:
            self._lease(row, lease)
        elif lease is not None:
            raise StateConflict('task worker cannot use an allocation outside its task')

    def _task_operation(self, db, operation, lease):
        op = db.execute('SELECT run FROM operations WHERE id=?',(operation,)).fetchone()
        self._check_access_actor(db,op['run'],lease)
        if not self._custody_guard(db, 'Operation', operation, lease):
            self._legacy_task_funding(db, self._operation_allocation(db, operation), lease)

    def _task_new_work(self, db, run):
        if db.execute("SELECT 1 FROM run_status WHERE run=? AND control IN ('Completed','Failed')", (run,)).fetchone():
            raise StateConflict('finished run cannot start new task control')

    def open_scope(self, run, principal, key, amount, *, allocation=None, parent_lease=None, speculative=False, control=PUBLIC):
        identifier(key)
        amount = resource_vector(amount)
        if type(speculative) is not bool:
            raise StateConflict('scope speculative flag must be boolean')
        with self._tx() as db:
            owner = self._run(db, run, principal)
            self._check_access_actor(db,run,parent_lease)
            parent = None
            if parent_lease is not None:
                parent = self._task(db,parent_lease.task,principal)
                self._lease(parent,parent_lease)
                if parent['run'] != run:
                    raise StateConflict('nested scope must use its parent task funding')
                allocation = parent['allocation'] if allocation is None else allocation
                speculative = speculative or self._speculative(db,parent['id'])
                control=control.join(self._task_flow(db,parent['id'])[0])
            source = self._allocation(db, run, allocation)
            old = db.execute('SELECT * FROM tasks WHERE run=? AND parent IS ? AND task_key=?', (run, None if parent is None else parent['id'], key)).fetchone()
            if old:
                funding = self._allocation(db, run, old['allocation'])
                if (old['kind'] != 'Scope' or old['amount'] != canonical(amount) or funding['parent'] != source['id']
                        or self._speculative(db,old['id']) != speculative or self._task_flow(db,old['id'])[0]!=control):
                    raise ReplayMismatch('scope key already binds different funding')
                return old['id']
            self._task_new_work(db, run)
            self._task_funding(db,source['id'],parent_lease)
            task = str(uuid.uuid4())
            child = self._split_budget(db, run, principal, 'task:'+task, amount, source['id'])
            db.execute("INSERT INTO tasks(id,run,parent,task_key,kind,program,allocation,amount,state) VALUES(?,?,?,?,'Scope',?,?,?,'Open')",
                       (task, run, None if parent is None else parent['id'], key, owner['program'], child, canonical(amount)))
            db.execute('INSERT INTO task_policies VALUES(?,?)', (task,int(speculative)))
            db.execute('INSERT INTO task_flows VALUES(?,?,?)',(task,canonical(control.to_data()),canonical(control.to_data())))
            self._event(db, run, 'ScopeOpened', task=task, allocation=child)
            self.fault('before_scope_commit')
        self.fault('after_scope_commit')
        return task

    def spawn_task(self, parent, principal, key, program, amount, *, parent_lease=None, inputs=None, control=PUBLIC, upper_bound=PUBLIC, caps=None, moves=None):
        from .custody import manifest
        move_descriptor = manifest(moves)
        identifier(key)
        identifier(program)
        amount = resource_vector(amount)
        inputs = normalize_bindings(inputs)
        if set(inputs) & set(move_descriptor):
            raise StateConflict('resource and data input names must be distinct')
        from .bundles import record_refs
        described={(record['kind'],record['resource']):record for record in move_descriptor.values()}
        template_resources=set()
        for record in inputs.values():
            if 'bundle' in record or 'variant' in record:
                for ref in record_refs(record).values():
                    if (ref.kind,ref.resource) in template_resources:
                        raise StateConflict('bundle input templates repeat a resource')
                    template_resources.add((ref.kind,ref.resource))
                    if described.get((ref.kind,ref.resource))!=ref.record():
                        raise StateConflict('bundle input template differs from its resource manifest')
        with self._tx() as db:
            owner = self._task(db, parent, principal)
            # Returning an existing spawn also schedules a TaskRunner. Fence
            # the parent before that replay path can reclaim a child worker.
            self._scope_controller(db,owner,principal,parent_lease)
            if caps is not None:
                self._check_capability_map(db,owner['run'],caps)
            control=control.join(self._task_flow(db,parent)[0],*(Label.from_data(v.get('label')) for v in inputs.values()))
            upper_bound=upper_bound.join(control)
            old = db.execute('SELECT * FROM tasks WHERE parent=? AND task_key=?', (parent, key)).fetchone()
            if old:
                saved = db.execute('SELECT bindings FROM task_inputs WHERE task=?', (old['id'],)).fetchone()
                if (old['kind'] != 'Task' or old['program'] != program or old['amount'] != canonical(amount)
                        or (saved['bindings'] if saved else '{}') != canonical(inputs)
                        or self._task_flow(db,old['id'])!=(control,upper_bound)):
                    raise ReplayMismatch('spawn key already binds a different program, budget or input')
                if caps is not None and self._task_capabilities(db,old['id'])!=caps:
                    raise ReplayMismatch('spawn key already binds different task capabilities')
                self._replay_resource_inputs(db,old['id'],moves)
                return old['id']
            self._task_new_work(db, owner['run'])
            if owner['kind'] == 'Task':
                self._lease(owner, parent_lease)
            elif owner['state'] != 'Open' or owner['disposition'] != 'Attached':
                raise StateConflict('scope is closed to new children')
            task = str(uuid.uuid4())
            child = self._split_budget(db, owner['run'], principal, 'task:'+task, amount, owner['allocation'])
            db.execute("INSERT INTO tasks(id,run,parent,task_key,kind,program,allocation,amount,state) VALUES(?,?,?,?,'Task',?,?,?,'Ready')",
                       (task, owner['run'], parent, key, program, child, canonical(amount)))
            db.execute('INSERT INTO task_policies VALUES(?,?)', (task,int(self._speculative(db,parent))))
            db.execute('INSERT INTO task_inputs VALUES(?,?)', (task,canonical(inputs)))
            if caps is not None:
                self._save_task_capabilities(db,owner['run'],task,caps)
            db.execute('INSERT INTO task_flows VALUES(?,?,?)',(task,canonical(control.to_data()),canonical(upper_bound.to_data())))
            self._spawn_resources(db,owner,task,principal,moves,parent_lease)
            self._event(db, owner['run'], 'TaskSpawned', task=task, parent=parent, allocation=child, program=program)
            self.fault('before_spawn_commit')
        self.fault('after_spawn_commit')
        return task

    @staticmethod
    def _task_capabilities(db,task):
        row=db.execute('SELECT bindings FROM task_capabilities WHERE task=?',(task,)).fetchone()
        return None if row is None else json.loads(row['bindings'])

    def _save_task_capabilities(self,db,run,task,caps):
        db.execute('INSERT INTO task_capabilities VALUES(?,?)',(task,canonical(caps)))
        self._event(db,run,'TaskCapabilitiesBound',task=task)

    def bind_task_capabilities(self,task,principal,caps=None):
        """Recover the immutable map, or bind a never-started host-created task.

        A legacy Running/Waiting task lacks evidence of its original map;
        never infer that map from recovery-time host grants.
        """
        with self._tx() as db:
            owner=self._task(db,task,principal)
            old=self._task_capabilities(db,task)
            if old is not None:
                if caps is not None and old!=caps:
                    raise ReplayMismatch('task capability binding changed')
                return old
            if caps is None or owner['kind']!='Task' or owner['state']!='Ready' or owner['epoch']!=0 or owner['disposition']!='Attached':
                raise StateConflict('task has no durable capability map; only a never-started task can bind one')
            self._run(db,owner['run'],principal,mutable=True)
            self._task_new_work(db,owner['run'])
            self._check_capability_map(db,owner['run'],caps)
            self._save_task_capabilities(db,owner['run'],task,caps)
            self.fault('before_task_capabilities_commit')
        self.fault('after_task_capabilities_commit')
        return dict(caps)

    def claim_task(self, task, principal, *, recover=False):
        """A supervisor explicitly fences an old worker with recover=True.

        No timeout or lease expiry automatically authorizes resending an attempt.
        Recovery resumes durable node identities under a newer control epoch.
        """
        with self._tx() as db:
            row = self._task(db, task, principal)
            self._run(db, row['run'], principal, mutable=True)
            self._task_new_work(db, row['run'])
            if (row['kind'] != 'Task' or row['disposition'] != 'Attached'
                    or row['state'] not in ('Ready', 'Waiting', 'Running')
                    or (row['state'] == 'Running' and not recover)):
                raise StateConflict('task is not available for this worker claim')
            epoch = row['epoch'] + 1
            db.execute("UPDATE tasks SET state='Running',epoch=?,waiting_on=NULL WHERE id=?", (epoch, task))
            self._event(db, row['run'], 'TaskClaimed', task=task, epoch=epoch, recovery=recover)
            return TaskLease(task, epoch)

    def task_context(self, lease, principal, program):
        with self._tx() as db:
            row = self._task(db, lease.task, principal)
            self._lease(row, lease)
            if row['program'] != program:
                raise ReplayMismatch('task is pinned to a different checked program')
            return {'run': row['run'], 'allocation': row['allocation'], 'speculative': self._speculative(db,row['id']),
                    'control':self._task_flow(db,row['id'])[0],'caps':self._task_capabilities(db,row['id'])}

    def wait_task(self, lease, principal, operation):
        with self._tx() as db:
            row = self._task(db, lease.task, principal)
            self._lease(row, lease)
            op = self._operation(db, operation, principal)
            if op['run'] != row['run']:
                raise StateConflict('waiting operation does not belong to this task')
            self._task_operation(db,operation,lease)
            db.execute("UPDATE tasks SET state='Waiting',waiting_on=? WHERE id=?", (operation, row['id']))
            self._event(db, row['run'], 'TaskWaiting', task=row['id'], operation=operation)

    def finish_task(self, lease, principal, result, *, failed=False, resources=None):
        encoded = canonical(result)
        outcome = 'Failed' if failed else 'Completed'
        with self._tx() as db:
            row = self._task(db, lease.task, principal)
            if row['epoch'] == lease.epoch and row['state'] == outcome and row['result'] == encoded:
                self._publish_resources(db,row,principal,resources,lease,replay=True)
                return
            self._lease(row, lease)
            if db.execute("SELECT 1 FROM tasks WHERE parent=? AND disposition='Attached'", (row['id'],)).fetchone():
                raise StateConflict('task must join or hand off its children before returning')
            if failed and resources:
                raise StateConflict('failed resource returns require an explicit checked outcome protocol')
            self._publish_resources(db,row,principal,resources,lease)
            db.execute('UPDATE tasks SET state=?,result=? WHERE id=?', (outcome, encoded, row['id']))
            self._event(db, row['run'], 'TaskFinished', task=row['id'], outcome=outcome)
            self.fault('before_task_finish_commit')
        self.fault('after_task_finish_commit')

    def _subtree(self, db, task):
        return db.execute('''WITH RECURSIVE descendants(id,depth) AS (
            SELECT id,0 FROM tasks WHERE id=? UNION ALL
            SELECT t.id,d.depth+1 FROM tasks t JOIN descendants d ON t.parent=d.id)
            SELECT t.* FROM tasks t JOIN descendants d ON t.id=d.id ORDER BY d.depth DESC,t.rowid''', (task,)).fetchall()

    def _task_obligations(self, db, row):
        allocations = {r['id'] for r in self._allocation_subtree(db,row['allocation'])}
        operations = {r['operation'] for r in db.execute('SELECT operation,allocation FROM operation_allocations')
                      if r['allocation'] in allocations and self._custody(db,'Operation',r['operation']) is None}
        operations.update(r['resource'] for r in db.execute("SELECT resource FROM resource_custody WHERE kind='Operation' AND owner=? AND owner_kind IN ('Task','Escrow','Runtime')",(row['id'],)))
        return [o for o in self._obligations(db, row['run']) if o['operation'] in operations]

    def _release_task(self, db, row, *, cancel):
        if row['disposition'] != 'Attached':
            return
        if not cancel and row['state'] not in ('Completed', 'Failed', 'Cancelled'):
            raise StateConflict('task has not reached a local terminal state; hand it off explicitly')
        if db.execute("SELECT 1 FROM tasks WHERE parent=? AND disposition='Attached'", (row['id'],)).fetchone():
            raise StateConflict('task still owns child control handles')
        if not cancel and db.execute("SELECT 1 FROM task_resource_returns WHERE task=? AND state='Published'",(row['id'],)).fetchone():
            raise StateConflict('resource return must be received before releasing this task')
        # Close queued work and fence further admissions/dispatch in this same
        # transaction. Already-exposed effects and their bills remain live.
        allocations={child['id'] for child in self._allocation_subtree(db,row['allocation'])}
        operations=[op for op in db.execute('SELECT o.*,a.allocation FROM operations o JOIN operation_allocations a ON o.id=a.operation WHERE o.run=?',(row['run'],))
                    if op['allocation'] in allocations and self._custody(db,'Operation',op['id']) is None]
        for operation in operations:
            if not operation['closed']:
                self._cancel_operation(db, operation)
        self._retire_task_resources(db,row)
        state = 'Cancelled' if cancel and row['state'] not in ('Completed', 'Failed') else row['state']
        disposition = 'Runtime' if self._task_obligations(db, row) else 'Joined'
        self._join_allocation_subtree(db, row['run'], row['allocation'])
        db.execute('UPDATE tasks SET state=?,disposition=? WHERE id=?', (state, disposition, row['id']))
        self._event(db, row['run'], 'TaskReleased', task=row['id'], disposition=disposition, cancelled=cancel)

    def join_task(self, task, principal, *, parent_lease=None):
        with self._tx() as db:
            row = self._task(db, task, principal)
            if row['kind'] != 'Task':
                raise StateConflict('use close_scope to release a scope')
            self._outcome_receiver(db,row,principal,parent_lease)
            self._receive_resources(db,row,principal,parent_lease)
            self._release_task(db, row, cancel=False)
            self.fault('before_task_join_commit')
        self.fault('after_task_join_commit')
        return self.task_snapshot(task, principal)

    def handoff_task(self, task, principal, *, parent_lease=None):
        return self._handoff_task(task,principal,parent_lease=parent_lease,supervisor=False)

    def supervise_task(self, task, principal):
        """Explicit trusted-host cancellation, outside a worker access context."""
        return self._handoff_task(task,principal,parent_lease=None,supervisor=True)

    def _handoff_task(self, task, principal, *, parent_lease, supervisor):
        with self._tx() as db:
            row = self._task(db, task, principal)
            if row['kind'] != 'Task':
                raise StateConflict('use close_scope to cancel a scope')
            if supervisor:
                if self._resource_access.get() is not None:
                    raise StateConflict('supervisor cancellation requires a separate host context')
            else:
                self._outcome_receiver(db,row,principal,parent_lease)
            for child in self._subtree(db, task):
                self._release_task(db, child, cancel=True)
            self.fault('before_handoff_commit')
        self.fault('after_handoff_commit')
        return self.task_snapshot(task, principal)

    def close_scope(self, scope, principal, *, cancel=False, parent_lease=None):
        return self._close_scope(scope,principal,cancel=cancel,parent_lease=parent_lease,supervisor=False)

    def supervise_scope(self, scope, principal):
        """Explicit trusted-host subtree cancellation; never a worker fallback."""
        return self._close_scope(scope,principal,cancel=True,parent_lease=None,supervisor=True)

    def _close_scope(self, scope, principal, *, cancel, parent_lease, supervisor):
        with self._tx() as db:
            row = self._task(db, scope, principal)
            if row['kind'] != 'Scope':
                raise StateConflict('only scope roots can be closed with close_scope')
            if supervisor:
                if self._resource_access.get() is not None:
                    raise StateConflict('supervisor cancellation requires a separate host context')
            else:
                self._scope_controller(db,row,principal,parent_lease)
            if row['disposition'] != 'Attached':
                return
            children = self._subtree(db, scope)
            if cancel:
                for child in children:
                    if child['id'] != scope:
                        self._release_task(db, child, cancel=True)
            elif any(c['disposition'] == 'Attached' for c in children if c['id'] != scope):
                raise StateConflict('scope must join or hand off every child before normal exit')
            self._join_allocation_subtree(db, row['run'], row['allocation'])
            disposition = 'Runtime' if self._task_obligations(db, row) else 'Joined'
            db.execute('UPDATE tasks SET state=?,disposition=? WHERE id=?', ('Cancelled' if cancel else 'Completed', disposition, scope))
            self._event(db, row['run'], 'ScopeClosed', task=scope, cancelled=cancel, disposition=disposition)
            self.fault('before_scope_close_commit')
        self.fault('after_scope_close_commit')

    def task_snapshot(self, task, principal):
        with self._tx() as db:
            row = self._task(db, task, principal)
            result = dict(row)
            result['result'] = json.loads(row['result']) if row['result'] is not None else None
            result['amount'] = json.loads(row['amount'])
            result['speculative'] = self._speculative(db,task)
            result['obligations'] = self._task_obligations(db, row)
            result['children'] = [r['id'] for r in db.execute('SELECT id FROM tasks WHERE parent=? ORDER BY rowid', (task,))]
            return result

    def bind_inputs(self, run, principal, bindings, *, task=None):
        encoded = canonical(normalize_bindings(bindings))
        with self._tx() as db:
            self._run(db,run,principal)
            if task is not None:
                owner = self._task(db,task,principal)
                if owner['run'] != run:
                    raise StateConflict('task inputs belong to another run')
                if canonical(self._task_execution_inputs(db,task)) != encoded:
                    raise ReplayMismatch('task inputs differ from its durable spawn binding')
            else:
                row = db.execute('SELECT bindings FROM run_inputs WHERE run=?', (run,)).fetchone()
                if row and row['bindings'] != encoded:
                    raise ReplayMismatch('run inputs are fixed for recovery')
                db.execute('INSERT OR IGNORE INTO run_inputs VALUES(?,?)', (run,encoded))

    def task_inputs(self, task, principal):
        with self._tx() as db:
            self._task(db,task,principal)
            row = db.execute('SELECT bindings FROM task_inputs WHERE task=?', (task,)).fetchone()
            return json.loads(row['bindings']) if row else {}

    def task_outcome(self, task, principal):
        with self._tx() as db:
            self._task(db,task,principal)
            row = db.execute('SELECT outcome FROM task_outcomes WHERE task=?', (task,)).fetchone()
            return json.loads(row['outcome']) if row else None

    def _receive_task_value(self, db, row, principal, parent_lease):
        """Validate the typed payload against its manifest before any transfer."""
        payload=json.loads(row['result'])
        validate_record(payload)
        saved=db.execute('SELECT bindings FROM task_resource_returns WHERE task=?',(row['id'],)).fetchone()
        published={} if saved is None else {k:ResourceRef(**v) for k,v in json.loads(saved['bindings']).items()}
        accept_record(payload,published)
        received=self._receive_resources(db,row,principal,parent_lease)
        return accept_record(payload,received)

    def _scope_controller(self, db, scope, principal, parent_lease):
        self._check_access_actor(db,scope['run'],parent_lease)
        actor=self._custody_actor(db,scope['run'],principal,parent_lease)
        if actor!=self._scope_sender(db,scope):
            raise StateConflict('task controller is outside the parent scope')

    def _outcome_receiver(self, db, row, principal, parent_lease):
        parent=self._task(db,row['parent'],principal)
        self._scope_controller(db,parent,principal,parent_lease)

    def take_task_outcome(self, task, principal, *, parent_lease=None, cancel=False):
        """Freeze the observation and responsibility transfer in one transaction."""
        if type(cancel) is not bool:
            raise StateConflict('outcome cancellation must be boolean')
        with self._tx() as db:
            row = self._task(db,task,principal)
            previous = db.execute('SELECT outcome FROM task_outcomes WHERE task=?', (task,)).fetchone()
            if previous:
                return json.loads(previous['outcome'])
            if row['kind']!='Task' or not cancel and row['state'] not in ('Completed','Failed','Waiting','Cancelled'):
                raise StateConflict('task has no observable outcome yet')
            self._outcome_receiver(db,row,principal,parent_lease)
            kind = 'Cancelled' if cancel and row['state'] not in ('Completed','Failed','Cancelled') else row['state']
            if kind=='Completed':
                payload = self._receive_task_value(db,row,principal,parent_lease)
            else:
                failure = json.loads(row['result']) if row['result'] is not None else {}
                payload = {'data':{'task':task,'operation':row['waiting_on'],
                                   'error_type':failure.get('error_type') if type(failure) is dict else None},'receipts':[]}
            label=self._task_flow(db,task)[1].join(Label.from_data(payload.get('label')))
            if label!=PUBLIC:
                payload['label']=label.to_data()
            else:
                payload.pop('label',None)
            if kind in ('Waiting','Cancelled'):
                for child in self._subtree(db,task):
                    self._release_task(db,child,cancel=True)
            else:
                self._release_task(db,row,cancel=False)
            outcome = {'kind':kind,'payload':payload}
            if label!=PUBLIC:
                outcome['label']=label.to_data()
            db.execute('INSERT INTO task_outcomes VALUES(?,?)', (task,canonical(outcome)))
            self._event(db,row['run'],'TaskOutcomeTaken',task=task,outcome=kind)
            self.fault('before_task_outcome_commit')
        self.fault('after_task_outcome_commit')
        return outcome

    def race_winner(self, scope, principal, key, candidates):
        with self._tx() as db:
            self._task(db,scope,principal)
            row = db.execute('SELECT * FROM task_races WHERE scope=? AND race_key=?', (scope,key)).fetchone()
            if row is None:
                return None
            if row['candidates'] != canonical(candidates):
                raise ReplayMismatch('race key binds a different ordered candidate set')
            return row['winner']

    def select_race(self, scope, principal, key, candidates, winner, *, parent_lease=None):
        """Atomically persist a successful winner and release all candidates."""
        identifier(key)
        if type(candidates) is not list or len(candidates)<2 or any(type(t) is not str for t in candidates) or len(set(candidates))!=len(candidates):
            raise StateConflict('race requires distinct task identities')
        with self._tx() as db:
            owner = self._task(db,scope,principal)
            old = db.execute('SELECT * FROM task_races WHERE scope=? AND race_key=?', (scope,key)).fetchone()
            if old:
                if old['candidates'] != canonical(candidates):
                    raise ReplayMismatch('race key binds different candidates')
                return old['winner']
            if owner['kind']!='Scope' or owner['state']!='Open' or not self._speculative(db,scope):
                raise StateConflict('race requires an open speculative scope')
            rows = [self._task(db,t,principal) for t in candidates]
            if any(r['parent']!=scope or r['kind']!='Task' or r['disposition']!='Attached' for r in rows):
                raise StateConflict('race candidates must be attached children of this scope')
            selected = next((r for r in rows if r['id']==winner),None)
            if selected is None or selected['state']!='Completed':
                raise StateConflict('race winner must have a durable successful result')
            self._outcome_receiver(db,selected,principal,parent_lease)
            payload=json.loads(selected['result'])
            if (db.execute('SELECT 1 FROM task_resource_returns WHERE task=?',(winner,)).fetchone()
                    or type(payload) is dict and {'data','receipts'}<=set(payload)
                    and ('resource' in payload or 'bundle' in payload or 'variant' in payload)):
                self._receive_task_value(db,selected,principal,parent_lease)
            for row in rows:
                if row['id']==winner:
                    self._release_task(db,row,cancel=False)
                else:
                    for child in self._subtree(db,row['id']):
                        self._release_task(db,child,cancel=True)
            db.execute('INSERT INTO task_races VALUES(?,?,?,?)', (scope,key,canonical(candidates),winner))
            self._event(db,owner['run'],'RaceSelected',scope=scope,key=key,winner=winner,candidates=candidates)
            self.fault('before_race_commit')
        self.fault('after_race_commit')
        return winner
