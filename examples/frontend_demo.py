"""Execute source through the shared interpreter, then resume from SDK IR."""
import json
from pathlib import Path
import tempfile

from al0.data import Principal
from al0.demo import Endpoint, adapters, catalog
from al0.execute import Broker, Interpreter
from al0.frontend import compile_source
from al0.store import Store
from examples.repair_sdk import build


def demo(directory):
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    source=Path(__file__).with_name('repair.al0')
    specs=catalog()
    compiled=compile_source(source.read_text(encoding='utf-8'),specs,filename=str(source))
    sdk=build(specs)
    if compiled.plan.encoded!=sdk.encoded:
        raise AssertionError('source and SDK must lower to identical IR')
    store=Store(directory/'runtime.sqlite')
    endpoint=Endpoint(directory/'endpoint.sqlite')
    who=Principal('example','developer')
    run=store.create_run(who,compiled.plan.fingerprint,{'units':10})
    grants={n:store.grant(run,n,s.scope,expires=store.clock()+1000) for n,s in specs.tools.items()}
    executor=Interpreter(Broker(store,specs,adapters(endpoint)))
    result=executor.run(compiled.plan,run,who,grants)
    count=len(store.events(run,who))
    replay=executor.run(sdk,run,who,grants)
    return {'result':result.data,'sdk_replay_result':replay.data,'identical_ir':True,
            'replay_added_events':len(store.events(run,who))-count,
            'budget':store.budget(run,who),'endpoint':endpoint.state()}


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as directory:
        print(json.dumps(demo(directory),ensure_ascii=False,indent=2))
