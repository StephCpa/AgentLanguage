"""Single-store durable execution kernel.

The trusted host provisions grants/contracts and authenticates Principal.
Untrusted plans never receive this object, SQL access, or receipt-ingress APIs.
SQLite BEGIN IMMEDIATE serializes allocation, admission and current-authority
checks across connections. Each method opens its own connection.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .data import Intent, Principal, Slot, canonical, digest, identifier, quantity, resource_vector
from .allocation import ALLOCATION_SCHEMA, AllocationMixin
from .tasks import TASK_SCHEMA, TaskMixin
from .flow_store import FLOW_SCHEMA, FlowMixin
from .inference import INFERENCE_SCHEMA, InferenceMixin
from .affine_loops import AFFINE_LOOP_SCHEMA, AffineLoopMixin
from .inference_progress import INFERENCE_PROGRESS_SCHEMA, InferenceProgressMixin
from .inference_admission import INFERENCE_ADMISSION_SCHEMA, InferenceAdmissionMixin
from .flow import Label, PUBLIC
from .control import CONTROL_SCHEMA, ControlMixin
from .admission import ADMISSION_SCHEMA, AdmissionMixin, unwrap_admission
from .capabilities import CAPABILITY_SCHEMA, CapabilityMixin
from .custody import CUSTODY_SCHEMA, CustodyMixin
from .errors import (AuthorizationDenied, BudgetExceeded, ContractBreach,
                     ContractError, EvidenceMismatch, InvalidData,
                     PermitConsumed, ReplayMismatch, StateConflict, FlowDenied)


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, subject TEXT NOT NULL,
 program TEXT NOT NULL, halted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS budgets(
 run TEXT NOT NULL REFERENCES runs(id), resource TEXT NOT NULL,
 total INTEGER NOT NULL, free INTEGER NOT NULL, reserved INTEGER NOT NULL DEFAULT 0,
 confirmed TEXT NOT NULL DEFAULT '0', retired INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(run,resource),
 CHECK(total>=0 AND free>=0 AND reserved>=0 AND retired>=0),
 CHECK(length(confirmed)>0 AND confirmed NOT GLOB '*[^0-9]*'));
CREATE TABLE IF NOT EXISTS contracts(
 tool TEXT NOT NULL, version TEXT NOT NULL, description TEXT NOT NULL,
 PRIMARY KEY(tool,version));
CREATE TABLE IF NOT EXISTS grants(
 id TEXT PRIMARY KEY, run TEXT NOT NULL REFERENCES runs(id), tool TEXT NOT NULL,
 scope TEXT NOT NULL, epoch INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
 expires INTEGER NOT NULL, max_attempts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS checks(
 id TEXT PRIMARY KEY, run TEXT NOT NULL REFERENCES runs(id), predicate TEXT NOT NULL,
 checker_version TEXT NOT NULL, artifact TEXT NOT NULL, dependencies TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS permits(
 id TEXT PRIMARY KEY, run TEXT NOT NULL REFERENCES runs(id), intent TEXT NOT NULL,
 grant_id TEXT NOT NULL REFERENCES grants(id), epoch INTEGER NOT NULL,
 expires INTEGER NOT NULL, max_attempts INTEGER NOT NULL, consumed_by TEXT,
 authorization_key TEXT, UNIQUE(run,authorization_key));
CREATE TABLE IF NOT EXISTS operations(
 id TEXT PRIMARY KEY, run TEXT NOT NULL REFERENCES runs(id), node TEXT NOT NULL,
 activation TEXT NOT NULL, intent TEXT NOT NULL, permit TEXT NOT NULL REFERENCES permits(id),
 business TEXT NOT NULL DEFAULT 'NotDispatched', business_evidence TEXT,
 cancelled INTEGER NOT NULL DEFAULT 0, closed INTEGER NOT NULL DEFAULT 0,
 first_started INTEGER, UNIQUE(run,node,activation));
CREATE TABLE IF NOT EXISTS attempts(
 id TEXT PRIMARY KEY, operation TEXT NOT NULL REFERENCES operations(id), seq INTEGER NOT NULL,
 phase TEXT NOT NULL DEFAULT 'Queued', billing TEXT NOT NULL DEFAULT 'Reserved',
 upper_bound TEXT NOT NULL, actual TEXT, final_evidence TEXT,
 UNIQUE(operation,seq));
CREATE TABLE IF NOT EXISTS outbox(
 attempt TEXT PRIMARY KEY REFERENCES attempts(id), accepted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS evidence(
 provider TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(provider,id));
CREATE TABLE IF NOT EXISTS events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT NOT NULL REFERENCES runs(id),
 kind TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reconcilers(
 tool TEXT NOT NULL, version TEXT NOT NULL, target_tool TEXT NOT NULL,
 target_version TEXT NOT NULL, PRIMARY KEY(tool,version),
 FOREIGN KEY(tool,version) REFERENCES contracts(tool,version),
 FOREIGN KEY(target_tool,target_version) REFERENCES contracts(tool,version));
CREATE TABLE IF NOT EXISTS run_status(
 run TEXT PRIMARY KEY REFERENCES runs(id),
 control TEXT NOT NULL CHECK(control IN ('Running','Waiting','Completed','Failed')));
"""


def new_id():
    return str(uuid.uuid4())


@dataclass(frozen=True)
class Dispatch:
    operation: str
    attempt: str
    intent: Intent
    upper_bound_json: str


@dataclass(frozen=True)
class DispatchBlocked:
    reason: str


class Store(AllocationMixin, TaskMixin, FlowMixin, ControlMixin, AdmissionMixin, CapabilityMixin, CustodyMixin, InferenceMixin, AffineLoopMixin, InferenceProgressMixin, InferenceAdmissionMixin):
    def __init__(self, path, *, clock=None, fault=None):
        self.path = str(Path(path).resolve())
        self.clock = clock or (lambda: int(time.time()))
        self.fault = fault or (lambda point: None)
        self._init_custody_context()
        db = self._connect()
        try:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21):
                raise ContractError('unsupported database schema version')
            if version == 0:
                existing = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone()
                if existing:
                    raise ContractError('unversioned database requires an explicit migration')
            if version in (0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20):
                db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA + ALLOCATION_SCHEMA + TASK_SCHEMA + FLOW_SCHEMA + CONTROL_SCHEMA + ADMISSION_SCHEMA + CAPABILITY_SCHEMA + CUSTODY_SCHEMA + INFERENCE_SCHEMA + AFFINE_LOOP_SCHEMA + INFERENCE_PROGRESS_SCHEMA + INFERENCE_ADMISSION_SCHEMA)
                self._migrate_allocations(db)
                self._migrate_attempt_allocations(db)
                db.execute('PRAGMA user_version=21')
                self.fault('before_attempt_funding_migration_commit')
                db.commit()
                self.fault('after_attempt_funding_migration_commit')
        finally:
            db.close()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def _tx(self):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _event(self, db, run, kind, **data):
        db.execute("INSERT INTO events(run,kind,data) VALUES(?,?,?)", (run, kind, canonical(data)))

    def _run(self, db, run, principal, *, mutable=False):
        row = db.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone()
        if not isinstance(principal, Principal) or row is None or (row['tenant'], row['subject']) != (principal.tenant, principal.subject):
            raise AuthorizationDenied("run is unavailable to this principal")
        if mutable and row['halted']:
            raise ContractBreach("run halted following a contract violation")
        return row

    def create_run(self, principal, program, budget, *, run_id=None):
        # Trusted host entry point, not a plan operation.
        identifier(program)
        resource_vector(budget)
        run_id = identifier(run_id) if run_id is not None else new_id()
        with self._tx() as db:
            db.execute("INSERT INTO runs(id,tenant,subject,program) VALUES(?,?,?,?)",
                       (run_id, principal.tenant, principal.subject, program))
            db.executemany("INSERT INTO budgets(run,resource,total,free) VALUES(?,?,?,?)",
                           [(run_id, k, n, n) for k, n in budget.items()])
            self._create_root_allocation(db, run_id, budget)
            self._event(db, run_id, "RunCreated", program=program)
        return run_id

    def register_contract(self, tool, version, upper_bound, *, predicates=(), retry_window=0):
        identifier(tool)
        identifier(version)
        resource_vector(upper_bound)
        quantity(retry_window)
        description = canonical({"upper_bound": upper_bound,
                                 "predicates": [identifier(p) for p in predicates],
                                 "retry_window": retry_window})
        with self._tx() as db:
            old = db.execute("SELECT description FROM contracts WHERE tool=? AND version=?", (tool, version)).fetchone()
            if old and old[0] != description:
                raise ContractError("contract version is immutable; register a new version")
            db.execute("INSERT OR IGNORE INTO contracts VALUES(?,?,?)", (tool, version, description))

    def grant(self, run, tool, scope, *, expires, max_attempts=1):
        quantity(expires)
        quantity(max_attempts)
        if max_attempts < 1:
            raise InvalidData("max_attempts must be positive")
        identifier(tool)
        identifier(scope)
        grant_id = new_id()
        with self._tx() as db:
            db.execute("INSERT INTO grants(id,run,tool,scope,expires,max_attempts) VALUES(?,?,?,?,?,?)",
                       (grant_id, run, tool, scope, expires, max_attempts))
            self._event(db, run, "GrantCreated", grant=grant_id)
        return grant_id

    def register_reconciler(self, tool, version, target_tool, target_version):
        """Trusted immutable route; query output alone cannot grant this authority."""
        for value in (tool, version, target_tool, target_version):
            identifier(value)
        with self._tx() as db:
            old = db.execute('SELECT * FROM reconcilers WHERE tool=? AND version=?', (tool, version)).fetchone()
            if old and (old['target_tool'], old['target_version']) != (target_tool, target_version):
                raise ContractError('reconciliation route version is immutable')
            db.execute('INSERT OR IGNORE INTO reconcilers VALUES(?,?,?,?)', (tool, version, target_tool, target_version))

    def _new_work(self, db, run, intent):
        row = db.execute('SELECT control FROM run_status WHERE run=?', (run,)).fetchone()
        if row and row['control'] in ('Completed', 'Failed'):
            value = intent.value
            route = db.execute('SELECT 1 FROM reconcilers WHERE tool=? AND version=?', (value['tool'], value['version'])).fetchone()
            if route is None:
                raise StateConflict('finished workflow only admits registered reconciliation work')

    def revoke(self, grant):
        with self._tx() as db:
            row = db.execute("SELECT run FROM grants WHERE id=?", (grant,)).fetchone()
            if row is None:
                raise AuthorizationDenied("unknown grant")
            db.execute("UPDATE grants SET active=0,epoch=epoch+1 WHERE id=?", (grant,))
            self._event(db, row['run'], "GrantRevoked", grant=grant)

    def record_check(self, run, principal, predicate, checker_version, artifact, dependencies, *, check_id=None):
        """Trusted checker ingress; never expose directly to model-generated code."""
        identifier(predicate)
        identifier(checker_version)
        receipt = identifier(check_id) if check_id is not None else new_id()
        with self._tx() as db:
            self._run(db, run, principal, mutable=True)
            old = db.execute("SELECT * FROM checks WHERE id=?", (receipt,)).fetchone()
            if old:
                expected = (run, predicate, checker_version, digest(artifact), canonical(dependencies))
                if tuple(old[k] for k in ('run', 'predicate', 'checker_version', 'artifact', 'dependencies')) != expected:
                    raise EvidenceMismatch("checker identity already binds different evidence")
                return receipt
            db.execute("INSERT INTO checks VALUES(?,?,?,?,?,?)",
                       (receipt, run, predicate, checker_version, digest(artifact), canonical(dependencies)))
            self._event(db, run, "CheckPassed", receipt=receipt, predicate=predicate)
        return receipt

    def _contract(self, db, intent):
        value = intent.value
        row = db.execute("SELECT description FROM contracts WHERE tool=? AND version=?", (value['tool'], value['version'])).fetchone()
        if row is None:
            raise ContractError("tool contract is not registered")
        return json.loads(row[0])

    def _evidence(self, db, run, intent, contract):
        value = intent.value
        found = set()
        for receipt in value['receipts']:
            row = db.execute("SELECT * FROM checks WHERE id=? AND run=?", (receipt, run)).fetchone()
            if row is None or row['artifact'] != digest(value['args']):
                raise EvidenceMismatch("check receipt does not match the action artifact")
            found.add(row['predicate'])
        if not set(contract['predicates']) <= found:
            raise EvidenceMismatch("required check receipt missing")

    def validate_input_evidence(self, run, principal, data, receipts, predicates):
        """Check transferred evidence without granting action authority."""
        with self._tx() as db:
            self._run(db,run,principal)
            found=set()
            for receipt in receipts:
                row=db.execute('SELECT * FROM checks WHERE id=? AND run=?',(receipt,run)).fetchone()
                if row is None or row['artifact']!=digest(data):
                    raise EvidenceMismatch('input evidence does not bind this run and artifact')
                found.add(row['predicate'])
            if not set(predicates)<=found:
                raise EvidenceMismatch('checked input lacks its required predicate receipts')

    def _authority(self, db, run, grant, intent, epoch=None):
        value = intent.value
        row=self._grant_chain(db,run,grant,epoch=epoch,args=value['args'],check_args=True)[0][0]
        if row['tool'] != value['tool'] or row['scope'] != value['scope']:
            raise AuthorizationDenied("current grant does not cover this action")
        return row

    def authorize(self, run, principal, intent, grant, *, authorization_key=None, task_lease=None):
        if not isinstance(intent, Intent):
            raise InvalidData("expected immutable intent")
        if authorization_key is not None:
            identifier(authorization_key)
        with self._tx() as db:
            return self._authorize_in_tx(db, run, principal, intent, grant,
                                         authorization_key=authorization_key, task_lease=task_lease)

    def _authorize_in_tx(self, db, run, principal, intent, grant, *, authorization_key=None, task_lease=None):
        self._run(db, run, principal)
        if authorization_key is not None:
            old = db.execute("SELECT * FROM permits WHERE run=? AND authorization_key=?", (run, authorization_key)).fetchone()
            if old:
                if old['intent'] != intent.encoded or old['grant_id'] != grant:
                    raise ReplayMismatch("authorization key already binds another request")
                return old['id']
        self._run(db, run, principal, mutable=True)
        self._custody_actor(db, run, principal, task_lease)
        self._new_work(db, run, intent)
        contract = self._contract(db, intent)
        self._check_flow(db,intent)
        self._evidence(db, run, intent, contract)
        auth = self._authority(db, run, grant, intent)
        permit = new_id()
        db.execute("INSERT INTO permits(id,run,intent,grant_id,epoch,expires,max_attempts,authorization_key) VALUES(?,?,?,?,?,?,?,?)",
                   (permit, run, intent.encoded, grant, auth['epoch'], auth['expires'], auth['max_attempts'], authorization_key))
        db.execute('INSERT INTO permit_origins VALUES(?,?)', (permit, None if task_lease is None else task_lease.task))
        self._custody_created(db, run, 'Permit', permit, task_lease)
        self._event(db, run, "Authorized", permit=permit, intent=intent.digest)
        return permit

    def _reserve(self, db, run, upper, allocation):
        self._allocation(db, run, allocation, open_only=True)
        for resource, amount in upper.items():
            row = db.execute("SELECT free FROM budgets WHERE run=? AND resource=?", (run, resource)).fetchone()
            local = db.execute('SELECT free FROM allocation_balances WHERE allocation=? AND resource=?', (allocation, resource)).fetchone()
            if row is None or row['free'] < amount or local is None or local['free'] < amount:
                raise BudgetExceeded("insufficient budget for the full attempt upper bound")
        for resource, amount in upper.items():
            db.execute("UPDATE budgets SET free=free-?,reserved=reserved+? WHERE run=? AND resource=?",
                       (amount, amount, run, resource))
            db.execute('UPDATE allocation_balances SET free=free-? WHERE allocation=? AND resource=?', (amount, allocation, resource))

    def _attempt(self, db, operation, seq, upper, allocation):
        op = db.execute('SELECT run FROM operations WHERE id=?', (operation,)).fetchone()
        if op is None:
            raise StateConflict('attempt requires an existing operation')
        self._allocation(db, op['run'], allocation, open_only=True)
        attempt = new_id()
        db.execute("INSERT INTO attempts(id,operation,seq,upper_bound) VALUES(?,?,?,?)",
                   (attempt, operation, seq, canonical(upper)))
        db.execute('INSERT INTO attempt_allocations VALUES(?,?)', (attempt, allocation))
        self.fault('after_attempt_funding_insert')
        db.execute("INSERT INTO outbox(attempt) VALUES(?)", (attempt,))
        return attempt

    def admit(self, run, principal, slot, intent, permit, *, allocation=None, task_lease=None):
        if not isinstance(slot, Slot) or not isinstance(intent, Intent):
            raise InvalidData("admission requires a Slot and immutable Intent")
        with self._tx() as db:
            replay=db.execute('SELECT 1 FROM operations WHERE run=? AND node=? AND activation=?',
                              (run,slot.node,slot.activation)).fetchone() is not None
            operation=self._admit_in_tx(db,run,principal,slot,intent,permit,allocation=allocation,task_lease=task_lease)
        if not replay:
            self.fault("after_admit_commit")
        return operation

    def _admit_in_tx(self,db,run,principal,slot,intent,permit,*,allocation=None,task_lease=None):
        self._run(db, run, principal)
        owner = self._allocation(db, run, allocation)
        decision=self._admission_result(db,run,slot,intent,permit,owner['id'])
        if decision is not None:
            return unwrap_admission(decision)
        old = db.execute("SELECT id,intent FROM operations WHERE run=? AND node=? AND activation=?", (run, slot.node, slot.activation)).fetchone()
        if old:
            if old['intent'] != intent.encoded or self._operation_allocation(db, old['id']) != owner['id']:
                raise ReplayMismatch("slot already binds a different descriptor or allocation")
            return old['id']
        self._run(db, run, principal, mutable=True)
        self._new_work(db, run, intent)
        self._task_funding(db, owner['id'], task_lease)
        self._task_effect(db,owner['id'],intent)
        self._check_flow(db,intent)
        if task_lease is not None and slot.activation != task_lease.task:
            raise StateConflict('task operation slot must use its stable task activation')
        self._custody_guard(db, 'Permit', permit, task_lease)
        contract=self._admission_permission(db,run,intent,permit)
        self._reserve(db, run, contract['upper_bound'], owner['id'])
        self.fault("after_reserve")
        operation = new_id()
        db.execute("UPDATE permits SET consumed_by=? WHERE id=?", (operation, permit))
        self._custody_consume(db, 'Permit', permit)
        self.fault("after_permit_consumption")
        db.execute("INSERT INTO operations(id,run,node,activation,intent,permit) VALUES(?,?,?,?,?,?)",
                   (operation, run, slot.node, slot.activation, intent.encoded, permit))
        db.execute('INSERT INTO operation_allocations VALUES(?,?)', (operation, owner['id']))
        self._custody_created(db, run, 'Operation', operation, task_lease)
        self.fault("after_operation_insert")
        attempt = self._attempt(db, operation, 1, contract['upper_bound'], owner['id'])
        self._event(db, run, "Admitted", operation=operation, attempt=attempt, node=slot.node, activation=slot.activation)
        self.fault("before_admit_commit")
        return operation

    def _operation(self, db, operation, principal, *, mutable=False):
        row = db.execute("SELECT * FROM operations WHERE id=?", (operation,)).fetchone()
        if row is None:
            raise AuthorizationDenied("operation is unavailable")
        self._run(db, row['run'], principal, mutable=mutable)
        return row

    @staticmethod
    def _intent(encoded):
        value = json.loads(encoded)
        return Intent(value['tool'], value['version'], value['scope'], value['args'], value['receipts'],label=Label.from_data(value.get('flow')))

    def _current_permission(self, db, op):
        permit = db.execute("SELECT * FROM permits WHERE id=?", (op['permit'],)).fetchone()
        intent = self._intent(op['intent'])
        self._check_flow(db,intent)
        self._authority(db, op['run'], permit['grant_id'], intent, permit['epoch'])
        if permit['expires'] <= self.clock():
            raise AuthorizationDenied("permit expired before dispatch")
        return permit, intent

    def start_attempt(self, operation, attempt, principal, *, task_lease=None):
        return self._start_attempt(operation, attempt, principal, task_lease=task_lease)

    def try_start_attempt(self, operation, attempt, principal, *, task_lease=None, control=PUBLIC):
        return self._start_attempt(operation, attempt, principal, task_lease=task_lease,
                                   control=control, classify_blocked=True)

    def _start_attempt(self, operation, attempt, principal, *, task_lease=None, control=PUBLIC, classify_blocked=False):
        if not isinstance(control, Label):
            raise InvalidData('dispatch control requires a label')
        with self._tx() as db:
            op = self._operation(db, operation, principal, mutable=True)
            self._task_operation(db, operation, task_lease)
            if op['cancelled'] or op['closed']:
                raise StateConflict("operation is closed to new dispatch")
            intent = self._intent(op['intent'])
            self._new_work(db, op['run'], intent)
            self._task_effect(db,self._operation_allocation(db,operation),intent)
            # Preserve the throwing API's historical authorization-first error
            # precedence. Only the checked API classifies a blocked dispatch,
            # so it must validate the attempt before entering that classifier.
            if not classify_blocked:
                self._current_permission(db, op)
            row = db.execute("SELECT * FROM attempts WHERE id=? AND operation=?", (attempt, operation)).fetchone()
            if row is None or row['phase'] != 'Queued' or row['billing'] != 'Reserved':
                raise StateConflict("attempt cannot acquire a second dispatch right")
            try:
                if intent.label.join(control) != intent.label:
                    raise FlowDenied('dispatch has stronger control dependencies than its prepared request')
                if classify_blocked:
                    self._current_permission(db, op)
            except (AuthorizationDenied, FlowDenied) as error:
                if not classify_blocked:
                    raise
                ticket = DispatchBlocked('FlowDenied' if isinstance(error, FlowDenied) else 'AuthorizationUnavailable')
            else:
                db.execute("UPDATE attempts SET phase='DispatchStarted' WHERE id=?", (attempt,))
                db.execute("UPDATE outbox SET accepted=1 WHERE attempt=?", (attempt,))
                db.execute("UPDATE operations SET business='Unknown', first_started=COALESCE(first_started,?) WHERE id=?",
                           (self.clock(), operation))
                self._event(db, op['run'], "DispatchStarted", operation=operation, attempt=attempt)
                ticket = Dispatch(operation, attempt, intent, row['upper_bound'])
        if isinstance(ticket, DispatchBlocked):
            self.fault('after_dispatch_blocked')
            return ticket
        self.fault("after_dispatch_commit")
        return ticket

    def retry(self, operation, principal, *, task_lease=None, allocation=None):
        with self._tx() as db:
            op = self._operation(db, operation, principal, mutable=True)
            self._task_operation(db, operation, task_lease)
            if op['cancelled'] or op['closed'] or op['business'] not in ('Unknown', 'Pending'):
                raise StateConflict("only an unresolved open operation can be retried")
            permit, intent = self._current_permission(db, op)
            self._task_effect(db,self._operation_allocation(db,operation),intent)
            self._new_work(db, op['run'], intent)
            contract = self._contract(db, intent)
            if not contract['retry_window'] or op['first_started'] is None or self.clock() >= op['first_started'] + contract['retry_window']:
                raise ContractError("retry requires a currently valid endpoint deduplication contract")
            rows = db.execute("SELECT phase FROM attempts WHERE operation=?", (operation,)).fetchall()
            if any(r['phase'] == 'Queued' for r in rows):
                raise StateConflict("an attempt is already queued")
            if len(rows) >= permit['max_attempts']:
                raise AuthorizationDenied("attempt limit reached")
            funding = self._operation_allocation(db, operation) if allocation is None else allocation
            self._task_funding(db, funding, task_lease)
            self._reserve(db, op['run'], contract['upper_bound'], funding)
            attempt = self._attempt(db, operation, len(rows) + 1, contract['upper_bound'], funding)
            self._event(db, op['run'], "RetryAdmitted", operation=operation, attempt=attempt)
        return attempt

    def _release_queued(self, db, op):
        for row in db.execute("SELECT * FROM attempts WHERE operation=? AND phase='Queued'", (op['id'],)).fetchall():
            for resource, upper in json.loads(row['upper_bound']).items():
                db.execute("UPDATE budgets SET reserved=reserved-?,free=free+? WHERE run=? AND resource=?",
                           (upper, upper, op['run'], resource))
            self._credit_allocation(db, op['run'], self._attempt_allocation(db, row['id']), json.loads(row['upper_bound']))
            db.execute("UPDATE attempts SET phase='Closed',billing='Settled',actual=? WHERE id=?",
                       (canonical({k: 0 for k in json.loads(row['upper_bound'])}), row['id']))
            db.execute("DELETE FROM outbox WHERE attempt=?", (row['id'],))

    def cancel(self, operation, principal, *, task_lease=None):
        with self._tx() as db:
            op = self._operation(db, operation, principal)
            self._custody_guard(db, 'Operation', operation, task_lease)
            self._cancel_operation(db, op)

    def _cancel_operation(self, db, op):
        if op['cancelled']:
            return
        self._release_queued(db, op)
        sent = db.execute("SELECT 1 FROM attempts WHERE operation=? AND phase='DispatchStarted'", (op['id'],)).fetchone()
        business = op['business'] if sent or op['business'] in ('Applied', 'ProvenNoEffect') else 'ProvenNoEffect'
        db.execute("UPDATE operations SET cancelled=1,closed=1,business=? WHERE id=?", (business, op['id']))
        self._event(db, op['run'], "CancelRequested", operation=op['id'])

    def _record_evidence(self, db, provider, evidence_id, payload):
        identifier(provider)
        identifier(evidence_id)
        old = db.execute("SELECT payload FROM evidence WHERE provider=? AND id=?", (provider, evidence_id)).fetchone()
        encoded = canonical(payload)
        if old:
            return "same" if old[0] == encoded else "conflict"
        db.execute("INSERT INTO evidence VALUES(?,?,?)", (provider, evidence_id, encoded))
        return "new"

    def _halt(self, db, run, reason):
        db.execute("UPDATE runs SET halted=1 WHERE id=?", (run,))
        self._event(db, run, "ContractBreach", reason=reason)

    def business_result(self, operation, principal, *, provider, evidence_id, outcome, details=None):
        """Trusted adapter ingress. ProvenNoEffect must exclude future effects."""
        if outcome not in ('Applied', 'ProvenNoEffect', 'Pending'):
            raise InvalidData("unsupported business evidence outcome")
        breach = None
        with self._tx() as db:
            op = self._operation(db, operation, principal)
            intent = self._intent(op['intent'])
            if provider != intent.value['tool']:
                raise EvidenceMismatch("evidence provider does not match operation")
            if op['first_started'] is None:
                raise StateConflict("undispatched operation cannot receive remote evidence")
            payload = {"kind": "business", "operation": operation, "intent": intent.digest,
                       "outcome": outcome, "details": details}
            status = self._record_evidence(db, provider, evidence_id, payload)
            if status == 'conflict':
                breach = "evidence identifier was reused with different content"
            elif status == 'same':
                return
            elif op['business'] in ('Applied', 'ProvenNoEffect'):
                if outcome == 'Pending':
                    # A delayed nonterminal observation cannot undo a terminal fact.
                    self._event(db, op['run'], "StalePendingEvidence", operation=operation)
                elif outcome != op['business']:
                    breach = "business evidence contradicts a terminal decision"
                elif op['business_evidence'] != canonical(payload):
                    breach = "terminal receipt content changed"
            else:
                terminal = outcome != 'Pending'
                if terminal:
                    self._release_queued(db, op)
                db.execute("UPDATE operations SET business=?,business_evidence=?,closed=MAX(closed,?) WHERE id=?",
                           (outcome, canonical(payload), int(terminal), operation))
                self._event(db, op['run'], "BusinessEvidence", operation=operation, outcome=outcome)
            if breach:
                self._halt(db, op['run'], breach)
        if breach:
            raise ContractBreach(breach)

    def settle(self, attempt, principal, *, provider, evidence_id, actual):
        """Trusted final billing ingress. A partial bill is not admissible here."""
        resource_vector(actual)
        breach = None
        with self._tx() as db:
            row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt,)).fetchone()
            if row is None:
                raise EvidenceMismatch("unknown attempt")
            op = self._operation(db, row['operation'], principal)
            if provider != self._intent(op['intent']).value['tool']:
                raise EvidenceMismatch("billing provider mismatch")
            upper = json.loads(row['upper_bound'])
            if set(actual) != set(upper):
                raise EvidenceMismatch("final billing must cover exactly the declared resource dimensions")
            if row['phase'] != 'DispatchStarted':
                raise StateConflict("remote bill requires an exposed attempt")
            payload = {"kind": "billing", "attempt": attempt, "actual": actual}
            status = self._record_evidence(db, provider, evidence_id, payload)
            if status == 'same':
                return
            if status == 'conflict':
                breach = "billing evidence identifier conflict"
            elif row['actual'] is not None:
                if row['actual'] != canonical(actual):
                    breach = "conflicting final billing amount"
            else:
                excessive = any(actual[k] > upper[k] for k in upper)
                if row['billing'] == 'Reserved':
                    for resource, amount in upper.items():
                        cost = actual[resource]
                        account = db.execute('SELECT confirmed FROM budgets WHERE run=? AND resource=?', (op['run'], resource)).fetchone()
                        # Exact decimal storage prevents SQLite integer overflow from
                        # rounding the actual spend after a provider contract breach.
                        confirmed = int(account['confirmed']) + cost
                        db.execute("UPDATE budgets SET reserved=reserved-?,confirmed=?,free=free+? WHERE run=? AND resource=?",
                                   (amount, str(confirmed), max(0, amount-cost), op['run'], resource))
                    self._credit_allocation(db, op['run'], self._attempt_allocation(db, attempt),
                                            {k: max(0, upper[k]-actual[k]) for k in upper})
                    db.execute("UPDATE attempts SET billing='Settled',actual=?,final_evidence=? WHERE id=?",
                               (canonical(actual), evidence_id, attempt))
                elif row['billing'] == 'Retired':
                    # Retired credit is never reissued, even after a late bill.
                    db.execute("UPDATE attempts SET actual=?,final_evidence=? WHERE id=?", (canonical(actual), evidence_id, attempt))
                if excessive:
                    breach = "provider charge exceeds reserved upper bound"
                self._event(db, op['run'], "BillingEvidence", attempt=attempt, actual=actual)
            if breach:
                self._halt(db, op['run'], breach)
            self.fault("before_settlement_commit")
        self.fault("after_settlement_commit")
        if breach:
            raise ContractBreach(breach)

    def retire(self, attempt, principal):
        with self._tx() as db:
            row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt,)).fetchone()
            if row is None:
                raise StateConflict("unknown attempt")
            op = self._operation(db, row['operation'], principal)
            if row['phase'] != 'DispatchStarted':
                raise StateConflict("queued attempts should be cancelled instead")
            if row['billing'] != 'Reserved':
                return
            for resource, upper in json.loads(row['upper_bound']).items():
                db.execute("UPDATE budgets SET reserved=reserved-?,retired=retired+? WHERE run=? AND resource=?",
                           (upper, upper, op['run'], resource))
            db.execute("UPDATE attempts SET billing='Retired' WHERE id=?", (attempt,))
            self._event(db, op['run'], "ReservationRetired", attempt=attempt)

    def snapshot(self, operation, principal):
        with self._tx() as db:
            op = self._operation(db, operation, principal)
            attempts = [dict(r) for r in db.execute("SELECT * FROM attempts WHERE operation=? ORDER BY seq", (operation,))]
            billing = {"confirmed": {}, "reserved": {}, "retired": {}}
            for row in attempts:
                key = {"Reserved": "reserved", "Settled": "confirmed", "Retired": "retired"}[row['billing']]
                vector = json.loads(row['actual']) if key == 'confirmed' else json.loads(row['upper_bound'])
                for resource, amount in vector.items():
                    billing[key][resource] = billing[key].get(resource, 0) + amount
            billing['attempts_closed'] = bool(op['closed']) and all(a['billing'] != 'Reserved' for a in attempts)
            return {"operation": operation, "business": op['business'], "cancel_requested": bool(op['cancelled']),
                    "billing": billing, "attempts": attempts,
                    "business_evidence": json.loads(op['business_evidence']) if op['business_evidence'] else None}

    def program(self, run, principal):
        with self._tx() as db:
            return self._run(db, run, principal)['program']

    def operation_info(self, operation, principal):
        with self._tx() as db:
            op = self._operation(db, operation, principal)
            return {'run': op['run'], 'intent': json.loads(op['intent']), 'allocation': self._operation_allocation(db, operation),
                    'slot': {'node': op['node'], 'activation': op['activation']}}

    def _obligations(self, db, run):
        result = []
        for op in db.execute('SELECT * FROM operations WHERE run=? ORDER BY rowid', (run,)).fetchall():
            attempts = db.execute('SELECT * FROM attempts WHERE operation=? ORDER BY seq', (op['id'],)).fetchall()
            billing_pending = [a['id'] for a in attempts if a['billing'] == 'Reserved']
            retired_unverified = [a['id'] for a in attempts if a['billing'] == 'Retired' and a['actual'] is None]
            unresolved = op['business'] not in ('Applied', 'ProvenNoEffect')
            if unresolved or billing_pending or retired_unverified:
                result.append({'operation': op['id'], 'business': op['business'],
                               'tool': json.loads(op['intent'])['tool'],
                               'unresolved_business': unresolved,
                               'billing_pending': billing_pending,
                               'retired_unverified': retired_unverified,
                               'cancel_requested': bool(op['cancelled'])})
        return result

    def obligations(self, run, principal):
        """Read a consistent local snapshot; this performs no provider queries."""
        with self._tx() as db:
            self._run(db, run, principal)
            return self._obligations(db, run)

    def finish_run(self, run, principal, *, outcome='Completed'):
        """Mark control-flow completion without erasing external obligations.

        Already-admitted queued operations remain executable. Cancellation is
        explicit. After this marker, new ordinary admissions/retries are denied;
        separately authorized, budgeted reconciliation can continue.
        """
        if outcome not in ('Completed', 'Failed'):
            raise InvalidData('terminal control outcome must be Completed or Failed')
        with self._tx() as db:
            self._run(db, run, principal)
            old = db.execute('SELECT control FROM run_status WHERE run=?', (run,)).fetchone()
            if old and old['control'] in ('Completed', 'Failed'):
                if old['control'] != outcome:
                    raise StateConflict('workflow control outcome is already terminal')
                return
            if db.execute("SELECT 1 FROM tasks WHERE run=? AND disposition='Attached'", (run,)).fetchone():
                raise StateConflict('run still owns unjoined task or scope control handles')
            db.execute('INSERT INTO run_status VALUES(?,?) ON CONFLICT(run) DO UPDATE SET control=excluded.control', (run, outcome))
            self._event(db, run, 'ControlFinished', outcome=outcome)

    def status(self, run, principal):
        with self._tx() as db:
            owner = self._run(db, run, principal)
            row = db.execute('SELECT control FROM run_status WHERE run=?', (run,)).fetchone()
            control = row['control'] if row else 'Running'
            pending = self._obligations(db, run)
            if owner['halted']:
                status = 'HaltedWithObligations' if pending else 'Halted'
            elif control == 'Completed':
                status = 'CompletedWithObligations' if pending else 'CompletedClean'
            elif control == 'Failed':
                status = 'FailedWithObligations' if pending else 'Failed'
            else:
                status = control
            return {'control': control, 'status': status, 'obligations': pending}

    def budget(self, run, principal):
        with self._tx() as db:
            self._run(db, run, principal)
            return {r['resource']: {k: int(r[k]) for k in ('total','free','reserved','confirmed','retired')}
                    for r in db.execute("SELECT * FROM budgets WHERE run=? ORDER BY resource", (run,))}

    def events(self, run, principal):
        with self._tx() as db:
            self._run(db, run, principal)
            return [{"sequence": r['seq'], "kind": r['kind'], "data": json.loads(r['data'])}
                    for r in db.execute("SELECT * FROM events WHERE run=? ORDER BY seq", (run,))]
