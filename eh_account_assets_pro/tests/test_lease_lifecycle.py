# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Lease lifecycle: create, compute schedule, activate, post due lines.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestLeaseLifecycle(EhAssetTestCase):

    def test_create_lease_assigns_sequence(self):
        lease = self._make_lease()
        self.assertNotEqual(lease.name, '/')
        self.assertTrue(lease.name.startswith('LSE/'))

    def test_compute_schedule_monthly_arrears(self):
        lease = self._make_lease(
            term_months=36, cadence='monthly',
            payment_timing='arrears', payment_amount=1000.0,
            incremental_borrowing_rate=6.0,
        )
        lease.action_compute_schedule()
        self.assertEqual(len(lease.schedule_line_ids), 36)
        # Liability initial value present.
        self.assertGreater(lease.liability_initial_value, 0)
        # Total payments == 36 * 1000.
        total_paid = sum(lease.schedule_line_ids.mapped('payment_amount'))
        self.assertAlmostEqual(total_paid, 36000.0, places=1)
        # Final liability should reach zero.
        last = lease.schedule_line_ids.sorted('sequence')[-1]
        self.assertAlmostEqual(last.liability_close, 0.0, places=1)

    def test_compute_schedule_quarterly(self):
        lease = self._make_lease(
            term_months=24, cadence='quarterly',
            payment_amount=3000.0,
        )
        lease.action_compute_schedule()
        lines = lease.schedule_line_ids.sorted('sequence')
        self.assertEqual(len(lines), 24)
        self.assertEqual(
            len(lines.filtered(lambda line: line.payment_amount > 0)), 8,
        )
        self.assertEqual(
            len(lines.filtered(lambda line: line.rou_amount > 0)), 24,
        )
        self.assertAlmostEqual(
            sum(lines.mapped('rou_amount')),
            lease.rou_initial_value,
            places=2,
        )

    def test_compute_schedule_invalid_cadence_term(self):
        lease = self._make_lease(
            term_months=10, cadence='quarterly',
        )
        with self.assertRaises(UserError):
            lease.action_compute_schedule()

    def test_activate_posts_opening_entry(self):
        lease = self._make_lease()
        lease.action_activate()
        self.assertEqual(lease.state, 'active')
        self.assertTrue(lease.opening_move_id)
        self.assertEqual(lease.opening_move_id.state, 'posted')
        # ROU = liability when no IDC and no prepaid.
        self.assertAlmostEqual(
            lease.rou_initial_value, lease.liability_initial_value, places=2,
        )

    def test_activate_with_initial_direct_costs(self):
        lease = self._make_lease(
            initial_direct_costs=500.0,
            prepaid_lease_payments=200.0,
        )
        lease.action_activate()
        # ROU = liability + IDC + prepaid.
        self.assertAlmostEqual(
            lease.rou_initial_value,
            lease.liability_initial_value + 700.0,
            places=2,
        )

    def test_activate_blocked_in_active_state(self):
        lease = self._make_lease()
        lease.action_activate()
        with self.assertRaises(UserError):
            lease.action_activate()

    def test_post_due_creates_period_moves(self):
        lease = self._make_lease(
            commencement_date='2025-01-31',
            term_months=12, cadence='monthly',
            payment_timing='arrears', payment_amount=1000.0,
        )
        lease.action_activate()
        lease.action_post_due_lines()
        posted = lease.schedule_line_ids.filtered(lambda l: l.is_posted)
        self.assertGreater(len(posted), 0)
        for line in posted:
            self.assertTrue(line.move_id)
            self.assertEqual(line.move_id.state, 'posted')

    def test_pv_advance_versus_arrears(self):
        lease_arrears = self._make_lease(
            payment_timing='arrears', payment_amount=1000.0,
            term_months=36, cadence='monthly',
            incremental_borrowing_rate=6.0,
        )
        lease_advance = self._make_lease(
            payment_timing='advance', payment_amount=1000.0,
            term_months=36, cadence='monthly',
            incremental_borrowing_rate=6.0,
        )
        lease_arrears.action_compute_schedule()
        lease_advance.action_compute_schedule()
        # Advance PV > arrears PV at the same rate and payment.
        self.assertGreater(
            lease_advance.liability_initial_value,
            lease_arrears.liability_initial_value,
        )

    # ---- amortisation invariants (regression for in-advance bug) ----

    def _assert_amortisation_invariants(self, lease):
        """The schedule must be internally consistent.

        Bug context: the original in-advance code computed liability_close
        from one principal/interest split, then rebalanced the stored
        principal/interest for the journal entry, so the schedule and the
        GL drifted apart. These invariants catch any future regression.
        """
        rounding = lease.currency_id.rounding or 0.01
        running = lease.liability_initial_value
        for line in lease.schedule_line_ids.sorted('sequence'):
            self.assertAlmostEqual(
                line.principal + line.interest,
                line.payment_amount,
                delta=rounding,
                msg="principal + interest must equal payment_amount",
            )
            self.assertAlmostEqual(
                line.liability_open,
                running,
                delta=rounding,
                msg="liability_open must equal previous liability_close",
            )
            self.assertAlmostEqual(
                line.liability_close,
                line.liability_open - line.principal,
                delta=rounding,
                msg="liability_close must equal liability_open - principal",
            )
            running = line.liability_close
        last = lease.schedule_line_ids.sorted('sequence')[-1]
        self.assertAlmostEqual(
            last.liability_close, 0.0, delta=rounding,
            msg="final liability must amortise to zero",
        )

    def test_invariants_monthly_arrears(self):
        lease = self._make_lease(
            term_months=36, cadence='monthly',
            payment_timing='arrears', payment_amount=1000.0,
        )
        lease.action_compute_schedule()
        self._assert_amortisation_invariants(lease)

    def test_invariants_monthly_advance(self):
        """The original in-advance bug: schedule and journal disagreed.

        Now principal+interest=pmt and liability_close=open-principal on
        every row, so a posted journal entry from a row reconciles
        exactly to the row's liability movement.
        """
        lease = self._make_lease(
            term_months=36, cadence='monthly',
            payment_timing='advance', payment_amount=1000.0,
        )
        lease.action_compute_schedule()
        self._assert_amortisation_invariants(lease)

    def test_invariants_quarterly_advance(self):
        lease = self._make_lease(
            term_months=24, cadence='quarterly',
            payment_timing='advance', payment_amount=3000.0,
        )
        lease.action_compute_schedule()
        self._assert_amortisation_invariants(lease)

    def test_invariants_annual_arrears(self):
        lease = self._make_lease(
            term_months=60, cadence='annual',
            payment_timing='arrears', payment_amount=12000.0,
        )
        lease.action_compute_schedule()
        self._assert_amortisation_invariants(lease)

    def test_rou_total_equals_initial(self):
        """ROU is spread over one row per month, independently of the payment
        cadence. Total recognised ROU must still equal rou_initial_value.
        """
        lease = self._make_lease(
            term_months=60, cadence='annual',
            payment_timing='arrears', payment_amount=12000.0,
        )
        lease.action_compute_schedule()
        rounding = lease.currency_id.rounding or 0.01
        total_rou = sum(lease.schedule_line_ids.mapped('rou_amount'))
        self.assertAlmostEqual(
            total_rou, lease.rou_initial_value, delta=rounding,
        )

    def test_period_move_balances(self):
        """Posting a period must produce a balanced journal entry."""
        lease = self._make_lease(
            commencement_date='2025-01-31',
            term_months=12, cadence='monthly',
            payment_timing='advance', payment_amount=1000.0,
        )
        lease.action_activate()
        lease.action_post_due_lines()
        posted = lease.schedule_line_ids.filtered(lambda l: l.is_posted)
        self.assertGreater(len(posted), 0)
        for line in posted:
            move = line.move_id
            self.assertTrue(move and move.state == 'posted')
            debit = sum(move.line_ids.mapped('debit'))
            credit = sum(move.line_ids.mapped('credit'))
            self.assertAlmostEqual(debit, credit, places=2)
