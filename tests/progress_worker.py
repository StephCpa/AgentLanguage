import json
import os
from pathlib import Path
import sys

from al0.data import Principal
from al0.execute import Broker, Interpreter, Response
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.inference_results import catalog


def plan(c, task=False):
    b=PlanBuilder('progress',c)
    child=b.finish(b.infer_progress('initial',b.tool('model'),b.literal('prompt','Text','question')))
    if not task:return child
    b=PlanBuilder('progress-parent',c);scope=b.scope('group',{'u':1})
    handle=b.spawn('task',scope,child,{'u':1});result=b.join('result',handle)
    b.close_scope('closed',scope)
    return b.finish(result)


if __name__=='__main__':
    config=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    def fault(point):
        if point==sys.argv[2]:os._exit(77)
    store=Store(config['database'],fault=fault)
    if config.get('migrate'):sys.exit(0)
    def model(ticket):
        with open(config['log'],'a',encoding='utf-8') as stream:
            stream.write(json.dumps({'operation':ticket.operation,'attempt':ticket.attempt})+'\n')
            stream.flush();os.fsync(stream.fileno())
        return Response(None,None,'Pending') if config.get('waiting') else Response('candidate',{'u':1})
    c=catalog();broker=Broker(store,c,{'model':model,'verify':lambda t:Response(True,{'u':1}),
                                    'send':lambda t:Response('sent',{'u':1})})
    result=Interpreter(broker).run(plan(c,config['task']),config['run'],Principal('tests','developer'),
                                   config['grants'],recover_tasks=True)
    broker.wait_for_workers(timeout=10)
    print(json.dumps(result.record()))
