import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from models.combined_audit import (State, context_keys, corpus, graph, ledger,
                                   successors, violations)
from models.combined_conformance import Harness, run
from models.extended_audit import explore


def advance(state, actions, mode):
    for action in actions:
        state = dict(successors(state, mode=mode))[action]
    return state


class CombinedModelTests(unittest.TestCase):
    def test_all_reachable_states_and_nonvacuity(self):
        for mode in ('apply', 'reject'):
            parents, edges = graph(mode)
            for state in parents:
                self.assertEqual(list(violations(state)), [])
            # The successful safety result includes useful progress, billed
            # rejection and two separately charged deduplicated attempts.
            target = advance(State(), ('dispatch:0', 'launch:0', 'retry', 'dispatch:1',
                'launch:1', 'endpoint:0', 'crash', 'endpoint:1', 'recover', 'revoke',
                'cancel', 'business', 'bill:1', 'bill:0'), mode)
            self.assertIn(target, parents)
            self.assertEqual(ledger(target)['confirmed'], 2)
            self.assertEqual(ledger(target)['reserved'], 0)
            self.assertEqual(target.business, 'Applied' if mode == 'apply' else 'ProvenNoEffect')
            self.assertTrue(target.revoked and target.cancelled and target.recovery == 2)

    def test_mutants_have_targeted_counterexamples(self):
        for mode in ('apply', 'reject'):
            for mutant, invariant in (('cancel_refunds_exposed', 'external_fees_not_covered'),
                                      ('retry_without_reservation', 'external_fees_not_covered'),
                                      ('leaf_only', 'new_work_after_ancestor_revocation')):
                result = explore(State(), lambda s: successors(s, mode=mode, mutant=mutant), violations)
                witness = result['shortest_counterexamples'][invariant]
                self.assertGreater(len(witness), 0)
                state = State()
                for step in witness:
                    state = dict(successors(state, mode=mode, mutant=mutant))[step['action']]
                self.assertIn(invariant, list(violations(state)))

    def test_selected_traces_cover_the_declared_partition_and_pairs(self):
        for mode in ('apply', 'reject'):
            parents, edges = graph(mode)
            required = set()
            for state, outgoing in edges.items():
                for action, following in outgoing:
                    required.update(context_keys(state, action))
                    required.update(('pair', action, next_action) for next_action, _ in edges[following])
            traces, report = corpus(mode)
            observed = set()
            for trace in traces:
                state, previous = State(), None
                for action in trace:
                    observed.update(context_keys(state, action))
                    if previous is not None:
                        observed.add(('pair', previous, action))
                    state = dict(edges[state])[action]
                    previous = action
            self.assertEqual(observed, required)
            self.assertEqual(report['coverage_obligations'], len(required))
            self.assertEqual(report['states'], len(parents))


class CombinedRuntimeTests(unittest.TestCase):
    def test_generated_corpus_against_runtime_and_external_oracle(self):
        # CI can retain reproducible trace evidence; ordinary unit runs keep
        # artifacts in a temporary directory and do not overwrite research data.
        with tempfile.TemporaryDirectory() as temp:
            evidence = Path(os.environ.get('AL0_COMBINED_EVIDENCE', str(Path(temp)/'traces.jsonl')))
            report = run(evidence_path=evidence)
            records = [json.loads(line) for line in evidence.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(records), sum(report[m]['passed_traces'] for m in ('apply', 'reject')))
            self.assertTrue(all(r['passed'] and r['provider'] and r['events'] for r in records))
            if 'AL0_COMBINED_EVIDENCE' in os.environ:
                evidence.with_suffix('.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
        for mode in ('apply', 'reject'):
            self.assertEqual(report[mode]['passed_traces'], report[mode]['selected_traces'])
            self.assertGreater(report[mode]['replay_rejection_duplicate_probes'], 0)

    def test_provider_duplicate_cost_is_visible_despite_effect_deduplication(self):
        with tempfile.TemporaryDirectory() as temp:
            h = Harness(temp, create=True)
            for action in ('dispatch:0', 'launch:0', 'endpoint:0'):
                h.step(action)
            # Deliberately broken adapter reuses one dispatch right twice.
            h.provider.launch(h.op, h.attempts[0])
            h.provider.finish(h.attempts[0])
            self.assertEqual(h.provider.snapshot()['effects'], [(h.op,)])
            self.assertEqual(h.provider.snapshot()['fees'], 2)
            with self.assertRaisesRegex(AssertionError, 'external cost uncovered'):
                h.verify_oracle()

    def test_external_oracle_catches_refund_after_exposed_cancel(self):
        with tempfile.TemporaryDirectory() as temp:
            h = Harness(temp, create=True, mode='reject')
            for action in ('dispatch:0', 'launch:0', 'endpoint:0', 'cancel'):
                h.step(action)
            # Deliberate ledger corruption models the invalid refund rule. The
            # independent fee check does not receive a model state to compare.
            with h.store._tx() as db:
                db.execute('UPDATE budgets SET free=free+reserved,reserved=0 WHERE run=?', (h.run,))
            with self.assertRaisesRegex(AssertionError, 'external cost uncovered'):
                h.verify_oracle()

    def check_crash(self, actions, fault=None):
        for mode in ('apply', 'reject'):
            with self.subTest(mode=mode, fault=fault), tempfile.TemporaryDirectory() as temp:
                Harness(temp, create=True, mode=mode)
                (Path(temp)/'worker.json').write_text(json.dumps({'actions': actions, 'fault': fault}), encoding='utf-8')
                result = subprocess.run([sys.executable, '-m', 'tests.combined_protocol_worker', temp],
                                        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 77, result.stdout+result.stderr)
                committed = actions[:-1] if fault == 'before_settlement_commit' else actions
                state = advance(State(), committed, mode)
                state = advance(state, ('crash',), mode)
                h = Harness(temp)
                h.running = False
                h.verify(state)
                # An external process can finish accepted requests while the
                # coordinator is still dead. Recover, revoke, cancel, then ingest
                # both independent receipt streams and check them again.
                for i, a in enumerate(state.attempts):
                    if a.remote == 'inflight':
                        action = f'endpoint:{i}'
                        h.step(action)
                        state = advance(state, (action,), mode)
                        h.verify(state)
                h.step('recover')
                state = advance(state, ('recover',), mode)
                h.verify(state)
                for action in ('revoke', 'cancel', 'business', 'bill:0', 'bill:1'):
                    following = dict(successors(state, mode=mode))
                    if action in following:
                        h.step(action)
                        state = following[action]
                        h.verify(state)

    def test_process_exit_after_dispatch_commit_loses_unsent_ticket(self):
        self.check_crash(('dispatch:0',))

    def test_process_exit_with_inflight_request_and_late_provider_completion(self):
        self.check_crash(('dispatch:0', 'launch:0'))

    def test_process_exit_after_retry_and_first_remote_execution(self):
        self.check_crash(('dispatch:0', 'launch:0', 'retry', 'dispatch:1', 'launch:1', 'endpoint:0'))

    def test_process_exit_before_late_billing_commit_rolls_back(self):
        self.check_crash(('dispatch:0', 'launch:0', 'retry', 'dispatch:1', 'launch:1',
                          'endpoint:0', 'endpoint:1', 'revoke', 'cancel', 'bill:0'), 'before_settlement_commit')

    def test_process_exit_after_late_billing_commit_replays_without_double_charge(self):
        self.check_crash(('dispatch:0', 'launch:0', 'retry', 'dispatch:1', 'launch:1',
                          'endpoint:0', 'endpoint:1', 'revoke', 'cancel', 'bill:0'), 'after_settlement_commit')


if __name__ == '__main__':
    unittest.main()
