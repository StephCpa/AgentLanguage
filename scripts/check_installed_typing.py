"""Check a built wheel's type information from outside the source checkout."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
CONSUMER = '''from al0.ir import Catalog, CheckedPlan
from al0.sdk import PlanBuilder, Ref

def plan() -> CheckedPlan[str]:
    b = PlanBuilder('child', Catalog())
    return b.finish(b.literal('message', 'Text', 'hello'))

b = PlanBuilder('parent', Catalog())
result: Ref[str] = b.invoke('child', plan())
compiled: CheckedPlan[str] = b.finish(result)
'''
NEGATIVE = '''from consumer import plan
from al0.ir import Catalog
from al0.sdk import PlanBuilder, Ref
b = PlanBuilder('wrong', Catalog())
result: Ref[int] = b.invoke('child', plan())
'''
GENERATED_CONSUMER = '''from al0.demo import catalog
from al0.ir import CheckedPlan
from al0.sdk import PlanBuilder, Candidate, Ref
from generated import Bindings, Schema_Patch
b = PlanBuilder('generated-consumer', catalog())
g = Bindings(b)
request: Ref[str] = g.literal_Text('request', 'repair')
draft: Ref[Candidate[Schema_Patch]] = g.infer_code_model('draft', request)
compiled: CheckedPlan[Candidate[Schema_Patch]] = b.finish(draft)
'''
GENERATED_NEGATIVE = '''from generated_consumer import g
g.literal_Text('wrong', 42)
'''
INFERENCE_PLAN = '''from al0.ir import Catalog, CheckedPlan, Tool
from al0.sdk import PlanBuilder, Ref, ToolRef, Variant

def catalog() -> Catalog:
    c = Catalog()
    c.register(Tool.define('model', 'infer', 'Text', 'Text', 'model', {'u': 1}))
    return c

def plan(c: Catalog) -> CheckedPlan[Variant]:
    b = PlanBuilder('installed-inference-result', c)
    model: ToolRef[str, str] = b.tool('model')
    result: Ref[Variant] = b.infer_result('result', model, b.literal('prompt', 'Text', 'question'))
    return b.finish(result)
'''
INFERENCE_CONSUMER = '''import tempfile
from pathlib import Path
from al0.data import Principal
from al0.execute import Broker, Interpreter, Response
from al0.store import Store
from inference_plan import catalog, plan

with tempfile.TemporaryDirectory() as directory:
    c = catalog()
    checked = plan(c)
    store = Store(Path(directory)/'runtime.sqlite')
    who = Principal('installed', 'consumer')
    calls = []
    def model(ticket):
        calls.append(ticket.attempt)
        return Response(17, {'u': 1})
    broker = Broker(store, c, {'model': model})
    run = store.create_run(who, checked.fingerprint, {'u': 2})
    grants = {'model': store.grant(run, 'model', 'model', expires=store.clock()+1000)}
    result = Interpreter(broker).run(checked, run, who, grants)
    assert result.data['tag'] == 'Error'
    assert result.data['payload']['data']['code'] == 'InvalidOutput'
    assert dict(result.variant.members)['Error'].name == 'InferenceError'
    count = len(store.events(run, who))
    assert Interpreter(broker).run(checked, run, who, grants) == result
    assert len(store.events(run, who)) == count
    assert len(calls) == 1 and store.budget(run, who)['u']['confirmed'] == 1
'''
PROGRESS_PLAN = '''from al0.ir import Catalog, CheckedPlan
from al0.sdk import PlanBuilder, Ref, ToolRef, Operation, Variant

def plan(c: Catalog) -> CheckedPlan[Variant]:
    b = PlanBuilder('installed-progress', c)
    model: ToolRef[str, str] = b.tool('model')
    result: Ref[Variant] = b.infer_progress('result', model, b.literal('prompt', 'Text', 'question'))
    return b.finish(result)

def resume(c: Catalog) -> CheckedPlan[Variant]:
    b = PlanBuilder('installed-resume', c)
    model: ToolRef[str, str] = b.tool('model')
    operation: Ref[Operation[str]] = b.operation_parameter('operation', model)
    result: Ref[Variant] = b.resume_infer('result', operation)
    return b.finish(result)
'''
PROGRESS_CONSUMER = '''import tempfile
from pathlib import Path
from al0.data import Principal
from al0.execute import Broker, Interpreter, Response, Value
from al0.store import Store
from inference_plan import catalog
from progress_plan import plan, resume

with tempfile.TemporaryDirectory() as directory:
    c = catalog(); checked = plan(c); resume(c)
    store = Store(Path(directory)/'runtime.sqlite'); who = Principal('installed', 'consumer')
    calls = []
    def model(ticket):
        calls.append(ticket)
        return Response(None, None, 'Pending')
    broker = Broker(store, c, {'model': model})
    run = store.create_run(who, checked.fingerprint, {'u': 2})
    grants = {'model': store.grant(run, 'model', 'model', expires=store.clock()+1000)}
    old = Interpreter(broker).run(checked, run, who, grants)
    assert old.data['tag'] == 'Waiting'
    store.business_result(calls[0].operation, who, provider='model', evidence_id='late', outcome='Applied', details={'output':'late'})
    source = Value.from_record(old.data['payload']).resource
    result = broker.inference_progress(run, who, 'next', source, c.tools['model'], grants, control=old.label)
    assert result.data['tag'] == 'Success'
    assert Interpreter(broker).run(checked, run, who, grants) == old
    assert broker.inference_progress(run, who, 'next', source, c.tools['model'], grants, control=old.label) == result
    assert store.budget(run, who)['u']['reserved'] == 1
    store.settle(calls[0].attempt, who, provider='model', evidence_id='bill', actual={'u':1})
    assert store.budget(run, who)['u']['confirmed'] == 1 and len(calls) == 1
'''
PREFLIGHT_PLAN = INFERENCE_PLAN.replace('installed-inference-result', 'installed-inference-admission').replace('.infer_result(', '.try_infer(')
PREFLIGHT_CONSUMER = '''import tempfile
from pathlib import Path
from al0.data import Principal
from al0.execute import Broker, Interpreter, Response
from al0.store import Store
from preflight_plan import catalog, plan

with tempfile.TemporaryDirectory() as directory:
    c = catalog(); checked = plan(c); who = Principal('installed', 'consumer')
    store = Store(Path(directory)/'runtime.sqlite'); calls = []
    def model(ticket):
        calls.append(ticket)
        return Response('candidate', {'u': 1})
    broker = Broker(store, c, {'model': model})
    for amount, bound in [(1, True), (0, True), (1, False)]:
        run = store.create_run(who, checked.fingerprint, {'u': amount})
        grants = {'model': store.grant(run, 'model', 'model', expires=store.clock()+1000)} if bound else {}
        result = Interpreter(broker).run(checked, run, who, grants)
        if amount and bound:
            assert result.data['tag'] == 'Started' and result.data['payload']['data']['tag'] == 'Success'
        else:
            assert result.data['tag'] == 'Rejected'
            assert result.data['payload']['data']['code'] == ('BudgetExceeded' if bound else 'AuthorizationUnavailable')
        count = len(store.events(run, who))
        assert Interpreter(broker).run(checked, run, who, grants) == result
        assert len(store.events(run, who)) == count
    assert len(calls) == 1
'''
PROVIDER_TYPES = '''from al0.inference_response import InferenceResponse

def responses() -> tuple[InferenceResponse, InferenceResponse]:
    return InferenceResponse('Output', 'candidate', {'u': 1}), InferenceResponse('Refused')
'''
PROVIDER_NEGATIVE = '''from al0.inference_response import InferenceResponse
InferenceResponse('Pending')
'''
PROVIDER_CONSUMER = '''import tempfile
from pathlib import Path
from al0.data import Principal
from al0.execute import Broker, Interpreter, Value
from al0.store import Store
from preflight_plan import catalog, plan
from provider_types import responses

with tempfile.TemporaryDirectory() as directory:
    c = catalog(); checked = plan(c); who = Principal('installed', 'consumer')
    for mode in ('Output', 'Refused', 'Blocked'):
        store = Store(Path(directory)/(mode+'.sqlite')); calls = []
        output, refusal = responses()
        def model(ticket):
            calls.append(ticket)
            return refusal if mode == 'Refused' else output
        broker = Broker(store, c, {'model': model})
        run = store.create_run(who, checked.fingerprint, {'u': 2})
        grants = {'model': store.grant(run, 'model', 'model', expires=store.clock()+1000)}
        def fault(point):
            if mode == 'Blocked' and point == 'after_inference_admission_commit':
                store.revoke(grants['model'])
        store.fault = fault
        result = Interpreter(broker).run(checked, run, who, grants)
        progress = Value.from_record(result.data['payload'])
        assert result.data['tag'] == 'Started'
        assert progress.data['tag'] == {'Output':'Success','Refused':'Error','Blocked':'Waiting'}[mode]
        if mode == 'Refused':
            assert progress.data['payload']['data']['code'] == 'Refused'
            assert store.budget(run, who)['u']['reserved'] == 1
            store.settle(calls[0].attempt, who, provider='model', evidence_id='late', actual={'u':1})
        elif mode == 'Blocked':
            reference = Value.from_record(progress.data['payload']).resource
            with store.resource_access(run, who, [reference]):store.cancel(reference.resource, who)
            assert store.budget(run, who)['u']['free'] == 2
        before = len(store.events(run, who))
        assert Interpreter(broker).run(checked, run, who, grants) == result
        assert len(store.events(run, who)) == before and len(calls) == (0 if mode == 'Blocked' else 1)
'''
AFFINE_LOOP_PLAN = '''from al0.ir import Catalog, CheckedPlan
from al0.sdk import PlanBuilder, Ref, Budget

def plan(c: Catalog) -> CheckedPlan[bool]:
    sample = PlanBuilder('sample', c)
    request = sample.literal('request', 'Text', 'question')
    sample_plan = sample.finish(sample.infer_result('result', sample.tool('model'), request))
    body = PlanBuilder('body', c)
    body.parameter('index', 'Int')
    state = body.budget_parameter('state')
    body.invoke('sample', sample_plan, budget=state)
    body_plan = body.finish(state)
    b = PlanBuilder('installed-affine-loop', c)
    funds = b.split_budget('funds', {'u': 2})
    result: Ref[Budget] = b.repeat('loop', b.literal('count', 'Int', 2), 2, funds, body_plan)
    return b.finish(b.join_budget('released', result))
'''
AFFINE_LOOP_CONSUMER = '''import tempfile
from pathlib import Path
from al0.data import Principal
from al0.execute import Broker, Interpreter, Response
from al0.store import Store
from inference_plan import catalog
from affine_loop_plan import plan

with tempfile.TemporaryDirectory() as directory:
    c = catalog(); checked = plan(c)
    store = Store(Path(directory)/'runtime.sqlite'); who = Principal('installed', 'consumer')
    calls = []
    def model(ticket):
        calls.append(ticket.attempt)
        return Response('candidate', {'u': 1})
    broker = Broker(store, c, {'model': model})
    run = store.create_run(who, checked.fingerprint, {'u': 3})
    grants = {'model': store.grant(run, 'model', 'model', expires=store.clock()+1000)}
    result = Interpreter(broker).run(checked, run, who, grants)
    assert result.data is True
    count = len(store.events(run, who))
    assert Interpreter(broker).run(checked, run, who, grants) == result
    assert len(store.events(run, who)) == count
    assert len(set(calls)) == len(calls) == 2
    assert store.budget(run, who)['u']['confirmed'] == 2
    assert store.budget(run, who)['u']['free'] == 1
'''
CUSTODY_CONSUMER = '''from pathlib import Path
import tempfile
from al0.data import Principal
from al0.errors import StateConflict
from al0.store import Store
with tempfile.TemporaryDirectory() as directory:
    store = Store(Path(directory)/'store.sqlite')
    principal = Principal('installed', 'consumer')
    run = store.create_run(principal, 'custody-consumer', {'cost': 2})
    allocation = store.split_budget(run, principal, 'first', {'cost': 2})
    ref = store.manage_resource(run, principal, 'Budget', allocation)
    with store.resource_access(run, principal, [ref]):
        child = store.split_budget(run, principal, 'second', {'cost': 1}, parent=allocation)
    assert store.resource_birth('Budget', child, principal).generation == 0
    try:
        store.split_budget(run, principal, 'without-ref', {'cost': 1}, parent=allocation)
    except StateConflict:
        pass
    else:
        raise AssertionError('managed budget accepted a plain string')
'''

TASK_RESOURCE_PLAN = '''from al0.ir import Catalog, CheckedPlan, Type
from al0.bundle_types import variant_type
from al0.sdk import PlanBuilder, Bundle, Ref, Task, TaskOutcome, Variant

def plan() -> CheckedPlan[bool]:
    catalog = Catalog()
    prototype = PlanBuilder('prototype', catalog)
    shape: CheckedPlan[Bundle] = prototype.finish(prototype.pack('packet', {'funds': prototype.split_budget('funds', {})}))
    child = PlanBuilder('child', catalog)
    packet = child.bundle_parameter('packet', shape)
    child_plan: CheckedPlan[Bundle] = child.finish(packet)
    parent = PlanBuilder('parent', catalog)
    source = parent.split_budget('funds', {'u': 2})
    outgoing = parent.pack('packet', {'funds': source})
    scope = parent.scope('scope', {})
    task: Ref[Task[Bundle]] = parent.spawn('task', scope, child_plan, {}, inputs={'packet': outgoing})
    returned: Ref[Bundle] = parent.join('returned', task)
    opened = parent.unpack('opened', returned)
    funds = parent.take('funds-returned', opened, 'funds')
    parent.close_scope('closed', scope)
    parent.join_budget('released', funds)
    race = parent.scope('race-scope', {}, speculative=True)
    first = parent.spawn('first', race, shape, {})
    second = parent.spawn('second', race, shape, {})
    winner: Ref[Bundle] = parent.race('winner', [first, second])
    task = parent.spawn('forward', race, child_plan, {}, inputs={'packet': winner})
    outcome: Ref[TaskOutcome[Bundle]] = parent.join_outcome('outcome', task)
    parent.close_scope('race-closed', race)
    success = PlanBuilder('success', catalog)
    result = success.bundle_parameter('result', shape)
    opened_result = success.unpack('opened', result)
    completed = success.finish(success.join_budget('released', success.take('funds', opened_result, 'funds')))
    other = PlanBuilder('other', catalog)
    failed = other.finish(other.literal('false', 'Bool', False))
    done = parent.match_task('handled', outcome, completed=completed,
                            failed=failed, waiting=failed, cancelled=failed)
    choices = variant_type({'Finished': Type('Data', 'Bool'), 'Funds': Type('Budget', 'Budget')})
    identity = PlanBuilder('variant-identity', catalog)
    identity_plan = identity.finish(identity.variant_parameter('value', choices))
    yes = PlanBuilder('finished', catalog)
    yes_plan: CheckedPlan[bool] = yes.finish(yes.parameter('result', 'Bool'))
    release = PlanBuilder('funds', catalog)
    release_plan = release.finish(release.join_budget('closed', release.budget_parameter('result')))
    variant_scope = parent.scope('variant-scope', {})
    empty: Ref[Variant] = parent.variant('empty', choices, 'Finished', done)
    forwarded = parent.spawn('empty-task', variant_scope, identity_plan, {}, inputs={'value': empty})
    empty_result = parent.join('empty-result', forwarded)
    first_ok = parent.match_variant('first-ok', empty_result, {'Finished': yes_plan, 'Funds': release_plan})
    funded = parent.variant('funded', choices, 'Funds', parent.split_budget('variant-funds', {'u': 1}))
    forwarded = parent.spawn('funded-task', variant_scope, identity_plan, {}, inputs={'value': funded})
    funded_result = parent.join('funded-result', forwarded)
    last_ok = parent.match_variant('last-ok', funded_result, {'Finished': yes_plan, 'Funds': release_plan})
    parent.close_scope('variants-closed', variant_scope)
    plain = PlanBuilder('pure-variant', catalog, pure=True)
    plain_type = variant_type({'Finished': Type('Data', 'Bool')})
    plain_plan = plain.finish(plain.variant('value', plain_type, 'Finished', plain.literal('yes', 'Bool', True)))
    plain_value = parent.call('pure-value', plain_plan, inputs={})
    plain_ok = parent.match_variant('pure-ok', plain_value, {'Finished': yes_plan})
    return parent.finish(parent.compute('all-ok', 'and', parent.compute('both', 'and', first_ok, last_ok), plain_ok))
'''

TASK_RESOURCE_CONSUMER = '''from pathlib import Path
import tempfile
from al0.data import Principal
from al0.execute import Broker, Interpreter
from al0.ir import Catalog
from al0.store import Store
from task_resource_plan import plan
with tempfile.TemporaryDirectory() as directory:
    store = Store(Path(directory)/'store.sqlite')
    principal = Principal('installed', 'consumer')
    checked = plan()
    run = store.create_run(principal, checked.fingerprint, {'u': 3})
    broker = Broker(store, Catalog(), {})
    try:
        assert Interpreter(broker).run(checked, run, principal, {}).data is True
        count = len(store.events(run, principal))
        assert Interpreter(broker).run(checked, run, principal, {}).data is True
        assert len(store.events(run, principal)) == count
        assert store.budget(run, principal)['u']['free'] == 3
    finally:
        broker.wait_for_workers(timeout=10)
'''


def audit(python, wheel):
    # Resolving a POSIX venv's python symlink selects the base environment.
    python, wheel = Path(python).absolute(), Path(wheel).resolve()
    with zipfile.ZipFile(wheel) as archive:
        if 'al0/py.typed' not in archive.namelist():
            raise AssertionError('wheel is missing the PEP 561 marker')
    env = dict(os.environ)
    env.pop('MYPYPATH', None)
    env.pop('PYTHONPATH', None)
    env['PYTHONIOENCODING'] = 'utf-8'
    with tempfile.TemporaryDirectory(prefix='al0-installed-typing-', ignore_cleanup_errors=True) as folder:
        folder = Path(folder)

        def run(arguments):
            return subprocess.run([str(a) for a in arguments], cwd=folder, env=env,
                                  capture_output=True, text=True, encoding='utf-8', timeout=180)

        def require(result):
            if result.returncode:
                raise AssertionError(result.stdout+result.stderr)
            return result

        venv = folder/'consumer-env'
        require(run([python, '-m', 'venv', '--without-pip', venv]))
        consumer_python = venv/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        require(run([python, '-m', 'pip', '--python', consumer_python, 'install', '--no-index', '--no-deps',
                     '--disable-pip-version-check', wheel]))
        (folder/'consumer.py').write_text(CONSUMER, encoding='utf-8')
        (folder/'negative.py').write_text(NEGATIVE, encoding='utf-8')
        origin = Path(require(run([consumer_python, '-c', 'import al0.sdk; print(al0.sdk.__file__)'])).stdout.strip()).resolve()
        if not origin.is_relative_to(venv):
            raise AssertionError('consumer imported the checkout instead of the wheel')
        require(run([consumer_python, 'consumer.py']))
        require(run([consumer_python, '-m', 'al0.codegen', '--demo-catalog', '--output', 'generated.py']))
        require(run([consumer_python, '-m', 'al0.codegen', '--demo-catalog', '--output', 'generated.py', '--check']))
        (folder/'generated_consumer.py').write_text(GENERATED_CONSUMER, encoding='utf-8')
        (folder/'generated_negative.py').write_text(GENERATED_NEGATIVE, encoding='utf-8')
        require(run([consumer_python, 'generated_consumer.py']))
        (folder/'custody_consumer.py').write_text(CUSTODY_CONSUMER, encoding='utf-8')
        require(run([consumer_python, 'custody_consumer.py']))
        (folder/'task_resource_plan.py').write_text(TASK_RESOURCE_PLAN, encoding='utf-8')
        (folder/'task_resource_consumer.py').write_text(TASK_RESOURCE_CONSUMER, encoding='utf-8')
        require(run([consumer_python, 'task_resource_consumer.py']))
        (folder/'inference_plan.py').write_text(INFERENCE_PLAN, encoding='utf-8')
        (folder/'inference_consumer.py').write_text(INFERENCE_CONSUMER, encoding='utf-8')
        require(run([consumer_python, 'inference_consumer.py']))
        (folder/'affine_loop_plan.py').write_text(AFFINE_LOOP_PLAN, encoding='utf-8')
        (folder/'affine_loop_consumer.py').write_text(AFFINE_LOOP_CONSUMER, encoding='utf-8')
        require(run([consumer_python, 'affine_loop_consumer.py']))
        (folder/'progress_plan.py').write_text(PROGRESS_PLAN, encoding='utf-8')
        (folder/'progress_consumer.py').write_text(PROGRESS_CONSUMER, encoding='utf-8')
        require(run([consumer_python, 'progress_consumer.py']))
        (folder/'preflight_plan.py').write_text(PREFLIGHT_PLAN, encoding='utf-8')
        (folder/'preflight_consumer.py').write_text(PREFLIGHT_CONSUMER, encoding='utf-8')
        require(run([consumer_python, 'preflight_consumer.py']))
        (folder/'provider_types.py').write_text(PROVIDER_TYPES, encoding='utf-8')
        (folder/'provider_consumer.py').write_text(PROVIDER_CONSUMER, encoding='utf-8')
        (folder/'provider_negative.py').write_text(PROVIDER_NEGATIVE, encoding='utf-8')
        require(run([consumer_python, 'provider_consumer.py']))
        common = [python, '-m', 'mypy', '--config-file', ROOT/'pyproject.toml', '--strict',
                  '--follow-imports=silent', '--python-executable', consumer_python,
                  '--no-pretty', '--no-color-output', '--show-error-codes']
        accepted = require(run([*common, '--no-incremental', '-v', 'consumer.py', 'generated.py', 'generated_consumer.py', 'task_resource_plan.py', 'inference_plan.py', 'affine_loop_plan.py', 'progress_plan.py', 'preflight_plan.py', 'provider_types.py']))
        logs = (accepted.stdout+accepted.stderr).replace('\\', '/').lower()
        if str(origin).replace('\\', '/').lower() not in logs:
            raise AssertionError('mypy did not record using the installed SDK source')
        rejected = run([*common, 'negative.py'])
        errors = [line for line in rejected.stdout.splitlines() if ': error:' in line]
        if rejected.returncode != 1 or len(errors) != 1 or not errors[0].startswith('negative.py:5: error:') or '[arg-type]' not in errors[0]:
            raise AssertionError(rejected.stdout+rejected.stderr)
        generated_rejected = run([*common, 'generated_negative.py'])
        generated_errors = [line for line in generated_rejected.stdout.splitlines() if ': error:' in line]
        if (generated_rejected.returncode != 1 or len(generated_errors) != 1
                or not generated_errors[0].startswith('generated_negative.py:2: error:')
                or '[arg-type]' not in generated_errors[0]):
            raise AssertionError(generated_rejected.stdout+generated_rejected.stderr)
        provider_rejected = run([*common, 'provider_negative.py'])
        provider_errors = [line for line in provider_rejected.stdout.splitlines() if ': error:' in line]
        if (provider_rejected.returncode != 1 or len(provider_errors) != 1
                or not provider_errors[0].startswith('provider_negative.py:2: error:') or '[arg-type]' not in provider_errors[0]):
            raise AssertionError(provider_rejected.stdout+provider_rejected.stderr)
        return {'passed': True, 'wheel': wheel.name, 'py_typed_present': True,
                'runtime_import_from_installed_wheel': True, 'mypy_import_from_installed_wheel': True,
                'positive_consumer_executed': True, 'positive_consumer_typechecked': True,
                'negative_diagnostic': errors[0],
                'installed_codegen_generated_and_checked': True,
                'generated_consumer_executed_and_typechecked': True,
                'custody_runtime_consumer_executed': True,
                'typed_task_resource_consumer_executed_and_plan_typechecked': True,
                'bundle_task_consumer_executed_and_plan_typechecked': True,
                'resource_outcome_and_race_consumer_executed_and_plan_typechecked': True,
                'pure_and_resource_variant_consumer_executed_and_plan_typechecked': True,
                'inference_result_consumer_executed_and_plan_typechecked': True,
                'affine_loop_consumer_executed_and_plan_typechecked': True,
                'inference_progress_consumer_executed_and_plan_typechecked': True,
                'inference_admission_consumer_executed_and_plan_typechecked': True,
                'provider_refusal_and_blocked_dispatch_consumer_executed': True,
                'provider_response_types_checked': True,
                'provider_negative_diagnostic': provider_errors[0],
                'generated_negative_diagnostic': generated_errors[0],
                'environment': 'temporary consumer venv; no runtime dependencies installed; outside checkout'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT/'models/sdk_wheel_audit.json')
    args = parser.parse_args()
    report = audit(args.python, args.wheel)
    args.output.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print('Installed wheel typing passed:', report['wheel'])


if __name__ == '__main__':
    main()
