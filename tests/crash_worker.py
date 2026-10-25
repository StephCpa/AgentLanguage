"""Fresh-process crash cuts. Invoked by test_store, never by production code."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from al0.data import Intent, Principal, Slot
from al0.store import Store


if __name__ == '__main__':
    spec = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    point = sys.argv[2]

    def crash(label):
        if label == point:
            os._exit(77)

    store = Store(spec['database'], clock=lambda: 10, fault=crash)
    principal = Principal('tenant', 'subject')
    if spec['action'] == 'admit':
        value = spec['intent']
        store.admit(spec['run'], principal, Slot('merge'),
                    Intent(value['tool'], value['version'], value['scope'], value['args'], value['receipts']), spec['permit'])
    elif spec['action'] == 'start':
        store.start_attempt(spec['operation'], spec['attempt'], principal)
    elif spec['action'] == 'settle':
        store.settle(spec['attempt'], principal, provider='merge', evidence_id='bill', actual={'cost': 1})
