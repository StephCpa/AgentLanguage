import json
import os
from pathlib import Path
import sys

from al0.data import Principal
from al0.execute import Broker, Interpreter, Response
from al0.store import Store
from tests.test_control import catalog, changing_status_plan


if __name__=='__main__':
    config=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    def fault(point):
        if point==sys.argv[2]:
            os._exit(77)
    def callback(ticket):
        if ticket.intent.value['tool']=='work':
            raise TimeoutError()
        with open(config['log'],'a',encoding='utf-8') as stream:
            stream.write(ticket.intent.value['args']+'\n')
        return Response(ticket.intent.value['args'],{'u':1})
    store=Store(config['database'],fault=fault)
    specs=catalog()
    broker=Broker(store,specs,{name:callback for name in specs.tools})
    result=Interpreter(broker).run(changing_status_plan(specs),config['run'],Principal('tenant','user'),config['grants'])
    print(json.dumps(result.record()))
