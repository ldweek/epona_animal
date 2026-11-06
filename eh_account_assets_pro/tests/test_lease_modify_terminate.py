# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Lease modification and termination flows.
"""

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestLeaseModifyTerminate(EhAssetTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref('account.group_account_manager')
        cls.account_lease_expense = cls._ensure_account(
            cls.env, '5295', 'Modified Lease Service Expense', 'expense',
        )

    @staticmethod
    def _post_through(lease, cutoff):
        """Post a deterministic historical slice, independent of wall time."""
        cutoff = fields.Date.to_date(cutoff)
        lease.schedule_line_ids.filtered(
            lambda line: line.period_date <= cutoff,
        ).action_post()

    def test_modify_extends_term(self):
        lease = self._make_lease(
            commencement_date='2025-01-31',
            term_months=24, cadence='monthly',
            payment_amount=1000.0,
        )
        lease.action_activate()
        self._post_through(lease, '2026-03-31')
        wizard = self.env['eh.lease.modify.wizard'].create({
            'lease_id': lease.id,
            'modification_date': '2026-04-30',
            'new_term_months': 30,
            'new_payment_amount': 800.0,
            'new_ibr': 7.0,
        })
        wizard.action_modify()
        self.assertEqual(lease.state, 'modified')
        # term_months remains the executed contract term.  The revised
        # remaining measurement is carried separately and must not rewrite
        # that source evidence.
        self.assertEqual(lease.term_months, 24)
        self.assertEqual(lease.current_measurement_term_months, 30)
        self.assertEqual(lease.payment_amount, 800.0)
        self.assertEqual(lease.modification_count, 1)
        move = self.env['account.move'].search([
            ('ref', '=', 'Lease modification %s' % lease.display_name),
            ('date', '=', '2026-04-30'),
        ])
        self.assertEqual(len(move), 1)
        self.assertTrue(
            move.eh_sealed,
            "a lease modification entry backs frozen measurements and must be sealed",
        )
        with self.assertRaises(UserError):
            move.button_draft()
        # Has unposted lines under the new schedule.
        unposted = lease.schedule_line_ids.filtered(lambda l: not l.is_posted)
        self.assertGreater(len(unposted), 0)

    def test_modify_blocked_in_draft(self):
        lease = self._make_lease()
        wizard = self.env['eh.lease.modify.wizard'].create({
            'lease_id': lease.id,
            'modification_date': '2026-04-30',
            'new_term_months': 24,
            'new_payment_amount': 800.0,
            'new_ibr': 7.0,
        })
        with self.assertRaises(UserError):
            wizard.action_modify()

    def test_modify_split_lease_excludes_and_preserves_service_component(self):
        lease = self._make_lease(
            commencement_date='2025-01-31',
            term_months=12, cadence='monthly',
            payment_timing='arrears', payment_amount=1_000.0,
            incremental_borrowing_rate=0.0,
            payment_service_pct=20.0,
            component_allocation_note='Maintenance stand-alone price.',
            lease_expense_account_id=self.account_lease_expense.id,
        )
        lease.action_activate()
        wizard = self.env['eh.lease.modify.wizard'].create({
            'lease_id': lease.id,
            'modification_date': '2025-01-31',
            'new_term_months': 12,
            'new_payment_amount': 800.0,
            'new_ibr': 0.0,
        })

        # Revised contractual payment 800 = lease 640 + service 160.
        self.assertAlmostEqual(wizard.new_liability, 7_680.00, places=2)
        wizard.action_modify()
        revised = lease.schedule_line_ids.filtered(
            lambda line: not line.is_posted,
        ).sorted('sequence')
        self.assertEqual(len(revised), 12)
        for line in revised:
            self.assertAlmostEqual(line.payment_amount, 640.00, places=2)
            self.assertAlmostEqual(line.service_amount, 160.00, places=2)
        self.assertAlmostEqual(
            sum(revised.mapped('payment_amount')), 7_680.00, places=2,
        )
        self.assertAlmostEqual(
            sum(revised.mapped('service_amount')), 1_920.00, places=2,
        )

        revised[0].action_post()
        cash = revised[0].move_id.line_ids.filtered(
            lambda line: line.account_id == self.account_cash,
        )
        service = revised[0].move_id.line_ids.filtered(
            lambda line: line.account_id == self.account_lease_expense,
        )
        self.assertAlmostEqual(cash.credit, 800.00, places=2)
        self.assertAlmostEqual(service.debit, 160.00, places=2)
        self.assertEqual(cash.partner_id, lease.lessor_id)

    def test_modify_posts_and_retains_prior_unposted_periods(self):
        lease = self._make_lease(
            commencement_date='2025-01-31',
            term_months=12, cadence='monthly',
            payment_timing='arrears', payment_amount=1_000.0,
            incremental_borrowing_rate=0.0,
        )
        lease.action_activate()
        original = lease.schedule_line_ids.sorted('sequence')
        earned = original[:2]
        replaced = original[2:]
        earned_ids = earned.ids
        replaced_ids = replaced.ids

        wizard = self.env['eh.lease.modify.wizard'].create({
            'lease_id': lease.id,
            'modification_date': '2025-04-15',
            'new_term_months': 12,
            'new_payment_amount': 800.0,
            'new_ibr': 0.0,
        })
        wizard.action_modify()

        retained = self.env['eh.lease.schedule.line'].browse(earned_ids).exists()
        removed = self.env['eh.lease.schedule.line'].browse(replaced_ids).exists()
        self.assertEqual(retained.ids, earned_ids)
        self.assertTrue(all(retained.mapped('is_posted')))
        self.assertTrue(all(retained.mapped('move_id')))
        self.assertFalse(removed)
        self.assertEqual(
            retained.mapped('period_date'),
            [fields.Date.to_date('2025-02-28'), fields.Date.to_date('2025-03-31')],
        )
        event_stub = lease.schedule_line_ids.filtered('is_event_accrual')
        self.assertEqual(len(event_stub), 1)
        self.assertTrue(event_stub.is_posted)
        self.assertEqual(str(event_stub.period_date), '2025-04-15')
        self.assertAlmostEqual(event_stub.rou_amount, 500.0, places=2)
        self.assertAlmostEqual(event_stub.interest, 0.0, places=2)
        revised = lease.schedule_line_ids.filtered(
            lambda line: not line.is_posted,
        ).sorted('sequence')
        self.assertEqual(str(revised[0].period_date), '2025-05-31')

    def test_terminate_records_pl_difference(self):
        lease = self._make_lease(
            commencement_date='2025-01-31',
            term_months=24, cadence='monthly',
            payment_amount=1000.0,
        )
        lease.action_activate()
        self._post_through(lease, '2026-03-31')
        due_line = lease.schedule_line_ids.filtered(
            lambda line: str(line.period_date) == '2026-04-30',
        )
        self.assertEqual(len(due_line), 1)
        due_line_id = due_line.id
        self.assertFalse(due_line.is_posted)
        wizard = self.env['eh.lease.terminate.wizard'].create({
            'lease_id': lease.id,
            'termination_date': '2026-04-30',
            'settlement_amount': 0.0,
            'pl_account_id': self.account_termination_pl.id,
        })
        wizard.action_terminate()
        self.assertEqual(lease.state, 'terminated')
        self.assertTrue(lease.termination_move_id)
        self.assertTrue(
            lease.termination_move_id.eh_sealed,
            "a termination entry backs frozen lease figures and must be sealed",
        )
        with self.assertRaises(UserError):
            lease.termination_move_id.button_draft()
        retained_due = self.env['eh.lease.schedule.line'].browse(
            due_line_id,
        ).exists()
        self.assertTrue(retained_due)
        self.assertTrue(retained_due.is_posted)
        self.assertTrue(retained_due.move_id)
        # No unposted schedule lines remain.
        self.assertFalse(lease.schedule_line_ids.filtered(
            lambda l: not l.is_posted,
        ))

    def test_terminate_with_settlement(self):
        lease = self._make_lease(
            commencement_date='2025-01-31',
            term_months=12, cadence='monthly',
            payment_amount=1000.0,
        )
        lease.action_activate()
        wizard = self.env['eh.lease.terminate.wizard'].create({
            'lease_id': lease.id,
            'termination_date': '2025-04-30',
            'settlement_amount': 500.0,
            'settlement_account_id': self.account_cash.id,
            'pl_account_id': self.account_termination_pl.id,
        })
        wizard.action_terminate()
        self.assertEqual(lease.state, 'terminated')
        # Cash credit leg posted.
        cash_lines = lease.termination_move_id.line_ids.filtered(
            lambda l: l.account_id == self.account_cash,
        )
        self.assertEqual(len(cash_lines), 1)
        self.assertAlmostEqual(cash_lines.credit, 500.0, places=2)

    def test_terminate_accrues_exact_mid_period_stub(self):
        lease = self._make_lease(
            commencement_date='2025-01-31',
            term_months=12, cadence='monthly',
            payment_timing='arrears', payment_amount=1_000.0,
            incremental_borrowing_rate=0.0,
        )
        lease.action_activate()
        wizard = self.env['eh.lease.terminate.wizard'].create({
            'lease_id': lease.id,
            'termination_date': '2025-04-15',
            'pl_account_id': self.account_termination_pl.id,
        })
        wizard.action_terminate()

        stub = lease.schedule_line_ids.filtered('is_event_accrual')
        self.assertEqual(len(stub), 1)
        self.assertEqual(str(stub.period_date), '2025-04-15')
        self.assertAlmostEqual(stub.rou_amount, 500.0, places=2)
        self.assertAlmostEqual(stub.interest, 0.0, places=2)
        self.assertTrue(stub.is_posted)
        rou_expense = stub.move_id.line_ids.filtered(
            lambda line: line.account_id == self.account_rou_dep,
        )
        self.assertAlmostEqual(rou_expense.debit, 500.0, places=2)
        self.assertFalse(lease.schedule_line_ids.filtered(
            lambda line: not line.is_posted,
        ))

    def test_terminate_blocked_when_terminated(self):
        lease = self._make_lease(
            commencement_date='2025-01-31',
            term_months=12, cadence='monthly',
            payment_amount=1000.0,
        )
        lease.action_activate()
        wizard = self.env['eh.lease.terminate.wizard'].create({
            'lease_id': lease.id,
            'termination_date': '2025-04-30',
            'pl_account_id': self.account_termination_pl.id,
        })
        wizard.action_terminate()
        wizard2 = self.env['eh.lease.terminate.wizard'].create({
            'lease_id': lease.id,
            'termination_date': '2025-04-30',
            'pl_account_id': self.account_termination_pl.id,
        })
        with self.assertRaises(UserError):
            wizard2.action_terminate()
