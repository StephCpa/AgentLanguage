import json
import os
from pathlib import Path
import sys

from al0.data import Principal
from al0.execute import Broker, Interpreter, Response, Value
from al0.store import Store
from examples.dynamic_budgets import build, catalog


if __name__=='__main__':
    config=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    hits=0
    def fault(point):
        global hits
        if point==sys.argv[2]:
            hits+=1
            if hits==int(sys.argv[3]):
                os._exit(77)
    def emit(ticket):
        with open(config['log'],'a',encoding='utf-8') as stream:
            stream.write(ticket.intent.value['args']+'\n')
        return Response(ticket.intent.value['args'],{'u':1})
    specs=catalog()
    store=Store(config['database'],fault=fault)
    broker=Broker(store,specs,{'emit':emit})
    result=Interpreter(broker).run(build(specs),config['run'],Principal('tenant','user'),config['grants'],
                                   inputs={'units':Value(2)},recover_tasks=True)
    broker.wait_for_workers(timeout=10)
    print(json.dumps(result.record()))
