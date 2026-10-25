"""Independent-process reconciliation recovery test helper."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from al0.data import Principal
from al0.demo import Endpoint, adapters, catalog
from al0.execute import Broker
from al0.store import Store


if __name__ == '__main__':
    config = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    point = sys.argv[2]
    def crash(label):
        if label == point:
            os._exit(77)
    store = Store(config['database'], fault=crash)
    endpoint = Endpoint(config['endpoint'])
    broker = Broker(store, catalog(reconciliation=True), adapters(endpoint,reconciliation=True))
    broker.reconcile(config['run'],Principal('tenant','subject'),'query-1',config['operation'],
                     'repo.query',config['grant'])
