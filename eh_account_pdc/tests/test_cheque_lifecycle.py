# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Cheque lifecycle tests: register, present, clear, cancel."""

from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhPdcTestCase


@tagged('eh_account_pdc', 'integration', 'post_install', '-at_install')
class TestChequeLifecycle(EhPdcTestCase):

    def _make_outgoing(self, **overrides):
        vals = {
            'direction': 'outgoing',
            'partner_id': self.partner_a.id,
            'journal_id': self.bank_journal.id,
            'book_id': self.book.id,
            'cheque_number': '1',
            'amount': 1000.0,
            'currency_id': self.company.currency_id.id,
            'company_id': self.company.id,
            'issue_date': self.today,
            'value_date': self.today,
        }
        vals.update(overrides)
        if 'value_date' in overrides and 'issue_date' not in overrides:
            vals['issue_date'] = vals['value_date']
        return self.env['eh.cheque'].create(vals)

    def _make_incoming(self, **overrides):
        vals = {
            'direction': 'incoming',
            'partner_id': self.partner_a.id,
            'journal_id': self.bank_journal.id,
            'cheque_number': 'CUST-12345',
            'issuer_bank_name': 'ABC Bank',
            'amount': 500.0,
            'currency_id': self.company.currency_id.id,
            'company_id': self.company.id,
            'issue_date': self.today,
            'value_date': self.today,
        }
        vals.update(overrides)
        if 'value_date' in overrides and 'issue_date' not in overrides:
            vals['issue_date'] = vals['value_date']
        return self.env['eh.cheque'].create(vals)

    # ---- creation ----

    def test_create_assigns_sequence_reference(self):
        cheque = self._make_incoming()
        self.assertNotEqual(cheque.name, '/')
        self.assertTrue(cheque.name.startswith('CHQ/'))

    def test_outgoing_cheque_serial_must_be_in_range(self):
        with self.assertRaises(UserError):
            self._make_outgoing(cheque_number='999')

    def test_uniq_serial_within_book(self):
        self._make_outgoing(cheque_number='1')
        with self.assertRaises(Exception):
            self._make_outgoing(cheque_number='1')

    # ---- transitions ----

    def test_register_advances_book_pointer_when_serial_matches(self):
        cheque = self._make_outgoing(cheque_number='1')
        cheque.action_register()
        self.assertEqual(cheque.state, 'registered')
        self.assertEqual(self.book.next_number, 2)

    def test_register_outgoing_requires_book(self):
        cheque = self._make_outgoing()
        cheque.book_id = False
        with self.assertRaises(UserError):
            cheque.action_register()

    def test_present_only_from_registered(self):
        cheque = self._make_incoming()
        with self.assertRaises(UserError):
            cheque.action_present()
        cheque.action_register()
        cheque.action_present()
        self.assertEqual(cheque.state, 'presented')
        self.assertEqual(cheque.presented_by_id, self.env.user)
        self.assertTrue(cheque.presented_at)

    def test_clear_only_from_presented(self):
        cheque = self._make_incoming()
        cheque.action_register()
        with self.assertRaises(UserError):
            cheque.action_clear()
        cheque.action_present()
        cheque.action_clear()
        self.assertEqual(cheque.state, 'cleared')
        self.assertEqual(cheque.cleared_by_id, self.env.user)

    def test_cancel_blocks_cleared(self):
        cheque = self._make_incoming()
        cheque.action_register()
        cheque.action_present()
        cheque.action_clear()
        with self.assertRaises(UserError):
            cheque.action_cancel()

    def test_cancel_from_draft(self):
        cheque = self._make_incoming()
        cheque.action_cancel()
        self.assertEqual(cheque.state, 'cancelled')
        self.assertEqual(cheque.cancelled_by_id, self.env.user)

    # ---- overdue ----

    def test_is_overdue_flag(self):
        past = self.today - timedelta(days=5)
        cheque = self._make_incoming(value_date=past)
        cheque.action_register()
        cheque.invalidate_recordset()
        self.assertTrue(cheque.is_overdue)

    def test_is_overdue_search(self):
        past = self.today - timedelta(days=5)
        c1 = self._make_incoming(value_date=past)
        c1.action_register()
        c2 = self._make_incoming(
            cheque_number='CUST-99999',
            value_date=self.today + timedelta(days=10),
        )
        c2.action_register()
        overdue_ids = self.env['eh.cheque'].search([
            ('is_overdue', '=', True),
        ]).ids
        self.assertIn(c1.id, overdue_ids)
        self.assertNotIn(c2.id, overdue_ids)

    # ---- cron ----

    def test_cron_auto_present_picks_due_cheques(self):
        past = self.today - timedelta(days=1)
        cheque = self._make_incoming(value_date=past)
        cheque.action_register()
        future_cheque = self._make_incoming(
            cheque_number='FUT-1',
            value_date=self.today + timedelta(days=10),
        )
        future_cheque.action_register()

        self.env['eh.cheque']._cron_auto_present()

        cheque.invalidate_recordset()
        future_cheque.invalidate_recordset()
        self.assertEqual(cheque.state, 'presented')
        self.assertEqual(future_cheque.state, 'registered')

    def test_cron_auto_present_isolates_failures(self):
        # Two due cheques; action_present raises for the first. The
        # shared eh.cron.batch.mixin runs each in its own savepoint, so
        # the first failure must not abort the run: the second still
        # gets presented and the first rolls back cleanly.
        past = self.today - timedelta(days=1)
        c1 = self._make_incoming(cheque_number='ISO-1', value_date=past)
        c1.action_register()
        c2 = self._make_incoming(cheque_number='ISO-2', value_date=past)
        c2.action_register()

        Cheque = type(self.env['eh.cheque'])
        real_present = Cheque.action_present
        failed_id = c1.id

        def flaky_present(self2):
            if failed_id in self2.ids:
                raise ValueError("forced failure")
            return real_present(self2)

        with patch.object(Cheque, 'action_present', flaky_present):
            self.env['eh.cheque']._cron_auto_present()

        c1.invalidate_recordset()
        c2.invalidate_recordset()
        self.assertEqual(
            c1.state, 'registered',
            "failed cheque must roll back and stay registered",
        )
        self.assertEqual(
            c2.state, 'presented',
            "second cheque must present despite the first failing",
        )

    def test_cron_rotates_past_persistent_failure_at_batch_boundary(self):
        past = self.today - timedelta(days=1)
        bad = self._make_incoming(
            cheque_number='ROTATE-BAD', value_date=past,
        )
        good = self._make_incoming(
            cheque_number='ROTATE-GOOD', value_date=past,
        )
        (bad | good).action_register()
        key = 'eh_account_pdc.auto_present_cursor'
        self.env['ir.config_parameter'].sudo().set_param(key, '0')
        Cheque = type(self.env['eh.cheque'])
        real_present = Cheque.action_present

        def fail_bad(cheques):
            if bad in cheques:
                raise UserError('persistent invalid cheque')
            return real_present(cheques)

        with patch.object(Cheque, 'action_present', fail_bad):
            first = self.env['eh.cheque']._cron_auto_present(batch_size=1)
            second = self.env['eh.cheque']._cron_auto_present(batch_size=1)

        bad.invalidate_recordset(['state'])
        good.invalidate_recordset(['state'])
        self.assertEqual([row[0] for row in first], [bad.id])
        self.assertFalse(second)
        self.assertEqual(bad.state, 'registered')
        self.assertEqual(good.state, 'presented')

    def test_value_date_cannot_precede_issue_date(self):
        with self.assertRaises(UserError):
            self._make_incoming(
                cheque_number='DATE-ORDER',
                issue_date=self.today,
                value_date=self.today - timedelta(days=1),
            )

    # ---- multi-currency ----

    def test_fx_cheque_sets_amount_currency_and_converts_balance(self):
        """A foreign-currency cheque must post the company-currency balance
        on debit/credit and the cheque-currency amount (signed) in
        amount_currency. Previously amount_currency was 0 and the balance
        was unconverted, breaking FX reconciliation and revaluation."""
        fx = self.env['res.currency'].create({
            'name': 'TFX', 'symbol': 'F', 'rounding': 0.01, 'active': True,
        })
        fx.rate_ids.unlink()
        self.env['res.currency.rate'].create({
            'currency_id': fx.id,
            'name': self.today,
            'rate': 2.0,  # 2 TFX per 1 unit of company currency
            'company_id': self.company.id,
        })
        cheque = self._make_incoming(
            cheque_number='FX-1', amount=1000.0, currency_id=fx.id,
        )
        cheque.action_register()
        cheque.action_present()
        move = cheque.present_move_id
        self.assertTrue(move)
        self.assertEqual(move.state, 'posted')
        # 1000 TFX at 2 TFX per company unit = 500 company currency.
        for line in move.line_ids:
            self.assertEqual(line.currency_id, fx)
            self.assertAlmostEqual(abs(line.amount_currency), 1000.0, places=2)
            self.assertAlmostEqual(abs(line.balance), 500.0, places=2)
            # amount_currency sign must match the balance sign.
            self.assertEqual(line.amount_currency > 0, line.balance > 0)
        # The move balances in company currency.
        self.assertAlmostEqual(
            sum(move.line_ids.mapped('debit')),
            sum(move.line_ids.mapped('credit')), places=2,
        )
