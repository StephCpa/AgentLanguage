"""Process crash/recovery probe for persisted flow labels and task outcomes."""
import json
import os
from pathlib import Path
import sys

from al0.data import Principal
from al0.execute import Broker, Interpreter, Response
from al0.store import Store
from tests.test_flow import catalog, outcome_plan


if __name__=='__main__':
    config=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    mode=sys.argv[2]
    def fault(point):
        if point==mode:
            os._exit(77)
    def callback(ticket):
        if mode=='recover':
            raise AssertionError('recovery must reuse the persisted result')
        return Response('private-value',{'u':1})
    store=Store(config['database'],fault=fault)
    specs=catalog()
    broker=Broker(store,specs,{name:callback for name in specs.tools})
    result=Interpreter(broker).run(outcome_plan(specs),config['run'],Principal('tenant','user'),config['grants'],recover_tasks=True)
    broker.wait_for_workers(timeout=10)
    print(json.dumps(result.record()))
