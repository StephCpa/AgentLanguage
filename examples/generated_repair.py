"""The repair program authored through generated Catalog bindings."""
from al0.demo import catalog
from al0.ir import Catalog, CheckedPlan
from al0.sdk import PlanBuilder
from .generated_repair_bindings import Bindings


def build(specs: Catalog | None=None) -> CheckedPlan[str]:
    b = PlanBuilder('repair-example', specs or catalog())
    g = Bindings(b)
    request = g.literal_Text('request', 'Fix the off-by-one error at base version 7.')
    draft = g.infer_code_model('draft', request)
    tested = g.check_patch_tests('tested', draft)
    intent = g.prepare_repo_merge('intent', tested)
    permit = b.authorize('permit', intent)
    operation = b.admit('operation', intent, permit)
    status = b.await_result('status', operation)
    result = b.match_business('result', status, 'Text', applied='Done',
                              proven_no_effect='NeedsRevision', pending='Waiting',
                              unknown='NeedsReconciliation', not_dispatched='Waiting')
    return b.finish(result)


if __name__ == '__main__':
    print(build().encoded)
