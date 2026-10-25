import contextlib
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import types
import unittest

from al0.codegen import generate_bindings, main
from al0.data import Principal
from al0.demo import Endpoint, adapters, catalog as demo_catalog
from al0.errors import InvalidData, TypeCheckError
from al0.execute import Broker, Interpreter
from al0.ir import Catalog, Tool
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.generated_repair import build
from examples.repair_sdk import build as sdk_build
from tests.type_cases.codegen_catalog import catalog
from tests.type_cases.codegen_positive import build_all
from tests.type_cases.generated_catalog import Bindings


ROOT = Path(__file__).resolve().parents[1]


def module(specs):
    result = types.ModuleType('generated_test')
    exec(compile(generate_bindings(specs), '<generated-bindings>', 'exec'), result.__dict__)
    return result


class CodegenTests(unittest.TestCase):
    def test_generation_is_deterministic_and_registration_order_independent(self):
        first = catalog()
        second = catalog()
        second.tools = dict(reversed(list(second.tools.items())))
        second._schemas = dict(reversed(list(second._schemas.items())))
        self.assertEqual(generate_bindings(first), generate_bindings(second))
        self.assertEqual(generate_bindings(first), generate_bindings(first))
        self.assertTrue(module(second).Schema_Envelope)

    def test_schema_snapshot_is_detached(self):
        specs = catalog()
        original = specs.fingerprint()
        snapshot = specs.record_schemas()
        snapshot['Payload']['text'] = 'Bool'
        snapshot['New'] = {'x': 'Int'}
        self.assertEqual(specs.fingerprint(), original)

    def test_checked_in_generated_bindings_are_current(self):
        for path, specs in [('examples/generated_repair_bindings.py', demo_catalog()),
                            ('tests/type_cases/generated_catalog.py', catalog())]:
            self.assertEqual((ROOT/path).read_text(encoding='utf-8'), generate_bindings(specs))

    def test_malformed_schema_dependencies_fail_explicitly(self):
        for definition in ('{"value":"Missing"}', '{"value":"Cycle"}'):
            specs = Catalog()
            specs._schemas['Cycle'] = definition
            with self.assertRaisesRegex(TypeCheckError, 'dependency or cycle'):
                generate_bindings(specs)

    def test_arbitrary_names_collisions_and_field_keys_remain_data(self):
        specs = Catalog()
        names = ['a-b', 'a_b', 'class', '中文', "x'); raise RuntimeError('injection'); #", 'a:b', 'a']
        for name in names:
            specs.schema(name, {'c' if name == 'a:b' else 'b:c': 'Text', 'with space': 'Int'})
            specs.register(Tool.define(name, 'observe', name, name, 'read', {'u': 1}))
        generated = module(specs)
        self.assertEqual(len(set(generated.SCHEMA_TYPES.values())), len(names))
        self.assertEqual(len(set(generated.TOOL_TAGS.values())), len(names))
        self.assertNotEqual(generated.METHOD_NAMES[('field', 'a:b', 'c')],
                            generated.METHOD_NAMES[('field', 'a', 'b:c')])
        for index, name in enumerate(names):
            b = PlanBuilder('names-'+str(index), specs)
            g = generated.Bindings(b)
            field = 'c' if name == 'a:b' else 'b:c'
            value = getattr(g, generated.METHOD_NAMES[('literal', name)])('value', {field: 'ok', 'with space': 2})
            result = getattr(g, generated.METHOD_NAMES[('field', name, field)])('result', value)
            self.assertEqual(b.finish(result).result_type.name, 'Text')

    def test_tool_helpers_follow_actual_kind_and_nominal_identity(self):
        generated = module(catalog())
        self.assertIsNot(generated.Tool_send_a, generated.Tool_send_b)
        self.assertFalse(issubclass(generated.Tool_send_a, generated.Tool_send_b))
        self.assertTrue(hasattr(generated.Bindings, 'infer_compose'))
        self.assertFalse(hasattr(generated.Bindings, 'observe_compose'))
        self.assertEqual(len(build_all()), 9)

    def test_field_projection_preserves_source_and_drops_check_receipts(self):
        plans = build_all()
        self.assertEqual([p.result_type.kind for p in plans[2:5]], ['Candidate', 'Observation', 'Data'])
        self.assertEqual(plans[5].result_type.kind, 'Variant')
        self.assertEqual([p.result_type.name for p in plans[2:5]], ['Text', 'Int', 'Text'])
        self.assertEqual(plans[4].result_type.predicates, frozenset())

    def test_wrong_catalog_is_rejected_before_binding_or_nodes(self):
        b = PlanBuilder('wrong', Catalog())
        with self.assertRaisesRegex(TypeCheckError, 'different catalog'):
            Bindings(b)
        self.assertEqual(b._nodes, [])
        self.assertIsNone(b._bound_catalog)

    def test_catalog_metadata_drift_rejects_generated_calls_without_mutation(self):
        mutations = []
        for change in ({'version': '2'}, {'scope': 'elsewhere'}, {'upper_bound_json': '{"u":2}'},
                       {'flow_json': '{"recipients":[],"accepted_taints":[],"output":{"readers":null,"taints":[]}}'}):
            mutations.append(lambda c, change=change: c.tools.__setitem__('send-a', replace(c.tools['send-a'], **change)))
        mutations.extend([lambda c: c.schema('Added', {'x': 'Text'}),
                          lambda c: c._schemas.__setitem__('Payload', '{"text":"Text"}'),
                          lambda c: c.register(Tool.define('extra', 'infer', 'Text', 'Text', 'extra', {'u': 1}))])
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                c = catalog()
                b = PlanBuilder('drift', c)
                g = Bindings(b)
                mutate(c)
                with self.assertRaisesRegex(TypeCheckError, 'catalog'):
                    g.literal_Text('value', 'hello')
                self.assertEqual((b._nodes, b._effects), ([], []))

    def test_pinned_builder_also_rejects_raw_sdk_calls_and_finish(self):
        c = catalog()
        b = PlanBuilder('raw-drift', c)
        g = Bindings(b)
        value = g.literal_Text('value', 'hello')
        tool = b.tool('compose')
        before = list(b._nodes)
        c.schema('Added', {'x': 'Text'})
        for operation in (lambda: b.tool('compose'), lambda: b.literal('extra', 'Text', 'x'),
                          lambda: b.infer('draft', tool, value), lambda: b.finish(value)):
            with self.assertRaisesRegex(TypeCheckError, 'catalog'):
                operation()
            self.assertEqual(b._nodes, before)
            self.assertEqual(b._effects, [])

    def test_unbound_builder_remains_available_for_catalog_configuration(self):
        c = Catalog()
        b = PlanBuilder('unbound', c)
        c.schema('Added', {'x': 'Text'})
        self.assertEqual(b.finish(b.literal('value', 'Added', {'x': 'ok'})).result_type.name, 'Added')

    def test_generated_helpers_keep_runtime_value_checks(self):
        for value in ({'text': 'ok', 'count': 1, 'extra': 0}, {'text': 'ok', 'count': True}):
            b = PlanBuilder('bad-value', catalog())
            with self.assertRaises(InvalidData):
                Bindings(b).literal_Payload('value', value)
            self.assertEqual(b._nodes, [])

    def test_cli_write_check_stale_check_and_input_preservation(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            output = Path(folder)/'generated.py'
            args = ['--catalog', str(ROOT/'examples/bounded_batches.catalog.json'), '--output', str(output)]
            self.assertEqual(main(args), 0)
            self.assertEqual(main([*args, '--check']), 0)
            output.write_text('stale\n', encoding='utf-8')
            self.assertEqual(main([*args, '--check']), 1)
            self.assertEqual(output.read_text(encoding='utf-8'), 'stale\n')
            self.assertEqual(main(['--catalog', str(output), '--output', str(output)]), 1)
            self.assertEqual(output.read_text(encoding='utf-8'), 'stale\n')
            output.unlink()
            self.assertEqual(main([*args, '--check']), 1)
            self.assertFalse(output.exists())

    def test_generated_repair_has_identical_ir_execution_and_replay(self):
        self.assertEqual(build().encoded, sdk_build().encoded)
        outcomes = []
        for plan in (build(), sdk_build()):
            with tempfile.TemporaryDirectory() as folder:
                c = demo_catalog()
                store = Store(Path(folder)/'runtime.sqlite')
                endpoint = Endpoint(Path(folder)/'endpoint.sqlite')
                principal = Principal('tenant', 'subject')
                run = store.create_run(principal, plan.fingerprint, {'units': 10})
                grants = {name: store.grant(run, name, spec.scope, expires=store.clock()+1000)
                          for name, spec in c.tools.items()}
                interpreter = Interpreter(Broker(store, c, adapters(endpoint)))
                result = interpreter.run(plan, run, principal, grants)
                events = store.events(run, principal)
                self.assertEqual(interpreter.run(plan, run, principal, grants).data, result.data)
                self.assertEqual(store.events(run, principal), events)
                trace = [(e['kind'], e['data'].get('node'), e['data'].get('outcome')) for e in events]
                outcomes.append((result.data, trace, store.budget(run, principal), endpoint.state()))
        self.assertEqual(outcomes[0], outcomes[1])
