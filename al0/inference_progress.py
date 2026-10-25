"""Fixed model observations and affine control transfers in one transaction."""
import json
from typing import TYPE_CHECKING

from .bindings import normalize_bindings
from .bundles import record_refs, replace_record_refs
from .custody import ResourceRef
from .data import canonical, identifier
from .errors import ReplayMismatch, StateConflict
from .flow import Label, PUBLIC

if TYPE_CHECKING:
    from .ir import Type


INFERENCE_PROGRESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS inference_observations(
 run TEXT NOT NULL REFERENCES runs(id), activation TEXT NOT NULL, node TEXT NOT NULL,
 operation TEXT NOT NULL REFERENCES operations(id), source TEXT NOT NULL,
 contract TEXT NOT NULL, snapshot TEXT NOT NULL, result TEXT NOT NULL,
 PRIMARY KEY(run,activation,node));
"""


def progress_type(tool: str, output: str) -> 'Type':
    from .bundle_types import variant_type
    from .ir import Type
    return variant_type({'Success': Type('Candidate', output), 'Error': Type('Data', 'InferenceError'),
                         'Waiting': Type('Operation', tool)})


class InferenceProgressMixin:
    @staticmethod
    def _inference_observation_view(row, source, contract):
        if row['source'] != canonical(source.record()) or row['contract'] != canonical(contract):
            raise ReplayMismatch('inference observation source or contract changed')
        return json.loads(row['result'])

    def inference_observation(self, run, principal, node, source, contract, *, task_lease=None):
        identifier(node)
        activation = '0' if task_lease is None else task_lease.task
        with self._tx() as db:
            self._run(db, run, principal)
            row = db.execute('SELECT * FROM inference_observations WHERE run=? AND activation=? AND node=?',
                             (run, activation, node)).fetchone()
            return None if row is None else self._inference_observation_view(row, source, contract)

    def observe_inference(self, run, principal, node, source, contract, classify, *, task_lease=None, control=PUBLIC):
        """classify is trusted, local schema validation; it must not perform I/O."""
        identifier(node)
        if not isinstance(source, ResourceRef) or source.kind != 'Operation':
            raise StateConflict('model observation requires an Operation reference')
        activation = '0' if task_lease is None else task_lease.task
        self.fault('before_inference_observation_transaction')
        with self._tx() as db:
            self._run(db, run, principal)
            previous = db.execute('SELECT * FROM inference_observations WHERE run=? AND activation=? AND node=?',
                                  (run, activation, node)).fetchone()
            if previous:
                return self._inference_observation_view(previous, source, contract)
            self._control_worker(db, run, principal, task_lease)
            op = self._operation(db, source.resource, principal)
            if op['cancelled']:
                raise StateConflict('cancelled model operation cannot publish a new observation')
            intent = self._intent(op['intent'])
            if op['run'] != run or (intent.value['tool'], intent.value['version']) != (contract['tool'], contract['version']):
                raise StateConflict('inference observation names another run or tool')
            if not self._custody_guard(db, 'Operation', source.resource, task_lease):
                raise StateConflict('model observation requires managed control')
            if self._custody(db, 'Operation', source.resource)['generation'] != source.generation:
                raise StateConflict('model observation source generation is stale')
            actor = self._custody_actor(db, run, principal, task_lease)
            policy = self._flow_policy(db, intent)
            label = control.join(intent.label, PUBLIC if policy is None else Label.from_data(policy['output']))
            state = {'business': op['business'], 'business_evidence': json.loads(op['business_evidence']) if op['business_evidence'] else None}
            result = normalize_bindings({'result': classify(state, label)})['result']
            refs = record_refs(result)
            if state['business'] in ('NotDispatched', 'Unknown', 'Pending'):
                if result['data']['tag'] != 'Waiting' or list(refs.values()) != [source]:
                    raise StateConflict('unresolved observation must return its operation')
                moved = self._custody_move(db, source, actor)
                result = replace_record_refs(result, {('Operation', source.resource): moved})
            elif state['business'] in ('Applied', 'ProvenNoEffect'):
                if result['data']['tag'] not in ('Success', 'Error') or refs:
                    raise StateConflict('terminal observation must close its control reference')
                self._custody_move(db, source, ('Runtime', actor[1]), state='Spent')
            else:
                raise StateConflict('unknown model business state')
            self.fault('after_inference_observation_move')
            snapshot = {'business': op['business'], 'cancel_requested': bool(op['cancelled'])}
            db.execute('INSERT INTO inference_observations VALUES(?,?,?,?,?,?,?,?)',
                       (run, activation, node, source.resource, canonical(source.record()), canonical(contract),
                        canonical(snapshot), canonical(result)))
            self._event(db, run, 'InferenceObserved', node=node, activation=activation, operation=source.resource,
                        business=op['business'], case=result['data']['tag'])
            self.fault('before_inference_observation_commit')
        self.fault('after_inference_observation_commit')
        return result
