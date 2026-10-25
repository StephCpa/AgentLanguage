"""Atomic model authorization, admission and fixed preflight rejection."""
import json
from typing import TYPE_CHECKING, TypedDict

from .data import Intent, Slot, canonical, identifier
from .errors import AuthorizationDenied, BudgetExceeded, FlowDenied, InvalidData, ReplayMismatch, StateConflict
from .flow import Label, PUBLIC
from .inference_progress import progress_type

if TYPE_CHECKING:
    from .ir import Type


class InferenceRejection(TypedDict):
    code: str
    tool: str
    version: str


REJECTION_FIELDS = {'code': 'Text', 'tool': 'Text', 'version': 'Text'}
INFERENCE_ADMISSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS inference_admissions(
 run TEXT NOT NULL REFERENCES runs(id), activation TEXT NOT NULL, node TEXT NOT NULL,
 request TEXT NOT NULL, contract TEXT NOT NULL, allocation TEXT NOT NULL REFERENCES allocations(id),
 grant_choice TEXT NOT NULL, result TEXT NOT NULL,
 PRIMARY KEY(run,activation,node));
"""


def call_type(tool: str, output: str) -> 'Type':
    from .bundle_types import variant_type
    from .ir import Type
    return variant_type({'Rejected': Type('Data', 'InferenceRejection'), 'Started': progress_type(tool, output)})


class InferenceAdmissionMixin:
    def try_inference(self, run, principal, slot, intent, grant, contract, *, allocation=None,
                      task_lease=None, control=PUBLIC):
        if not isinstance(slot, Slot) or not isinstance(intent, Intent) or not isinstance(control, Label):
            raise InvalidData('inference admission requires a slot, intent and control label')
        if grant is not None:
            identifier(grant)
        encoded_contract=canonical(contract)
        request=canonical({'intent':intent.value,'control':control.to_data()})
        if (contract['tool'],contract['version']) != (intent.value['tool'],intent.value['version']):
            raise StateConflict('inference admission contract does not match request')
        self.fault('before_inference_admission_transaction')
        with self._tx() as db:
            self._run(db,run,principal)
            owner=self._allocation(db,run,allocation)
            previous=db.execute('SELECT * FROM inference_admissions WHERE run=? AND activation=? AND node=?',
                                (run,slot.activation,slot.node)).fetchone()
            if previous:
                if (previous['request'],previous['contract'],previous['allocation'],previous['grant_choice']) != (
                        request,encoded_contract,owner['id'],canonical(grant)):
                    raise ReplayMismatch('inference admission request, contract, funding or grant changed')
                return json.loads(previous['result'])
            self._control_worker(db,run,principal,task_lease)
            self._task_funding(db,owner['id'],task_lease)
            self._task_effect(db,owner['id'],intent)
            if slot.activation != ('0' if task_lease is None else task_lease.task):
                raise StateConflict('inference admission slot has a different activation')
            access=self._resource_access.get()
            if access is None:
                raise StateConflict('inference admission requires managed resource access')
            self._check_access_actor(db,run,task_lease)
            if task_lease is not None and grant is not None:
                self._check_capability_resources(db,run,principal,self._task_capabilities(db,task_lease.task),[('Cap',grant)])
            # These invariant failures are never classified as ordinary refusal.
            if db.execute('SELECT 1 FROM operations WHERE run=? AND activation=? AND node=?',
                          (run,slot.activation,slot.node)).fetchone():
                raise ReplayMismatch('inference admission slot already has an unbound operation')
            self._evidence(db,run,intent,self._contract(db,intent))
            label=intent.label.join(control)
            derived=None if grant is None else db.execute('SELECT label FROM grant_derivations WHERE child=? AND run=?',(grant,run)).fetchone()
            if derived is not None:
                label=label.join(Label.from_data(json.loads(derived['label'])))
            prepared=Intent(intent.value['tool'],intent.value['version'],intent.value['scope'],
                            intent.value['args'],intent.value['receipts'],label=label)
            presented=dict(access[2])
            db.execute('SAVEPOINT inference_attempt')
            try:
                try:
                    if grant is None:
                        raise AuthorizationDenied('host did not bind the required model grant')
                    permit=self._authorize_in_tx(db,run,principal,prepared,grant,task_lease=task_lease)
                    self.fault('after_inference_authorization')
                    operation=self._admit_in_tx(db,run,principal,slot,prepared,permit,allocation=owner['id'],task_lease=task_lease)
                except (AuthorizationDenied, BudgetExceeded, FlowDenied) as error:
                    db.execute('ROLLBACK TO inference_attempt')
                    access[2].clear();access[2].update(presented)
                    code=('BudgetExceeded' if isinstance(error,BudgetExceeded) else
                          'FlowDenied' if isinstance(error,FlowDenied) else 'AuthorizationUnavailable')
                    result={'kind':'Rejected','reason':{'code':code,'tool':contract['tool'],'version':contract['version']},
                            'label':label.to_data()}
                else:
                    result={'kind':'Started','operation':operation,'label':label.to_data()}
                db.execute('RELEASE inference_attempt')
                db.execute('INSERT INTO inference_admissions VALUES(?,?,?,?,?,?,?,?)',
                           (run,slot.activation,slot.node,request,encoded_contract,owner['id'],canonical(grant),canonical(result)))
                self._event(db,run,'InferenceAdmissionDecided',node=slot.node,activation=slot.activation,kind_result=result['kind'])
                self.fault('before_inference_admission_commit')
            except BaseException:
                # The surrounding transaction rolls back database state as well.
                access[2].clear();access[2].update(presented)
                raise
        self.fault('after_inference_admission_commit')
        return result
