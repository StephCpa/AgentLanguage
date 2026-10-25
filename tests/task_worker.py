"""Fresh-process cuts in durable task lifecycle transactions."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from al0.data import Principal
from al0.store import Store
from al0.concurrency import TaskRunner
from al0.execute import Broker, Response
from al0.ir import Catalog, Tool


if __name__ == '__main__':
    config = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    point = sys.argv[2]

    def crash(label):
        if label == point:
            os._exit(77)

    store = Store(config['database'], clock=lambda: 10, fault=crash)
    principal = Principal('tenant', 'subject')
    if config['action'] == 'spawn':
        store.spawn_task(config['scope'], principal, 'child', 'child-program', {'cost': 6})
    elif config['action'] == 'close':
        store.close_scope(config['scope'], principal, cancel=True)
    else:
        catalog = Catalog()
        catalog.register(Tool.define('model','infer','Text','Text','model',{'units':1}))
        broker = Broker(store, catalog, {'model':lambda t:Response('recorded',{'units':1})})
        TaskRunner(broker).run(config['task'],principal,config['plan'],config['grants'])
