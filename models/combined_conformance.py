"""Replay a declared coverage corpus against Store and a separate provider DB.

The provider never reads the protocol model or the runtime database. Business
deduplication is per operation; billing is per physical request. Reopening Store
models coordinator recovery in the corpus. Separate subprocess tests force exit.
"""
from contextlib import contextmanager, nullcontext
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import uuid

from al0.data import Intent, Principal, Slot
from al0.errors import AL0Error
from al0.store import Store
from .combined_audit import State, corpus, dispatch_error, ledger, retry_error, successors


class Provider:
    def __init__(self, path, mode):
        self.path, self.mode = path, mode
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS requests(
                    id TEXT PRIMARY KEY, operation TEXT NOT NULL,
                    attempt TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS decisions(
                    operation TEXT PRIMARY KEY, outcome TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS effects(operation TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS fees(request TEXT PRIMARY KEY, amount INTEGER NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def launch(self, operation, attempt):
        # No uniqueness constraint on attempt: an adapter duplicate is a second
        # physical request and can incur a second charge, despite business dedup.
        with self.connect() as db:
            db.execute('INSERT INTO requests VALUES(?,?,?,0)',
                       (str(uuid.uuid4()), operation, attempt))

    def finish(self, attempt):
        with self.connect() as db:
            row = db.execute('SELECT id,operation FROM requests WHERE attempt=? AND done=0 ORDER BY rowid LIMIT 1',
                             (attempt,)).fetchone()
            if row is None:
                raise AssertionError('provider has no inflight request')
            request, operation = row
            outcome = 'Applied' if self.mode == 'apply' else 'ProvenNoEffect'
            db.execute('INSERT OR IGNORE INTO decisions VALUES(?,?)', (operation, outcome))
            if outcome == 'Applied':
                db.execute('INSERT OR IGNORE INTO effects VALUES(?)', (operation,))
            db.execute('INSERT INTO fees VALUES(?,1)', (request,))
            db.execute('UPDATE requests SET done=1 WHERE id=?', (request,))

    def business(self, operation):
        with self.connect() as db:
            row = db.execute('SELECT outcome FROM decisions WHERE operation=?', (operation,)).fetchone()
        if row is None:
            raise AssertionError('provider has no business receipt')
        return row[0]

    def bill(self, attempt):
        with self.connect() as db:
            total, count = db.execute('SELECT SUM(f.amount),COUNT(*) FROM fees f JOIN requests r ON r.id=f.request WHERE r.attempt=?',
                                      (attempt,)).fetchone()
        if not count:
            raise AssertionError('provider has no fee receipt')
        return total

    def snapshot(self):
        with self.connect() as db:
            return {'requests': db.execute('SELECT operation,attempt,done FROM requests ORDER BY rowid').fetchall(),
                    'effects': db.execute('SELECT operation FROM effects').fetchall(),
                    'fees': db.execute('SELECT COALESCE(SUM(amount),0) FROM fees').fetchone()[0]}

    def evidence(self):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            return {table: [dict(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                    for table in ('requests', 'decisions', 'effects', 'fees')}


class Harness:
    who = Principal('combined-audit', 'coordinator')
    intent = Intent('send', '1', 'outbox', {'channel': 'reports'})
    slot = Slot('send')

    def __init__(self, directory, *, create=False, mode='apply', fault=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory/'runtime.sqlite'
        self.store = Store(self.path, clock=lambda: 10, fault=fault)
        self.tickets = {}
        self.running = True
        if create:
            self.store.register_contract('send', '1', {'cost': 1}, retry_window=100)
            run = self.store.create_run(self.who, 'combined-protocol-v1', {'cost': 2})
            root = self.store.grant(run, 'send', 'outbox', expires=1000, max_attempts=2)
            child = self.store.attenuate_capability(run, self.who, 'reports', root,
                                                   constraints={'channel': 'reports'})['grant']
            permit = self.store.authorize(run, self.who, self.intent, child, authorization_key='send')
            operation = self.store.admit(run, self.who, self.slot, self.intent, permit)
            metadata = dict(run=run, root=root, child=child, permit=permit, operation=operation, mode=mode)
            (self.directory/'scenario.json').write_text(json.dumps(metadata), encoding='utf-8')
        self.meta = json.loads((self.directory/'scenario.json').read_text(encoding='utf-8'))
        self.run, self.op = self.meta['run'], self.meta['operation']
        self.provider = Provider(self.directory/'provider.sqlite', self.meta['mode'])
        self.refresh_attempts()

    def refresh_attempts(self):
        self.attempts = [a['id'] for a in self.store.snapshot(self.op, self.who)['attempts']]

    def step(self, action):
        command, _, number = action.partition(':')
        index = int(number) if number else None
        if not self.running and command not in ('endpoint', 'recover'):
            raise AssertionError('coordinator is down')
        if command == 'dispatch':
            self.tickets[index] = self.store.start_attempt(self.op, self.attempts[index], self.who)
        elif command == 'launch':
            ticket = self.tickets.pop(index)
            self.provider.launch(ticket.operation, ticket.attempt)
        elif command == 'endpoint':
            self.provider.finish(self.attempts[index])
        elif command == 'bill':
            attempt = self.attempts[index]
            self.store.settle(attempt, self.who, provider='send', evidence_id='bill-'+attempt,
                              actual={'cost': self.provider.bill(attempt)})
        elif command == 'business':
            self.store.business_result(self.op, self.who, provider='send', evidence_id='business-'+self.op,
                                       outcome=self.provider.business(self.op))
        elif command == 'retry':
            self.store.retry(self.op, self.who)
            self.refresh_attempts()
        elif command == 'revoke':
            self.store.revoke(self.meta['root'])
        elif command == 'cancel':
            self.store.cancel(self.op, self.who)
        elif command == 'crash':
            self.tickets.clear()
            self.running = False
        elif command == 'recover':
            self.store = Store(self.path, clock=lambda: 10)
            self.running = True
            self.refresh_attempts()
        else:
            raise AssertionError(action)

    def verify_oracle(self):
        """Checks deriving from real external records, without model state."""
        actual = self.provider.snapshot()
        b = self.store.budget(self.run, self.who)['cost']
        assert actual['fees'] <= b['reserved'] + b['confirmed'] + b['retired'], ('external cost uncovered', actual, b)
        assert b['total'] == sum(b[k] for k in ('free', 'reserved', 'confirmed', 'retired')), b
        assert len(actual['effects']) <= 1, actual
        assert not actual['effects'] or self.meta['mode'] == 'apply', actual
        snapshot = self.store.snapshot(self.op, self.who)
        by_id = {a['id']: a for a in snapshot['attempts']}
        counts = {}
        for operation, attempt, done in actual['requests']:
            assert operation == self.op and attempt in by_id, actual
            assert by_id[attempt]['phase'] == 'DispatchStarted', actual
            counts[attempt] = counts.get(attempt, 0) + 1
            assert counts[attempt] == 1, ('physical duplicate of one attempt', actual)
        if snapshot['business'] == 'Applied':
            assert actual['effects'] == [(self.op,)], actual
        if snapshot['business'] == 'ProvenNoEffect':
            assert not actual['effects'], actual
        for attempt in by_id.values():
            if attempt['phase'] == 'DispatchStarted' and attempt['actual'] is not None:
                assert json.loads(attempt['actual'])['cost'] == self.provider.bill(attempt['id'])
        return actual, snapshot, b

    def verify(self, expected, *, probes=True):
        actual, snapshot, b = self.verify_oracle()
        assert b == ledger(expected), (b, ledger(expected))
        assert snapshot['business'] == expected.business, (snapshot, expected)
        assert snapshot['cancel_requested'] == expected.cancelled
        assert len(snapshot['attempts']) == len(expected.attempts)
        assert actual['fees'] == sum(a.remote == 'received' for a in expected.attempts)
        assert len(actual['effects']) == int(self.meta['mode'] == 'apply' and actual['fees'] > 0)
        for i, (row, a) in enumerate(zip(snapshot['attempts'], expected.attempts)):
            assert row['phase'] == a.phase, (row, a)
            assert row['billing'] == ('Reserved' if a.held else 'Settled'), (row, a)
            assert row['actual'] == (None if a.held else json.dumps({'cost': int(a.billed)}, separators=(',', ':')))
            requests = [r for r in actual['requests'] if r[1] == row['id']]
            remote = 'absent' if not requests else ('received' if requests[0][2] else 'inflight')
            assert remote == a.remote
            assert (i in self.tickets) == a.ticket
        if not self.running or not probes:
            return 0
        before = len(self.store.events(self.run, self.who))
        # Historical identity must be readable even after revocation/cancellation.
        assert self.store.authorize(self.run, self.who, self.intent, self.meta['child'], authorization_key='send') == self.meta['permit']
        assert self.store.admit(self.run, self.who, self.slot, self.intent, self.meta['permit']) == self.op
        checks = 2
        rejected = [(dispatch_error(expected, i), lambda i=i: self.store.start_attempt(self.op, self.attempts[i], self.who))
                    for i in range(len(self.attempts))]
        rejected.append((retry_error(expected), lambda: self.store.retry(self.op, self.who)))
        for error, call in rejected:
            if error is not None:
                try:
                    call()
                except AL0Error as exc:
                    assert type(exc).__name__ == error, (type(exc).__name__, error)
                else:
                    raise AssertionError('forbidden work was accepted: '+error)
                checks += 1
        # Repeated evidence and repeated cancellation are stuttering steps.
        for i, a in enumerate(expected.attempts):
            if a.billed:
                self.step('bill:'+str(i))
                checks += 1
        if snapshot['business_evidence'] is not None:
            self.step('business')
            checks += 1
        if expected.cancelled:
            self.step('cancel')
            checks += 1
        assert len(self.store.events(self.run, self.who)) == before
        assert self.store.snapshot(self.op, self.who) == snapshot
        assert self.store.budget(self.run, self.who)['cost'] == b
        return checks


def replay(directory, trace, mode):
    h = Harness(directory, create=True, mode=mode)
    state, probes = State(), 0
    probes += h.verify(state)
    for position, action in enumerate(trace):
        following = dict(successors(state, mode=mode))[action]
        try:
            h.step(action)
            probes += h.verify(following)
        except Exception as exc:
            raise AssertionError((mode, position, trace, as_diagnostic(state))) from exc
        state = following
    return {'mode': mode, 'actions': trace, 'passed': True, 'probes': probes,
            'metadata': h.meta, 'runtime': h.store.snapshot(h.op, h.who),
            'budget': h.store.budget(h.run, h.who), 'events': h.store.events(h.run, h.who),
            'provider': h.provider.evidence()}


def as_diagnostic(s):
    return {'business': s.business, 'cancelled': s.cancelled, 'revoked': s.revoked,
            'recovery': s.recovery, 'ledger': ledger(s)}


def run(*, evidence_path=None):
    started = time.monotonic()
    report = {'scope': 'selected model traces against Store; separate provider effects/fees; no full refinement proof',
              'crash_mode': 'Store reopen in corpus; forced process exits tested separately'}
    output = nullcontext(None) if evidence_path is None else Path(evidence_path).open('w', encoding='utf-8')
    with tempfile.TemporaryDirectory() as temp, output as evidence:
        for mode in ('apply', 'reject'):
            traces, coverage = corpus(mode)
            probes = 0
            for i, trace in enumerate(traces):
                record = replay(Path(temp)/f'{mode}-{i}', trace, mode)
                probes += record['probes']
                if evidence is not None:
                    evidence.write(json.dumps(record, separators=(',', ':'))+'\n')
                    evidence.flush()
            report[mode] = {**coverage, 'passed_traces': len(traces), 'replay_rejection_duplicate_probes': probes}
    report['elapsed_seconds'] = round(time.monotonic()-started, 3)
    return report


if __name__ == '__main__':
    report = run(evidence_path=Path(__file__).with_suffix('.jsonl'))
    Path(__file__).with_suffix('.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
