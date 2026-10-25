from al0.sdk import Cap, Intent, Operation, Permit, PlanBuilder, Ref
from .generated_catalog import Bindings, Schema_Payload, Tool_send_a, Tool_send_b


def wrong_field(g: Bindings) -> None:
    g.literal_Payload('bad', {'text': 1, 'count': 2})  # E: record_field_type


def missing_field(g: Bindings) -> None:
    g.literal_Payload('bad', {'text': 'hello'})  # E: missing_record_field


def unknown_field(g: Bindings) -> None:
    g.literal_Payload('bad', {'text': 'hello', 'count': 2, 'extra': True})  # E: unknown_record_field


def nested_field(g: Bindings) -> None:
    g.literal_Envelope('bad', {'payload': {'text': 'x', 'count': 'one'}, 'extras': None})  # E: nested_record_field


def wrong_tool_input(g: Bindings, value: Ref[int]) -> None:
    g.infer_compose('bad', value)  # E: generated_tool_input


def not_an_observer(g: Bindings, value: Ref[str]) -> None:
    g.observe_compose('bad', value)  # E: nonexistent_tool_kind


def json_float(g: Bindings) -> None:
    g.literal_Json('bad', 1.5)  # E: json_float


def candidate_erasure(g: Bindings) -> None:
    value: Ref[Schema_Payload] = g.parameter_Payload('bad', kind='Candidate')  # E: candidate_parameter_erasure


def projected_candidate_erasure(g: Bindings) -> None:
    source = g.parameter_Payload('candidate', kind='Candidate')
    value: Ref[str] = g.field_Payload_text('bad', source)  # E: candidate_projection_erasure


def wrong_permission(b: PlanBuilder, intent: Ref[Intent[Tool_send_a]], permit: Ref[Permit[Tool_send_b]]) -> None:
    b.admit('bad', intent, permit)  # E: same_shape_tool_permit


def wrong_cap(b: PlanBuilder, intent: Ref[Intent[Tool_send_a]], cap: Ref[Cap[Tool_send_b]]) -> None:
    b.authorize_with('bad', intent, cap)  # E: same_shape_tool_cap


def wrong_reconciler(g: Bindings, operation: Ref[Operation[Tool_send_b]]) -> None:
    g.reconcile_query_a('bad', operation)  # E: generated_reconciliation_target


def wrong_model_operation(g: Bindings, operation: Ref[Operation[Tool_send_b]]) -> None:
    g.resume_infer_compose('bad', operation)  # E: generated_model_operation_identity


def wrong_try_infer(g: Bindings) -> None:
    g.try_infer_compose('bad', g.literal_Int('number', 1))  # E: generated_try_infer_input
