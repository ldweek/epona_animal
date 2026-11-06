# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""PDC transit, bank-reconciliation, and clear-date FX regressions."""

from datetime import date

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

try:  # Odoo 18+ re-exports freeze_time
    from odoo.tests import freeze_time
except ImportError:  # pragma: no cover - Odoo 16/17 shim
    from freezegun import freeze_time

from .common import EhPdcTestCase


@tagged('eh_account_pdc', 'integration', 'post_install', '-at_install')
class TestPdcSettlementAccounting(EhPdcTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fx_gain = cls._ensure_account(
            cls.env, '7996', 'PDC FX Gain', 'income',
        )
        cls.fx_loss = cls._ensure_account(
            cls.env, '5996', 'PDC FX Loss', 'expense',
        )
        cls.company.sudo().write({
            'currency_exchange_journal_id': cls.journal_misc.id,
            'income_currency_exchange_account_id': cls.fx_gain.id,
            'expense_currency_exchange_account_id': cls.fx_loss.id,
        })
        cls.eur = cls.env.ref('base.EUR')
        if not cls.eur.active:
            cls.eur.sudo().active = True

    def _incoming(self, number, amount=1000.0, currency=None,
                  value_date=None):
        cheque = self.env['eh.cheque'].create({
            'direction': 'incoming',
            'partner_id': self.partner_a.id,
            'journal_id': self.bank_journal.id,
            'cheque_number': number,
            'issuer_bank_name': 'Settlement Test Bank',
            'amount': amount,
            'currency_id': (currency or self.company.currency_id).id,
            'company_id': self.company.id,
            'issue_date': value_date or self.today,
            'value_date': value_date or self.today,
        })
        cheque.action_register()
        return cheque

    def _outgoing(self, amount=1000.0):
        cheque = self.env['eh.cheque'].create({
            'direction': 'outgoing',
            'partner_id': self.partner_a.id,
            'journal_id': self.bank_journal.id,
            'book_id': self.book.id,
            'cheque_number': str(self.book.next_number),
            'amount': amount,
            'currency_id': self.company.currency_id.id,
            'company_id': self.company.id,
            'issue_date': self.today,
            'value_date': self.today,
        })
        cheque.action_register()
        return cheque

    def _pin_eur_rates(self, rates):
        self.env['res.currency.rate'].sudo().search([
            ('currency_id', '=', self.eur.id),
            ('company_id', 'in', [self.company.id, False]),
        ]).unlink()
        for day, rate in rates:
            self.env['res.currency.rate'].create({
                'currency_id': self.eur.id,
                'name': day,
                'rate': rate,
                'company_id': self.company.id,
            })

    def test_present_uses_dedicated_transit_not_statement_suspense(self):
        cheque = self._incoming('PDC-TRANSIT-1')
        cheque.action_present()

        accounts = cheque.present_move_id.line_ids.account_id
        self.assertIn(self.incoming_transit, accounts)
        self.assertNotIn(self.statement_suspense, accounts)
        self.assertNotIn(self.bank_journal.default_account_id, accounts)

    def test_incoming_clear_uses_outstanding_receipts_and_zeros_transit(self):
        cheque = self._incoming('PDC-CLEAR-IN')
        cheque.action_present()
        cheque.action_clear()

        clear = cheque.clear_move_id
        accounts = clear.line_ids.account_id
        self.assertIn(self.outstanding_receipts, accounts)
        self.assertIn(self.incoming_transit, accounts)
        self.assertNotIn(self.statement_suspense, accounts)
        self.assertNotIn(self.bank_journal.default_account_id, accounts)
        transit_lines = (
            cheque.present_move_id.line_ids | clear.line_ids
        ).filtered(lambda line: line.account_id == self.incoming_transit)
        self.assertEqual(len(transit_lines), 2)
        self.assertTrue(all(transit_lines.mapped('reconciled')))
        self.assertAlmostEqual(
            sum(transit_lines.mapped('amount_residual')), 0.0, places=2,
        )

    def test_outgoing_clear_uses_outstanding_payments_not_bank_gl(self):
        cheque = self._outgoing()
        cheque.action_present()
        cheque.action_clear()

        accounts = cheque.clear_move_id.line_ids.account_id
        self.assertIn(self.outstanding_payments, accounts)
        self.assertIn(self.outgoing_transit, accounts)
        self.assertNotIn(self.statement_suspense, accounts)
        self.assertNotIn(self.bank_journal.default_account_id, accounts)

    def test_fx_clear_posts_exchange_difference_and_zeros_transit(self):
        """EUR 1,000 falls from USD 1,250 to USD 1,000 before clear.

        Presentation at 0.8 EUR/USD: Dr transit USD 1,250.
        Clearance at 1.0 EUR/USD: Cr transit USD 1,000.
        Full reconciliation: Cr transit USD 250 / Dr FX loss USD 250.
        """
        present_day = date(2026, 3, 1)
        clear_day = date(2026, 3, 10)
        self._pin_eur_rates(((present_day, 0.8), (clear_day, 1.0)))
        with freeze_time('2026-03-01'):
            cheque = self._incoming(
                'PDC-FX-CLEAR', currency=self.eur,
                value_date=present_day,
            )
            cheque.action_present()
        with freeze_time('2026-03-10'):
            cheque.action_clear()

        present_line = cheque.present_move_id.line_ids.filtered(
            lambda line: line.account_id == self.incoming_transit)
        clear_line = cheque.clear_move_id.line_ids.filtered(
            lambda line: line.account_id == self.incoming_transit)
        self.assertAlmostEqual(present_line.debit, 1250.0, places=2)
        self.assertAlmostEqual(clear_line.credit, 1000.0, places=2)
        self.assertTrue(present_line.reconciled)
        self.assertTrue(clear_line.reconciled)

        transit_lines = present_line | clear_line
        if 'exchange_move_ids' in transit_lines._fields:
            exchange_moves = transit_lines.exchange_move_ids
        else:
            # Odoo 16-18 expose exchange-difference provenance on the
            # partial reconcile rather than directly on journal items.
            partials = (
                transit_lines.matched_debit_ids
                | transit_lines.matched_credit_ids
            )
            exchange_moves = partials.mapped('exchange_move_id')
        self.assertEqual(len(exchange_moves), 1)
        exchange_move = exchange_moves
        self.assertEqual(exchange_move.state, 'posted')
        self.assertEqual(exchange_move.date, clear_day)
        loss_line = exchange_move.line_ids.filtered(
            lambda line: line.account_id == self.fx_loss)
        transit_fx_line = exchange_move.line_ids.filtered(
            lambda line: line.account_id == self.incoming_transit)
        self.assertAlmostEqual(loss_line.debit, 250.0, places=2)
        self.assertAlmostEqual(transit_fx_line.credit, 250.0, places=2)
        self.assertAlmostEqual(
            present_line.balance + clear_line.balance
            + transit_fx_line.balance,
            0.0,
            places=2,
        )

    def test_bounce_charge_uses_outstanding_payments_not_bank_gl(self):
        charges = self._ensure_account(
            self.env, '5711', 'PDC Dishonour Charges', 'expense',
        )
        self.bank_journal.eh_pdc_bounce_charge_account_id = charges
        cheque = self._incoming('PDC-BOUNCE-CHARGE', amount=500.0)
        cheque.action_present()
        cheque._mark_bounced(
            self.reason_funds,
            charges=25.0,
            dishonour_date=self.today,
        )

        charge_move = cheque.bounce_charge_move_id
        credit_line = charge_move.line_ids.filtered('credit')
        self.assertEqual(credit_line.account_id, self.outstanding_payments)
        self.assertNotEqual(
            credit_line.account_id, self.bank_journal.default_account_id,
        )

    def test_missing_dedicated_transit_blocks_presentation_atomically(self):
        self.bank_journal.eh_pdc_incoming_transit_account_id = False
        cheque = self._incoming('PDC-NO-TRANSIT')

        with self.assertRaises(UserError):
            cheque.action_present()
        cheque.invalidate_recordset()
        self.assertEqual(cheque.state, 'registered')
        self.assertFalse(cheque.present_move_id)

    def test_statement_suspense_cannot_be_configured_as_transit(self):
        if not self.statement_suspense.reconcile:
            self.statement_suspense.sudo().reconcile = True
        with self.assertRaises(ValidationError):
            self.bank_journal.eh_pdc_incoming_transit_account_id = (
                self.statement_suspense
            )

    def test_later_payment_method_alias_is_rechecked_at_presentation(self):
        """Method-line edits do not trigger journal constraints; action must."""
        method_line = self.bank_journal.eh_pdc_inbound_payment_method_line_id
        method_line.sudo().payment_account_id = self.incoming_transit
        cheque = self._incoming('PDC-LATE-ALIAS')

        with self.assertRaises(UserError):
            cheque.action_present()
        cheque.invalidate_recordset()
        self.assertEqual(cheque.state, 'registered')
        self.assertFalse(cheque.present_move_id)
