import json
import os
from pathlib import Path
import sys

from al0.data import Principal
from al0.store import Store
from al0.execute import Broker, Interpreter, Response
from examples.resource_task_outcomes import build, catalog, race_plan


if __name__=='__main__':
    config=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    def fault(point):
        if point==sys.argv[2]:os._exit(77)
    store=Store(config['database'],fault=fault)
    who=Principal('tests','developer')
    if config['action']=='execute':
        specs=catalog()
        def send(ticket):
            with open(config['log'],'a',encoding='utf-8') as stream:
                stream.write(json.dumps([ticket.attempt,ticket.intent.value['args']])+'\n')
            return Response('sent',{'u':1})
        broker=Broker(store,specs,{'send':send})
        plan=build(specs) if config['mode']=='outcome' else race_plan(specs)
        result=Interpreter(broker).run(plan,config['run'],who,config['grants'],recover_tasks=True)
        broker.wait_for_workers(timeout=10)
        print(json.dumps(result.record()))
        sys.exit(0)
    with store.resource_access(config['run'],who):
        if config['action']=='race':
            store.select_race(config['scope'],who,'race',config['tasks'],config['tasks'][0])
        else:
            store.take_task_outcome(config['tasks'][0],who,cancel=config['action']=='cancel')
