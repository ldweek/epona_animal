# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden IFRS 2 worked examples for eh_account_share_based_payment.

Each test is a hand-computed worked example in the shape of the IFRS 2
illustrative material (numbers only, recomputed by hand from the inputs
stated in the test). The exact journal entry the engine posts is asserted
line by line against literal amounts; nothing is read back from the
engine under test to build an expected value.

Engine conventions (read from models/sbp_plan.py and sbp_run.py):

* Vested fraction = calendar months elapsed since the grant date, including
  a proportional current month, / the exact service period, capped at 1.
  Run dates in these worked examples sit on grant-date anniversaries.
* The cumulative measure is rounded ONCE to company currency (USD, 2dp)
  per run; the period charge is the difference of rounded cumulatives.
* Graded plans: tranche instruments = expected-to-vest x portion_pct/100.
  portion_pct stores at 6dp, so a one-third split stores as 33.333333 and
  the raw cumulative sits a hair under the ideal fraction; at 2dp the
  rounded figures coincide with the ideal hand derivation (shown per
  test).
* Modifications spread expected-to-vest x incremental FV over the remaining
  exact service period (per tranche for graded awards).
"""

from datetime import date

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

try:  # Odoo 18+ re-exports freeze_time; 16/17 pull it from freezegun
    from odoo.tests import freeze_time
except ImportError:  # pragma: no cover - version shim
    from freezegun import freeze_time

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_golden', 'eh_account_share_based_payment',
        'post_install', '-at_install')
class TestGoldenIfrs2(EhGoldenTestCase):
    """IFRS 2 worked examples: cliff true-up, graded vesting, market
    condition stickiness, non-market reversal, cash-settled SARs,
    cancellation acceleration, modification incremental FV, and the
    valuation helper."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.sbp_expense = cls._ensure_account(
            cls.env, '6150', 'Share-based Payment Expense', 'expense')
        cls.sbp_reserve = cls._ensure_account(
            cls.env, '3150', 'SBP Equity Reserve', 'equity')
        cls.sbp_liability = cls._ensure_account(
            cls.env, '2350', 'SBP Liability', 'liability_current')

    def _plan(self, grants, **vals):
        base = {
            'name': '/',
            'settlement': 'equity',
            'condition_kind': 'service',
            'grant_date': '2026-01-01',
            'vesting_years': 3,
            'vesting_months': 0,
            'expense_account_id': self.sbp_expense.id,
            'equity_account_id': self.sbp_reserve.id,
            'liability_account_id': self.sbp_liability.id,
            'settlement_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
            'grant_ids': [(0, 0, g) for g in grants],
        }
        base.update(vals)
        plan = self.env['eh.sbp.plan'].create(base)
        plan.action_activate()
        return plan

    def _run(self, plan, period_end, fv=0.0):
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id,
            'period_end': period_end,
            'current_fair_value': fv,
        })
        run.action_post()
        return run

    # ------------------------------------------------------------------
    # 1. equity-settled, service condition, 3-year cliff with true-up
    # ------------------------------------------------------------------
    def test_golden_equity_service_cliff_trueup(self):
        """IFRS 2.19-20: 300 options, grant-date FV 10.00, 3-year cliff,
        forfeiture estimate trued up each period, actuals at vesting.

        Year 1 (12/36 vested, estimate 10% forfeiture):
          cumulative = 300 x 0.90 x 10.00 x 12/36 = 270 x 10 / 3 = 900.00
          charge     = 900.00.        JE: Dr expense / Cr reserve 900.00
        Year 2 (24/36 vested, estimate revised to 5%):
          cumulative = 300 x 0.95 x 10.00 x 24/36 = 285 x 10 x 2/3
                     = 1,900.00
          charge     = 1,900.00 - 900.00 = 1,000.00 (true-up flows
          through the current period, IFRS 2.20)
        Year 3 (vested, actual 280 instruments vest):
          cumulative = 280 x 10.00 = 2,800.00
          charge     = 2,800.00 - 1,900.00 = 900.00
        """
        plan = self._plan(
            [{'partner_id': self.partner_a.id, 'instruments_granted': 300,
              'grant_date_fair_value': 10.0,
              'expected_forfeiture_pct': 10.0}])
        grant = plan.grant_ids

        run1 = self._run(plan, '2027-01-01')
        self.assertAlmostEqual(run1.cumulative_target, 900.00, places=2,
                               msg='year-1 cumulative must be 900.00')
        self.assertMoveLines(run1.move_id, [
            (self.sbp_expense, 900.00, 0.0),
            (self.sbp_reserve, 0.0, 900.00),
        ])
        self.assertBalanced(run1.move_id)
        self.assertTrue(run1.move_id.eh_sealed,
                        'generated SBP entries must be sealed')

        grant.expected_forfeiture_pct = 5.0
        run2 = self._run(plan, '2028-01-01')
        self.assertAlmostEqual(run2.cumulative_target, 1900.00, places=2,
                               msg='year-2 cumulative must be 1,900.00')
        self.assertMoveLines(run2.move_id, [
            (self.sbp_expense, 1000.00, 0.0),
            (self.sbp_reserve, 0.0, 1000.00),
        ])

        grant.write({'vesting_finalised': True, 'actual_vested_qty': 280})
        run3 = self._run(plan, '2029-01-01')
        self.assertAlmostEqual(run3.cumulative_target, 2800.00, places=2,
                               msg='final cumulative must be 2,800.00')
        self.assertMoveLines(run3.move_id, [
            (self.sbp_expense, 900.00, 0.0),
            (self.sbp_reserve, 0.0, 900.00),
        ])
        self.assertAlmostEqual(plan.recognised_cumulative, 2800.00,
                               places=2)
        # Ledger: reserve credit 900 + 1,000 + 900 = 2,800; expense the
        # same on the debit side.
        self.assertAlmostEqual(
            self.posted_balance(self.sbp_reserve), -2800.00, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.sbp_expense), 2800.00, places=2)

    # ------------------------------------------------------------------
    # 2. graded vesting (IFRS 2.IG11)
    # ------------------------------------------------------------------
    def test_golden_graded_vesting_tranches(self):
        """IFRS 2.IG11: 300 options in three equal tranches vesting after
        1/2/3 years with grant-date FVs 10/9/8; each tranche is expensed
        over its OWN vesting period off its own FV. No forfeitures.

        Ideal hand derivation (tranche count 100 each):
          Year 1: 100x10x(12/12) + 100x9x(12/24) + 100x8x(12/36)
                = 1,000 + 450 + 266.67 = 1,716.67
          Year 2: 1,000 + 900 + 100x8x(24/36) = 2,433.33
          Year 3: 1,000 + 900 + 800 = 2,700.00
        Engine storage detail: portion_pct stores at 6dp (33.333333), so
        each tranche counts 99.999999 instruments and the raw cumulatives
        are 1,716.6666495 / 2,433.3333090 / 2,699.9999730; rounded once
        at 2dp they coincide with the ideal figures above, giving charges
          1,716.67 / (2,433.33 - 1,716.67) = 716.66 / (2,700.00 -
          2,433.33) = 266.67.
        """
        third = 100.0 / 3.0
        plan = self._plan(
            [{'partner_id': self.partner_a.id, 'instruments_granted': 300,
              'expected_forfeiture_pct': 0.0}],
            graded_vesting=True,
            tranche_ids=[
                (0, 0, {'name': 'T1', 'portion_pct': third,
                        'vesting_end_date': '2027-01-01',
                        'fair_value': 10.0}),
                (0, 0, {'name': 'T2', 'portion_pct': third,
                        'vesting_end_date': '2028-01-01',
                        'fair_value': 9.0}),
                (0, 0, {'name': 'T3', 'portion_pct': third,
                        'vesting_end_date': '2029-01-01',
                        'fair_value': 8.0}),
            ])

        run1 = self._run(plan, '2027-01-01')
        self.assertAlmostEqual(run1.cumulative_target, 1716.67, places=2,
                               msg='year-1 graded cumulative')
        self.assertMoveLines(run1.move_id, [
            (self.sbp_expense, 1716.67, 0.0),
            (self.sbp_reserve, 0.0, 1716.67),
        ])

        run2 = self._run(plan, '2028-01-01')
        self.assertAlmostEqual(run2.cumulative_target, 2433.33, places=2,
                               msg='year-2 graded cumulative')
        self.assertMoveLines(run2.move_id, [
            (self.sbp_expense, 716.66, 0.0),
            (self.sbp_reserve, 0.0, 716.66),
        ])

        run3 = self._run(plan, '2029-01-01')
        self.assertAlmostEqual(run3.cumulative_target, 2700.00, places=2,
                               msg='year-3 graded cumulative')
        self.assertMoveLines(run3.move_id, [
            (self.sbp_expense, 266.67, 0.0),
            (self.sbp_reserve, 0.0, 266.67),
        ])
        # Total expense = 300 instruments x average tranche FV 9 = 2,700.
        self.assertAlmostEqual(
            self.posted_balance(self.sbp_expense), 2700.00, places=2)

    # ------------------------------------------------------------------
    # 3. market condition: failure never reverses (IFRS 2.21)
    # ------------------------------------------------------------------
    def test_golden_market_condition_failure_sticks(self):
        """IFRS 2.21: a TSR (market) condition is priced into the
        grant-date FV; if it fails but service is completed, the expense
        is NOT reversed.

        200 options, market-adjusted grant-date FV 6.00, 2-year service.
        Year 1: cumulative = 200 x 6 x 12/24 = 600.00, charge 600.00.
        Then the TSR target is missed but all 200 grantees serve out the
        period (condition_failed on, finalised with 200 service
        completers).
        Year 2: cumulative = 200 x 6 x 24/24 = 1,200.00 - unchanged by
        the market outcome - charge 600.00. Nothing reverses.
        """
        plan = self._plan(
            [{'partner_id': self.partner_a.id, 'instruments_granted': 200,
              'grant_date_fair_value': 6.0,
              'expected_forfeiture_pct': 0.0}],
            condition_kind='market', vesting_years=2)
        grant = plan.grant_ids

        run1 = self._run(plan, '2027-01-01')
        self.assertMoveLines(run1.move_id, [
            (self.sbp_expense, 600.00, 0.0),
            (self.sbp_reserve, 0.0, 600.00),
        ])

        # Market condition fails; service completed by all 200.
        grant.write({'condition_failed': True,
                     'vesting_finalised': True,
                     'actual_vested_qty': 200})
        run2 = self._run(plan, '2028-01-01')
        self.assertAlmostEqual(
            run2.cumulative_target, 1200.00, places=2,
            msg='market failure must not reduce the cumulative')
        self.assertMoveLines(run2.move_id, [
            (self.sbp_expense, 600.00, 0.0),
            (self.sbp_reserve, 0.0, 600.00),
        ])
        self.assertAlmostEqual(
            self.posted_balance(self.sbp_expense), 1200.00, places=2,
            msg='IFRS 2.21: expense stands although the market condition '
                'failed')

    # ------------------------------------------------------------------
    # 4. non-market condition failure reverses in full (contrast case)
    # ------------------------------------------------------------------
    def test_golden_non_market_failure_reverses(self):
        """IFRS 2 contrast to the market case: a NON-market performance
        condition that fails means no goods/services vest, so the
        cumulative expense reverses (the no-reversal rule of IFRS 2.23
        protects only market conditions).

        100 options, FV 5.00, 2-year vesting, no forfeiture estimate.
        Year 1: cumulative = 100 x 5 x 12/24 = 250.00, charge 250.00.
        Condition fails. Year 2: cumulative = 0.00, charge -250.00:
        JE Dr reserve 250.00 / Cr expense 250.00 (full reversal).
        """
        plan = self._plan(
            [{'partner_id': self.partner_a.id, 'instruments_granted': 100,
              'grant_date_fair_value': 5.0,
              'expected_forfeiture_pct': 0.0}],
            condition_kind='non_market', vesting_years=2)
        grant = plan.grant_ids

        run1 = self._run(plan, '2027-01-01')
        self.assertMoveLines(run1.move_id, [
            (self.sbp_expense, 250.00, 0.0),
            (self.sbp_reserve, 0.0, 250.00),
        ])

        grant.condition_failed = True
        run2 = self._run(plan, '2028-01-01')
        self.assertAlmostEqual(run2.cumulative_target, 0.00, places=2)
        self.assertMoveLines(run2.move_id, [
            (self.sbp_reserve, 250.00, 0.0),
            (self.sbp_expense, 0.0, 250.00),
        ])
        self.assertAlmostEqual(
            self.posted_balance(self.sbp_reserve), 0.00, places=2,
            msg='non-market failure must clear the reserve')
        self.assertAlmostEqual(
            self.posted_balance(self.sbp_expense), 0.00, places=2)

    # ------------------------------------------------------------------
    # 5. cash-settled SARs (IFRS 2.30-33)
    # ------------------------------------------------------------------
    def test_golden_cash_settled_sar_lifecycle(self):
        """IFRS 2.30-33: 100 SARs, 2-year service, liability remeasured
        to CURRENT fair value x vested fraction each period.

        Year 1 (FV 12.00, 12/24 vested):
          liability = 100 x 12 x 1/2 = 600.00, charge 600.00
          JE: Dr expense 600.00 / Cr liability 600.00
        Year 2 (FV 15.00, fully vested):
          liability = 100 x 15 = 1,500.00, charge 900.00
        Settlement at 1,400.00 (intrinsic value paid):
          true-up  = 1,400.00 - 1,500.00 = -100.00 through expense:
                     Dr liability 100.00 / Cr expense 100.00
          payment  = Dr liability 1,400.00 / Cr cash 1,400.00
        Net expense over the life = 600 + 900 - 100 = 1,400.00 = cash
        paid; the liability closes at nil.
        """
        plan = self._plan(
            [{'partner_id': self.partner_a.id, 'instruments_granted': 100,
              'expected_forfeiture_pct': 0.0}],
            settlement='cash', vesting_years=2)

        run1 = self._run(plan, '2027-01-01', fv=12.0)
        self.assertAlmostEqual(run1.cumulative_target, 600.00, places=2)
        self.assertMoveLines(run1.move_id, [
            (self.sbp_expense, 600.00, 0.0),
            (self.sbp_liability, 0.0, 600.00),
        ])

        run2 = self._run(plan, '2028-01-01', fv=15.0)
        self.assertAlmostEqual(run2.cumulative_target, 1500.00, places=2)
        self.assertMoveLines(run2.move_id, [
            (self.sbp_expense, 900.00, 0.0),
            (self.sbp_liability, 0.0, 900.00),
        ])
        self.assertAlmostEqual(plan.recognised_cumulative, 1500.00,
                               places=2)

        before = plan.move_ids
        with freeze_time('2028-01-15'):
            plan.settlement_amount = 1400.00
            plan.action_settle()
        self.assertAlmostEqual(
            plan.settlement_amount, 1400.00, places=2,
            msg='the terminal plan must preserve the sealed payment basis',
        )
        settle_moves = plan.move_ids - before
        self.assertEqual(len(settle_moves), 2,
                         'settlement posts a true-up and a payment')
        trueup = settle_moves.filtered(
            lambda m: any(l.credit and l.account_id == self.sbp_expense
                          for l in m.line_ids))
        payment = settle_moves - trueup
        self.assertMoveLines(trueup, [
            (self.sbp_liability, 100.00, 0.0),
            (self.sbp_expense, 0.0, 100.00),
        ])
        self.assertMoveLines(payment, [
            (self.sbp_liability, 1400.00, 0.0),
            (self.account_cash, 0.0, 1400.00),
        ])
        self.assertEqual(plan.state, 'settled')
        self.assertAlmostEqual(plan.recognised_cumulative, 0.00, places=2)
        # Liability: 600 + 900 - 100 - 1,400 = 0; expense nets to 1,400.
        self.assertAlmostEqual(
            self.posted_balance(self.sbp_liability), 0.00, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.sbp_expense), 1400.00, places=2)

    # ------------------------------------------------------------------
    # 6. cancellation acceleration (IFRS 2.28(a))
    # ------------------------------------------------------------------
    def test_golden_cancellation_accelerates_remaining(self):
        """IFRS 2.28(a): cancelling during vesting recognises immediately
        the amount that would otherwise have been recognised over the
        remainder of the vesting period.

        100 options, FV 9.00, 3-year cliff, no forfeitures.
        Year 1: cumulative = 100 x 9 x 12/36 = 300.00, charge 300.00.
        Cancel mid-year-2: full measure = 100 x 9 = 900.00, so the
        acceleration charge = 900.00 - 300.00 = 600.00:
        JE Dr expense 600.00 / Cr reserve 600.00; plan is cancelled.
        """
        plan = self._plan(
            [{'partner_id': self.partner_a.id, 'instruments_granted': 100,
              'grant_date_fair_value': 9.0,
              'expected_forfeiture_pct': 0.0}])
        self._run(plan, '2027-01-01')
        self.assertAlmostEqual(plan.recognised_cumulative, 300.00,
                               places=2)

        before = plan.move_ids
        with freeze_time('2027-06-30'):
            plan.action_cancel()
        accel = plan.move_ids - before
        self.assertEqual(len(accel), 1)
        self.assertMoveLines(accel, [
            (self.sbp_expense, 600.00, 0.0),
            (self.sbp_reserve, 0.0, 600.00),
        ])
        self.assertEqual(plan.state, 'cancelled')
        self.assertAlmostEqual(plan.recognised_cumulative, 900.00,
                               places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.sbp_reserve), -900.00, places=2)

    def test_cancellation_posts_negative_terminal_true_down(self):
        """A terminal cancellation cannot strand an over-recognised estimate.

        Year 2 first recognises 600.  A later 50% forfeiture estimate lowers
        the full current measure to 450, so cancellation must debit reserve
        and credit expense for 150 before closing the plan.
        """
        plan = self._plan(
            [{'partner_id': self.partner_a.id, 'instruments_granted': 100,
              'grant_date_fair_value': 9.0,
              'expected_forfeiture_pct': 0.0}])
        self._run(plan, '2028-01-01')
        self.assertAlmostEqual(plan.recognised_cumulative, 600.00, places=2)
        plan.grant_ids.write({'expected_forfeiture_pct': 50.0})

        before = plan.move_ids
        with freeze_time('2028-06-30'):
            plan.action_cancel()
        true_down = plan.move_ids - before
        self.assertEqual(len(true_down), 1)
        self.assertMoveLines(true_down, [
            (self.sbp_reserve, 150.00, 0.0),
            (self.sbp_expense, 0.0, 150.00),
        ])
        self.assertEqual(plan.state, 'cancelled')
        self.assertAlmostEqual(plan.recognised_cumulative, 450.00, places=2)

    def test_equity_settlement_posts_final_unrecognised_measure(self):
        """Settlement performs final cumulative recognition before closure."""
        plan = self._plan(
            [{'partner_id': self.partner_a.id, 'instruments_granted': 100,
              'grant_date_fair_value': 9.0,
              'expected_forfeiture_pct': 0.0}])
        before = plan.move_ids
        with freeze_time('2029-01-01'):
            plan.action_settle()
        final_move = plan.move_ids - before
        self.assertEqual(len(final_move), 1)
        self.assertMoveLines(final_move, [
            (self.sbp_expense, 900.00, 0.0),
            (self.sbp_reserve, 0.0, 900.00),
        ])
        self.assertEqual(plan.state, 'settled')
        self.assertAlmostEqual(plan.recognised_cumulative, 900.00, places=2)

    # ------------------------------------------------------------------
    # 7. modification incremental fair value (IFRS 2.27)
    # ------------------------------------------------------------------
    def test_golden_modification_incremental_fv(self):
        """IFRS 2.27: a beneficial modification (repricing) adds the
        incremental fair value, expensed over the REMAINING vesting
        period; the original grant-date expense continues untouched.

        100 options, FV 10.00, 4-year cliff (48 months).
        Year 1: cumulative = 100 x 10 x 12/48 = 250.00, charge 250.00.
        Repriced at 2027-01-01 with incremental FV 3.00; remaining
        vesting = 36 months (2027-01-01 -> 2030-01-01).
        Year 2 (24/48 original, 12/36 incremental):
          original    = 100 x 10 x 24/48 = 500.00
          incremental = 100 x 3 x 12/36  = 100.00
          cumulative  = 600.00, charge 350.00
        Final (2030-01-01, both fully vested):
          cumulative  = 1,000 + 300 = 1,300.00, charge 700.00
        A negative incremental FV is refused outright: a modification
        that reduces fair value is ignored for measurement.
        """
        plan = self._plan(
            [{'partner_id': self.partner_a.id, 'instruments_granted': 100,
              'grant_date_fair_value': 10.0,
              'expected_forfeiture_pct': 0.0}],
            vesting_years=4)
        self._run(plan, '2027-01-01')
        self.assertAlmostEqual(plan.recognised_cumulative, 250.00,
                               places=2)

        self.env['eh.sbp.modification'].create({
            'plan_id': plan.id, 'name': 'Repricing',
            'date': '2027-01-01', 'incremental_fv': 3.0,
        })

        run2 = self._run(plan, '2028-01-01')
        self.assertAlmostEqual(run2.cumulative_target, 600.00, places=2,
                               msg='original 500 + incremental 100')
        self.assertMoveLines(run2.move_id, [
            (self.sbp_expense, 350.00, 0.0),
            (self.sbp_reserve, 0.0, 350.00),
        ])

        run3 = self._run(plan, '2030-01-01')
        self.assertAlmostEqual(run3.cumulative_target, 1300.00, places=2)
        self.assertMoveLines(run3.move_id, [
            (self.sbp_expense, 700.00, 0.0),
            (self.sbp_reserve, 0.0, 700.00),
        ])

        with self.assertRaises(ValidationError, msg='a reduction in FV is '
                               'ignored, never booked'):
            self.env['eh.sbp.modification'].create({
                'plan_id': plan.id, 'name': 'Reduction',
                'date': '2030-01-02', 'incremental_fv': -2.0,
            })

    # ------------------------------------------------------------------
    # 8. valuation helper sanity (Black-Scholes + binomial)
    # ------------------------------------------------------------------
    def test_golden_valuation_black_scholes_and_binomial(self):
        """Black-Scholes sanity: S=100, K=100, vol 20%, r 5%, T=1, no
        dividend. Hand value: d1 = (0.05 + 0.02)/0.20 = 0.35,
        d2 = 0.15, N(0.35) = 0.63683, N(0.15) = 0.55962,
        C = 100 x 0.63683 - 100 x e^-0.05 x 0.55962 = 10.4506.
        The A-S 7.1.26 CDF approximation is accurate to 1.5e-7, far
        inside the 0.05 test tolerance. The CRR binomial at N=100
        converges on Black-Scholes (computed diff here is about 0.02,
        asserted within 0.15).
        """
        bs = self.env['eh.sbp.valuation'].create({
            'name': '/', 'pricing_model': 'black_scholes',
            'spot': 100.0, 'strike': 100.0, 'volatility_pct': 20.0,
            'rate_pct': 5.0, 'term_years': 1.0,
            'dividend_yield_pct': 0.0,
        })
        bs.action_compute()
        self.assertLessEqual(
            abs(bs.result_value - 10.45), 0.05,
            'Black-Scholes ATM call must sit at about 10.45, got %s'
            % bs.result_value)

        bino = self.env['eh.sbp.valuation'].create({
            'name': '/', 'pricing_model': 'binomial',
            'spot': 100.0, 'strike': 100.0, 'volatility_pct': 20.0,
            'rate_pct': 5.0, 'term_years': 1.0,
            'dividend_yield_pct': 0.0, 'steps': 100,
        })
        bino.action_compute()
        self.assertLessEqual(
            abs(bino.result_value - bs.result_value), 0.15,
            'CRR(100) must converge near Black-Scholes, got %s vs %s'
            % (bino.result_value, bs.result_value))

        # Degenerate guards: zero term prices at intrinsic (110-100=10).
        intrinsic = self.env['eh.sbp.valuation'].create({
            'name': '/', 'pricing_model': 'black_scholes',
            'spot': 110.0, 'strike': 100.0, 'volatility_pct': 20.0,
            'rate_pct': 5.0, 'term_years': 0.0,
        })
        intrinsic.action_compute()
        self.assertAlmostEqual(intrinsic.result_value, 10.0, places=4)

        # Result is copyable onto a grant as its grant-date FV.
        plan = self.env['eh.sbp.plan'].create({
            'name': '/', 'grant_date': '2026-01-01',
            'expense_account_id': self.sbp_expense.id,
            'equity_account_id': self.sbp_reserve.id,
            'journal_id': self.journal_misc.id,
            'grant_ids': [(0, 0, {
                'partner_id': self.partner_a.id,
                'instruments_granted': 50,
                'valuation_id': bs.id,
            })],
        })
        plan.grant_ids.action_use_valuation()
        self.assertAlmostEqual(
            plan.grant_ids.grant_date_fair_value, bs.result_value,
            places=4, msg='valuation result must copy onto the grant FV')

    # ------------------------------------------------------------------
    # 9. disclosure rollforward (IFRS 2.45)
    # ------------------------------------------------------------------
    def test_golden_disclosure_rollforward_and_waep(self):
        """IFRS 2.45: rollforward of granted / forfeited / exercised /
        expired with WAEP of the outstanding priced instruments.

        Grant A: 300 granted at strike 12.00, finalised with 280 vested
        (20 forfeited), 100 exercised, 30 expired:
        outstanding = 300 - 20 - 100 - 30 = 150.
        Grant B: 100 granted at strike 8.00, fully outstanding.
        Totals: granted 400, forfeited 20, exercised 100, expired 30,
        outstanding 250.
        WAEP = (150 x 12 + 100 x 8) / 250 = (1,800 + 800) / 250 = 10.40.
        """
        plan = self._plan(
            [{'partner_id': self.partner_a.id, 'instruments_granted': 300,
              'grant_date_fair_value': 10.0, 'exercise_price': 12.0,
              'expected_forfeiture_pct': 0.0},
             {'partner_id': self.partner_b.id, 'instruments_granted': 100,
              'grant_date_fair_value': 10.0, 'exercise_price': 8.0,
              'expected_forfeiture_pct': 0.0}])
        grant_a = plan.grant_ids[0]
        grant_a.write({
            'vesting_finalised': True, 'actual_vested_qty': 280,
            'exercised_qty': 100, 'expired_qty': 30,
        })
        self.assertEqual(plan.granted_total, 400)
        self.assertEqual(plan.forfeited_total, 20)
        self.assertEqual(plan.exercised_total, 100)
        self.assertEqual(plan.expired_total, 30)
        self.assertEqual(plan.outstanding_total, 250)
        self.assertAlmostEqual(plan.waep_outstanding, 10.40, places=4)

    # ------------------------------------------------------------------
    # 10. guardrails: frozen inputs, sealed moves, chronology
    # ------------------------------------------------------------------
    def test_golden_guardrails(self):
        """Non-compliant states are blocked, not honour-system."""
        plan = self._plan(
            [{'partner_id': self.partner_a.id, 'instruments_granted': 100,
              'grant_date_fair_value': 10.0,
              'expected_forfeiture_pct': 0.0}])
        run1 = self._run(plan, '2027-01-01')

        # Measurement inputs freeze once a run posts.
        with self.assertRaises(UserError):
            plan.grant_date = '2026-06-01'
        with self.assertRaises(UserError):
            plan.grant_ids.instruments_granted = 500
        with self.assertRaises(UserError):
            self.env['eh.sbp.grant'].create({
                'plan_id': plan.id, 'partner_id': self.partner_b.id,
                'instruments_granted': 50})
        # Estimates stay updatable (IFRS 2.20 true-up path).
        plan.grant_ids.expected_forfeiture_pct = 2.0

        # State re-key by a non-superuser is blocked by eh.workflow.guard
        # (covered in test_workflow_guard with a low-privilege user). This
        # golden runs as superuser, where state is action-driven by convention
        # and the sealed move below is the binding protection.

        # Runs post chronologically; duplicates per period are refused.
        with self.assertRaises(UserError):
            self._run(plan, '2026-06-01')
        # Posted runs cannot be deleted, and their move is sealed.
        with self.assertRaises(UserError):
            run1.unlink()
        self.assertTrue(run1.move_id.eh_sealed)

        # Zero-charge runs post a subledger checkpoint without a noise journal
        # entry. Reset the
        # forfeiture estimate first: the 2% true-up above would otherwise
        # give this run a real (negative) charge and defeat the premise.
        plan.grant_ids.expected_forfeiture_pct = 0.0
        self._run(plan, '2029-01-01')
        zero = self._run(plan, '2029-01-02')
        self.assertEqual(zero.state, 'posted')
        self.assertTrue(zero.is_zero_charge)
        self.assertFalse(zero.move_id)

        # Tracking is configured on the audit-relevant fields.
        self.assertTrue(
            self.env['eh.sbp.plan']._fields['state'].tracking,
            'plan state must be chatter-tracked')
        self.assertTrue(
            self.env['eh.sbp.period.run']._fields['period_charge'].tracking,
            'run charge must be chatter-tracked')

    def test_locked_period_refuses_instead_of_silent_redating(self):
        plan = self._plan([
            {'partner_id': self.partner_a.id, 'instruments_granted': 100,
             'grant_date_fair_value': 10.0,
             'expected_forfeiture_pct': 0.0},
        ])
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id,
            'period_end': '2027-01-01',
        })
        self.env.company.sudo().fiscalyear_lock_date = date(2027, 1, 1)

        with self.assertRaisesRegex(UserError, 'accounting date.*locked'):
            run.action_post()
        self.assertFalse(run.move_id)
