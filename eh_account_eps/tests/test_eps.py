# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IAS 33 earnings per share tests."""

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('eh_account_eps', 'integration', 'post_install', '-at_install')
class TestEps(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Computing a run is manager-gated; grant the group so the existing
        # dilution/weighting tests can call action_compute. The group_ids /
        # groups_id field split across Odoo series is handled at runtime.
        field = ('group_ids' if 'group_ids' in cls.env.user._fields
                 else 'groups_id')
        cls.env.user.write({field: [
            (4, cls.env.ref('eh_account_base.group_eh_manager').id)]})

    def _run(self, **vals):
        base = {
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'net_profit': 100000.0,
        }
        base.update(vals)
        return self.env['eh.eps.run'].create(base)

    def test_basic_eps_simple(self):
        run = self._run(net_profit=100000.0)
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 50000.0})
        self.assertAlmostEqual(run.weighted_avg_shares, 50000.0, places=1)
        self.assertAlmostEqual(run.basic_eps, 2.0, places=4)

    def test_preference_dividends_reduce_earnings(self):
        run = self._run(net_profit=100000.0, preference_dividends=10000.0)
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 50000.0})
        self.assertAlmostEqual(run.basic_earnings, 90000.0, places=2)
        self.assertAlmostEqual(run.basic_eps, 1.8, places=4)

    def test_weighted_average_mid_year_issue(self):
        run = self._run(net_profit=100000.0)
        # 40,000 shares all year, +20,000 from 1 July (184 of 365 days).
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 40000.0})
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-07-01',
            'shares_outstanding': 60000.0})
        # 40000*181/365 + 60000*184/365 = 19835.6 + 30246.6 = 50082.2
        self.assertAlmostEqual(run.weighted_avg_shares, 50082.19, places=0)

    def test_diluted_eps_with_options(self):
        run = self._run(net_profit=100000.0)
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 50000.0})
        # Options add 10,000 shares, no earnings adjustment.
        self.env['eh.eps.potential'].create({
            'run_id': run.id, 'name': 'Options', 'instrument_type': 'options',
            'potential_shares': 10000.0, 'earnings_adjustment': 0.0})
        run.action_compute()
        # 100000 / 60000 = 1.667.
        self.assertAlmostEqual(run.diluted_eps, 100000.0 / 60000.0, places=4)
        self.assertTrue(run.potential_ids.is_dilutive)

    def test_antidilutive_excluded(self):
        run = self._run(net_profit=100000.0)
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 50000.0})
        # A convertible whose incremental EPS (5.0) exceeds basic (2.0) is
        # anti-dilutive and must be excluded.
        self.env['eh.eps.potential'].create({
            'run_id': run.id, 'name': 'Rich convertible',
            'instrument_type': 'convertible_bond',
            'potential_shares': 10000.0, 'earnings_adjustment': 50000.0})
        run.action_compute()
        self.assertAlmostEqual(run.diluted_eps, 2.0, places=4)
        self.assertFalse(run.potential_ids.is_dilutive)
        # Diluted never above basic.
        self.assertLessEqual(run.diluted_eps, run.basic_eps + 1e-9)

    def test_diluted_never_above_basic(self):
        run = self._run(net_profit=100000.0)
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 50000.0})
        # A dilutive convertible: incremental EPS 1.0 < basic 2.0.
        self.env['eh.eps.potential'].create({
            'run_id': run.id, 'name': 'Convertible',
            'instrument_type': 'convertible_bond',
            'potential_shares': 20000.0, 'earnings_adjustment': 20000.0})
        run.action_compute()
        self.assertLess(run.diluted_eps, run.basic_eps)
        # (100000+20000)/(50000+20000) = 1.714.
        self.assertAlmostEqual(run.diluted_eps, 120000.0 / 70000.0, places=4)

    def test_treasury_stock_in_the_money(self):
        # In-the-money options: 10,000 potential shares, exercise price 8,
        # average market price 10. Treasury-stock net = 10000*(1-8/10)=2,000.
        run = self._run(net_profit=100000.0)
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 50000.0})
        pot = self.env['eh.eps.potential'].create({
            'run_id': run.id, 'name': 'ITM options',
            'instrument_type': 'options', 'potential_shares': 10000.0,
            'earnings_adjustment': 0.0,
            'exercise_price': 8.0, 'average_market_price': 10.0})
        self.assertAlmostEqual(pot.net_incremental_shares, 2000.0, places=1)
        run.action_compute()
        # Net method adds only 2,000 shares: 100000/52000 = 1.923.
        self.assertAlmostEqual(run.diluted_eps, 100000.0 / 52000.0, places=4)
        # Higher (less dilutive) than the naive gross add of 10,000 shares.
        self.assertGreater(run.diluted_eps, 100000.0 / 60000.0)
        self.assertTrue(pot.is_dilutive)

    def test_treasury_stock_out_of_the_money(self):
        # Out-of-the-money options (exercise 12 > market 10) add zero.
        run = self._run(net_profit=100000.0)
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 50000.0})
        pot = self.env['eh.eps.potential'].create({
            'run_id': run.id, 'name': 'OTM options',
            'instrument_type': 'options', 'potential_shares': 10000.0,
            'earnings_adjustment': 0.0,
            'exercise_price': 12.0, 'average_market_price': 10.0})
        self.assertAlmostEqual(pot.net_incremental_shares, 0.0, places=1)
        run.action_compute()
        # No dilution: diluted equals basic.
        self.assertAlmostEqual(run.diluted_eps, run.basic_eps, places=4)
        self.assertFalse(pot.is_dilutive)

    def test_restatement_factor_halves_basic_eps(self):
        # A 2-for-1 split (restatement_factor 2.0) doubles the share count
        # retrospectively, halving basic EPS.
        run = self._run(net_profit=100000.0, restatement_factor=2.0)
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 50000.0})
        self.assertAlmostEqual(run.weighted_avg_shares, 100000.0, places=1)
        # 100000 / 100000 = 1.0, half of the un-restated 2.0.
        self.assertAlmostEqual(run.basic_eps, 1.0, places=4)

    def test_compute_requires_opening_share_coverage(self):
        run = self._run(net_profit=100000.0)
        with self.assertRaises(UserError):
            run.action_compute()
        self.assertEqual(run.state, 'draft')
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-07-01',
            'shares_outstanding': 50000.0,
        })
        with self.assertRaises(UserError):
            run.action_compute()
        self.assertEqual(run.state, 'draft')

    def test_dated_potential_uses_only_later_restatement_events(self):
        run = self._run(net_profit=500000.0)
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 1000000.0,
        })
        self.env['eh.eps.restatement.event'].create({
            'run_id': run.id, 'date': '2026-03-15',
            'kind': 'bonus', 'factor': 1.25,
        })
        self.env['eh.eps.restatement.event'].create({
            'run_id': run.id, 'date': '2026-09-01',
            'kind': 'split', 'factor': 2.0,
        })
        potential = self.env['eh.eps.potential'].create({
            'run_id': run.id, 'name': 'Post-bonus options',
            'instrument_type': 'options', 'potential_shares': 100000.0,
            'share_count_date': '2026-07-01',
        })
        run.action_compute()
        self.assertTrue(potential.is_dilutive)
        self.assertAlmostEqual(run.weighted_avg_shares, 2500000.0, places=2)
        # Potential count is already post-March bonus. Only September split
        # applies: 100,000 * 2, not cumulative 1.25 * 2.
        self.assertAlmostEqual(run.diluted_shares, 2700000.0, places=2)

    def test_dated_restatement_requires_potential_basis_date(self):
        run = self._run(net_profit=100000.0)
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 50000.0,
        })
        self.env['eh.eps.restatement.event'].create({
            'run_id': run.id, 'date': '2026-06-01',
            'kind': 'split', 'factor': 2.0,
        })
        self.env['eh.eps.potential'].create({
            'run_id': run.id, 'name': 'Ambiguous options',
            'instrument_type': 'options', 'potential_shares': 10000.0,
        })
        with self.assertRaises(UserError):
            run.action_compute()
        self.assertEqual(run.state, 'draft')

    # ---- governance: manager gate + freeze after compute ----

    def _plain_user(self, login):
        field = ('group_ids' if 'group_ids' in self.env.user._fields
                 else 'groups_id')
        return self.env['res.users'].create({
            'name': 'plain', 'login': login, 'email': login,
            field: [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})

    def test_compute_requires_manager(self):
        # A non-manager (plain EH user) cannot compute the run.
        run = self._run(net_profit=100000.0)
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 50000.0})
        user = self._plain_user('eps_plain_compute@test')
        with self.assertRaises(UserError):
            run.with_user(user).action_compute()
        # State unchanged; nothing was computed.
        self.assertEqual(run.state, 'draft')

    def test_run_figures_frozen_after_compute(self):
        # Editing the earnings/share figures on a computed run is blocked so
        # the disclosed weighted shares, earnings and EPS cannot silently drift.
        run = self._run(net_profit=100000.0)
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 50000.0})
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        self.assertAlmostEqual(run.basic_eps, 2.0, places=4)
        with self.assertRaises(UserError):
            run.net_profit = 200000.0
        with self.assertRaises(UserError):
            run.preference_dividends = 5000.0
        with self.assertRaises(UserError):
            run.restatement_factor = 2.0
        with self.assertRaises(UserError):
            run.period_end = '2026-06-30'
        # The computed figures are untouched.
        self.assertAlmostEqual(run.net_profit, 100000.0, places=2)
        self.assertAlmostEqual(run.basic_eps, 2.0, places=4)
        # Setting back to draft releases the freeze so a recompute is possible.
        run.action_set_to_draft()
        run.net_profit = 200000.0
        self.assertAlmostEqual(run.net_profit, 200000.0, places=2)

    def test_share_movement_frozen_after_compute(self):
        # A parent-only guard would be bypassable by editing the child line;
        # the movement itself is frozen once the run is computed.
        run = self._run(net_profit=100000.0)
        mv = self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 50000.0})
        run.action_compute()
        with self.assertRaises(UserError):
            mv.shares_outstanding = 90000.0
        with self.assertRaises(UserError):
            mv.unlink()
        # Appending a NEW movement to a computed run must also be blocked (the
        # create path), else it silently swings the weighted-average shares.
        with self.assertRaises(UserError):
            self.env['eh.eps.share.movement'].create({
                'run_id': run.id, 'effective_date': '2026-06-01',
                'shares_outstanding': 75000.0})
        with self.assertRaises(UserError):
            self.env['eh.eps.potential'].create({
                'run_id': run.id, 'name': 'Late options',
                'instrument_type': 'options',
                'potential_shares': 10000.0, 'earnings_adjustment': 0.0})
        self.assertAlmostEqual(mv.shares_outstanding, 50000.0, places=2)

    def test_potential_line_frozen_after_compute(self):
        # Potential-share lines drive diluted EPS; freeze them after compute.
        run = self._run(net_profit=100000.0)
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 50000.0})
        pot = self.env['eh.eps.potential'].create({
            'run_id': run.id, 'name': 'Options',
            'instrument_type': 'options',
            'potential_shares': 10000.0, 'earnings_adjustment': 0.0})
        run.action_compute()
        diluted_before = run.diluted_eps
        with self.assertRaises(UserError):
            pot.potential_shares = 30000.0
        with self.assertRaises(UserError):
            pot.unlink()
        # Diluted EPS is unchanged by the blocked edits.
        self.assertAlmostEqual(run.diluted_eps, diluted_before, places=6)
