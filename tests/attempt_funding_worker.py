"""Physical process exits at funding creation and migration commit boundaries."""
import json
import os
from pathlib import Path
import sys

from al0.data import Intent, Principal, Slot
from al0.store import Store


if __name__ == '__main__':
    config = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))

    def crash(point):
        if point == config['point']:
            os._exit(77)

    store = Store(config['database'], clock=lambda: 10, fault=crash)
    principal = Principal('tenant', 'subject')
    if config['action'] == 'admit':
        store.admit(config['run'], principal, Slot('work'), Intent('work', '1', 'scope', {'x': 1}),
                    config['permit'], allocation=config['allocation'])
    elif config['action'] == 'retry':
        store.retry(config['operation'], principal)
