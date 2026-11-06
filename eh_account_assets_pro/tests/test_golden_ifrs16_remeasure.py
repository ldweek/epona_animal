# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Golden worked examples for IFRS 16 lessee remeasurement and termination
(IFRS 16.39-46): a discount-rate change, a partial scope decrease, the
ROU floor at zero, and an early termination gain.

Module conventions the derivations follow (lease_contract.py /
lease_modify_wizard.py):

* periodic rate r = (1 + annual)^(m/12) - 1 with m months per period;
  annual cadence => r = annual;
* liability PV = pmt * (1 - (1+r)^-n) / r (times (1+r) in advance);
* remeasurement (IFRS 16.39-43): liability -> PV of revised payments at
  the revised rate, ROU adjusted by the same signed amount; a decrease
  is floored so the ROU never goes below zero, with the excess to P&L;
* partial scope decrease (IFRS 16.45-46): ROU reduced proportionately,
  and (liability reduction - proportionate ROU reduction) -> P&L;
* termination: derecognise remaining ROU (gross cost less accumulated
  depreciation) and remaining liability, difference to P&L.

Every expected figure below is hand-derived from these formulas with the
inputs stated in each test.
"""

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase

from .common import EhAssetTestCase


@tagged('eh_golden', 'eh_account_assets_pro', 'post_install', '-at_install')
class TestGoldenIfrs16Remeasure(EhGoldenTestCase, EhAssetTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref('account.group_account_manager')
        cls.account_mod_pl = cls._ensure_account(
            cls.env, '5940', 'Lease Remeasurement P/L', 'expense',
        )

    # ------------------------------------------------------------------
    # 1. Discount-rate change remeasurement (IFRS 16.40-43)
    # ------------------------------------------------------------------
    def test_golden_rate_change_remeasurement(self):
        """3 annual payments of 10,000 arrears at 5 percent; post period
        1 at 5 percent, then change the discount rate to 6 percent on the
        remaining 2 payments (a remeasurement, IFRS 16.42(b)).

        Liability at 5% = 10,000 * (1 - 1.05^-3)/0.05
                        = 10,000 * 2.72324803 = 27,232.48.
        Period 1 @5%: interest 27,232.48 * 0.05 = 1,361.62;
          principal 10,000 - 1,361.62 = 8,638.38; remaining 18,594.10.
        ROU (no IDC) = 27,232.48 over 36 monthly rows. The regular rounded
          monthly charge is 756.46, so after 12 rows carrying is
          27,232.48 - 9,077.52 = 18,154.96 (the final row trues rounding).

        Remeasure the remaining 2 payments at 6%:
          new liability = 10,000 * (1 - 1.06^-2)/0.06
                        = 10,000 * 1.83339267 = 18,333.93.
          delta = 18,333.93 - 18,594.10 = -260.17 (a decrease).
        The decrease (260.17) is well below the ROU carrying (18,154.96),
        so it is fully absorbed by the ROU: no P&L.

        Remeasurement JE (IFRS 16.39): Dr Liability 260.17 / Cr ROU 260.17.
        """
        lease = self._make_lease(
            commencement_date='2025-01-31',
            term_months=36, cadence='annual',
            payment_timing='arrears', payment_amount=10_000.0,
            incremental_borrowing_rate=5.0,
        )
        lease.action_activate()
        self.assertAlmostEqual(
            lease.liability_initial_value, 27_232.48, places=2,
        )
        # Post all 12 monthly ROU rows earned through the first annual payment.
        lines = lease.schedule_line_ids.sorted('sequence')
        earned = lines.filtered(
            lambda line: str(line.period_date) <= '2026-01-31',
        )
        earned.action_post()
        payment = earned.filtered(lambda line: line.payment_amount > 0)
        self.assertEqual(len(payment), 1)
        self.assertAlmostEqual(payment.interest, 1_361.62, places=2)
        self.assertAlmostEqual(payment.principal, 8_638.38, places=2)
        self.assertAlmostEqual(payment.liability_close, 18_594.10, places=2)
        self.assertAlmostEqual(
            lease._liability_balance_after_last_post(), 18_594.10, places=2,
        )
        self.assertAlmostEqual(
            lease._rou_carrying_amount(), 18_154.96, places=2,
        )

        wizard = self.env['eh.lease.modify.wizard'].create({
            'lease_id': lease.id,
            'modification_date': '2026-01-31',
            'modification_type': 'remeasure',
            'new_term_months': 24,
            'new_payment_amount': 10_000.0,
            'new_ibr': 6.0,
        })
        self.assertAlmostEqual(wizard.new_liability, 18_333.93, places=2)
        self.assertAlmostEqual(wizard.delta, -260.17, places=2)
        self.assertAlmostEqual(wizard.pl_amount, 0.00, places=2)
        self.assertAlmostEqual(wizard.rou_reduction, 260.17, places=2)
        wizard.action_modify()

        # Remeasurement move: liability down 260.17, ROU down 260.17.
        mods = lease.schedule_line_ids  # rebuilt schedule
        self.assertEqual(lease.state, 'modified')
        move = self.env['account.move'].search(
            [('ref', '=', 'Lease modification %s' % lease.display_name)],
        )
        self.assertMoveLines(move, [
            (self.account_lease_liability, 260.17, 0.0),
            (self.account_rou, 0.0, 260.17),
        ])
        # New liability the rebuilt schedule runs on.
        self.assertAlmostEqual(
            lease.liability_initial_value, 18_333.93, places=2,
        )
        # ROU carrying after adjustment = 18,154.96 - 260.17 = 17,894.79.
        self.assertAlmostEqual(
            lease._rou_carrying_amount(), 17_894.79, places=2,
        )
        # Two years of rebuilt monthly ROU rows; annual liability cash flows
        # remain embedded on the month-12 and month-24 rows.
        new_rows = mods.filtered(lambda l: not l.is_posted).sorted('sequence')
        self.assertEqual(len(new_rows), 24)
        self.assertEqual(
            len(new_rows.filtered(lambda line: line.payment_amount > 0)), 2,
        )

    # ------------------------------------------------------------------
    # 2. Partial scope decrease (IFRS 16.45-46)
    # ------------------------------------------------------------------
    def test_golden_partial_scope_decrease(self):
        """Base lease: 4 annual payments of 5,000 arrears at 0 percent
        (rate 0 isolates the IFRS 16.46(a) mechanic from discounting).

        Liability = ROU = 5,000 * 4 = 20,000 (no IDC).
        At commencement (nothing posted), decrease the scope by 25% and
        cut the payments to 3,000/yr over the same 4 years:
          proportionate liability and ROU reduction = 5,000 each;
          IFRS 16.46(a) scope gain/loss = 5,000 - 5,000 = zero;
          remaining liability and ROU before remeasurement = 15,000 each;
          revised liability = 3,000 * 4 = 12,000;
          subsequent remeasurement delta = -3,000, absorbed by the ROU.

        Scope-decrease JE:
          Dr Lease Liability 8,000
            Cr ROU Asset       8,000.
        New ROU carrying and new liability both equal 12,000.
        """
        lease = self._make_lease(
            commencement_date='2025-01-31',
            term_months=48, cadence='annual',
            payment_timing='arrears', payment_amount=5_000.0,
            incremental_borrowing_rate=0.0,
        )
        lease.action_activate()
        self.assertAlmostEqual(
            lease.liability_initial_value, 20_000.00, places=2,
        )
        self.assertAlmostEqual(lease.rou_initial_value, 20_000.00, places=2)
        self.assertAlmostEqual(
            lease._rou_carrying_amount(), 20_000.00, places=2,
        )

        wizard = self.env['eh.lease.modify.wizard'].create({
            'lease_id': lease.id,
            'modification_date': '2025-01-31',
            'modification_type': 'scope_decrease',
            'scope_decrease_pct': 25.0,
            'new_term_months': 48,
            'new_payment_amount': 3_000.0,
            'new_ibr': 0.0,
            'pl_account_id': self.account_mod_pl.id,
        })
        self.assertAlmostEqual(wizard.new_liability, 12_000.00, places=2)
        self.assertAlmostEqual(wizard.scope_gain_loss, 0.00, places=2)
        self.assertAlmostEqual(wizard.remeasurement_delta, -3_000.00, places=2)
        self.assertAlmostEqual(wizard.rou_reduction, 8_000.00, places=2)
        self.assertAlmostEqual(wizard.pl_amount, 0.00, places=2)
        wizard.action_modify()

        move = self.env['account.move'].search(
            [('ref', '=', 'Lease modification %s' % lease.display_name)],
        )
        self.assertMoveLines(move, [
            (self.account_lease_liability, 8_000.00, 0.0),
            (self.account_rou, 0.0, 8_000.00),
        ])
        self.assertAlmostEqual(
            lease.liability_initial_value, 12_000.00, places=2,
        )
        self.assertAlmostEqual(
            lease._rou_carrying_amount(), 12_000.00, places=2,
        )

    # ------------------------------------------------------------------
    # 3. Remeasurement decrease floored at zero, excess to P&L (16.39)
    # ------------------------------------------------------------------
    def test_golden_remeasurement_rou_floor_excess_to_pl(self):
        """A remeasurement DECREASE larger than the ROU carrying amount
        cannot take the ROU negative: floor it at zero and post the
        excess to P&L (IFRS 16.39).

        Base lease: 1 annual payment of 8,000 arrears at 0 percent, and
        useful-life depreciation contrived so the ROU carrying is small.
        To make a clean floor case, start from a 4-year 0% lease of
        2,000/yr (liability = ROU = 8,000), post 3 of the 4 periods so
        the ROU carrying falls to 8,000 * 1/4 = 2,000 and the remaining
        liability is 2,000, then remeasure the single remaining payment
        DOWN to zero (payment cut to a nominal 1 over 0 years is not
        valid, so instead cut the remaining payment so the liability
        decrease exceeds the 2,000 ROU).

        Simpler exact construction: 4 annual payments of 2,000 at 0%.
          liability = ROU = 8,000; per-year ROU dep = 2,000.
        Post 3 periods: remaining liability = 8,000 - 3*2,000 = 2,000;
          ROU carrying = 8,000 - 3*2,000 = 2,000.
        Remeasure the last year's payment to 0 is disallowed (payment
        must be positive); instead we drop the remaining term to a fresh
        lease is out of scope. So force the floor with a bigger cut:
        remeasure remaining 1 payment to 500/yr -> new liability 500,
        a decrease of 1,500, all absorbed by the 2,000 ROU (no floor).

        To actually hit the floor we need the decrease to exceed the ROU.
        Use instead: post 3 periods (ROU 2,000, liability 2,000) then a
        scope decrease of 100% would zero it; the floor case is a
        remeasurement where the liability falls by MORE than the ROU.
        That arises when payments are re-based very low relative to the
        already-depreciated ROU. Construct it directly: a 2-year 0% lease
        of 5,000/yr (liability = ROU = 10,000). Post period 1: remaining
        liability 5,000, ROU carrying 5,000. Now the underlying is
        partially returned so payments drop to 500 for the last year:
          new liability = 500; decrease = 5,000 - 500 = 4,500.
        The ROU can only absorb 5,000, so 4,500 < 5,000 -> still no floor.

        A genuine floor needs the ROU already BELOW the liability
        decrease. Depreciate faster than the liability amortises by using
        a positive rate. 2 annual payments of 5,000 at 20 percent:
          liability = 5,000*(1-1.2^-2)/0.2 = 5,000*1.52777778 = 7,638.89.
          ROU = 7,638.89 over 24 monthly rows. The regular rounded monthly
          charge is 318.29; 12 rows total 3,819.48 and leave 3,819.41
          carrying amount (the final row trues rounding).
        Post period 1 @20%: interest 7,638.89*0.2 = 1,527.78;
          principal 5,000 - 1,527.78 = 3,472.22; remaining 4,166.67.
          ROU dep through year 1 = 3,819.48; ROU carrying = 3,819.41.
        Remeasure the last payment DOWN to 100 at 20% (1 period):
          new liability = 100/1.2 = 83.33; decrease = 4,166.67 - 83.33
                        = 4,083.34.
        ROU carrying is 3,819.41, so it absorbs 3,819.41 and the excess
          round(4,083.34 - 3,819.41) = 263.93 goes to P&L (gain).

        Floor JE (IFRS 16.39):
          Dr Lease Liability 4,083.34
            Cr ROU Asset       3,819.41
            Cr P&L (gain)        263.93.
        """
        lease = self._make_lease(
            commencement_date='2025-01-31',
            term_months=24, cadence='annual',
            payment_timing='arrears', payment_amount=5_000.0,
            incremental_borrowing_rate=20.0,
        )
        lease.action_activate()
        self.assertAlmostEqual(
            lease.liability_initial_value, 7_638.89, places=2,
        )
        lines = lease.schedule_line_ids.sorted('sequence')
        earned = lines.filtered(
            lambda line: str(line.period_date) <= '2026-01-31',
        )
        earned.action_post()
        payment = earned.filtered(lambda line: line.payment_amount > 0)
        self.assertEqual(len(payment), 1)
        self.assertAlmostEqual(payment.interest, 1_527.78, places=2)
        self.assertAlmostEqual(payment.principal, 3_472.22, places=2)
        self.assertAlmostEqual(
            lease._liability_balance_after_last_post(), 4_166.67, places=2,
        )
        # The first 12 monthly ROU rows total 3,819.48 after currency
        # rounding, leaving the ROU carrying at 3,819.41.
        self.assertAlmostEqual(
            lease._rou_carrying_amount(), 3_819.41, places=2,
        )

        wizard = self.env['eh.lease.modify.wizard'].create({
            'lease_id': lease.id,
            'modification_date': '2026-01-31',
            'modification_type': 'remeasure',
            'new_term_months': 12,
            'new_payment_amount': 100.0,
            'new_ibr': 20.0,
            'pl_account_id': self.account_mod_pl.id,
        })
        self.assertAlmostEqual(wizard.new_liability, 83.33, places=2)
        self.assertAlmostEqual(wizard.delta, -4_083.34, places=2)
        # ROU carrying 3,819.41 absorbs the decrease; excess to P&L is
        # 4,083.34 - 3,819.41 = 263.93.
        self.assertAlmostEqual(wizard.rou_reduction, 3_819.41, places=2)
        self.assertAlmostEqual(wizard.pl_amount, 263.93, places=2)
        wizard.action_modify()

        move = self.env['account.move'].search(
            [('ref', '=', 'Lease modification %s' % lease.display_name)],
        )
        self.assertMoveLines(move, [
            (self.account_lease_liability, 4_083.34, 0.0),
            (self.account_rou, 0.0, 3_819.41),
            (self.account_mod_pl, 0.0, 263.93),
        ])
        # ROU floored at zero.
        self.assertAlmostEqual(
            lease._rou_carrying_amount(), 0.00, places=2,
        )

    # ------------------------------------------------------------------
    # 4. Early termination gain (IFRS 16 derecognition)
    # ------------------------------------------------------------------
    def test_golden_early_termination_gain(self):
        """3 annual payments of 10,000 arrears at 8 percent; post period
        1, then terminate early.

        Liability at 8% = 10,000 * (1 - 1.08^-3)/0.08
                        = 10,000 * 2.57709699 = 25,770.97.
        Period 1 @8%: interest 25,770.97 * 0.08 = 2,061.68;
          principal 10,000 - 2,061.68 = 7,938.32; remaining 17,832.65.
        ROU (no IDC) = 25,770.97 over 3 years = 8,590.3233/yr; after 1
          period ROU carrying = 25,770.97 - 8,590.32 = 17,180.65,
          accumulated depreciation 8,590.32.

        Termination derecognises both sides (no settlement):
          liability released 17,832.65; ROU given up 17,180.65;
          gain = 17,832.65 - 17,180.65 = 652.00 (liability > ROU).

        Termination JE:
          Dr Lease Liability            17,832.65
          Dr ROU Accumulated Depreciation 8,590.32
            Cr ROU Asset                  25,770.97   (gross cost)
            Cr P&L (gain)                    652.00.
        """
        lease = self._make_lease(
            commencement_date='2025-01-31',
            term_months=36, cadence='annual',
            payment_timing='arrears', payment_amount=10_000.0,
            incremental_borrowing_rate=8.0,
        )
        lease.action_activate()
        self.assertAlmostEqual(
            lease.liability_initial_value, 25_770.97, places=2,
        )
        lines = lease.schedule_line_ids.sorted('sequence')
        earned = lines.filtered(
            lambda line: str(line.period_date) <= '2026-01-31',
        )
        earned.action_post()
        payment = earned.filtered(lambda line: line.payment_amount > 0)
        self.assertEqual(len(payment), 1)
        self.assertAlmostEqual(payment.interest, 2_061.68, places=2)
        self.assertAlmostEqual(payment.principal, 7_938.32, places=2)
        self.assertAlmostEqual(
            lease._liability_balance_after_last_post(), 17_832.65, places=2,
        )
        self.assertAlmostEqual(
            lease._rou_carrying_amount(), 17_180.65, places=2,
        )

        wizard = self.env['eh.lease.terminate.wizard'].create({
            'lease_id': lease.id,
            'termination_date': '2026-01-31',
            'settlement_amount': 0.0,
            'pl_account_id': self.account_mod_pl.id,
        })
        self.assertAlmostEqual(wizard.current_liability, 17_832.65, places=2)
        self.assertAlmostEqual(wizard.current_rou, 17_180.65, places=2)
        self.assertAlmostEqual(wizard.rou_accumulated, 8_590.32, places=2)
        self.assertAlmostEqual(wizard.pl_amount, 652.00, places=2)
        wizard.action_terminate()

        self.assertEqual(lease.state, 'terminated')
        self.assertMoveLines(lease.termination_move_id, [
            (self.account_lease_liability, 17_832.65, 0.0),
            (self.account_rou_accum, 8_590.32, 0.0),
            (self.account_rou, 0.0, 25_770.97),
            (self.account_mod_pl, 0.0, 652.00),
        ])
