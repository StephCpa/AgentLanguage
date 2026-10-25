from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from al0.data import Intent, Principal, Slot
from al0.demo import Endpoint, adapters, catalog
from al0.errors import AuthorizationDenied, BudgetExceeded, EvidenceMismatch, StateConflict, TypeCheckError
from al0.execute import Broker, Interpreter, Response, Suspended
from al0.ir import check_plan
from al0.store import Store
from examples.repair_sdk import build


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.store = Store(self.folder/'runtime.sqlite')
        self.endpoint = Endpoint(self.folder/'endpoint.sqlite')
        self.catalog = catalog(reconciliation=True)
        self.plan = build(self.catalog)
        self.principal = Principal('tenant','subject')
        self.run = self.store.create_run(self.principal,self.plan.fingerprint,{'units':10})
        self.grants = {name:self.store.grant(self.run,name,spec.scope,expires=self.store.clock()+1000,max_attempts=2)
                       for name,spec in self.catalog.tools.items()}
        self.adapter_map = adapters(self.endpoint,reconciliation=True)
        def lost(ticket):
            self.endpoint.merge(ticket)
            raise ConnectionError('committed remotely, response lost')
        self.adapter_map['repo.merge'] = lost
        self.broker = Broker(self.store,self.catalog,self.adapter_map)
        result = Interpreter(self.broker).run(self.plan,self.run,self.principal,self.grants)
        self.assertEqual(result.data,'NeedsReconciliation')
        self.operation = next(x['operation'] for x in self.store.obligations(self.run,self.principal) if x['tool']=='repo.merge')
        self.store.finish_run(self.run,self.principal)

    def query(self, node='query-1', grant=None, broker=None):
        return (broker or self.broker).reconcile(self.run,self.principal,node,self.operation,'repo.query',
                                                grant or self.grants['repo.query'])

    def test_finished_control_retains_obligations_then_reconciles(self):
        self.assertEqual(self.store.status(self.run,self.principal)['status'],'CompletedWithObligations')
        self.assertEqual(self.store.budget(self.run,self.principal)['units']['reserved'],1)
        state = self.query()
        self.assertEqual(state['business'],'Applied')
        self.assertEqual(self.store.status(self.run,self.principal)['status'],'CompletedClean')
        self.assertEqual(self.store.budget(self.run,self.principal)['units']['confirmed'],5)
        self.assertEqual(self.endpoint.state()['business_operations'],1)
        self.assertEqual(self.endpoint.state()['query_charges'],1)

    def test_replaying_query_does_not_repeat_query_or_evidence_consumption(self):
        self.query()
        events = self.store.events(self.run,self.principal)
        self.query()
        self.assertEqual(events,self.store.events(self.run,self.principal))
        self.assertEqual(self.endpoint.state()['query_charges'],1)

    def test_old_program_can_replay_after_completion_and_reconciliation(self):
        self.query()
        result = Interpreter(self.broker).run(self.plan,self.run,self.principal,self.grants)
        self.assertEqual(result.data,'Done')
        self.assertEqual(self.endpoint.state()['business_operations'],1)

    def test_reading_obligations_does_not_query_or_charge(self):
        before = self.store.events(self.run,self.principal)
        self.store.obligations(self.run,self.principal)
        self.store.status(self.run,self.principal)
        self.assertEqual(before,self.store.events(self.run,self.principal))
        self.assertEqual(self.endpoint.state()['query_charges'],0)

    def test_original_mutation_grant_does_not_authorize_queries(self):
        with self.assertRaises(AuthorizationDenied):
            self.query(grant=self.grants['repo.merge'])
        self.assertEqual(self.endpoint.state()['query_charges'],0)

    def test_query_revocation_is_checked(self):
        self.store.revoke(self.grants['repo.query'])
        with self.assertRaises(AuthorizationDenied):
            self.query()
        self.assertEqual(self.endpoint.state()['query_charges'],0)

    def test_completed_run_rejects_new_ordinary_work(self):
        info = self.store.operation_info(self.operation,self.principal)['intent']
        intent = Intent(info['tool'],info['version'],info['scope'],info['args'],info['receipts'])
        with self.assertRaises(StateConflict):
            self.store.authorize(self.run,self.principal,intent,self.grants['repo.merge'])
        with self.assertRaises(StateConflict):
            self.store.retry(self.operation,self.principal)

    def test_query_budget_cannot_use_original_held_reservation(self):
        # Six query operations can use the six free units. The
        # original one-unit reservation is not a source of query funding.
        def empty(ticket):
            request=ticket.intent.value['args']
            return Response({**request,'business':None,'bills':[]},{'units':1})
        local = dict(self.adapter_map)
        local['repo.query']=empty
        broker=Broker(self.store,self.catalog,local)
        for i in range(6):
            self.query(f'query-{i}',broker=broker)
        # Initial spend 3 + held 1 leaves 6 free, all six queries consumed it.
        with self.assertRaises(BudgetExceeded):
            self.query('query-extra',broker=broker)
        self.assertEqual(self.store.budget(self.run,self.principal)['units']['reserved'],1)

    def test_absent_remote_evidence_keeps_unknown_and_reservation(self):
        local=dict(self.adapter_map)
        local['repo.query']=lambda ticket:Response({**ticket.intent.value['args'],'business':None,'bills':[]},{'units':1})
        state=self.query(broker=Broker(self.store,self.catalog,local))
        self.assertEqual(state['business'],'Unknown')
        self.assertEqual(state['billing']['reserved']['units'],1)

    def test_mismatched_digest_rejected_before_target_mutation(self):
        local=dict(self.adapter_map)
        def wrong(ticket):
            response=self.endpoint.query(ticket)
            response.output['intent_digest']='wrong'
            return response
        local['repo.query']=wrong
        with self.assertRaises(EvidenceMismatch):
            self.query(broker=Broker(self.store,self.catalog,local))
        self.assertEqual(self.store.snapshot(self.operation,self.principal)['business'],'Unknown')
        # The invalid query response does not erase its real fee.
        self.assertEqual(self.store.budget(self.run,self.principal)['units']['confirmed'],4)

    def test_foreign_attempt_in_bill_rejected_before_business_update(self):
        local=dict(self.adapter_map)
        def wrong(ticket):
            response=self.endpoint.query(ticket)
            response.output['bills'][0]['attempt']=ticket.attempt
            return response
        local['repo.query']=wrong
        with self.assertRaises(EvidenceMismatch):
            self.query(broker=Broker(self.store,self.catalog,local))
        self.assertEqual(self.store.snapshot(self.operation,self.principal)['business'],'Unknown')

    def test_query_timeout_remains_an_additional_obligation(self):
        calls=[]
        local=dict(self.adapter_map)
        def timeout(ticket):
            calls.append(ticket.attempt)
            raise TimeoutError()
        local['repo.query']=timeout
        broker=Broker(self.store,self.catalog,local)
        for _ in range(2):
            with self.assertRaises(Suspended):
                self.query(broker=broker)
        self.assertEqual(len(calls),1)
        self.assertEqual(self.store.budget(self.run,self.principal)['units']['reserved'],2)
        self.assertEqual(len(self.store.obligations(self.run,self.principal)),2)

    def test_same_principal_cannot_reconcile_another_run_with_current_budget(self):
        other=self.store.create_run(self.principal,'other',{'units':10})
        with self.assertRaises(AuthorizationDenied):
            self.broker.reconcile(other,self.principal,'query',self.operation,'repo.query',self.grants['repo.query'])

    def test_terminal_control_decision_is_idempotent_and_monotone(self):
        events=self.store.events(self.run,self.principal)
        self.store.finish_run(self.run,self.principal)
        self.assertEqual(events,self.store.events(self.run,self.principal))
        with self.assertRaises(StateConflict):
            self.store.finish_run(self.run,self.principal,outcome='Failed')

    def crash_then_resume(self, point):
        config={'database':self.store.path,'endpoint':self.endpoint.path,'run':self.run,
                'operation':self.operation,'grant':self.grants['repo.query']}
        path=self.folder/'crash.json'
        path.write_text(json.dumps(config),encoding='utf-8')
        result=subprocess.run([sys.executable,str(Path(__file__).with_name('reconcile_worker.py')),str(path),point],
                               capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,77,result.stdout+result.stderr)
        self.store=Store(self.store.path)
        self.broker=Broker(self.store,self.catalog,adapters(self.endpoint,reconciliation=True))
        self.query()
        self.assertEqual(self.store.status(self.run,self.principal)['status'],'CompletedClean')
        self.assertEqual(self.store.budget(self.run,self.principal)['units']['confirmed'],5)
        self.assertEqual(self.endpoint.state()['query_charges'],1)

    def test_crash_after_query_before_target_application(self):
        self.crash_then_resume('before_reconciliation_apply')

    def test_crash_between_business_and_billing_application(self):
        self.crash_then_resume('after_reconciliation_business')

    def test_crash_after_bill_does_not_settle_twice(self):
        self.crash_then_resume('after_reconciliation_bill')

    def test_additive_schema_migration_preserves_obligations_and_permit_consumption(self):
        before=self.store.budget(self.run,self.principal)
        db=sqlite3.connect(self.store.path)
        db.execute('DROP TABLE run_status')
        db.execute('DROP TABLE reconcilers')
        db.execute('PRAGMA user_version=2')
        db.commit()
        db.close()
        reopened=Store(self.store.path)
        self.assertEqual(before,reopened.budget(self.run,self.principal))
        self.assertEqual(len(reopened.obligations(self.run,self.principal)),1)
        self.assertEqual(reopened.snapshot(self.operation,self.principal)['business'],'Unknown')
        self.assertEqual(Interpreter(Broker(reopened,self.catalog,self.adapter_map)).run(self.plan,self.run,self.principal,self.grants).data,
                         'NeedsReconciliation')
        self.assertEqual(self.endpoint.state()['business_operations'],1)


class ReconcileIRTests(unittest.TestCase):
    def document(self):
        specs=catalog(reconciliation=True)
        doc=json.loads(build(specs).encoded)
        doc['nodes'][-1]['id']='first_result'
        doc['uses'].append('reconcile:repo.query')
        doc['nodes'].append({'id':'reconciled','op':'reconcile','tool':'repo.query','operation':'operation'})
        branch=deepcopy(doc['nodes'][-2])
        branch.update(id='result',input='reconciled')
        doc['nodes'].append(branch)
        return specs,doc

    def test_ir_reconciliation_remains_a_declared_effect_with_exhaustive_result(self):
        specs,doc=self.document()
        check_plan(doc,specs)
        doc['uses'].remove('reconcile:repo.query')
        with self.assertRaises(TypeCheckError):
            check_plan(doc,specs)

    def test_plain_data_cannot_forge_operation_reference(self):
        specs,doc=self.document()
        doc['nodes'][-2]['operation']='request'
        with self.assertRaises(TypeCheckError):
            check_plan(doc,specs)


if __name__ == '__main__':
    unittest.main()
