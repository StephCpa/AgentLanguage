"""Atomic loop progress and affine resource generations with fixed receipts."""
import json

from .bindings import normalize_bindings
from .bundle_types import is_affine, restore
from .bundles import record_refs, replace_record_refs
from .data import canonical, identifier
from .errors import EvaluationError, ReplayMismatch, StateConflict
from .flow import PUBLIC
from .limits import MAX_ITERATIONS


AFFINE_LOOP_SCHEMA = """
CREATE TABLE IF NOT EXISTS loop_resource_receipts(
 run TEXT NOT NULL, activation TEXT NOT NULL, node TEXT NOT NULL,
 step INTEGER NOT NULL, source TEXT NOT NULL, received TEXT NOT NULL,
 PRIMARY KEY(run,activation,node,step),
 FOREIGN KEY(run,activation,node) REFERENCES loop_frames(run,activation,node));
"""


class AffineLoopMixin:
    def _loop_move_state(self, db, run, principal, value, interface, lease):
        typ = restore(interface)
        if not is_affine(typ) or typ.kind == 'Permit':
            raise StateConflict('loop state requires a closed affine result interface')
        if typ.kind in ('Bundle', 'Variant'):
            if value.get(typ.kind.lower()) != interface:
                raise StateConflict('loop state differs from its closed interface')
        elif typ.kind not in ('Budget', 'Operation') or value.get('resource', {}).get('kind') != typ.kind:
            raise StateConflict('scalar loop state requires a matching resource reference')
        self._check_access_actor(db, run, lease)
        actor = self._custody_actor(db, run, principal, lease)
        refs = record_refs(value)
        for ref in refs.values():
            if not self._custody_guard(db, ref.kind, ref.resource, lease):
                raise StateConflict('loop state resources must be managed')
            row = self._custody(db, ref.kind, ref.resource)
            if row['run'] != run or row['generation'] != ref.generation:
                raise StateConflict('loop state contains a stale resource reference')
        received = {(ref.kind, ref.resource): self._custody_move(db, ref, actor) for ref in refs.values()}
        self.fault('after_loop_resource_moves')
        return replace_record_refs(value, received)

    def begin_resource_loop(self, run, principal, node, count, limit, initial, inputs, *, interface,
                            task_lease=None, control=PUBLIC):
        identifier(node)
        initial = normalize_bindings({'state': initial})['state']
        inputs = normalize_bindings(inputs)
        if any(record_refs(value) for value in inputs.values()):
            raise StateConflict('loop resources must travel in state, not repeated captures')
        activation = '0' if task_lease is None else task_lease.task
        with self._tx() as db:
            self._run(db, run, principal)
            previous = db.execute('SELECT * FROM loop_frames WHERE run=? AND activation=? AND node=?',
                                  (run, activation, node)).fetchone()
            if previous:
                view = self._loop_view(previous)
                if view['frame'].get('resource_interface') != interface:
                    raise ReplayMismatch('loop resource interface changed')
                source = db.execute('SELECT source FROM loop_resource_receipts WHERE run=? AND activation=? AND node=? AND step=-1',
                                    (run, activation, node)).fetchone()
                if source is None or record_refs(json.loads(source['source'])) != record_refs(initial):
                    raise ReplayMismatch('loop initial resource binding changed')
                return view
            self._control_worker(db, run, principal, task_lease)
            if type(limit) is not int or not 0 <= limit <= MAX_ITERATIONS or type(count) is not int or not 0 <= count <= limit:
                raise EvaluationError('repeat count is outside its declared bound')
            received = self._loop_move_state(db, run, principal, initial, interface, task_lease)
            frame = {'count': count, 'limit': limit, 'inputs': inputs, 'control': control.to_data(),
                     'resource_interface': interface}
            db.execute('INSERT INTO loop_frames VALUES(?,?,?,?,0,?)',
                       (run, activation, node, canonical(frame), canonical(received)))
            db.execute('INSERT INTO loop_resource_receipts VALUES(?,?,?,-1,?,?)',
                       (run, activation, node, canonical(initial), canonical(received)))
            self._event(db, run, 'ResourceLoopStarted', node=node, activation=activation, count=count)
            self.fault('before_resource_loop_begin_commit')
        self.fault('after_resource_loop_begin_commit')
        return {'frame': frame, 'next': 0, 'current': received}

    def advance_resource_loop(self, run, principal, node, index, result, *, task_lease=None):
        identifier(node)
        result = normalize_bindings({'state': result})['state']
        activation = '0' if task_lease is None else task_lease.task
        self.fault('before_resource_loop_advance_transaction')
        with self._tx() as db:
            self._run(db, run, principal)
            row = db.execute('SELECT * FROM loop_frames WHERE run=? AND activation=? AND node=?',
                             (run, activation, node)).fetchone()
            if row is None or type(index) is not int or index < 0:
                raise StateConflict('resource loop progress is unavailable')
            frame = json.loads(row['frame'])
            if 'resource_interface' not in frame:
                raise StateConflict('data loop cannot advance through resource protocol')
            previous = db.execute('SELECT * FROM loop_resource_receipts WHERE run=? AND activation=? AND node=? AND step=?',
                                  (run, activation, node, index)).fetchone()
            if previous:
                if previous['source'] != canonical(result):
                    raise ReplayMismatch('loop iteration result changed')
                # Never refresh a historical state to a later iteration's generation.
                return {'frame': frame, 'next': index + 1, 'current': json.loads(previous['received'])}
            self._control_worker(db, run, principal, task_lease)
            if index != row['next_index'] or index >= frame['count']:
                raise StateConflict('resource loop iterations must commit in order')
            received = self._loop_move_state(db, run, principal, result, frame['resource_interface'], task_lease)
            db.execute('INSERT INTO loop_resource_receipts VALUES(?,?,?,?,?,?)',
                       (run, activation, node, index, canonical(result), canonical(received)))
            db.execute('UPDATE loop_frames SET next_index=?,current_value=? WHERE run=? AND activation=? AND node=?',
                       (index + 1, canonical(received), run, activation, node))
            self._event(db, run, 'ResourceLoopAdvanced', node=node, activation=activation, index=index)
            self.fault('before_resource_loop_advance_commit')
        self.fault('after_resource_loop_advance_commit')
        return {'frame': frame, 'next': index + 1, 'current': received}
