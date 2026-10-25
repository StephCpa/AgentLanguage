from typing import Any, TYPE_CHECKING
from al0.ir import CheckedPlan
from al0.sdk import Candidate, Checked, Observation, Operation, PlanBuilder, Ref, Variant
from .codegen_catalog import catalog
from .generated_catalog import Bindings, Schema_Payload, Tool_send_a, Tool_compose

if TYPE_CHECKING:
    from typing_extensions import assert_type
else:
    def assert_type(value: Any, typ: Any) -> Any:
        return value


def build_all() -> list[CheckedPlan[Any]]:
    c = catalog()
    b = PlanBuilder('generated-tools', c)
    g = Bindings(b)
    request = g.literal_Text('request', 'hello')
    draft = g.infer_compose('draft', request)
    assert_type(draft, Ref[Candidate[Schema_Payload]])
    observed = g.observe_read('observed', draft)
    assert_type(observed, Ref[Observation[Schema_Payload]])
    checked = g.check_validate('checked', observed)
    assert_type(checked, Ref[Checked[Schema_Payload]])
    cap = g.capability_send_a('cap')
    intent = g.prepare_send_a('intent', checked)
    op = b.admit('op', intent, b.authorize_with('permit', intent, cap))
    assert_type(op, Ref[Operation[Tool_send_a]])
    snapshot = g.reconcile_query_a('reconciled', op)
    result = b.match_business('result', snapshot, 'Text', applied='Done', proven_no_effect='NoEffect',
                              pending='Pending', unknown='Unknown', not_dispatched='Queued')
    plans: list[CheckedPlan[Any]] = [b.finish(result)]
    b = PlanBuilder('typed-literals', c)
    g = Bindings(b)
    literal = g.literal_Envelope('envelope', {'payload': {'text': 'hello', 'count': 2}, 'extras': {'ok': [True, None]}})
    payload = g.field_Envelope_payload('payload', literal)
    assert_type(payload, Ref[Schema_Payload])
    count = g.field_Payload_count('count', payload)
    assert_type(count, Ref[int])
    plans.append(b.finish(count))
    b = PlanBuilder('typed-candidate-field', c)
    g = Bindings(b)
    candidate = g.parameter_Payload('value', kind='Candidate')
    assert_type(candidate, Ref[Candidate[Schema_Payload]])
    projected = g.field_Payload_text('projected', candidate)
    assert_type(projected, Ref[Candidate[str]])
    plans.append(b.finish(projected))
    b = PlanBuilder('typed-observation-field', c)
    g = Bindings(b)
    observation = g.parameter_Payload('value', kind='Observation')
    assert_type(observation, Ref[Observation[Schema_Payload]])
    observed_field = g.field_Payload_count('projected', observation)
    assert_type(observed_field, Ref[Observation[int]])
    plans.append(b.finish(observed_field))
    b = PlanBuilder('typed-checked-field', c)
    g = Bindings(b)
    checked_parameter = g.parameter_Payload('value', kind='Checked', predicates=['valid'])
    assert_type(checked_parameter, Ref[Checked[Schema_Payload]])
    changed = g.field_Payload_text('projected', checked_parameter)
    assert_type(changed, Ref[str])
    plans.append(b.finish(changed))
    b = PlanBuilder('generated-inference-result', c)
    g = Bindings(b)
    request = g.literal_Text('request', 'hello')
    inference = g.infer_result_compose('result', request)
    assert_type(inference, Ref[Variant])
    plans.append(b.finish(inference))
    b = PlanBuilder('generated-progress', c)
    g = Bindings(b)
    progress = g.infer_progress_compose('result', g.literal_Text('prompt', 'question'))
    assert_type(progress, Ref[Variant])
    plans.append(b.finish(progress))
    b = PlanBuilder('generated-resume', c)
    g = Bindings(b)
    operation = g.operation_parameter_compose('operation')
    assert_type(operation, Ref[Operation[Tool_compose]])
    resumed = g.resume_infer_compose('result', operation)
    assert_type(resumed, Ref[Variant])
    plans.append(b.finish(resumed))
    b = PlanBuilder('generated-try-infer', c)
    g = Bindings(b)
    attempted = g.try_infer_compose('result', g.literal_Text('prompt', 'question'))
    assert_type(attempted, Ref[Variant])
    plans.append(b.finish(attempted))
    return plans
