"""Statically accepted programs whose errors require shared IR/SDK checking.

Keeping these in the type gate prevents attributing dependent or affine rules
to ordinary Python generics. None is executed as an external action.
"""
from collections.abc import Callable
from typing import Any

from al0.errors import InvalidData, TypeCheckError
from al0.ir import CheckedPlan
from al0.sdk import PlanBuilder, Ref, ToolRef
from .positive import catalog, send


def reused_permit() -> CheckedPlan[Any]:
    b = PlanBuilder('reused-permit', catalog())
    intent = b.prepare('intent', send(b), b.literal('message', 'Text', 'hello'))
    permit = b.authorize('permit', intent)
    b.admit('first', intent, permit)
    second = b.admit('second', intent, permit)
    return b.finish(second)


def different_intent_same_type() -> CheckedPlan[Any]:
    b = PlanBuilder('different-intent', catalog())
    message = b.literal('message', 'Text', 'hello')
    first = b.prepare('first', send(b), message)
    second = b.prepare('second', send(b), message)
    permit = b.authorize('permit', first)
    return b.finish(b.admit('operation', second, permit))


def foreign_owner() -> CheckedPlan[Any]:
    c = catalog()
    first, second = PlanBuilder('first', c), PlanBuilder('second', c)
    value = first.literal('value', 'Text', 'hello')
    reader: ToolRef[str, str] = second.tool('read')
    return second.finish(second.observe('wrong-owner', reader, value))


def tool_kind() -> CheckedPlan[Any]:
    b = PlanBuilder('wrong-tool-kind', catalog())
    observer: ToolRef[str, str] = b.tool('read')
    return b.finish(b.infer('wrong-kind', observer, b.literal('value', 'Text', 'hello')))


def declared_schema() -> CheckedPlan[Any]:
    b = PlanBuilder('declared-schema', catalog())
    return b.finish(b.literal('wrong-schema', 'Int', 'hello'))


def dynamic_projection() -> CheckedPlan[Any]:
    b = PlanBuilder('dynamic-projection', catalog())
    record = b.record('record', 'Record', {'number': b.literal('number', 'Int', 1),
                                          'text': b.literal('text', 'Text', 'hello')})
    projection: Ref[int] = b.field('projection', record, 'text')
    result = b.compute('bad-add', 'add', projection, b.literal('one', 'Int', 1))
    return b.finish(result)


def boolean_budget() -> CheckedPlan[Any]:
    b = PlanBuilder('boolean-budget', catalog())
    funds = b.split_budget('funds', {'u': True})
    return b.finish(b.join_budget('joined', funds))


CASES: tuple[tuple[Callable[[], CheckedPlan[Any]], type[Exception]], ...] = (
    (reused_permit, TypeCheckError),
    (different_intent_same_type, TypeCheckError),
    (foreign_owner, TypeCheckError),
    (tool_kind, TypeCheckError),
    (declared_schema, InvalidData),
    (dynamic_projection, TypeCheckError),
    (boolean_budget, InvalidData),
)


def verify() -> list[str]:
    checked = []
    for build, error in CASES:
        try:
            build()
        except error:
            checked.append(build.__name__)
        else:
            raise AssertionError('shared checker accepted '+build.__name__)
    return checked


if __name__ == '__main__':
    print('Shared checker rejected:', ', '.join(verify()))
