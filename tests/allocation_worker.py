"""Process-exit cuts in allocation transfer transactions."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from al0.data import Principal
from al0.store import Store


if __name__ == '__main__':
    config = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    point = sys.argv[2]

    def crash(label):
        if label == point:
            os._exit(77)

    store = Store(config['database'], fault=crash)
    principal = Principal('tenant', 'subject')
    if config['action'] == 'split':
        store.split_budget(config['run'], principal, 'worker', {'cost': 6})
    else:
        store.join_budget(config['run'], principal, config['allocation'])
