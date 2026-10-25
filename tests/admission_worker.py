import json
import os
from pathlib import Path
import sys

from al0.data import Principal
from al0.execute import Broker, Interpreter, Response
from al0.store import Store
from examples.admission_outcomes import build, catalog


if __name__=='__main__':
    config=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    def fault(point):
        if point==sys.argv[2]:
            os._exit(77)
    def send(ticket):
        with open(config['log'],'a',encoding='utf-8') as stream:
            stream.write(ticket.intent.value['args']+'\n')
        return Response({'sent':True},{'u':1})
    specs=catalog()
    store=Store(config['database'],fault=fault)
    result=Interpreter(Broker(store,specs,{'send':send})).run(build(specs),config['run'],Principal('tenant','user'),config['grants'])
    print(json.dumps(result.record()))
