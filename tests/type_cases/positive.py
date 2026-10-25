"""Well-typed SDK programs, each also accepted by the shared IR checker."""
from typing import Any, TYPE_CHECKING

from al0.flow import Label
from al0.ir import Catalog, CheckedPlan, Tool, Type
from al0.bundle_types import variant_type
from al0.sdk import (Admission, Budget, Bundle, Unpacked, Candidate, Cap, Checked, Intent, Observation,
                     Operation, OpRef, PlanBuilder, Ref, Snapshot, Task, TaskOutcome, ToolRef, Variant)

if TYPE_CHECKING:
    from typing_extensions import assert_type
else:
    def assert_type(value: Any, typ: Any) -> Any:
        return value


def catalog() -> Catalog:
    c = Catalog()
    c.schema('Record', {'number': 'Int', 'text': 'Text'})
    c.register(Tool.define('model', 'infer', 'Text', 'Text', 'model', {'u': 1}))
    c.register(Tool.define('read', 'observe', 'Text', 'Text', 'read', {'u': 1}))
    c.register(Tool.define('check', 'check', 'Text', 'Bool', 'check', {'u': 1}, predicate='valid'))
    c.register(Tool.define('send', 'action', 'Text', 'Text', 'outbox', {'u': 1}))
    c.register(Tool.define('query', 'reconcile', 'Json', 'Json', 'outbox', {'u': 1}, reconciles='send'))
    return c


def send(b: PlanBuilder) -> ToolRef[str, str]:
    return b.tool('send')


def handled(b: PlanBuilder, name: str, status: Ref[Snapshot[str]]) -> Ref[str]:
    return b.match_business(name, status, 'Text', applied='Done', proven_no_effect='NoEffect',
                            pending='Pending', unknown='Unknown', not_dispatched='Queued')


def text(c: Catalog, name: str, *, pure: bool=False) -> CheckedPlan[str]:
    b = PlanBuilder(name, c, pure=pure)
    return b.finish(b.literal('value', 'Text', name))


def sequential(c: Catalog) -> CheckedPlan[str]:
    b = PlanBuilder('typed-sequential', c)
    model: ToolRef[str, str] = b.tool('model')
    reader: ToolRef[str, str] = b.tool('read')
    checker: ToolRef[str, bool] = b.tool('check')
    query: ToolRef[dict[str, Any], dict[str, Any]] = b.tool('query')
    message = b.literal('message', 'Text', 'hello')
    draft = b.infer('draft', model, message)
    assert_type(draft, Ref[Candidate[str]])
    observed = b.observe('observed', reader, draft)
    assert_type(observed, Ref[Observation[str]])
    checked = b.check('checked', checker, observed)
    assert_type(checked, Ref[Checked[str]])
    restricted = b.restrict('restricted', checked, Label())
    assert_type(restricted, Ref[Checked[str]])
    cap = b.attenuate('narrow', b.capability('cap', send(b)), max_attempts=1)
    assert_type(cap, Ref[Cap[str]])
    intent = b.prepare('intent', send(b), restricted)
    assert_type(intent, Ref[Intent[str]])
    permit = b.authorize_with('permit', intent, cap)
    operation = b.admit('operation', intent, permit)
    assert_type(operation, Ref[Operation[str]])
    borrowed = b.borrow_operation('borrowed', operation)
    assert_type(borrowed, Ref[OpRef[str]])
    handled(b, 'inspection', b.inspect('snapshot', borrowed))
    handled(b, 'awaited', b.await_result('status', operation))
    return b.finish(handled(b, 'result', b.reconcile('reconciled', query, operation)))


def parameters(c: Catalog) -> list[CheckedPlan[Any]]:
    b = PlanBuilder('intent-parameter', c)
    intent = b.intent_parameter('intent', send(b))
    permit = b.permit_parameter('permit', intent)
    operation = b.admit('operation', intent, permit)
    first = b.finish(operation)
    assert_type(first, CheckedPlan[Operation[str]])
    b = PlanBuilder('operation-parameter', c)
    op = b.operation_parameter('operation', send(b))
    second = b.finish(handled(b, 'result', b.await_result('status', op)))
    b = PlanBuilder('opref-parameter', c)
    third = b.finish(b.opref_parameter('operation', send(b)))
    b = PlanBuilder('budget-parameter', c)
    funds = b.budget_parameter('funds')
    assert_type(funds, Ref[Budget])
    fourth = b.finish(b.join_budget('release', funds))
    b = PlanBuilder('cap-parameter', c)
    permission = b.cap_parameter('permission', send(b))
    fifth = b.finish(b.attenuate('narrow', permission, max_attempts=1))
    return [first, second, third, fourth, fifth]


def pure_and_calls(c: Catalog) -> CheckedPlan[str]:
    f = PlanBuilder('identity', c, pure=True)
    value: Ref[str] = f.parameter('value', 'Text')
    function = f.finish(value)
    loop = PlanBuilder('loop', c)
    index: Ref[int] = loop.parameter('index', 'Int')
    state: Ref[str] = loop.parameter('state', 'Text')
    assert_type(index, Ref[int])
    body = loop.finish(state)
    b = PlanBuilder('pure-and-calls', c)
    message = b.literal('message', 'Text', 'hello')
    count = b.literal('count', 'Int', 2)
    selected = b.if_else('choice', b.literal('condition', 'Bool', True),
                         then=text(c, 'then'), otherwise=text(c, 'otherwise'))
    assert_type(selected, Ref[str])
    called = b.call('called', function, inputs={'value': message})
    assert_type(called, Ref[str])
    repeated = b.repeat('repeated', count, 2, called, body)
    assert_type(repeated, Ref[str])
    invoked = b.invoke('invoked', text(c, 'workflow'))
    assert_type(invoked, Ref[str])
    # Pure projections/combinations still depend on dynamic registered schemas
    # and provenance joins. Their Any boundary is explicit, not a fake str.
    record = b.record('record', 'Record', {'number': count, 'text': repeated})
    assert_type(record, Ref[Any])
    field = b.field('field', record, 'number')
    computed = b.compute('computed', 'add', count, field)
    assert_type(computed, Ref[Any])
    amounts = b.resources('amounts', {'u': computed})
    assert_type(amounts, Ref[Any])
    funds = b.split_budget('funds', amounts)
    child_funds = b.split_budget('child-funds', {'u': 1}, parent=funds)
    b.invoke('funded', text(c, 'funded'), budget=child_funds)
    b.join_budget('child-release', child_funds)
    b.join_budget('release', funds)
    return b.finish(repeated)


def tasks(c: Catalog) -> list[CheckedPlan[Any]]:
    child = text(c, 'child')
    b = PlanBuilder('join-task', c)
    scope = b.scope('scope', {})
    task = b.spawn('task', scope, child, {})
    assert_type(task, Ref[Task[str]])
    value = b.join('joined', task)
    assert_type(value, Ref[str])
    b.close_scope('close', scope)
    joined = b.finish(value)
    b = PlanBuilder('race-tasks', c)
    scope = b.scope('scope', {}, speculative=True)
    first = b.spawn('first', scope, child, {})
    second = b.spawn('second', scope, child, {})
    winner = b.race('winner', [first, second])
    assert_type(winner, Ref[str])
    b.close_scope('close', scope)
    raced = b.finish(winner)
    b = PlanBuilder('handoff-task', c)
    scope = b.scope('scope', {})
    handed = b.handoff('handed', b.spawn('task', scope, child, {}))
    assert_type(handed, Ref[dict[str, Any]])
    b.close_scope('close', scope)
    transferred = b.finish(handed)
    results: list[CheckedPlan[Any]] = [joined, raced, transferred]
    for cancel in (False, True):
        b = PlanBuilder('task-outcome-'+str(cancel), c)
        scope = b.scope('scope', {})
        task = b.spawn('task', scope, child, {})
        outcome = b.cancel_outcome('outcome', task) if cancel else b.join_outcome('outcome', task)
        assert_type(outcome, Ref[TaskOutcome[str]])
        b.close_scope('close', scope)
        value = b.match_task('result', outcome, completed=text(c, 'completed'), failed=text(c, 'failed'),
                             waiting=text(c, 'waiting'), cancelled=text(c, 'cancelled'))
        assert_type(value, Ref[str])
        results.append(b.finish(value))
    return results


def capabilities(c: Catalog) -> CheckedPlan[Cap[str]]:
    child = PlanBuilder('child-cap', c)
    cap = child.cap_parameter('permission', send(child))
    plan = child.finish(child.attenuate('narrow', cap, max_attempts=1))
    assert_type(plan, CheckedPlan[Cap[str]])
    b = PlanBuilder('task-cap', c)
    root = b.capability('root', send(b))
    local = b.invoke_caps('local', plan, caps={'send': root}, inputs={'permission': root})
    assert_type(local, Ref[Cap[str]])
    scope = b.scope('scope', {})
    task = b.spawn_caps('child', scope, plan, {}, caps={'send': local}, inputs={'permission': local})
    assert_type(task, Ref[Task[Cap[str]]])
    returned = b.join('returned', task)
    assert_type(returned, Ref[Cap[str]])
    b.close_scope('close', scope)
    return b.finish(returned)


def admission(c: Catalog) -> CheckedPlan[str]:
    a = PlanBuilder('admitted', c)
    op = a.operation_parameter('result', send(a))
    admitted = a.finish(handled(a, 'handled', a.await_result('status', op)))
    r = PlanBuilder('returned', c)
    intent = r.intent_parameter('intent', send(r))
    r.permit_parameter('result', intent)
    returned = r.finish(r.literal('defer', 'Text', 'Deferred'))
    denied = text(c, 'denied')
    b = PlanBuilder('admission', c)
    message = b.literal('message', 'Text', 'hello')
    request = b.prepare('intent', send(b), message)
    permission = b.authorize('permit', request)
    result = b.try_admit('decision', request, permission)
    assert_type(result, Ref[Admission[str]])
    value = b.match_admission('result', result, admitted=admitted, returned=returned,
                              no_permit=denied, inputs={'intent': request})
    assert_type(value, Ref[str])
    return b.finish(value)


def affine_tasks(c: Catalog) -> CheckedPlan[str]:
    child=PlanBuilder('submit-child',c)
    request=child.intent_parameter('request',send(child))
    permit=child.permit_parameter('permit',request)
    operation=child.admit('operation',request,permit)
    child_plan=child.finish(operation)
    assert_type(child_plan,CheckedPlan[Operation[str]])
    b=PlanBuilder('affine-tasks',c)
    request=b.prepare('request',send(b),b.literal('message','Text','hello'))
    permit=b.authorize('permit',request)
    group=b.scope('group',{'u':1})
    task=b.spawn('task',group,child_plan,{'u':1},inputs={'request':request,'permit':permit})
    assert_type(task,Ref[Task[Operation[str]]])
    result=b.join('result',task)
    assert_type(result,Ref[Operation[str]])
    b.close_scope('closed',group)
    return b.finish(handled(b,'handled',b.await_result('state',result)))


def resource_bundles(c: Catalog) -> CheckedPlan[bool]:
    b=PlanBuilder('bundle-prototype',c)
    packet=b.pack('packet',{'funds':b.split_budget('funds',{})})
    assert_type(packet,Ref[Bundle])
    prototype=b.finish(packet)
    child=PlanBuilder('bundle-child',c)
    child_plan=child.finish(child.bundle_parameter('packet',prototype))
    b=PlanBuilder('bundle-parent',c)
    packet=b.pack('packet',{'funds':b.split_budget('funds',{})})
    scope=b.scope('scope',{})
    task=b.spawn('task',scope,child_plan,{},inputs={'packet':packet})
    assert_type(task,Ref[Task[Bundle]])
    returned=b.join('returned',task)
    opened=b.unpack('opened',returned)
    assert_type(opened,Ref[Unpacked])
    funds=b.take('funds-returned',opened,'funds')
    assert_type(funds,Ref[Any])  # Field names are checked by shared IR.
    b.close_scope('closed',scope)
    return b.finish(b.join_budget('released',funds))


def resource_outcomes(c: Catalog) -> list[CheckedPlan[Any]]:
    p=PlanBuilder('packet-prototype',c)
    prototype=p.finish(p.pack('packet',{'funds':p.split_budget('funds',{})}))
    branch=PlanBuilder('completed-packet',c)
    result=branch.bundle_parameter('result',prototype)
    opened=branch.unpack('opened',result)
    completed=branch.finish(branch.join_budget('released',branch.take('funds',opened,'funds')))
    other=PlanBuilder('other',c)
    failed=other.finish(other.literal('false','Bool',False))
    b=PlanBuilder('outcome-packet',c)
    group=b.scope('scope',{})
    task=b.spawn('task',group,prototype,{})
    outcome=b.join_outcome('outcome',task)
    assert_type(outcome,Ref[TaskOutcome[Bundle]])
    result_bool=b.match_task('result',outcome,completed=completed,failed=failed,waiting=failed,cancelled=failed)
    assert_type(result_bool,Ref[bool])
    b.close_scope('closed',group)
    outcome_plan=b.finish(result_bool)
    b=PlanBuilder('race-packet',c)
    group=b.scope('scope',{},speculative=True)
    first=b.spawn('first',group,prototype,{})
    second=b.spawn('second',group,prototype,{})
    winner=b.race('winner',[first,second])
    assert_type(winner,Ref[Bundle])
    b.close_scope('closed',group)
    return [outcome_plan,b.finish(winner)]


def variants(c: Catalog) -> list[CheckedPlan[Any]]:
    interface=variant_type({'Text':Type('Data','Text'),'Funds':Type('Budget','Budget')})
    branch=PlanBuilder('funds',c)
    funds=branch.budget_parameter('result')
    branch.join_budget('closed',funds)
    funds_case=branch.finish(branch.literal('text','Text','closed'))
    branch=PlanBuilder('text',c)
    text_case: CheckedPlan[str]=branch.finish(branch.parameter('result','Text'))
    handler=PlanBuilder('handler',c)
    argument=handler.variant_parameter('result',interface)
    handled=handler.match_variant('handled',argument,{'Text':text_case,'Funds':funds_case})
    assert_type(handled,Ref[str])
    handler_plan=handler.finish(handled)
    b=PlanBuilder('variant-resource',c)
    value=b.variant('value',interface,'Funds',b.split_budget('funds',{}))
    assert_type(value,Ref[Variant])
    result=b.invoke('result',handler_plan,inputs={'result':value})
    resource=b.finish(result)
    plain=variant_type({'Text':Type('Data','Text')})
    b=PlanBuilder('variant-pure',c,pure=True)
    value=b.variant('value',plain,'Text',b.literal('text','Text','hello'))
    return [resource,b.finish(value)]


def inference_results(c: Catalog) -> CheckedPlan[Variant]:
    b=PlanBuilder('typed-inference-result',c)
    model: ToolRef[str,str]=b.tool('model')
    value=b.infer_result('candidate-or-error',model,b.literal('prompt','Text','question'))
    assert_type(value,Ref[Variant])
    return b.finish(value)


def affine_loops(c: Catalog) -> list[CheckedPlan[Any]]:
    body=PlanBuilder('budget-step',c)
    body.parameter('index','Int')
    state=body.budget_parameter('state')
    step=body.finish(state)
    b=PlanBuilder('budget-loop',c)
    result=b.repeat('loop',b.literal('n','Int',2),2,b.split_budget('funds',{}),step)
    assert_type(result,Ref[Budget])
    budget_plan=b.finish(b.join_budget('released',result))
    shape=variant_type({'Funds':Type('Budget','Budget'),'Empty':Type('Data','Text')})
    body=PlanBuilder('variant-step',c)
    body.parameter('index','Int')
    variant_step=body.finish(body.variant_parameter('state',shape))
    b=PlanBuilder('variant-loop',c)
    initial=b.variant('initial',shape,'Empty',b.literal('text','Text','empty'))
    choice=b.repeat('loop',b.literal('n','Int',2),2,initial,variant_step)
    assert_type(choice,Ref[Variant])
    return [budget_plan,b.finish(choice)]


def build_all() -> list[CheckedPlan[Any]]:
    c = catalog()
    return [sequential(c), *parameters(c), pure_and_calls(c), *tasks(c), capabilities(c), admission(c), affine_tasks(c), resource_bundles(c), *resource_outcomes(c), *variants(c), inference_results(c), *affine_loops(c), *inference_progress(c)]


def inference_progress(c: Catalog) -> list[CheckedPlan[Any]]:
    b=PlanBuilder('typed-progress',c)
    model: ToolRef[str,str]=b.tool('model')
    value=b.infer_progress('result',model,b.literal('prompt','Text','question'))
    assert_type(value,Ref[Variant])
    initial=b.finish(value)
    b=PlanBuilder('typed-resume',c)
    model=b.tool('model')
    operation=b.operation_parameter('operation',model)
    assert_type(operation,Ref[Operation[str]])
    resumed=b.resume_infer('result',operation)
    assert_type(resumed,Ref[Variant])
    resumed_plan=b.finish(resumed)
    b=PlanBuilder('typed-try-infer',c)
    model=b.tool('model')
    attempted=b.try_infer('result',model,b.literal('prompt','Text','question'))
    assert_type(attempted,Ref[Variant])
    return [initial,resumed_plan,b.finish(attempted)]


if __name__ == '__main__':
    print('Shared IR accepted', len(build_all()), 'typed fixture plans.')
