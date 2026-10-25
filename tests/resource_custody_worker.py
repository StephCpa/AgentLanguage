"""Exit inside or just after real resource-transfer transactions."""
import json
import os
from pathlib import Path
import sys

from al0.custody import ResourceRef
from al0.data import Principal
from al0.store import Store
from al0.tasks import TaskLease


if __name__ == '__main__':
    config=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    def fault(point):
        if point==config['point']:
            os._exit(77)
    store=Store(config['database'],clock=lambda:10,fault=fault)
    who=Principal('tenant','subject')
    refs={k:ResourceRef(**v) for k,v in config['refs'].items()}
    if config['action']=='spawn':
        with store.resource_access(config['run'],who,refs.values()):
            store.spawn_task(config['scope'],who,'child','host-child',{},caps={'work':config['grant']},moves=refs)
    elif config['action']=='publish':
        lease=TaskLease(config['task'],config['epoch'])
        with store.resource_access(config['run'],who,refs.values(),task_lease=lease):
            store.finish_task(lease,who,{'data':'done','receipts':[]},resources=refs)
    elif config['action']=='cancel':
        store.handoff_task(config['task'],who)
    else:
        with store.resource_access(config['run'],who):
            store.join_task(config['task'],who)
