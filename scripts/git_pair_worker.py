"""Trusted local experiment worker; process separation is not a sandbox."""
import importlib.util
import json
import os
from pathlib import Path
import sys

from al0.data import Principal
from al0.execute import Broker, Interpreter
from al0.frontend import read_catalog
from al0.git_adapter import GitPublisher, PUBLISH
from al0.ir import check_plan
from al0.store import Store


def main():
    mode, config_path = sys.argv[1:]
    config = json.loads(Path(config_path).read_text(encoding='utf-8'))
    if mode == 'build':
        sys.path.insert(0, str(Path(config['submission']).parent))
        sys.path.insert(0, str(Path(config['catalog']).parent))
        spec = importlib.util.spec_from_file_location('candidate', config['submission'])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        catalog = read_catalog(Path(config['catalog']).read_text(encoding='utf-8'))
        plan = module.build(catalog, config['commit'])
        # Do not trust a returned CheckedPlan wrapper or candidate fingerprint.
        plan = check_plan(plan.encoded, catalog)
        Path(config['plan']).write_text(plan.encoded, encoding='utf-8')
        return
    store = Store(config['database'])
    who = Principal('paired-publication', 'host')
    adapter = GitPublisher(config['source'], config['remote'], recipients=['public'],
                           commits=config['approved'])
    plan = check_plan(Path(config['plan']).read_text(encoding='utf-8'), adapter.catalog)
    callbacks = adapter.callbacks(store, who)
    def publish(ticket):
        response = adapter.publish(ticket)
        # Independent from Store receipt persistence; flush before process death.
        with open(config['accepted'], 'a', encoding='utf-8') as stream:
            stream.write(json.dumps({'operation': ticket.operation, 'output': response.output,
                                     'phase': mode, 'fault': config['fault']})+'\n')
            stream.flush()
            os.fsync(stream.fileno())
        if mode == 'first' and config['fault'] == 'crash_after_push':
            os._exit(77)
        if mode == 'first' and config['fault'] == 'lost_reply':
            raise ConnectionError('scheduled reply loss after Git accepted publication')
        return response
    callbacks[PUBLISH] = publish
    result = Interpreter(Broker(store, adapter.catalog, callbacks)).run(
        plan, config['run'], who, config['grants'])
    Path(config['result']).write_text(json.dumps(result.record()), encoding='utf-8')


if __name__ == '__main__':
    main()
