import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from al0.data import Principal
from al0.execute import Broker, Interpreter, Response
from al0.store import Store
from tests.test_task_ir import catalog

if __name__=='__main__':
    config=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    def crash(point):
        if point==sys.argv[2]:
            os._exit(77)
    store=Store(config['database'],fault=crash)
    broker=Broker(store,catalog(),{'model':lambda t:Response('recorded-child',{'units':1}),
                                 'log':lambda t:Response('unused',{'units':1})})
    Interpreter(broker).run(config['plan'],config['run'],Principal('tenant','subject'),config['grants'])
