"""Exit after a real Git push, before the runtime can persist its response."""
import json
import os
from pathlib import Path
import sys

from al0.data import Principal
from al0.execute import Broker, Interpreter
from al0.git_adapter import GitPublisher, PUBLISH
from al0.store import Store
from examples.git_publication import build


if __name__ == '__main__':
    config = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    store = Store(config['database'])
    who = Principal('git-tests', 'owner')
    adapter = GitPublisher(config['source'], config['remote'], recipients=['public'], commits=config['approved'])
    def crash(ticket):
        adapter.publish(ticket)
        os._exit(77)
    callbacks = adapter.callbacks(store, who)
    callbacks[PUBLISH] = crash
    Interpreter(Broker(store, adapter.catalog, callbacks)).run(
        build(adapter.catalog, config['commit']), config['run'], who, config['grants'])
