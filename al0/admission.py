"""Atomic, first-decision admission results; rejected slots never reopen."""
import json

from .data import Intent, Slot, canonical
from .flow import Label, PUBLIC
from .errors import (AuthorizationDenied, BudgetExceeded, EvidenceMismatch,
                     FlowDenied, InvalidData, PermitConsumed, ReplayMismatch,
                     StateConflict)


ADMISSION_SCHEMA="""
CREATE TABLE IF NOT EXISTS admission_decisions(
 run TEXT NOT NULL REFERENCES runs(id), node TEXT NOT NULL, activation TEXT NOT NULL,
 intent TEXT NOT NULL, permit TEXT NOT NULL, allocation TEXT NOT NULL REFERENCES allocations(id),
 result TEXT NOT NULL, PRIMARY KEY(run,node,activation));
"""

REJECTIONS=(AuthorizationDenied,BudgetExceeded,EvidenceMismatch,FlowDenied,PermitConsumed)
ERRORS={error.code:error for error in REJECTIONS}


def unwrap_admission(result):
    if result['kind']=='Admitted':
        return result['operation']
    error=ERRORS[result['reason']['code']]
    raise error('admission slot has a durable rejection; use its returned permit at a new slot')


class AdmissionMixin:
    def _admission_result(self,db,run,slot,intent,permit,allocation):
        row=db.execute('SELECT * FROM admission_decisions WHERE run=? AND node=? AND activation=?',
                       (run,slot.node,slot.activation)).fetchone()
        if row is None:
            return None
        if (row['intent'],row['permit'],row['allocation'])!=(intent.encoded,permit,allocation):
            raise ReplayMismatch('admission slot already binds another descriptor, permit or allocation')
        return json.loads(row['result'])

    def try_admit(self,run,principal,slot,intent,permit,*,allocation=None,task_lease=None,control=PUBLIC):
        if not isinstance(slot,Slot) or not isinstance(intent,Intent) or type(permit) is not str:
            raise InvalidData('admission requires a slot, immutable intent and permit reference')
        if not isinstance(control,Label):
            raise InvalidData('admission control must be a Label')
        with self._tx() as db:
            self._run(db,run,principal)
            owner=self._allocation(db,run,allocation)
            previous=self._admission_result(db,run,slot,intent,permit,owner['id'])
            if previous is not None:
                return previous
            # Legacy successful admissions already provide a durable decision.
            old=db.execute('SELECT * FROM operations WHERE run=? AND node=? AND activation=?',
                           (run,slot.node,slot.activation)).fetchone()
            if old:
                if (old['intent'],old['permit'],self._operation_allocation(db,old['id']))!=(intent.encoded,permit,owner['id']):
                    raise ReplayMismatch('existing operation has another admission request')
                return {'kind':'Admitted','operation':old['id'],'label':intent.label.join(control).to_data()}
            # Authentication, lifecycle and worker fencing failures are not
            # recoverable business rejections and must not publish a decision.
            self._run(db,run,principal,mutable=True)
            self._new_work(db,run,intent)
            self._task_funding(db,owner['id'],task_lease)
            self._task_effect(db,owner['id'],intent)
            if task_lease is not None and slot.activation!=task_lease.task:
                raise StateConflict('task operation slot must use its stable task activation')
            db.execute('SAVEPOINT admission_attempt')
            try:
                if intent.label.join(control)!=intent.label:
                    raise FlowDenied('intent was prepared before the current control context')
                operation=self._admit_in_tx(db,run,principal,slot,intent,permit,allocation=owner['id'],task_lease=task_lease)
            except REJECTIONS as error:
                db.execute('ROLLBACK TO admission_attempt')
                returned=None
                if isinstance(error,BudgetExceeded):
                    # Recheck after rollback. A clock deadline may have crossed
                    # since the initial permission check in the failed attempt.
                    try:
                        self._admission_permission(db,run,intent,permit)
                    except REJECTIONS as expired:
                        error=expired
                    else:
                        returned=permit
                result={'kind':'Rejected','reason':{'code':error.code},'permit':returned}
            else:
                result={'kind':'Admitted','operation':operation}
            result['label']=intent.label.join(control).to_data()
            db.execute('RELEASE admission_attempt')
            db.execute('INSERT INTO admission_decisions VALUES(?,?,?,?,?,?,?)',
                       (run,slot.node,slot.activation,intent.encoded,permit,owner['id'],canonical(result)))
            self._event(db,run,'AdmissionDecided',node=slot.node,activation=slot.activation,
                        kind_result=result['kind'],returned_permit=result.get('permit') is not None)
            self.fault('before_admission_decision_commit')
        self.fault('after_admission_decision_commit')
        if result['kind']=='Admitted':
            self.fault('after_admit_commit')
        return result

    def _admission_permission(self,db,run,intent,permit):
        row=db.execute('SELECT * FROM permits WHERE id=? AND run=?',(permit,run)).fetchone()
        if row is None or row['intent']!=intent.encoded:
            raise AuthorizationDenied('permit does not bind this action')
        if row['consumed_by'] is not None:
            raise PermitConsumed('permit is already consumed')
        self._authority(db,run,row['grant_id'],intent,row['epoch'])
        if row['expires']<=self.clock():
            raise AuthorizationDenied('permit expired')
        contract=self._contract(db,intent)
        self._evidence(db,run,intent,contract)
        self._check_flow(db,intent)
        return contract
