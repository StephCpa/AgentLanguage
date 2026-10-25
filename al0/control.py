"""First-observation branch decisions and captured inputs, committed together."""
import json

from .bindings import normalize_bindings
from .data import canonical, identifier, resource_vector
from .errors import EvaluationError, InvalidData, ReplayMismatch, StateConflict
from .flow import Label, PUBLIC
from .limits import MAX_ITERATIONS
from .bundle_types import is_affine, restore


def _data_loop_value(value):
    if ('resource' in value or 'bundle' in value or
            'variant' in value and is_affine(restore(value['variant']))):
        raise StateConflict('resource loop values require the affine checkpoint protocol')


CONTROL_SCHEMA="""
CREATE TABLE IF NOT EXISTS branch_choices(
 run TEXT NOT NULL REFERENCES runs(id), activation TEXT NOT NULL, node TEXT NOT NULL,
 decision TEXT NOT NULL, PRIMARY KEY(run,activation,node));
CREATE TABLE IF NOT EXISTS loop_frames(
 run TEXT NOT NULL REFERENCES runs(id), activation TEXT NOT NULL, node TEXT NOT NULL,
 frame TEXT NOT NULL, next_index INTEGER NOT NULL, current_value TEXT NOT NULL,
 PRIMARY KEY(run,activation,node));
CREATE TABLE IF NOT EXISTS invocation_frames(
 run TEXT NOT NULL REFERENCES runs(id), activation TEXT NOT NULL, node TEXT NOT NULL,
 frame TEXT NOT NULL, PRIMARY KEY(run,activation,node));
CREATE TABLE IF NOT EXISTS budget_evaluations(
 run TEXT NOT NULL REFERENCES runs(id), activation TEXT NOT NULL, node TEXT NOT NULL,
 purpose TEXT NOT NULL, amount TEXT NOT NULL, label TEXT NOT NULL,
 PRIMARY KEY(run,activation,node));
"""


class ControlMixin:
    def bind_budget_amount(self,run,principal,node,purpose,amount,*,task_lease=None,control=PUBLIC):
        """Persist the first evaluated vector; this does not transfer credit.

        A later allocation failure can retry the same amount at this site.
        Changing the requested amount requires a new site, iteration or run.
        """
        identifier(node)
        if purpose not in ('split','scope','spawn') or not isinstance(control,Label):
            raise InvalidData('budget evaluation requires a known purpose and control label')
        activation='0' if task_lease is None else task_lease.task
        with self._tx() as db:
            self._run(db,run,principal)
            old=db.execute('SELECT * FROM budget_evaluations WHERE run=? AND activation=? AND node=?',
                           (run,activation,node)).fetchone()
            if old:
                if old['purpose']!=purpose:
                    raise ReplayMismatch('budget evaluation purpose changed')
                return {'amount':json.loads(old['amount']),'label':json.loads(old['label'])}
            self._control_worker(db,run,principal,task_lease)
            amount=resource_vector(amount)
            label=control.to_data()
            db.execute('INSERT INTO budget_evaluations VALUES(?,?,?,?,?,?)',
                       (run,activation,node,purpose,canonical(amount),canonical(label)))
            self._event(db,run,'BudgetAmountBound',activation=activation,node=node,purpose=purpose)
            self.fault('before_budget_amount_commit')
        self.fault('after_budget_amount_commit')
        return {'amount':amount,'label':label}

    def begin_invocation(self,run,principal,node,inputs,*,allocation=None,task_lease=None,control=PUBLIC,caps=None):
        identifier(node)
        activation='0' if task_lease is None else task_lease.task
        with self._tx() as db:
            self._run(db,run,principal)
            owner=self._allocation(db,run,allocation)
            previous=db.execute('SELECT frame FROM invocation_frames WHERE run=? AND activation=? AND node=?',
                                (run,activation,node)).fetchone()
            if previous:
                frame=json.loads(previous['frame'])
                if frame['allocation']!=owner['id']:
                    raise ReplayMismatch('invocation funding changed')
                if frame.get('caps')!=caps:
                    raise ReplayMismatch('invocation capability boundary changed')
                return frame
            self._control_worker(db,run,principal,task_lease)
            self._task_funding(db,owner['id'],task_lease)
            self._allocation(db,run,owner['id'],open_only=True)
            frame={'inputs':normalize_bindings(inputs),'allocation':owner['id'],'control':control.to_data()}
            if caps is not None:
                self._check_capability_map(db,run,caps)
                frame['caps']=dict(caps)
            db.execute('INSERT INTO invocation_frames VALUES(?,?,?,?)',(run,activation,node,canonical(frame)))
            self._event(db,run,'InvocationBound',node=node,activation=activation,allocation=owner['id'])
            self.fault('before_invocation_commit')
        self.fault('after_invocation_commit')
        return frame

    def _control_worker(self, db, run, principal, lease):
        self._run(db,run,principal,mutable=True)
        self._task_new_work(db,run)
        if lease is not None:
            owner=self._task(db,lease.task,principal)
            self._lease(owner,lease)
            if owner['run']!=run:
                raise StateConflict('control worker belongs to another run')

    @staticmethod
    def _loop_view(row):
        return {'frame':json.loads(row['frame']),'next':row['next_index'],'current':json.loads(row['current_value'])}

    def begin_loop(self, run, principal, node, count, limit, initial, inputs, *, task_lease=None, control=PUBLIC):
        identifier(node)
        activation='0' if task_lease is None else task_lease.task
        with self._tx() as db:
            self._run(db,run,principal)
            previous=db.execute('SELECT * FROM loop_frames WHERE run=? AND activation=? AND node=?',(run,activation,node)).fetchone()
            if previous:
                if 'resource_interface' in json.loads(previous['frame']):
                    raise ReplayMismatch('resource loop cannot resume through data protocol')
                return self._loop_view(previous)
            self._control_worker(db,run,principal,task_lease)
            if type(limit) is not int or not 0<=limit<=MAX_ITERATIONS or type(count) is not int or not 0<=count<=limit:
                raise EvaluationError('repeat count is outside its declared bound')
            initial=normalize_bindings({'state':initial})['state']
            frame={'count':count,'limit':limit,'inputs':normalize_bindings(inputs),'control':control.to_data()}
            for value in [initial,*frame['inputs'].values()]:
                _data_loop_value(value)
            db.execute('INSERT INTO loop_frames VALUES(?,?,?,?,0,?)',(run,activation,node,canonical(frame),canonical(initial)))
            self._event(db,run,'LoopStarted',node=node,activation=activation,count=count)
            self.fault('before_loop_begin_commit')
        self.fault('after_loop_begin_commit')
        return {'frame':frame,'next':0,'current':initial}

    def advance_loop(self, run, principal, node, index, result, *, task_lease=None):
        identifier(node)
        activation='0' if task_lease is None else task_lease.task
        result=normalize_bindings({'state':result})['state']
        with self._tx() as db:
            self._run(db,run,principal)
            row=db.execute('SELECT * FROM loop_frames WHERE run=? AND activation=? AND node=?',(run,activation,node)).fetchone()
            if row is None or type(index) is not int or index<0:
                raise StateConflict('loop progress is unavailable')
            if 'resource_interface' in json.loads(row['frame']):
                raise StateConflict('resource loop cannot advance through data protocol')
            if index<row['next_index']:
                return self._loop_view(row)
            self._control_worker(db,run,principal,task_lease)
            frame=json.loads(row['frame'])
            if index!=row['next_index'] or index>=frame['count']:
                raise StateConflict('loop iterations must advance in order and within the frozen count')
            _data_loop_value(result)
            db.execute('UPDATE loop_frames SET next_index=?,current_value=? WHERE run=? AND activation=? AND node=?',
                       (index+1,canonical(result),run,activation,node))
            self._event(db,run,'LoopAdvanced',node=node,activation=activation,index=index)
            self.fault('before_loop_advance_commit')
        self.fault('after_loop_advance_commit')
        return {'frame':frame,'next':index+1,'current':result}

    def choose_branch(self, run, principal, node, condition, inputs, *, task_lease=None, control=PUBLIC):
        condition=normalize_bindings({'condition':condition})['condition']
        if type(condition['data']) is not bool:
            raise StateConflict('branch condition must be a Bool value')
        return self._choose_case(run,principal,node,'Then' if condition['data'] else 'Else',condition,inputs,
                                 task_lease=task_lease,control=control)

    def choose_admission_branch(self,run,principal,node,case,condition,inputs,*,task_lease=None,control=PUBLIC):
        if case not in ('Admitted','Returned','NoPermit'):
            raise StateConflict('invalid admission alternative')
        return self._choose_case(run,principal,node,case,condition,inputs,task_lease=task_lease,control=control)

    def choose_variant_branch(self,run,principal,node,condition,inputs,*,task_lease=None,control=PUBLIC):
        condition=normalize_bindings({'condition':condition})['condition']
        if 'variant' not in condition:
            raise StateConflict('Variant selection requires a typed value record')
        return self._choose_case(run,principal,node,condition['data']['tag'],condition,inputs,task_lease=task_lease,control=control)

    def _choose_case(self,run,principal,node,case,condition,inputs,*,task_lease=None,control=PUBLIC):
        identifier(node)
        bindings=normalize_bindings(inputs)
        condition=normalize_bindings({'condition':condition})['condition']
        activation='0' if task_lease is None else task_lease.task
        with self._tx() as db:
            self._run(db,run,principal)
            previous=db.execute('SELECT decision FROM branch_choices WHERE run=? AND activation=? AND node=?',
                                (run,activation,node)).fetchone()
            if previous:
                return json.loads(previous['decision'])
            self._run(db,run,principal,mutable=True)
            self._task_new_work(db,run)
            if task_lease is not None:
                owner=self._task(db,task_lease.task,principal)
                self._lease(owner,task_lease)
                if owner['run']!=run:
                    raise StateConflict('branch worker belongs to another run')
            decision={'case':case,'condition':condition,
                      'inputs':bindings,'control':control.to_data()}
            db.execute('INSERT INTO branch_choices VALUES(?,?,?,?)',(run,activation,node,canonical(decision)))
            self._event(db,run,'BranchChosen',node=node,activation=activation,case=decision['case'])
            self.fault('before_branch_commit')
        self.fault('after_branch_commit')
        return decision
