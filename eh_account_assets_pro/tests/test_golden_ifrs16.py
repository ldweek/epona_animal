# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Golden worked examples for the IFRS 16 additions: recognition
exemptions, term options, the lease / non-lease component split, and
basic lessor accounting.

Module conventions the derivations follow (lease_contract.py):

* periodic rate r = (1 + annual)^(m/12) - 1 with m months per period;
* liability PV = pmt * (1 - (1+r)^-n) / r (times (1+r) in advance),
  plus balloon / (1+r)^n for a reasonably-certain purchase price or
  termination penalty; one terminal rounding at 2dp;
* per-row rounding: interest and payment rounded, principal =
  round(payment - interest), close = round(open - principal); the LAST
  row trues up off the running balance (arrears: interest = open * r,
  payment = open + interest), so terminal rounding drift lands in the
  final payment, and the closing liability is exactly zero.

Every expected figure below is derived from those formulas with the
inputs stated in each test; closed forms are hand-checkable because the
rates are chosen so (1+r)^n collapses to powers of 1.05 or 1.10 (e.g.
monthly r at 5 percent annual gives (1+r)^24 = 1.05^2 = 1.1025).
"""

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase

from .common import EhAssetTestCase


@tagged('eh_golden', 'eh_account_assets_pro', 'post_install', '-at_install')
class TestGoldenIfrs16(EhGoldenTestCase, EhAssetTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account_lease_expense = cls._ensure_account(
            cls.env, '5300', 'Lease Expense', 'expense',
        )
        cls.account_rental_income = cls._ensure_account(
            cls.env, '4400', 'Rental Income', 'income_other',
        )
        cls.account_lease_interest_income = cls._ensure_account(
            cls.env, '4410', 'Lease Interest Income', 'income_other',
        )
        cls.account_net_investment = cls._ensure_account(
            cls.env, '1560', 'Net Investment in Leases', 'asset_current',
        )
        cls.account_dealer_revenue = cls._ensure_account(
            cls.env, '4420', 'Lease Selling Revenue', 'income',
        )
        cls.account_dealer_cost_of_sale = cls._ensure_account(
            cls.env, '5400', 'Lease Cost of Sale', 'expense_direct_cost',
        )

    # ------------------------------------------------------------------
    # 1. Short-term exemption (IFRS 16.5-8)
    # ------------------------------------------------------------------
    def test_golden_short_term_exemption_expense_only(self):
        """10-month monthly lease at 500/month, short-term election.

        IFRS 16.6: no ROU, no liability; payments expense straight line.
        Equal fixed payments, so each period's expense is exactly the
        500 payment; 10 rows, total expense 5,000 = total payments.
        """
        lease = self._make_lease(
            term_months=10, cadence='monthly',
            payment_timing='arrears', payment_amount=500.0,
            exemption='short_term',
            exemption_election_note='Class: office equipment; '
                                    'short-term election per IFRS 16.8.',
            lease_expense_account_id=self.account_lease_expense.id,
        )
        lease.action_compute_schedule()
        lease.action_activate()

        # No opening entry, no ROU, no liability.
        self.assertFalse(lease.opening_move_id)
        self.assertAlmostEqual(lease.rou_initial_value, 0.00, places=2)
        self.assertAlmostEqual(
            lease.liability_initial_value, 0.00, places=2,
        )
        lines = lease.schedule_line_ids.sorted('sequence')
        self.assertEqual(len(lines), 10)
        for line in lines:
            self.assertAlmostEqual(line.payment_amount, 500.00, places=2)
            self.assertAlmostEqual(line.interest, 0.00, places=2)
            self.assertAlmostEqual(line.principal, 0.00, places=2)
            self.assertAlmostEqual(line.rou_amount, 0.00, places=2)
            self.assertAlmostEqual(line.liability_close, 0.00, places=2)

        lines[0].action_post()
        self.assertMoveLines(lines[0].move_id, [
            (self.account_lease_expense, 500.00, 0.0),
            (self.account_cash, 0.0, 500.00),
        ])

    def test_short_term_exemption_blocked_above_12_months(self):
        with self.assertRaises(ValidationError):
            self._make_lease(
                term_months=15, exemption='short_term',
                lease_expense_account_id=self.account_lease_expense.id,
            )

    def test_short_term_blocked_by_certain_extension_beyond_12(self):
        """10-month base + reasonably-certain 6-month extension = 16
        months effective: the short-term election dies (IFRS 16.18)."""
        lease = self._make_lease(
            term_months=10, exemption='short_term',
            lease_expense_account_id=self.account_lease_expense.id,
        )
        with self.assertRaises(ValidationError):
            self.env['eh.lease.option'].create({
                'lease_id': lease.id,
                'option_type': 'extension',
                'extension_months': 6,
                'reasonably_certain': True,
            })

    # ------------------------------------------------------------------
    # 2. Low-value exemption (IFRS 16.6, B3-B8)
    # ------------------------------------------------------------------
    def test_golden_low_value_exemption_expense_only(self):
        """12-month lease of a 4,800-value asset (threshold 5,000):
        qualifies; pure expense schedule, exact rows as short-term."""
        lease = self._make_lease(
            term_months=12, cadence='monthly',
            payment_timing='arrears', payment_amount=400.0,
            exemption='low_value',
            underlying_asset_value=4_800.0,
            exemption_election_note='Laptop; low value when new.',
            lease_expense_account_id=self.account_lease_expense.id,
        )
        lease.action_compute_schedule()
        lease.action_activate()
        self.assertFalse(lease.opening_move_id)
        lines = lease.schedule_line_ids.sorted('sequence')
        self.assertEqual(len(lines), 12)
        self.assertAlmostEqual(
            sum(lines.mapped('payment_amount')), 4_800.00, places=2,
        )
        lines[0].action_post()
        self.assertMoveLines(lines[0].move_id, [
            (self.account_lease_expense, 400.00, 0.0),
            (self.account_cash, 0.0, 400.00),
        ])

    def test_low_value_blocked_above_threshold(self):
        # Company threshold defaults to 5,000; a 6,000 asset fails.
        with self.assertRaises(ValidationError):
            self._make_lease(
                term_months=12, exemption='low_value',
                underlying_asset_value=6_000.0,
                lease_expense_account_id=self.account_lease_expense.id,
            )

    # ------------------------------------------------------------------
    # 3. Extension option in the term (IFRS 16.18)
    # ------------------------------------------------------------------
    def test_golden_extension_option_extends_liability_term(self):
        """12m base + reasonably-certain 12m extension, monthly arrears,
        1,000/month at 5 percent annual.

        r = 1.05^(1/12) - 1 = 0.00407412...; (1+r)^24 = 1.05^2 = 1.1025.
        PV = 1000 * (1 - 1/1.1025) / r
           = 1000 * 0.09297052 / 0.00407412 = 22,819.76.
        Row 1: interest = 22,819.76 * r = 92.9695 -> 92.97;
               principal = 1,000 - 92.97 = 907.03; close 21,912.73.
        Row 24 (true-up): open 995.92, interest = 995.92 * r = 4.0575
               -> 4.06, payment = 995.92 + 4.06 = 999.98, close 0.00
               (the 2c terminal rounding drift lands here).
        Totals: payments 23,999.98; interest 1,180.22;
                principal 22,819.76 = the opening liability.
        """
        lease = self._make_lease(
            term_months=12, cadence='monthly',
            payment_timing='arrears', payment_amount=1_000.0,
            incremental_borrowing_rate=5.0,
        )
        self.env['eh.lease.option'].create({
            'lease_id': lease.id,
            'option_type': 'extension',
            'extension_months': 12,
            'reasonably_certain': True,
            'note': 'Renewal priced below market; exercise certain.',
        })
        self.assertEqual(lease.effective_term_months, 24)
        lease.action_compute_schedule()
        self.assertAlmostEqual(
            lease.liability_initial_value, 22_819.76, places=2,
        )
        lines = lease.schedule_line_ids.sorted('sequence')
        self.assertEqual(len(lines), 24)
        self.assertAlmostEqual(lines[0].interest, 92.97, places=2)
        self.assertAlmostEqual(lines[0].principal, 907.03, places=2)
        self.assertAlmostEqual(
            lines[0].liability_close, 21_912.73, places=2,
        )
        self.assertAlmostEqual(lines[-1].payment_amount, 999.98, places=2)
        self.assertAlmostEqual(lines[-1].interest, 4.06, places=2)
        self.assertAlmostEqual(lines[-1].liability_close, 0.00, places=2)
        self.assertAlmostEqual(
            sum(lines.mapped('payment_amount')), 23_999.98, places=2,
        )
        self.assertAlmostEqual(
            sum(lines.mapped('interest')), 1_180.22, places=2,
        )
        self.assertAlmostEqual(
            sum(lines.mapped('principal')), 22_819.76, places=2,
        )

    # ------------------------------------------------------------------
    # 4. Termination penalty in the liability (IFRS 16.27(e))
    # ------------------------------------------------------------------
    def test_golden_termination_penalty_in_liability(self):
        """12m monthly arrears 1,000 at 5 percent, reasonably-certain
        termination penalty 2,000 at end of term.

        (1+r)^12 = 1.05.
        PV = 1000 * (1 - 1/1.05) / r + 2000 / 1.05
           = 11,688.17 + 1,904.76 = 13,592.93.
        Row 12 (true-up): open 2,987.83, interest = open * r = 12.1728
               -> 12.17, payment = 2,987.83 + 12.17 = 3,000.00
               (= the 1,000 rent + the 2,000 penalty), close 0.
        """
        lease = self._make_lease(
            term_months=12, cadence='monthly',
            payment_timing='arrears', payment_amount=1_000.0,
            incremental_borrowing_rate=5.0,
        )
        self.env['eh.lease.option'].create({
            'lease_id': lease.id,
            'option_type': 'termination',
            'termination_penalty': 2_000.0,
            'reasonably_certain': True,
        })
        lease.action_compute_schedule()
        self.assertAlmostEqual(
            lease.liability_initial_value, 13_592.93, places=2,
        )
        lines = lease.schedule_line_ids.sorted('sequence')
        self.assertEqual(len(lines), 12)
        self.assertAlmostEqual(lines[0].interest, 55.38, places=2)
        self.assertAlmostEqual(lines[0].principal, 944.62, places=2)
        self.assertAlmostEqual(
            lines[-1].liability_open, 2_987.83, places=2,
        )
        self.assertAlmostEqual(lines[-1].interest, 12.17, places=2)
        self.assertAlmostEqual(
            lines[-1].payment_amount, 3_000.00, places=2,
        )
        self.assertAlmostEqual(lines[-1].liability_close, 0.00, places=2)

    def test_advance_balloon_settles_at_end_of_term(self):
        """Advance rent is paid at t0..t(N-1); a termination penalty is
        still payable at tN.  It must not be folded into the t(N-1) row at
        discounted value with the final accrual omitted."""
        lease = self._make_lease(
            commencement_date='2026-01-31',
            term_months=12, cadence='monthly',
            payment_timing='advance', payment_amount=1_000.0,
            incremental_borrowing_rate=5.0,
        )
        self.env['eh.lease.option'].create({
            'lease_id': lease.id,
            'option_type': 'termination',
            'termination_penalty': 2_000.0,
            'reasonably_certain': True,
        })

        lease.action_compute_schedule()
        lines = lease.schedule_line_ids.sorted('sequence')

        self.assertEqual(len(lines), 13)
        last_rent = lines[11]
        balloon = lines[12]
        self.assertEqual(str(last_rent.period_date), '2026-12-31')
        self.assertGreater(last_rent.interest, 0.0)
        self.assertAlmostEqual(last_rent.payment_amount, 1_000.01, places=2)
        self.assertAlmostEqual(last_rent.liability_close, 2_000.00, places=2)
        self.assertEqual(str(balloon.period_date), '2027-01-31')
        self.assertAlmostEqual(balloon.liability_open, 2_000.00, places=2)
        self.assertAlmostEqual(balloon.payment_amount, 2_000.00, places=2)
        self.assertAlmostEqual(balloon.interest, 0.00, places=2)
        self.assertAlmostEqual(balloon.principal, 2_000.00, places=2)
        self.assertAlmostEqual(balloon.liability_close, 0.00, places=2)
        # The final monthly ROU charge is earned at tN and legitimately shares
        # the balloon row; the opening t0 rent row carries no depreciation.
        self.assertAlmostEqual(lines[0].rou_amount, 0.00, places=2)
        self.assertAlmostEqual(balloon.rou_amount, 1_136.74, places=2)
        self.assertAlmostEqual(
            sum(lines.mapped('principal')),
            lease.liability_initial_value,
            places=2,
        )
        self.assertAlmostEqual(
            sum(lines.mapped('rou_amount')),
            lease.rou_initial_value,
            places=2,
        )

    # ------------------------------------------------------------------
    # 5. Purchase option: ROU over useful life (IFRS 16.27(d)/.32)
    # ------------------------------------------------------------------
    def test_golden_purchase_option_rou_over_useful_life(self):
        """24m monthly arrears 1,000 at 5 percent; reasonably-certain
        purchase option 5,000; underlying useful life 36 months.

        Liability: PV = annuity(24) + 5000 / 1.1025
                      = 22,819.76 + 4,535.15 = 27,354.91.
        Row 24 (true-up): open 5,975.65, interest = open * r = 24.3455
               -> 24.35, payment = 5,975.65 + 24.35 = 6,000.00
               (= 1,000 rent + 5,000 purchase price), close 0.
        ROU = liability (no IDC/prepaid) = 27,354.91, depreciated over
        36 months (useful life, NOT the 24-month term):
            per month = 27,354.91 / 36 = 759.8586 -> 759.86
            rows 1..35 at 759.86; row 36 true-up
              = 27,354.91 - 35 * 759.86 = 759.81.
        Rows 25-36 carry ROU depreciation only (no payment, no
        interest, no principal).
        """
        lease = self._make_lease(
            term_months=24, cadence='monthly',
            payment_timing='arrears', payment_amount=1_000.0,
            incremental_borrowing_rate=5.0,
            underlying_useful_life_months=36,
        )
        self.env['eh.lease.option'].create({
            'lease_id': lease.id,
            'option_type': 'purchase',
            'purchase_price': 5_000.0,
            'reasonably_certain': True,
        })
        lease.action_compute_schedule()
        self.assertAlmostEqual(
            lease.liability_initial_value, 27_354.91, places=2,
        )
        self.assertAlmostEqual(
            lease.rou_initial_value, 27_354.91, places=2,
        )
        lines = lease.schedule_line_ids.sorted('sequence')
        self.assertEqual(len(lines), 36)
        # Payment rows.
        self.assertAlmostEqual(lines[0].interest, 111.45, places=2)
        self.assertAlmostEqual(lines[0].principal, 888.55, places=2)
        self.assertAlmostEqual(lines[0].rou_amount, 759.86, places=2)
        self.assertAlmostEqual(
            lines[23].payment_amount, 6_000.00, places=2,
        )
        self.assertAlmostEqual(lines[23].interest, 24.35, places=2)
        self.assertAlmostEqual(lines[23].principal, 5_975.65, places=2)
        self.assertAlmostEqual(lines[23].liability_close, 0.00, places=2)
        # Depreciation-only tail (IFRS 16.32).
        self.assertAlmostEqual(lines[24].payment_amount, 0.00, places=2)
        self.assertAlmostEqual(lines[24].interest, 0.00, places=2)
        self.assertAlmostEqual(lines[24].principal, 0.00, places=2)
        self.assertAlmostEqual(lines[24].rou_amount, 759.86, places=2)
        self.assertAlmostEqual(lines[35].rou_amount, 759.81, places=2)
        self.assertAlmostEqual(
            sum(lines.mapped('rou_amount')), 27_354.91, places=2,
        )
        # A depreciation-only row posts a clean two-leg entry.
        lease.action_activate()
        lines[24].action_post()
        self.assertMoveLines(lines[24].move_id, [
            (self.account_rou_dep, 759.86, 0.0),
            (self.account_rou_accum, 0.0, 759.86),
        ])

    def test_purchase_option_requires_useful_life(self):
        lease = self._make_lease(
            term_months=24, payment_amount=1_000.0,
            underlying_useful_life_months=0,
        )
        self.env['eh.lease.option'].create({
            'lease_id': lease.id,
            'option_type': 'purchase',
            'purchase_price': 5_000.0,
            'reasonably_certain': True,
        })
        with self.assertRaises(UserError):
            lease.action_compute_schedule()

    # ------------------------------------------------------------------
    # 6. Lease / non-lease component split (IFRS 16.13-16)
    # ------------------------------------------------------------------
    def test_golden_component_split_liability_from_lease_share_only(self):
        """12m monthly arrears at 5 percent; contractual payment 1,000
        of which 20 percent is service (maintenance).

        Lease component = 800; service = 200 per period.
        Liability = PV of the 800s only:
            PV = 800 * (1 - 1/1.05) / r = 9,350.54.
        Row 1: interest = 9,350.54 * r = 38.0954 -> 38.10;
               principal = 800 - 38.10 = 761.90.
        ROU = 9,350.54 over 12 months = 779.2117 -> 779.21 per month.
        Posting row 1 settles the FULL 1,000 in cash: principal 761.90
        + interest 38.10 into the liability legs, 200 straight to
        expense, plus the ROU depreciation pair.
        """
        lease = self._make_lease(
            term_months=12, cadence='monthly',
            payment_timing='arrears', payment_amount=1_000.0,
            incremental_borrowing_rate=5.0,
            payment_service_pct=20.0,
            component_allocation_note='Maintenance priced standalone at '
                                      '200/month by the supplier.',
            lease_expense_account_id=self.account_lease_expense.id,
        )
        lease.action_compute_schedule()
        self.assertAlmostEqual(
            lease.liability_initial_value, 9_350.54, places=2,
        )
        lines = lease.schedule_line_ids.sorted('sequence')
        self.assertEqual(len(lines), 12)
        self.assertAlmostEqual(lines[0].payment_amount, 800.00, places=2)
        self.assertAlmostEqual(lines[0].service_amount, 200.00, places=2)
        self.assertAlmostEqual(lines[0].interest, 38.10, places=2)
        self.assertAlmostEqual(lines[0].principal, 761.90, places=2)

        lease.action_activate()
        # Opening entry runs off the lease component only.
        self.assertMoveLines(lease.opening_move_id, [
            (self.account_rou, 9_350.54, 0.0),
            (self.account_lease_liability, 0.0, 9_350.54),
        ])
        lines[0].action_post()
        self.assertMoveLines(lines[0].move_id, [
            (self.account_lease_liability, 761.90, 0.0),
            (self.account_interest, 38.10, 0.0),
            (self.account_lease_expense, 200.00, 0.0),
            (self.account_cash, 0.0, 1_000.00),
            (self.account_rou_dep, 779.21, 0.0),
            (self.account_rou_accum, 0.0, 779.21),
        ])

    # ------------------------------------------------------------------
    # 7. Finance lessor: net investment + interest income (IFRS 16.67-77)
    # ------------------------------------------------------------------
    def test_golden_finance_lessor_net_investment(self):
        """3 annual receipts of 1,000 in arrears at the 10 percent rate
        implicit in the lease.

        Net investment = 1000 * (1 - 1.1^-3) / 0.1 = 2,486.85.
        Period 1: interest = 2,486.85 * 0.1 = 248.685 -> 248.69;
                  principal = 1,000 - 248.69 = 751.31; close 1,735.54.
        Period 2: interest = 173.554 -> 173.55; principal 826.45;
                  close 909.09.
        Period 3 (true-up): interest = 90.909 -> 90.91; payment =
                  909.09 + 90.909 = 999.999 -> 1,000.00; principal =
                  1,000.00 - 90.91 = 909.09; close 0.
        Interest income total 513.15; principal total 2,486.85.
        """
        lease = self._make_lease(
            commencement_date='2023-01-31',
            term_months=36, cadence='annual',
            payment_timing='arrears', payment_amount=1_000.0,
            incremental_borrowing_rate=10.0,
            lessor_mode='finance',
            net_investment_account_id=self.account_net_investment.id,
            lessor_interest_income_account_id=(
                self.account_lease_interest_income.id
            ),
            lessor_counterpart_account_id=self.account_fixed.id,
        )
        lease.action_compute_schedule()
        self.assertAlmostEqual(
            lease.liability_initial_value, 2_486.85, places=2,
        )
        self.assertAlmostEqual(lease.rou_initial_value, 0.00, places=2)
        lines = lease.schedule_line_ids.sorted('sequence')
        self.assertEqual(len(lines), 3)
        expected_rows = [
            # (open, payment, interest, principal, close)
            (2_486.85, 1_000.00, 248.69, 751.31, 1_735.54),
            (1_735.54, 1_000.00, 173.55, 826.45, 909.09),
            (909.09, 1_000.00, 90.91, 909.09, 0.00),
        ]
        for line, row in zip(lines, expected_rows):
            self.assertAlmostEqual(line.liability_open, row[0], places=2)
            self.assertAlmostEqual(line.payment_amount, row[1], places=2)
            self.assertAlmostEqual(line.interest, row[2], places=2)
            self.assertAlmostEqual(line.principal, row[3], places=2)
            self.assertAlmostEqual(line.liability_close, row[4], places=2)
            self.assertAlmostEqual(line.rou_amount, 0.00, places=2)

        lease.action_activate()
        # IFRS 16.67: net investment recognised at commencement.
        self.assertMoveLines(lease.opening_move_id, [
            (self.account_net_investment, 2_486.85, 0.0),
            (self.account_fixed, 0.0, 2_486.85),
        ])
        # All three receipts are due (commencement 2023): post them.
        lease.action_post_due_lines()
        self.assertTrue(all(lines.mapped('is_posted')))
        self.assertMoveLines(lines[0].move_id, [
            (self.account_cash, 1_000.00, 0.0),
            (self.account_lease_interest_income, 0.0, 248.69),
            (self.account_net_investment, 0.0, 751.31),
        ])
        # The receivable amortises exactly to zero on the ledger.
        self.assertAlmostEqual(
            self.posted_balance(self.account_net_investment), 0.00,
            places=2,
        )
        self.assertEqual(lease.state, 'ended')

    # ------------------------------------------------------------------
    # 7b. Manufacturer / dealer finance lessor (IFRS 16.71-74)
    # ------------------------------------------------------------------
    def test_golden_dealer_finance_lessor_selling_profit(self):
        """Dealer / manufacturer finance lessor, IFRS 16.71-74.

        Inputs: fair value 100,000; carrying amount (cost) 80,000;
        5 annual payments of 22,541.08 in arrears at the 8 percent rate
        implicit; unguaranteed residual value (undiscounted) 11,754.62.

        annuity factor 5yr @8% = (1 - 1.08^-5)/0.08 = 3.99271004.
        PV of payments = 22,541.08 * 3.99271004 = 90,000.00 (to 2dp).
        PV of unguaranteed residual = 11,754.62 / 1.08^5
                                    = 11,754.62 / 1.4693280768 = 8,000.00.

        Net investment  = revenue + PV(residual) = 90,000 + 8,000 = 98,000.
        Selling revenue = lower of fair value (100,000) and PV of the
                          payments (90,000) = 90,000.
        Cost of sale    = carrying (80,000) - PV(residual) (8,000)
                        = 72,000.
        Selling profit  = 90,000 - 72,000 = 18,000.

        Commencement JE (IFRS 16.71-74):
          Dr Net investment      98,000.00
          Dr Cost of sale        72,000.00
            Cr Selling revenue     90,000.00
            Cr Asset derecognition 80,000.00   (= carrying amount).

        Period-1 interest income (IFRS 16.75) on the net investment:
          98,000 * 0.08 = 7,840.00; principal recovery
          22,541.08 - 7,840.00 = 14,701.08; close 83,298.92.
        The receivable amortises DOWN to the undiscounted residual
        11,754.62 after 5 receipts (recovered when the asset returns,
        not through the receipts).
        """
        lease = self._make_lease(
            commencement_date='2023-01-31',
            term_months=60, cadence='annual',
            payment_timing='arrears', payment_amount=22_541.08,
            incremental_borrowing_rate=8.0,
            lessor_mode='finance', lessor_dealer=True,
            fair_value_of_asset=100_000.0,
            carrying_amount_of_asset=80_000.0,
            unguaranteed_residual_value=11_754.62,
            net_investment_account_id=self.account_net_investment.id,
            lessor_interest_income_account_id=(
                self.account_lease_interest_income.id
            ),
            lessor_counterpart_account_id=self.account_fixed.id,
            dealer_revenue_account_id=self.account_dealer_revenue.id,
            dealer_cost_of_sale_account_id=(
                self.account_dealer_cost_of_sale.id
            ),
        )
        lease.action_compute_schedule()
        # Net investment (opening balance) = 98,000.
        self.assertAlmostEqual(
            lease.liability_initial_value, 98_000.00, places=2,
        )
        self.assertAlmostEqual(lease.rou_initial_value, 0.00, places=2)
        m = lease._dealer_measurement()
        self.assertAlmostEqual(m['pv_payments'], 90_000.00, places=2)
        self.assertAlmostEqual(m['pv_residual'], 8_000.00, places=2)
        self.assertAlmostEqual(m['net_investment'], 98_000.00, places=2)
        self.assertAlmostEqual(m['revenue'], 90_000.00, places=2)
        self.assertAlmostEqual(m['cost_of_sale'], 72_000.00, places=2)
        self.assertAlmostEqual(m['selling_profit'], 18_000.00, places=2)

        lines = lease.schedule_line_ids.sorted('sequence')
        self.assertEqual(len(lines), 5)
        expected_rows = [
            # (open, payment, interest, principal, close)
            (98_000.00, 22_541.08, 7_840.00, 14_701.08, 83_298.92),
            (83_298.92, 22_541.08, 6_663.91, 15_877.17, 67_421.75),
            (67_421.75, 22_541.08, 5_393.74, 17_147.34, 50_274.41),
            (50_274.41, 22_541.08, 4_021.95, 18_519.13, 31_755.28),
            (31_755.28, 22_541.08, 2_540.42, 20_000.66, 11_754.62),
        ]
        for line, row in zip(lines, expected_rows):
            self.assertAlmostEqual(line.liability_open, row[0], places=2)
            self.assertAlmostEqual(line.payment_amount, row[1], places=2)
            self.assertAlmostEqual(line.interest, row[2], places=2)
            self.assertAlmostEqual(line.principal, row[3], places=2)
            self.assertAlmostEqual(line.liability_close, row[4], places=2)
        # Receivable ends at the unguaranteed residual, not zero.
        self.assertAlmostEqual(
            lines[-1].liability_close, 11_754.62, places=2,
        )

        lease.action_activate()
        # IFRS 16.71-74 commencement entry recognises selling profit.
        self.assertMoveLines(lease.opening_move_id, [
            (self.account_net_investment, 98_000.00, 0.0),
            (self.account_dealer_cost_of_sale, 72_000.00, 0.0),
            (self.account_dealer_revenue, 0.0, 90_000.00),
            (self.account_fixed, 0.0, 80_000.00),
        ])
        # Period 1 receipt: interest income + net-investment recovery.
        lines[0].action_post()
        self.assertMoveLines(lines[0].move_id, [
            (self.account_cash, 22_541.08, 0.0),
            (self.account_lease_interest_income, 0.0, 7_840.00),
            (self.account_net_investment, 0.0, 14_701.08),
        ])

    # ------------------------------------------------------------------
    # 8. Operating lessor: straight-line rental income (IFRS 16.81)
    # ------------------------------------------------------------------
    def test_golden_operating_lessor_straight_line_income(self):
        lease = self._make_lease(
            term_months=12, cadence='monthly',
            payment_timing='arrears', payment_amount=1_000.0,
            lessor_mode='operating',
            lessor_income_account_id=self.account_rental_income.id,
        )
        lease.action_compute_schedule()
        lease.action_activate()
        # No derecognition, no net investment, no opening entry: the
        # underlying asset stays on the lessor's books.
        self.assertFalse(lease.opening_move_id)
        self.assertAlmostEqual(
            lease.liability_initial_value, 0.00, places=2,
        )
        lines = lease.schedule_line_ids.sorted('sequence')
        self.assertEqual(len(lines), 12)
        lines[0].action_post()
        self.assertMoveLines(lines[0].move_id, [
            (self.account_cash, 1_000.00, 0.0),
            (self.account_rental_income, 0.0, 1_000.00),
        ])
