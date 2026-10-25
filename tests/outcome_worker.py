import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from al0.data import Principal
from al0.store import Store

if __name__=='__main__':
    config=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    def crash(point):
        if point==sys.argv[2]:
            os._exit(77)
    store=Store(config['database'],fault=crash)
    principal=Principal('tenant','subject')
    if config['action']=='take':
        store.take_task_outcome(config['task'],principal)
    else:
        store.spawn_task(config['scope'],principal,'with-input','program',{'units':1},inputs=config['inputs'])
