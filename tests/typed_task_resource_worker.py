import json
import os
from pathlib import Path
import sys

from al0.data import Principal
from al0.execute import Broker, Interpreter, Response
from al0.store import Store
from examples.typed_task_resources import build, catalog


if __name__=='__main__':
    config=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    def fault(point):
        if point==sys.argv[2]:
            os._exit(77)
    def send(ticket):
        with open(config['log'],'a',encoding='utf-8') as stream:
            stream.write(ticket.attempt+'\n')
        return Response('sent',{'u':1})
    specs=catalog()
    store=Store(config['database'],fault=fault)
    broker=Broker(store,specs,{'send':send})
    result=Interpreter(broker).run(build(specs),config['run'],Principal('tests','developer'),
                                   config['grants'],recover_tasks=True)
    broker.wait_for_workers(timeout=10)
    print(json.dumps(result.record()))
