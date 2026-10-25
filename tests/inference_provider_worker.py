import json
import os
from pathlib import Path
import sys

from al0.data import Principal
from al0.execute import Broker, Interpreter, Response, InferenceResponse
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.inference_results import catalog


def plan(c, method='try_infer', task=False):
    b=PlanBuilder('provider',c);prompt=b.literal('prompt','Text','question')
    child=b.finish(getattr(b,method)('result',b.tool('model'),prompt))
    if not task:return child
    b=PlanBuilder('provider-parent',c);scope=b.scope('group',{'u':1})
    handle=b.spawn('task',scope,child,{'u':1});result=b.join('result',handle)
    b.close_scope('closed',scope)
    return b.finish(result)


if __name__=='__main__':
    config=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    def fault(point):
        if point=='after_inference_admission_commit' and config.get('blocked'):
            store.revoke(config['grants']['model'])
        if point==sys.argv[2]:os._exit(77)
    store=Store(config['database'],fault=fault)
    def model(ticket):
        with open(config['log'],'a',encoding='utf-8') as stream:
            stream.write(json.dumps({'operation':ticket.operation,'attempt':ticket.attempt,'cost':1})+'\n')
            stream.flush();os.fsync(stream.fileno())
        return InferenceResponse('Refused',actual={'u':1})
    c=catalog();broker=Broker(store,c,{'model':model,'verify':lambda t:Response(True,{'u':1}),'send':lambda t:Response('sent',{'u':1})})
    result=Interpreter(broker).run(plan(c,config['method'],config['task']),config['run'],Principal('tests','developer'),config['grants'],recover_tasks=True)
    broker.wait_for_workers(timeout=10)
    print(json.dumps(result.record()))
