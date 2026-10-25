"""Terminal inference values, separate from operation and billing responsibility."""
import json
from typing import TYPE_CHECKING, TypedDict

from .bindings import normalize_bindings
from .data import canonical
from .errors import ReplayMismatch, StateConflict

if TYPE_CHECKING:
    from .ir import Type


class InferenceError(TypedDict):
    code: str
    tool: str
    version: str


ERROR_FIELDS = {'code': 'Text', 'tool': 'Text', 'version': 'Text'}
INFERENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS inference_results(
 operation TEXT PRIMARY KEY REFERENCES operations(id),
 contract TEXT NOT NULL, result TEXT NOT NULL);
"""


def result_type(output: str) -> 'Type':
    from .bundle_types import variant_type
    from .ir import Type
    return variant_type({'Success': Type('Candidate', output), 'Error': Type('Data', 'InferenceError')})


class InferenceMixin:
    def inference_result(self, operation, principal, contract):
        """Read a committed result even after its task or run has finished."""
        with self._tx() as db:
            self._operation(db, operation, principal)
            row = db.execute('SELECT * FROM inference_results WHERE operation=?', (operation,)).fetchone()
            if row is None:
                return None
            if row['contract'] != canonical(contract):
                raise ReplayMismatch('inference result contract changed')
            return json.loads(row['result'])

    def bind_inference_result(self, operation, principal, contract, result, *, task_lease=None):
        result = normalize_bindings({'result': result})['result']
        with self._tx() as db:
            op = self._operation(db, operation, principal)
            old = db.execute('SELECT * FROM inference_results WHERE operation=?', (operation,)).fetchone()
            if old:
                if old['contract'] != canonical(contract):
                    raise ReplayMismatch('inference result contract changed')
                return json.loads(old['result'])
            self._control_worker(db, op['run'], principal, task_lease)
            if op['business'] not in ('Applied', 'ProvenNoEffect'):
                raise StateConflict('unresolved inference cannot publish a terminal result')
            # No task may publish another task's operation as its own result.
            if (op['activation'] != ('0' if task_lease is None else task_lease.task)):
                raise StateConflict('inference result belongs to another activation')
            db.execute('INSERT INTO inference_results VALUES(?,?,?)',
                       (operation, canonical(contract), canonical(result)))
            self._event(db, op['run'], 'InferenceResultBound', operation=operation)
            self.fault('before_inference_result_commit')
        self.fault('after_inference_result_commit')
        return result
