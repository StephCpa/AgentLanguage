"""Each E marker must produce a mypy diagnostic on that exact source line."""
from al0.ir import CheckedPlan, Type
from al0.inference_response import InferenceResponse
from al0.sdk import (Budget, Bundle, Candidate, Cap, Intent, Operation, OpRef, Permit,
                     PlanBuilder, Ref, Scope, Snapshot, Task, TaskOutcome, ToolRef, Variant)


def wrong_model_input(b: PlanBuilder, tool: ToolRef[str, str], value: Ref[int]) -> None:
    b.infer('bad', tool, value)  # E: wrong_model_input


def candidate_bypasses_check(b: PlanBuilder, tool: ToolRef[str, str], value: Ref[Candidate[str]]) -> None:
    b.prepare('bad', tool, value)  # E: candidate_bypasses_check


def authorize_data(b: PlanBuilder, value: Ref[str]) -> None:
    b.authorize('bad', value)  # E: authorize_data


def wrong_permit_category(b: PlanBuilder, intent: Ref[Intent[str]], funds: Ref[Budget]) -> None:
    b.admit('bad', intent, funds)  # E: wrong_permit_category


def incompatible_permit(b: PlanBuilder, intent: Ref[Intent[str]], permit: Ref[Permit[int]]) -> None:
    b.admit('bad', intent, permit)  # E: incompatible_permit


def incompatible_cap(b: PlanBuilder, intent: Ref[Intent[str]], cap: Ref[Cap[int]]) -> None:
    b.authorize_with('bad', intent, cap)  # E: incompatible_cap


def await_borrowed_operation(b: PlanBuilder, borrowed: Ref[OpRef[str]]) -> None:
    b.await_result('bad', borrowed)  # E: await_borrowed_operation


def inspect_funds(b: PlanBuilder, funds: Ref[Budget]) -> None:
    b.inspect('bad', funds)  # E: inspect_funds


def close_task(b: PlanBuilder, task: Ref[Task[str]]) -> None:
    b.close_scope('bad', task)  # E: close_task


def join_scope(b: PlanBuilder, scope: Ref[Scope]) -> None:
    b.join('bad', scope)  # E: join_scope


def scope_as_budget(b: PlanBuilder, scope: Ref[Scope]) -> None:
    b.join_budget('bad', scope)  # E: scope_as_budget


def noninteger_resources(b: PlanBuilder, value: Ref[str]) -> None:
    b.resources('bad', {'u': value})  # E: noninteger_resources


def nonboolean_condition(b: PlanBuilder, condition: Ref[str], branch: CheckedPlan[str]) -> None:
    b.if_else('bad', condition, then=branch, otherwise=branch)  # E: nonboolean_condition


def inconsistent_branches(b: PlanBuilder, condition: Ref[bool], text: CheckedPlan[str], number: CheckedPlan[int]) -> None:
    b.if_else('bad', condition, then=text, otherwise=number)  # E: inconsistent_branches


def wrong_invoke_result(b: PlanBuilder, child: CheckedPlan[str]) -> None:
    value: Ref[int] = b.invoke('bad', child)  # E: wrong_invoke_result


def wrong_task_result(b: PlanBuilder, scope: Ref[Scope], child: CheckedPlan[str]) -> None:
    task: Ref[Task[int]] = b.spawn('bad', scope, child, {})  # E: wrong_task_result


def wrong_join_result(b: PlanBuilder, task: Ref[Task[str]]) -> None:
    value: Ref[int] = b.join('bad', task)  # E: wrong_join_result


def wrong_literal_result(b: PlanBuilder) -> None:
    value: Ref[int] = b.literal('bad', 'Text', 'hello')  # E: wrong_literal_result


def noninteger_loop_count(b: PlanBuilder, count: Ref[str], state: Ref[str], body: CheckedPlan[str]) -> None:
    b.repeat('bad', count, 2, state, body)  # E: noninteger_loop_count


def wrong_loop_state(b: PlanBuilder, count: Ref[int], state: Ref[str], body: CheckedPlan[int]) -> None:
    b.repeat('bad', count, 2, state, body)  # E: wrong_loop_state


def incompatible_race(b: PlanBuilder, first: Ref[Task[str]], second: Ref[Task[int]]) -> None:
    b.race('bad', [first, second])  # E: incompatible_race


def invalid_literal_budget(b: PlanBuilder) -> None:
    b.scope('bad', {'u': 'one'})  # E: invalid_literal_budget


def wrong_snapshot_type(b: PlanBuilder, operation: Ref[Operation[str]]) -> None:
    value: Ref[Snapshot[int]] = b.await_result('bad', operation)  # E: wrong_snapshot_type


def task_outcome_is_not_business_status(b: PlanBuilder, outcome: Ref[TaskOutcome[str]]) -> None:
    b.match_business('bad', outcome, 'Text', applied='', proven_no_effect='', pending='', unknown='', not_dispatched='')  # E: task_outcome_is_not_business_status


def inconsistent_task_branches(b: PlanBuilder, outcome: Ref[TaskOutcome[str]], text: CheckedPlan[str], number: CheckedPlan[int]) -> None:
    b.match_task('bad', outcome, completed=text, failed=number, waiting=text, cancelled=text)  # E: inconsistent_task_branches


def unpack_scalar_budget(b: PlanBuilder, budget: Ref[Budget]) -> None:
    b.unpack('bad', budget)  # E: unpack_scalar_budget


def pack_unreferenced_data(b: PlanBuilder) -> None:
    b.pack('bad', {'value': True})  # E: pack_unreferenced_data


def take_without_unpack(b: PlanBuilder, packet: Ref[Bundle]) -> None:
    b.take('bad', packet, 'funds')  # E: take_without_unpack


def inject_raw_variant_payload(b: PlanBuilder, interface: Type) -> None:
    b.variant('bad', interface, 'Ok', 3)  # E: inject_raw_variant_payload


def match_bundle_as_variant(b: PlanBuilder, packet: Ref[Bundle], case: CheckedPlan[str]) -> None:
    b.match_variant('bad', packet, {'Ok': case})  # E: match_bundle_as_variant


def unpack_variant_as_bundle(b: PlanBuilder, value: Ref[Variant]) -> None:
    b.unpack('bad', value)  # E: unpack_variant_as_bundle


def inference_result_misuse(b: PlanBuilder) -> None:
    model: ToolRef[str, str] = b.tool('model')
    prompt = b.literal('prompt', 'Text', 'question')
    bad: Ref[Candidate[str]] = b.infer_result('result', model, prompt)  # E: inference_result_is_not_candidate
    b.infer_result('wrong-input', model, b.literal('integer', 'Int', 4))  # E: inference_result_input_type


def inference_progress_misuse(b: PlanBuilder, candidate: Ref[Candidate[str]], view: Ref[OpRef[str]]) -> None:
    b.resume_infer('candidate', candidate)  # E: resume_candidate_not_operation
    b.resume_infer('readonly', view)  # E: resume_readonly_not_control
    bad: Ref[Candidate[str]] = b.infer_progress('result', b.tool('model'), b.literal('prompt','Text','q'))  # E: progress_not_candidate


def try_infer_misuse(b: PlanBuilder, model: ToolRef[str, str]) -> None:
    b.try_infer('wrong', model, b.literal('number', 'Int', 1))  # E: try_infer_input_type
    bad: Ref[Candidate[str]] = b.try_infer('result', model, b.literal('prompt', 'Text', 'q'))  # E: try_infer_not_candidate


def invalid_provider_envelope() -> None:
    InferenceResponse('Pending')  # E: provider_response_kind
    InferenceResponse('Refused', actual={'u': 1.5})  # E: provider_fee_type
