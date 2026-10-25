import unittest

from models.affine_transfer import (MUTANTS, SCENARIOS, Rejected, State, actions,
    counterexample, graph, run_scenario, step, violations)


class AffineTransferModelTests(unittest.TestCase):
    def test_all_reachable_states_and_transitions_preserve_invariants(self):
        parents, transitions, _ = graph()
        self.assertGreater(len(parents), 1000)
        self.assertGreater(transitions, 4000)
        self.assertEqual({s.child for s in parents}, {'Absent', 'Running', 'Returning', 'Joined', 'Cancelled'})
        self.assertEqual({s.recovery for s in parents}, {0, 1, 2})
        for state in parents:
            self.assertEqual(list(violations(state)), [])

    def test_each_mutant_has_a_reproducible_targeted_counterexample(self):
        for mutant, invariant in MUTANTS.items():
            with self.subTest(mutant=mutant):
                witness = counterexample(mutant)
                state = run_scenario(witness['actions'], mutant)
                self.assertIn(invariant, list(violations(state)))
                # The same requests either remain safe or are rejected by the
                # correct protocol; an unrelated exception is not a witness.
                valid = State()
                for action in witness['actions']:
                    try:
                        valid = step(valid, action)
                    except Rejected:
                        break
                    self.assertEqual(list(violations(valid)), [])

    def test_useful_progress_includes_return_cancellation_and_late_bills(self):
        parents, _, _ = graph()
        for name, sequence in SCENARIOS.items():
            with self.subTest(name=name):
                state = run_scenario(sequence)
                self.assertIn(state, parents)
                self.assertFalse(any(a.held for a in state.attempts))
                self.assertGreater(state.confirmed, 0)
                self.assertEqual(sum(state.free)+state.confirmed, 6)

    def test_custody_generation_blocks_parent_child_parent_aba(self):
        state = run_scenario(('spawn', 'publish', 'join'))
        self.assertEqual((state.resources[2].owner, state.resources[2].generation), ('parent', 3))
        with self.assertRaises(Rejected):
            step(state, 'stale_generation')
        # An unused permit remains usable by the new owner under the new token.
        admitted = step(state, 'admit:parent:1')
        self.assertEqual(admitted.admitted, 1)
        with self.assertRaises(Rejected):
            step(admitted, 'admit:parent:1')

    def test_worker_epoch_and_custody_generation_are_independent(self):
        state = run_scenario(('spawn', 'crash', 'recover'))
        self.assertEqual(state.epoch, 1)
        self.assertEqual(state.resources[2].generation, 1)
        with self.assertRaises(Rejected):
            step(state, 'stale_epoch')
        self.assertEqual(step(state, 'admit:child:1').admitted, 1)

    def test_replay_never_restores_old_resources_or_reallocates_money(self):
        parents, _, _ = graph()
        for state in parents:
            if state.spawn_receipt and state.recovery != 1:
                self.assertEqual(step(state, 'replay_spawn'), state)
            if state.child == 'Joined' and state.recovery != 1:
                self.assertEqual(step(state, 'join'), state)

    def test_custody_moves_do_not_rewrite_attempt_funding(self):
        state = run_scenario(SCENARIOS['returned_operation_uses_two_funding_origins'])
        self.assertEqual([a.reserved_from for a in state.attempts], [1, 2])
        self.assertEqual([a.funding for a in state.attempts], [1, 2])
        self.assertEqual(state.free, (0, 3, 1))
        self.assertTrue(state.child_cancelled)
        self.assertTrue(state.operation_open)

    def test_cancel_escrow_refunds_queued_but_keeps_exposed_reservation(self):
        state = run_scenario(('admit:parent:1', 'spawn', 'dispatch:child:0',
                              'retry:child:2', 'publish', 'cancel_child'))
        self.assertEqual([a.held for a in state.attempts], [True, False])
        self.assertEqual([a.phase for a in state.attempts], ['Exposed', 'Closed'])
        self.assertEqual(state.free, (4, 0, 0))
        self.assertEqual([r.owner for r in state.resources], ['spent', 'runtime', 'runtime', 'runtime'])
        with self.assertRaises(Rejected):
            step(state, 'join')
        settled = step(step(state, 'provider:0'), 'bill:0')
        self.assertEqual(settled.free, (5, 0, 0))
        self.assertEqual(settled.confirmed, 1)

    def test_atomic_moves_and_recovery_preserve_financial_records(self):
        parents, _, _ = graph()
        for state in parents:
            for action in ('publish', 'join', 'crash', 'recover', 'replay_spawn'):
                try:
                    target = step(state, action)
                except Rejected:
                    continue
                self.assertEqual((target.free, target.opened, target.attempts, target.confirmed),
                                 (state.free, state.opened, state.attempts, state.confirmed))
                if action in ('crash', 'recover'):
                    self.assertEqual(target.resources, state.resources)

    def test_provider_can_finish_during_coordinator_outage(self):
        state = run_scenario(('admit:parent:1', 'dispatch:parent:0', 'crash'))
        delivered = step(state, 'provider:0')
        self.assertTrue(delivered.attempts[0].delivered)
        for action in actions(delivered):
            if action not in ('recover',) and not action.startswith('provider:'):
                with self.assertRaises(Rejected):
                    step(delivered, action)
        settled = step(step(delivered, 'recover'), 'bill:0')
        with self.assertRaises(Rejected):
            step(settled, 'bill:0')
        self.assertEqual(settled.confirmed, 1)
