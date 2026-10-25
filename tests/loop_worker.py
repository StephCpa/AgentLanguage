import json
import os
from pathlib import Path
import sys

from al0.data import Principal
from al0.execute import Broker, Interpreter, Response
from al0.store import Store
from examples.bounded_batches import build, catalog


if __name__=='__main__':
    config=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    def fault(point):
        if point==sys.argv[2]:
            os._exit(77)
    def emit(ticket):
        value=ticket.intent.value['args']
        with open(config['log'],'a',encoding='utf-8') as stream:
            stream.write(str(value)+'\n')
        return Response(value,{'u':1})
    store=Store(config['database'],fault=fault)
    specs=catalog()
    broker=Broker(store,specs,{'emit':emit})
    result=Interpreter(broker).run(build(specs),config['run'],Principal('tenant','user'),config['grants'])
    print(json.dumps(result.record()))
