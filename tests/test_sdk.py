from pathlib import Path
import tempfile
import unittest

from al0.data import Principal
from al0.demo import Endpoint, adapters, catalog
from al0.errors import TypeCheckError
from al0.execute import Broker, Interpreter
from al0.ir import check_plan
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.repair_sdk import build
from examples.reconcile_sdk import demo as reconcile_demo
from examples.allocated_recovery import demo as allocated_demo


class SDKTests(unittest.TestCase):
    def test_worker_and_recovery_funds_share_one_root_without_releasing_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            result = allocated_demo(folder)
        self.assertEqual(result['first_result'], 'NeedsReconciliation')
        self.assertEqual(result['before_reconciliation']['status'], 'CompletedWithObligations')
        self.assertEqual(result['before_reconciliation']['budget']['units']['reserved'], 1)
        self.assertEqual(result['replayed_result'], 'Done')
        self.assertEqual(result['status'], 'CompletedClean')
        self.assertEqual(result['budget']['units'], dict(total=10, free=5, reserved=0, confirmed=5, retired=0))
        self.assertEqual(result['replay_added_events'], 0)
        self.assertEqual(result['endpoint']['business_operations'], 1)
        self.assertEqual(result['endpoint']['query_charges'], 1)
        self.assertEqual([a['state'] for a in result['allocations']], ['Open', 'Joined', 'Joined'])
        self.assertEqual(result['allocations'][0]['budget']['units']['free'], 5)

    def test_sdk_reconciliation_recovers_lost_reply_and_replays(self):
        with tempfile.TemporaryDirectory() as folder:
            result = reconcile_demo(folder)
        self.assertEqual(result['result'], 'Done')
        self.assertEqual(result['replayed_result'], 'Done')
        self.assertEqual(result['replay_added_events'], 0)
        self.assertEqual(result['run_status']['status'], 'CompletedClean')
        self.assertEqual(result['budget']['units']['confirmed'], 5)
        self.assertEqual(result['endpoint']['business_operations'], 1)
        self.assertEqual(result['endpoint']['query_charges'], 1)

    def test_sdk_and_handwritten_ir_have_identical_checked_representation(self):
        specs = catalog()
        source = (Path(__file__).resolve().parents[1]/'examples/repair.ir.json').read_text(encoding='utf-8')
        hand = check_plan(source, specs)
        sdk = build(specs)
        self.assertEqual(hand.encoded, sdk.encoded)
        self.assertEqual(hand.fingerprint, sdk.fingerprint)

    def test_independent_execution_matches_trace_budget_and_external_state(self):
        specs = catalog()
        source = (Path(__file__).resolve().parents[1]/'examples/repair.ir.json').read_text(encoding='utf-8')
        outcomes = []
        for plan in (check_plan(source, specs), build(specs)):
            with tempfile.TemporaryDirectory() as folder:
                store = Store(Path(folder)/'runtime.sqlite')
                endpoint = Endpoint(Path(folder)/'endpoint.sqlite')
                principal = Principal('tenant','subject')
                run = store.create_run(principal, plan.fingerprint, {'units': 10})
                grants = {name: store.grant(run, name, spec.scope, expires=store.clock()+1000)
                          for name, spec in specs.tools.items()}
                result = Interpreter(Broker(store, specs, adapters(endpoint))).run(plan, run, principal, grants)
                events = store.events(run, principal)
                # UUIDs deliberately differ; preserve event ordering and source sites.
                trace = [(e['kind'], e['data'].get('node'), e['data'].get('activation'),
                          e['data'].get('outcome'), e['data'].get('actual')) for e in events]
                outcomes.append((result.data, trace, store.budget(run, principal), endpoint.state()))
        self.assertEqual(outcomes[0], outcomes[1])

    def test_foreign_builder_reference_cannot_be_captured(self):
        first, second = PlanBuilder('a',catalog()), PlanBuilder('b',catalog())
        value = first.literal('request','Text','hello')
        with self.assertRaises(TypeCheckError):
            second.infer('draft', second.tool('code-model'), value)

    def test_foreign_tool_binding_is_rejected(self):
        first, second = PlanBuilder('a',catalog()), PlanBuilder('b',catalog())
        value = second.literal('request','Text','hello')
        with self.assertRaises(TypeCheckError):
            second.infer('draft', first.tool('code-model'), value)

    def test_explicit_effect_boundary_is_not_silently_expanded(self):
        builder = PlanBuilder('a',catalog(), uses=[])
        value = builder.literal('request','Text','hello')
        draft = builder.infer('draft',builder.tool('code-model'),value)
        with self.assertRaises(TypeCheckError):
            builder.finish(draft)

    def test_resource_moves_still_checked_by_shared_checker(self):
        builder = PlanBuilder('a',catalog())
        raw = builder.literal('raw','Patch',{'patch':'fix','base_version':7})
        checked = builder.check('tested',builder.tool('patch-tests'),raw)
        intent = builder.prepare('intent',builder.tool('repo.merge'),checked)
        permit = builder.authorize('permit',intent)
        builder.admit('first',intent,permit)
        second = builder.admit('second',intent,permit)
        with self.assertRaises(TypeCheckError):
            builder.finish(second)

    def test_literal_detaches_mutable_source(self):
        builder = PlanBuilder('a',catalog())
        value = {'x': 1}
        ref = builder.literal('value','Json',value)
        value['x'] = 2
        self.assertIn('"value":{"x":1}',builder.finish(ref).encoded)


if __name__ == '__main__':
    unittest.main()
