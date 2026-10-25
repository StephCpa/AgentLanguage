"""The Python SDK equivalent of repair.ir.json. Run as a module from the repo."""
from typing import TypedDict

from al0.demo import catalog
from al0.ir import Catalog, CheckedPlan
from al0.sdk import PlanBuilder, ToolRef


class Patch(TypedDict):
    patch: str
    base_version: int


def build(specs: Catalog | None = None) -> CheckedPlan[str]:
    builder = PlanBuilder('repair-example', specs or catalog())
    model: ToolRef[str, Patch] = builder.tool('code-model')
    checker: ToolRef[Patch, bool] = builder.tool('patch-tests')
    merge: ToolRef[Patch, object] = builder.tool('repo.merge')
    request = builder.literal('request', 'Text', 'Fix the off-by-one error at base version 7.')
    draft = builder.infer('draft', model, request)
    tested = builder.check('tested', checker, draft)
    intent = builder.prepare('intent', merge, tested)
    permit = builder.authorize('permit', intent)
    operation = builder.admit('operation', intent, permit)
    status = builder.await_result('status', operation)
    result = builder.match_business('result', status, 'Text', applied='Done',
                                     proven_no_effect='NeedsRevision', pending='Waiting',
                                     unknown='NeedsReconciliation', not_dispatched='Waiting')
    return builder.finish(result)


if __name__ == '__main__':
    print(build().encoded)
