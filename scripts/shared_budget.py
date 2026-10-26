"""One transactional USD ledger shared by prospective task journals."""
import json
from pathlib import Path
import sqlite3
import uuid
import time

from al0.data import digest


def legacy_rows(folder):
    with sqlite3.connect((Path(folder)/'journal.sqlite').absolute().as_uri()+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute('SELECT id,upper_usd,cost_usd,receipt FROM rounds ORDER BY id')]


def legacy_digest(rows):
    # Include receipts so a later bill/usage change cannot silently escape the
    # imported liability. This is a frozen import, not a two-way sync.
    return digest(json.dumps(rows, sort_keys=True))


def initialize(path, total_usd, *, legacy_journals=()):
    from scripts.generation_journal import money
    path = Path(path).absolute()
    if money(total_usd) <= 0:
        raise ValueError('positive shared authorization required')
    imported = [(str(Path(p).absolute()), legacy_rows(p)) for p in legacy_journals]
    charges = [r for _, rows in imported for r in rows]
    if len({r['id'] for r in charges}) != len(charges):
        raise ValueError('duplicate historical request import')
    used = sum((money(r['cost_usd'] if r['cost_usd'] is not None else r['upper_usd']) for r in charges), money('0'))
    if used > money(total_usd):
        raise ValueError('historical liabilities exceed authorization')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb'):
        pass
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE ledger_meta(id TEXT, total_usd TEXT);
            CREATE TABLE charges(request_id TEXT PRIMARY KEY, journal TEXT, upper_usd TEXT, cost_usd TEXT);
            CREATE TABLE imports(journal TEXT PRIMARY KEY, snapshot_digest TEXT);
            CREATE TABLE import_updates(journal TEXT, previous_digest TEXT, current_digest TEXT, changes TEXT, recorded_at REAL);''')
        db.execute('INSERT INTO ledger_meta VALUES (?,?)', (str(uuid.uuid4()), total_usd))
        for folder, rows in imported:
            db.execute('INSERT INTO imports VALUES (?,?)', (folder, legacy_digest(rows)))
            db.executemany('INSERT INTO charges VALUES (?,?,?,?)',
                          [(r['id'], folder, r['upper_usd'], r['cost_usd']) for r in rows])
    return inspect(path)


def inspect(path):
    from scripts.generation_journal import money
    with sqlite3.connect(Path(path).absolute().as_uri()+'?mode=ro', uri=True) as db:
        ledger_id, total = db.execute('SELECT id,total_usd FROM ledger_meta').fetchone()
        rows = db.execute('SELECT upper_usd,cost_usd FROM charges').fetchall()
    committed = sum((money(cost if cost is not None else upper) for upper, cost in rows), money('0'))
    return {'ledger_id': ledger_id, 'total_usd': total, 'committed_usd': str(committed),
            'remaining_usd': str(money(total)-committed), 'requests': len(rows),
            'actual_total_usd': str(committed) if all(cost is not None for _, cost in rows) else None}


def attach(db, config):
    path = Path(config['shared_ledger'])
    if not path.is_file():
        raise ValueError('shared ledger missing; cannot reset authorization')
    db.execute('ATTACH DATABASE ? AS budget', (str(path),))
    row = db.execute('SELECT id FROM budget.ledger_meta').fetchone()
    if row is None or row[0] != config['shared_ledger_id']:
        raise ValueError('shared ledger identity changed')
    if any(db.execute('PRAGMA '+schema+'.journal_mode').fetchone()[0] != 'delete' for schema in ('main', 'budget')):
        raise ValueError('attached budget transactions require rollback journals')


def require_history(path, request_ids):
    with sqlite3.connect(Path(path).absolute().as_uri()+'?mode=ro', uri=True) as db:
        existing = {r[0] for r in db.execute('SELECT request_id FROM charges')}
    if not set(request_ids) <= existing:
        raise ValueError('shared ledger must carry all six historical provider requests before new tasks')


def reconcile_history(path, journal):
    """Explicitly import late liability updates without replacing the ledger."""
    from scripts.generation_journal import money
    journal = str(Path(journal).absolute())
    rows = legacy_rows(journal)
    with sqlite3.connect(Path(path).absolute().as_uri()+'?mode=rw', uri=True) as db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT snapshot_digest FROM imports WHERE journal=?', (journal,)).fetchone()
        if previous is None:
            raise ValueError('journal was not imported into this ledger')
        original = {r[0]: {'upper_usd': r[1], 'cost_usd': r[2]} for r in db.execute(
            'SELECT request_id,upper_usd,cost_usd FROM charges WHERE journal=?', (journal,))}
        if set(original) != {r['id'] for r in rows}:
            raise ValueError('historical request set changed; do not silently import new calls')
        changes = []
        for row in rows:
            old = original[row['id']]
            if (money(row['upper_usd']) > money(old['upper_usd']) or
                    old['cost_usd'] is not None and row['cost_usd'] != old['cost_usd']):
                raise ValueError('historical bound or settled bill changed incompatibly')
            changes.append({'request_id': row['id'], 'before': old,
                            'after': {'upper_usd': row['upper_usd'], 'cost_usd': row['cost_usd']}})
            db.execute('UPDATE charges SET upper_usd=?,cost_usd=? WHERE request_id=?',
                       (row['upper_usd'], row['cost_usd'], row['id']))
        current = legacy_digest(rows)
        # CREATE IF NOT EXISTS supports the first local ledger created before
        # this optional history-sync table existed; it never resets charges.
        db.execute('CREATE TABLE IF NOT EXISTS import_updates(journal TEXT, previous_digest TEXT, current_digest TEXT, changes TEXT, recorded_at REAL)')
        if previous[0] != current:
            db.execute('INSERT INTO import_updates VALUES (?,?,?,?,?)',
                       (journal, previous[0], current, json.dumps(changes), time.time()))
            db.execute('UPDATE imports SET snapshot_digest=? WHERE journal=?', (current, journal))
    return inspect(path)


def reserve(db, folder, request_id, upper):
    from scripts.generation_journal import money
    for row in db.execute('SELECT journal,snapshot_digest FROM budget.imports'):
        if legacy_digest(legacy_rows(row[0])) != row[1]:
            raise ValueError('imported historical liability changed; reconcile ledger before spending')
    total = money(db.execute('SELECT total_usd FROM budget.ledger_meta').fetchone()[0])
    if any(r[1] is not None and money(r[1]) > money(r[0]) for r in db.execute('SELECT upper_usd,cost_usd FROM budget.charges')):
        raise ValueError('shared request cost bound exceeded')
    used = sum((money(r[1] if r[1] is not None else r[0]) for r in db.execute(
        'SELECT upper_usd,cost_usd FROM budget.charges')), money('0'))
    if used+money(upper) > total:
        raise ValueError('shared budget unavailable')
    db.execute('INSERT INTO budget.charges VALUES (?,?,?,NULL)', (request_id, str(Path(folder).absolute()), upper))


def update(db, request_id, *, upper=None, cost=None):
    if upper is not None:
        changed = db.execute('UPDATE budget.charges SET upper_usd=? WHERE request_id=?', (upper, request_id))
    else:
        changed = db.execute('UPDATE budget.charges SET cost_usd=? WHERE request_id=?', (cost, request_id))
    if changed.rowcount != 1:
        raise ValueError('request has no shared reservation')


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', required=True, type=Path)
    sub = parser.add_subparsers(dest='mode', required=True)
    command = sub.add_parser('init')
    command.add_argument('--total-usd', required=True)
    command.add_argument('--legacy-journal', dest='legacy_journals', action='append', type=Path, default=[])
    sub.add_parser('inspect')
    command = sub.add_parser('reconcile-history')
    command.add_argument('--journal', required=True, type=Path)
    args = vars(parser.parse_args())
    mode, path = args.pop('mode'), args.pop('ledger')
    result = initialize(path, **args) if mode == 'init' else reconcile_history(path, **args) if mode == 'reconcile-history' else inspect(path)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
