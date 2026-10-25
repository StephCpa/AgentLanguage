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
    store.select_race(config['scope'],Principal('tenant','subject'),'race',config['tasks'],config['tasks'][0])
