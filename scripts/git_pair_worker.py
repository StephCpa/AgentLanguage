"""Trusted local experiment worker; process separation is not a sandbox."""
import importlib.util
import json
import os
from pathlib import Path
import sys
from threading import Lock

from al0.data import Principal
from al0.execute import Broker, Interpreter, Value
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
    trace_lock = Lock()
    if config.get('revoke_after_authorize') and mode == 'first':
        authorize = store.authorize
        def revoke_after_authorize(*args, **kwargs):
            permit = authorize(*args, **kwargs)
            store.revoke(config['grants'][PUBLISH])
            return permit
        store.authorize = revoke_after_authorize
    def publish(ticket):
        if config['fault'] == 'disconnect_before_push':
            # The host knows this injection precedes Git, but the workflow has
            # only an ambiguous transport failure. A real empty-ref query must
            # not be promoted to ProvenNoEffect or Applied.
            with open(config['fault_trace'], 'a', encoding='utf-8') as stream:
                stream.write(json.dumps({'operation': ticket.operation, 'phase': mode,
                                         'fault': config['fault']})+'\n')
                stream.flush()
                os.fsync(stream.fileno())
            raise ConnectionError('scheduled disconnect before Git push')
        response = adapter.publish(ticket)
        # Independent from Store receipt persistence; flush before process death.
        with trace_lock:
            accepted = Path(config['accepted'])
            ordinal = len(accepted.read_text(encoding='utf-8').splitlines())+1 if accepted.exists() else 1
            with open(config['accepted'], 'a', encoding='utf-8') as stream:
                stream.write(json.dumps({'operation': ticket.operation, 'output': response.output,
                                         'phase': mode, 'fault': config['fault'], 'ordinal': ordinal})+'\n')
                stream.flush()
                os.fsync(stream.fileno())
        inject = mode == 'first' and ordinal == config.get('fault_after', 1)
        if inject and config['fault'] == 'crash_after_push':
            os._exit(77)
        if inject and config['fault'] == 'lost_reply':
            raise ConnectionError('scheduled reply loss after Git accepted publication')
        return response
    callbacks[PUBLISH] = publish
    result = Interpreter(Broker(store, adapter.catalog, callbacks)).run(
        plan, config['run'], who, config['grants'],
        inputs={name: Value(value) for name, value in config.get('inputs', {}).items()})
    Path(config['result']).write_text(json.dumps(result.record()), encoding='utf-8')


if __name__ == '__main__':
    main()
