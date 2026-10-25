import json
import os
from pathlib import Path
import sys
from al0.data import Principal
from al0.execute import Broker, Interpreter, Response
from al0.store import Store
from examples.business_variants import build,catalog


if __name__=='__main__':
    config=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    seen=0
    def fault(point):
        global seen
        if point==sys.argv[2]:
            seen+=1
            if seen==int(sys.argv[3]):os._exit(77)
    def send(ticket):
        with open(config['log'],'a',encoding='utf-8') as stream:stream.write(json.dumps([ticket.attempt,'send'])+'\n')
        return Response('sent',{'u':1})
    store=Store(config['database'],fault=fault)
    specs=catalog();broker=Broker(store,specs,{'send':send})
    result=Interpreter(broker).run(build(specs,funded=config['funded']),config['run'],Principal('tests','developer'),config['grants'],recover_tasks=True)
    broker.wait_for_workers(timeout=10)
    print(json.dumps(result.record()))
