# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden IAS 33 worked examples for the EPS engine.

Each test encodes a hand-computed IAS 33 example (weighted-average shares,
treasury-stock options, if-converted convertibles with the anti-dilution
sequencing test, and bonus-issue restatement). Every expected figure is
derived by hand in a comment from the stated inputs; nothing is read back
from the engine under test.

Day-count convention of eh.eps.run._weighted_average (asserted by these
goldens): calendar days, fully inclusive. The period weight base is
(period_end - period_start).days + 1, and each share movement is outstanding
from its effective date through the day BEFORE the next movement's effective
date (the last movement runs through period end). For the calendar year 2026
(not a leap year) the base is 365 days.
"""

from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_golden', 'eh_account_eps', 'post_install', '-at_install')
class TestGoldenIas33(EhGoldenTestCase):
    """IAS 33 golden worked examples: basic, diluted, restated EPS."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Computing a run (the diluted sequencing test) is manager-gated.
        # Grant the group; the group_ids / groups_id field split across Odoo
        # series is resolved at runtime, as in the module's integration tests.
        field = ('group_ids' if 'group_ids' in cls.env.user._fields
                 else 'groups_id')
        cls.env.user.write({field: [
            (4, cls.env.ref('eh_account_base.group_eh_manager').id)]})

    def _run(self, **vals):
        base = {
            'period_start': '2026-01-01',
            'period_end': '2026-12-31',
        }
        base.update(vals)
        return self.env['eh.eps.run'].create(base)

    def test_golden_weighted_average_mid_year_issue(self):
        """IAS 33.20 weighted-average shares with a mid-year issue.

        Inputs: 1,000,000 ordinary shares outstanding all year; 200,000 new
        shares issued 2026-07-01 (total outstanding 1,200,000 from that
        date); period 2026-01-01 .. 2026-12-31; earnings 550,410.96.

        Derivation (inclusive day-count, 365-day base for 2026):
          segment 1: 1,000,000 shares, 2026-01-01 .. 2026-06-30 = 181 days
                     -> 181,000,000 share-days
          segment 2: 1,200,000 shares, 2026-07-01 .. 2026-12-31 = 184 days
                     -> 220,800,000 share-days
          WA = 401,800,000 / 365 = 1,100,821.9178...
             (equivalently 1,000,000 + 200,000 * 184/365)
          stored at 2dp -> 1,100,821.92
          basic EPS = 550,410.96 / 1,100,821.92 = 0.500000 exactly
             (earnings chosen as 1,100,821.92 * 0.50).
        """
        run = self._run(net_profit=550410.96)
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 1000000.0})
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-07-01',
            'shares_outstanding': 1200000.0})
        self.assertAlmostEqual(run.weighted_avg_shares, 1100821.92, places=2)
        self.assertAlmostEqual(run.basic_earnings, 550410.96, places=2)
        self.assertAlmostEqual(run.basic_eps, 0.500000, places=6)

    def test_golden_treasury_stock_options(self):
        """IAS 33.45-46 treasury-stock method for options.

        Inputs: 1,000,000 shares all year; earnings 500,000 (basic EPS
        500,000 / 1,000,000 = 0.50); 100,000 options with exercise price
        4.00 and average market price 5.00.

        Derivation:
          assumed proceeds = 100,000 * 4.00 = 400,000
          assumed buy-back = 400,000 / 5.00 = 80,000 shares
          net increment   = 100,000 - 80,000 = 20,000
             (engine form: 100,000 * (1 - 4.00/5.00) = 20,000)
          incremental EPS = 0 / 20,000 = 0 (options add no earnings)
          diluted EPS = 500,000 / (1,000,000 + 20,000)
                      = 500,000 / 1,020,000 = 0.490196 (6dp)
        """
        run = self._run(net_profit=500000.0)
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 1000000.0})
        pot = self.env['eh.eps.potential'].create({
            'run_id': run.id, 'name': 'Employee options',
            'instrument_type': 'options',
            'potential_shares': 100000.0,
            'earnings_adjustment': 0.0,
            'exercise_price': 4.0,
            'average_market_price': 5.0})
        self.assertAlmostEqual(run.weighted_avg_shares, 1000000.0, places=2)
        self.assertAlmostEqual(run.basic_eps, 0.500000, places=6)
        self.assertAlmostEqual(pot.net_incremental_shares, 20000.0, places=2)
        self.assertAlmostEqual(pot.incremental_eps, 0.0, places=6)
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        self.assertAlmostEqual(run.diluted_shares, 1020000.0, places=2)
        self.assertAlmostEqual(run.diluted_earnings, 500000.0, places=2)
        self.assertAlmostEqual(run.diluted_eps, 0.490196, places=6)
        self.assertTrue(pot.is_dilutive)

    def test_golden_if_converted_and_anti_dilution(self):
        """IAS 33.49 if-converted method and the IAS 33.44 anti-dilution gate.

        Run A (dilutive): 1,000,000 shares all year; earnings 500,000
        (basic 0.50); convertible bond saving 30,000 after-tax interest and
        converting into 200,000 shares.

        Derivation A:
          incremental EPS = 30,000 / 200,000 = 0.15 < 0.50 -> dilutive
          diluted EPS = (500,000 + 30,000) / (1,000,000 + 200,000)
                      = 530,000 / 1,200,000 = 0.441667 (6dp)

        Run B (anti-dilutive): identical, but after-tax interest 150,000.

        Derivation B:
          incremental EPS = 150,000 / 200,000 = 0.75 > 0.50
          trial diluted = 650,000 / 1,200,000 = 0.541667 > basic 0.50
          -> excluded; diluted EPS = basic EPS = 0.50 and the denominator
             stays at 1,000,000 (diluted never above basic, IAS 33.44).
        """
        # -- Run A: dilutive convertible -------------------------------
        run_a = self._run(net_profit=500000.0)
        self.env['eh.eps.share.movement'].create({
            'run_id': run_a.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 1000000.0})
        pot_a = self.env['eh.eps.potential'].create({
            'run_id': run_a.id, 'name': 'Convertible bond 3% net',
            'instrument_type': 'convertible_bond',
            'potential_shares': 200000.0,
            'earnings_adjustment': 30000.0})
        self.assertAlmostEqual(run_a.basic_eps, 0.500000, places=6)
        self.assertAlmostEqual(pot_a.incremental_eps, 0.15, places=6)
        run_a.action_compute()
        self.assertAlmostEqual(run_a.diluted_earnings, 530000.0, places=2)
        self.assertAlmostEqual(run_a.diluted_shares, 1200000.0, places=2)
        self.assertAlmostEqual(run_a.diluted_eps, 0.441667, places=6)
        self.assertTrue(pot_a.is_dilutive)
        self.assertLess(run_a.diluted_eps, run_a.basic_eps)

        # -- Run B: anti-dilutive convertible excluded -----------------
        run_b = self._run(net_profit=500000.0)
        self.env['eh.eps.share.movement'].create({
            'run_id': run_b.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 1000000.0})
        pot_b = self.env['eh.eps.potential'].create({
            'run_id': run_b.id, 'name': 'Convertible bond 15% net',
            'instrument_type': 'convertible_bond',
            'potential_shares': 200000.0,
            'earnings_adjustment': 150000.0})
        self.assertAlmostEqual(pot_b.incremental_eps, 0.75, places=6)
        run_b.action_compute()
        self.assertAlmostEqual(run_b.diluted_earnings, 500000.0, places=2)
        self.assertAlmostEqual(run_b.diluted_shares, 1000000.0, places=2)
        self.assertAlmostEqual(run_b.diluted_eps, 0.500000, places=6)
        self.assertAlmostEqual(run_b.diluted_eps, run_b.basic_eps, places=6)
        self.assertFalse(pot_b.is_dilutive)

    def test_golden_bonus_issue_restatement(self):
        """IAS 33.26-28/64 retrospective bonus-issue restatement.

        Inputs: 1,000,000 shares outstanding all year; a 1-for-4 bonus
        issue during the period; earnings 500,000.

        A bonus issue brings no consideration, so it is NOT a share
        movement weighted from its date: the whole weighted average is
        restated as if the bonus had always been in issue. 1-for-4 means
        every 4 shares become 5, a restatement factor of 5/4 = 1.25.

        Derivation:
          WA before restatement = 1,000,000 (constant all year)
          WA restated = 1,000,000 * 1.25 = 1,250,000
          basic EPS = 500,000 / 1,250,000 = 0.400000
        """
        run = self._run(net_profit=500000.0, restatement_factor=1.25)
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 1000000.0})
        self.assertAlmostEqual(run.weighted_avg_shares, 1250000.0, places=2)
        self.assertAlmostEqual(run.basic_earnings, 500000.0, places=2)
        self.assertAlmostEqual(run.basic_eps, 0.400000, places=6)

    # ------------------------------------------------------------------
    # Continuing / discontinued split (IAS 33.66-68)
    # ------------------------------------------------------------------

    def test_golden_continuing_discontinued_split(self):
        """IAS 33.66-68 EPS split between continuing and discontinued.

        Inputs: 1,000,000 shares all year; net profit 500,000 split as
        continuing 380,000 + discontinued 120,000; no preference
        dividends; options adding a 20,000-share net increment (no
        earnings adjustment).

        Derivation (basic):
          WA = 1,000,000
          basic total        = 500,000 / 1,000,000 = 0.500000
          basic continuing   = 380,000 / 1,000,000 = 0.380000 (IAS 33.66)
          basic discontinued = 0.50 - 0.38         = 0.120000 (IAS 33.68,
            disclosed as the difference; equals 120,000 / 1,000,000)

        Derivation (diluted; the control number is CONTINUING earnings,
        IAS 33.42-43):
          control 380,000/1,000,000 = 0.38
          add 20,000 shares: 380,000 / 1,020,000 = 0.372549 < 0.38
            -> dilutive, included
          diluted continuing   = 380,000 / 1,020,000 = 0.372549 (6dp)
          diluted total        = 500,000 / 1,020,000 = 0.490196 (6dp)
          diluted discontinued = 0.490196078 - 0.372549020
                               = 0.117647 (6dp; equals 120,000/1,020,000)
        """
        run = self._run(
            net_profit=500000.0,
            profit_continuing=380000.0,
            profit_discontinued=120000.0)
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 1000000.0})
        self.env['eh.eps.potential'].create({
            'run_id': run.id, 'name': 'Employee options (net)',
            'instrument_type': 'options',
            'potential_shares': 20000.0,
            'earnings_adjustment': 0.0})
        self.assertAlmostEqual(run.weighted_avg_shares, 1000000.0, places=2)
        self.assertAlmostEqual(run.basic_eps, 0.500000, places=6)
        self.assertAlmostEqual(run.basic_eps_continuing, 0.380000, places=6)
        self.assertAlmostEqual(run.basic_eps_discontinued, 0.120000, places=6)
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        self.assertAlmostEqual(run.diluted_shares, 1020000.0, places=2)
        self.assertAlmostEqual(run.diluted_eps, 0.490196, places=6)
        self.assertAlmostEqual(run.diluted_eps_continuing, 0.372549, places=6)
        self.assertAlmostEqual(
            run.diluted_eps_discontinued, 0.117647, places=6)
        self.assertTrue(run.potential_ids.is_dilutive)

    def test_golden_split_must_tie_to_net_profit(self):
        """IAS 33.66 guardrail: a continuing/discontinued split that does
        not decompose net profit exactly is refused (380,000 + 100,000 !=
        500,000)."""
        with self.assertRaises(ValidationError):
            self._run(
                net_profit=500000.0,
                profit_continuing=380000.0,
                profit_discontinued=100000.0)

    def test_prefill_discontinued_hook(self):
        """The held-for-sale ledger hook prefills the split when installed
        (soft registry lookup); without it the action explains itself and
        the split stays manual."""
        run = self._run(net_profit=500000.0)
        if 'eh.disposal.group' not in self.env:
            self.assertFalse(run.has_held_for_sale)
            with self.assertRaises(UserError):
                run.action_prefill_discontinued()
            return
        self.assertTrue(run.has_held_for_sale)
        DisposalGroup = type(self.env['eh.disposal.group'])
        expected = 125000.0
        with patch.object(
                DisposalGroup, 'eh_discontinued_pl_amount',
                autospec=True, return_value=expected) as ledger_hook:
            run.action_prefill_discontinued()
        ledger_hook.assert_called_once_with(
            self.env['eh.disposal.group'],
            run.period_start, run.period_end, run.company_id,
        )
        self.assertAlmostEqual(run.profit_discontinued, expected, places=2)
        self.assertAlmostEqual(
            run.profit_continuing, 500000.0 - expected, places=2)

    # ------------------------------------------------------------------
    # Multi-event retrospective restatement (IAS 33.26-28, 64)
    # ------------------------------------------------------------------

    def test_golden_multi_event_restatement(self):
        """IAS 33.64 chained bonus + split, both retrospective.

        Inputs: 1,000,000 shares outstanding all year (single movement
        2026-01-01, recorded without the bonus/split); earnings 500,000;
        restatement events: 1-for-4 bonus issue 2026-03-15 (factor 1.25)
        and 2-for-1 split 2026-09-01 (factor 2.0).

        Engine convention (asserted here): an event restates every share
        movement recorded BEFORE its date; a movement from the event date
        on already carries the post-event count. The single 2026-01-01
        movement precedes both events, so:

          WA = 1,000,000 * 1.25 * 2.0 = 2,500,000 (constant all year)
          basic EPS = 500,000 / 2,500,000 = 0.200000

        Cumulative/comparative factor: both events fall after the
        comparative period start, so the factor that restates the prior
        period's EPS is the product 1.25 * 2.0 = 2.5, surfaced on the
        restatement_factor alias (IAS 33.64 comparative restatement).
        """
        run = self._run(net_profit=500000.0)
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 1000000.0})
        self.env['eh.eps.restatement.event'].create({
            'run_id': run.id, 'date': '2026-03-15',
            'kind': 'bonus', 'factor': 1.25})
        self.env['eh.eps.restatement.event'].create({
            'run_id': run.id, 'date': '2026-09-01',
            'kind': 'split', 'factor': 2.0})
        self.assertAlmostEqual(run.restatement_factor, 2.5, places=6)
        self.assertAlmostEqual(run.weighted_avg_shares, 2500000.0, places=2)
        self.assertAlmostEqual(run.basic_eps, 0.200000, places=6)

    def test_golden_multi_event_restatement_with_mid_year_issue(self):
        """IAS 33.64 movement-splitting convention with a consideration
        issue between the two events.

        Inputs: movements 1,000,000 from 2026-01-01 and 1,200,000 from
        2026-07-01 (a 200,000-share issue for cash, counts recorded
        without the bonus/split); events 1-for-4 bonus 2026-03-15 (1.25)
        and 2-for-1 split 2026-09-01 (2.0); earnings 500,000.

        Derivation (inclusive day-count, 365-day base for 2026; each
        movement is scaled by the factors of the events dated after its
        effective date):
          seg 1: 2026-01-01 .. 2026-06-30 = 181 days
                 1,000,000 * 1.25 * 2.0 = 2,500,000 shares
                 -> 452,500,000 share-days
          seg 2: 2026-07-01 .. 2026-12-31 = 184 days
                 1,200,000 * 2.0 = 2,400,000 shares (only the September
                 split is after 1 July; the March bonus predates the
                 movement, whose count is taken as post-bonus)
                 -> 441,600,000 share-days
          WA = 894,100,000 / 365 = 2,449,589.0410958...
             stored at 2dp -> 2,449,589.04
          basic EPS = 500,000 / 2,449,589.04 = 0.204116 (6dp)
        """
        run = self._run(net_profit=500000.0)
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 1000000.0})
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-07-01',
            'shares_outstanding': 1200000.0})
        self.env['eh.eps.restatement.event'].create({
            'run_id': run.id, 'date': '2026-03-15',
            'kind': 'bonus', 'factor': 1.25})
        self.env['eh.eps.restatement.event'].create({
            'run_id': run.id, 'date': '2026-09-01',
            'kind': 'split', 'factor': 2.0})
        self.assertAlmostEqual(run.restatement_factor, 2.5, places=6)
        self.assertAlmostEqual(
            run.weighted_avg_shares, 2449589.04, places=2)
        self.assertAlmostEqual(run.basic_eps, 0.204116, places=6)

    def test_golden_restatement_event_direction_guardrails(self):
        """A bonus/split factor at or below 1, or a consolidation factor
        at or above 1, restates EPS the wrong way and is refused."""
        run = self._run(net_profit=100000.0)
        with self.assertRaises(ValidationError):
            self.env['eh.eps.restatement.event'].create({
                'run_id': run.id, 'date': '2026-06-01',
                'kind': 'split', 'factor': 0.5})
        with self.assertRaises(ValidationError):
            self.env['eh.eps.restatement.event'].create({
                'run_id': run.id, 'date': '2026-06-01',
                'kind': 'consolidation', 'factor': 2.0})

    # ------------------------------------------------------------------
    # Average market price from observations (IAS 33.45)
    # ------------------------------------------------------------------

    def test_golden_price_observations_average(self):
        """IAS 33.45 treasury-stock method at the PERIOD-AVERAGE market
        price resolved from dated observations.

        Inputs: 1,000,000 shares all year; earnings 500,000 (basic 0.50);
        100,000 options at exercise price 4.00; quarterly price
        observations 4.80 (2026-03-31), 5.00 (2026-06-30), 5.20
        (2026-09-30), plus an out-of-period observation 9.90 (2025-12-31)
        that must be ignored.

        Derivation:
          average market price = (4.80 + 5.00 + 5.20) / 3 = 5.00
          net increment = 100,000 * (1 - 4.00/5.00) = 20,000
          diluted EPS = 500,000 / 1,020,000 = 0.490196 (6dp)
        identical to the scalar average_market_price = 5.00 example
        (test_golden_treasury_stock_options), proving the observation
        series is a drop-in refinement of the scalar input.
        """
        run = self._run(net_profit=500000.0)
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 1000000.0})
        pot = self.env['eh.eps.potential'].create({
            'run_id': run.id, 'name': 'Employee options',
            'instrument_type': 'options',
            'potential_shares': 100000.0,
            'earnings_adjustment': 0.0,
            'exercise_price': 4.0,
            'observation_ids': [
                (0, 0, {'date': '2025-12-31', 'price': 9.9}),
                (0, 0, {'date': '2026-03-31', 'price': 4.8}),
                (0, 0, {'date': '2026-06-30', 'price': 5.0}),
                (0, 0, {'date': '2026-09-30', 'price': 5.2}),
            ]})
        self.assertAlmostEqual(pot.average_market_price, 5.0, places=2)
        self.assertAlmostEqual(pot.net_incremental_shares, 20000.0, places=2)
        run.action_compute()
        self.assertAlmostEqual(run.diluted_shares, 1020000.0, places=2)
        self.assertAlmostEqual(run.diluted_eps, 0.490196, places=6)
        self.assertTrue(pot.is_dilutive)

    def test_price_observations_frozen_after_compute(self):
        """Observations resolve the treasury-stock average, so they freeze
        with the run like every other input child."""
        run = self._run(net_profit=500000.0)
        self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 1000000.0})
        pot = self.env['eh.eps.potential'].create({
            'run_id': run.id, 'name': 'Options',
            'instrument_type': 'options',
            'potential_shares': 100000.0,
            'exercise_price': 4.0,
            'observation_ids': [
                (0, 0, {'date': '2026-06-30', 'price': 5.0}),
            ]})
        run.action_compute()
        obs = pot.observation_ids
        with self.assertRaises(UserError):
            obs.write({'price': 9.0})
        with self.assertRaises(UserError):
            obs.unlink()
        with self.assertRaises(UserError):
            self.env['eh.eps.price.observation'].create({
                'potential_id': pot.id, 'date': '2026-09-30',
                'price': 8.0})
        with self.assertRaises(UserError):
            self.env['eh.eps.restatement.event'].create({
                'run_id': run.id, 'date': '2026-06-01',
                'kind': 'split', 'factor': 2.0})
        self.assertAlmostEqual(pot.average_market_price, 5.0, places=2)
