from copy import deepcopy
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest

from al0.data import Principal
from al0.demo import Endpoint, adapters, catalog, demo
from al0.errors import AuthorizationDenied, EvidenceMismatch, InvalidData, ReplayMismatch, TypeCheckError
from al0.execute import Broker, Interpreter, Response, Suspended
from al0.ir import CheckedPlan, check_plan
from al0.store import Store


EXAMPLE = Path(__file__).resolve().parents[1] / 'examples/repair.ir.json'


class IRTests(unittest.TestCase):
    def setUp(self):
        self.doc = json.loads(EXAMPLE.read_text(encoding='utf-8'))
        self.catalog = catalog()

    def reject(self, modify):
        modify(self.doc)
        with self.assertRaises((TypeCheckError, InvalidData)):
            check_plan(self.doc, self.catalog)

    def test_reference_plan_is_checked(self):
        self.assertEqual(check_plan(self.doc, self.catalog).fingerprint,
                         check_plan(json.dumps(self.doc), self.catalog).fingerprint)

    def test_candidate_cannot_bypass_required_check(self):
        self.reject(lambda doc: doc['nodes'][3].update(input='draft'))

    def test_ordinary_json_cannot_forge_a_permit(self):
        def modify(doc):
            doc['nodes'][4] = {'id': 'permit', 'op': 'literal', 'type': 'Json', 'value': {'approved': True}}
        self.reject(modify)

    def test_permit_cannot_be_moved_twice(self):
        self.reject(lambda doc: doc['nodes'].insert(6, {'id': 'second', 'op': 'admit', 'intent': 'intent', 'permit': 'permit'}))

    def test_permit_cannot_authorize_another_preparation(self):
        def modify(doc):
            doc['nodes'].insert(5, {'id': 'other', 'op': 'prepare', 'tool': 'repo.merge', 'input': 'tested'})
            doc['nodes'][6]['intent'] = 'other'
        self.reject(modify)

    def test_undeclared_effect_rejected(self):
        self.reject(lambda doc: doc['uses'].remove('admit:repo.merge@repo'))

    def test_unknown_must_be_handled(self):
        self.reject(lambda doc: doc['nodes'][-1]['cases'].pop('Unknown'))

    def test_ignored_status_does_not_count_as_handled(self):
        def modify(doc):
            doc['nodes'][-1] = {'id': 'result', 'op': 'literal', 'type': 'Text', 'value': 'Done'}
        self.reject(modify)

    def test_returning_operation_reference_is_allowed(self):
        self.doc['nodes'] = self.doc['nodes'][:6]
        self.doc['return'] = 'operation'
        check_plan(self.doc, self.catalog)

    def test_host_code_and_unknown_fields_rejected(self):
        self.reject(lambda doc: doc['nodes'][0].update(python='print(1)'))

    def test_forward_reference_rejected(self):
        self.reject(lambda doc: doc['nodes'][1].update(input='tested'))

    def test_duplicate_node_identity_rejected(self):
        self.reject(lambda doc: doc['nodes'][1].update(id='request'))


class InterpreterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        self.store = Store(directory / 'runtime.sqlite')
        self.endpoint = Endpoint(directory / 'endpoint.sqlite')
        self.catalog = catalog()
        self.plan = check_plan(EXAMPLE.read_text(encoding='utf-8'), self.catalog)
        self.principal = Principal('tenant', 'subject')
        self.run = self.store.create_run(self.principal, self.plan.fingerprint, {'units': 10})
        self.grants = {name: self.store.grant(self.run, name, spec.scope, expires=self.store.clock()+3600)
                       for name, spec in self.catalog.tools.items()}
        self.adapters = adapters(self.endpoint)

    def execute(self, plan=None, grants=None):
        return Interpreter(Broker(self.store, self.catalog, self.adapters)).run(
            plan or self.plan, self.run, self.principal, self.grants if grants is None else grants)

    def test_end_to_end_replay_has_no_new_effects_or_cost(self):
        self.assertEqual(self.execute().data, 'Done')
        before = self.store.events(self.run, self.principal)
        self.store = Store(self.store.path)
        self.assertEqual(self.execute().data, 'Done')
        self.assertEqual(before, self.store.events(self.run, self.principal))
        self.assertEqual(self.endpoint.state()['version'], 8)
        self.assertEqual(self.endpoint.state()['business_operations'], 1)
        self.assertEqual(self.store.budget(self.run, self.principal)['units']['confirmed'], 4)

    def test_changed_program_cannot_resume_old_run(self):
        doc = json.loads(self.plan.encoded)
        doc['nodes'][0]['value'] = 'different request'
        with self.assertRaises(ReplayMismatch):
            self.execute(check_plan(doc, self.catalog))

    def test_checked_plan_constructor_is_not_a_validation_bypass(self):
        doc = json.loads(self.plan.encoded)
        doc['nodes'][3]['input'] = 'draft'
        with self.assertRaises(TypeCheckError):
            self.execute(CheckedPlan(json.dumps(doc), 'fake'))

    def test_no_host_grant_means_no_authority(self):
        with self.assertRaises(AuthorizationDenied):
            self.execute(grants={})
        self.assertEqual(self.store.budget(self.run, self.principal)['units']['free'], 10)

    def test_model_schema_error_still_records_actual_cost(self):
        self.adapters['code-model'] = lambda _: Response('invalid patch shape', {'units': 2})
        with self.assertRaises(InvalidData):
            self.execute()
        self.assertEqual(self.store.budget(self.run, self.principal)['units']['confirmed'], 2)
        self.assertEqual(self.endpoint.state()['business_operations'], 0)

    def test_checker_rejection_does_not_create_action(self):
        self.adapters['patch-tests'] = lambda _: Response(False, {'units': 1})
        with self.assertRaises(EvidenceMismatch):
            self.execute()
        self.assertEqual(self.endpoint.state()['business_operations'], 0)
        self.assertEqual(self.store.budget(self.run, self.principal)['units']['confirmed'], 3)

    def test_timeout_preserves_unknown_and_resume_does_not_reinvoke(self):
        calls = []
        def timeout(ticket):
            calls.append(ticket.attempt)
            raise TimeoutError('response lost')
        self.adapters['code-model'] = timeout
        for _ in range(2):
            with self.assertRaises(Suspended) as caught:
                self.execute()
            self.assertEqual(caught.exception.business, 'Unknown')
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.budget(self.run, self.principal)['units']['reserved'], 2)

    def test_external_effect_with_lost_response_is_not_repeated(self):
        calls = []
        def lost_response(ticket):
            calls.append(ticket.attempt)
            self.endpoint.merge(ticket)
            raise ConnectionError('reply lost after endpoint commit')
        self.adapters['repo.merge'] = lost_response
        self.assertEqual(self.execute().data, 'NeedsReconciliation')
        self.assertEqual(self.execute().data, 'NeedsReconciliation')
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.endpoint.state()['version'], 8)
        self.assertEqual(self.endpoint.state()['attempt_charges'], 1)
        self.assertEqual(self.store.budget(self.run, self.principal)['units']['reserved'], 1)

    def test_endpoint_rejects_stale_version_and_still_charges(self):
        db = sqlite3.connect(self.endpoint.path)
        db.execute('UPDATE repo SET version=8 WHERE id=1')
        db.commit()
        db.close()
        self.assertEqual(self.execute().data, 'NeedsRevision')
        self.assertEqual(self.endpoint.state()['version'], 8)
        self.assertEqual(self.endpoint.state()['patch'], 'old')
        self.assertEqual(self.store.budget(self.run, self.principal)['units']['confirmed'], 4)


if __name__ == '__main__':
    unittest.main()
