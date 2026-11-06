# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Open-item integrity: a PDC linked to an invoice must reconcile against
that invoice's open receivable/payable line so the invoice is marked paid,
rather than leaving the invoice and the cheque leg as separate open items on
the same partner for the cheque's whole lifecycle (IFRS 9)."""

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhPdcTestCase


@tagged('eh_account_pdc', 'integration', 'post_install', '-at_install')
class TestChequeInvoiceReconcile(EhPdcTestCase):

    def _customer_invoice(self, partner, amount):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'invoice_date': self.today,
            'invoice_line_ids': [(0, 0, {
                'name': 'PDC test sale',
                'quantity': 1,
                'price_unit': amount,
                'account_id': self.account_revenue.id,
            })],
        })
        move.action_post()
        return move

    def test_incoming_pdc_reconciles_and_closes_linked_invoice(self):
        """Presenting an incoming customer cheque linked to an invoice must
        reconcile the present move's receivable leg against the invoice's
        open receivable line, driving the invoice to fully paid."""
        invoice = self._customer_invoice(self.partner_a, 500.0)
        self.assertEqual(invoice.payment_state, 'not_paid')
        self.assertAlmostEqual(invoice.amount_residual, 500.0, places=2)

        cheque = self.env['eh.cheque'].create({
            'direction': 'incoming',
            'partner_id': self.partner_a.id,
            'journal_id': self.bank_journal.id,
            'cheque_number': 'CUST-INV-1',
            'issuer_bank_name': 'ABC Bank',
            'amount': 500.0,
            'currency_id': self.company.currency_id.id,
            'company_id': self.company.id,
            'issue_date': self.today,
            'value_date': self.today,
            'invoice_id': invoice.id,
        })
        cheque.action_register()
        cheque.action_present()

        invoice.invalidate_recordset()
        self.assertAlmostEqual(
            invoice.amount_residual, 0.0, places=2,
            msg="linked invoice must be fully reconciled by the PDC",
        )
        self.assertIn(
            invoice.payment_state, ('paid', 'in_payment'),
            "linked invoice must be marked paid once the cheque is deposited",
        )

    def test_unlinked_pdc_leaves_invoice_untouched(self):
        """Default/unset behaviour: a cheque with no invoice_id must not touch
        any invoice, so existing lifecycle tests stay byte-identical."""
        invoice = self._customer_invoice(self.partner_a, 500.0)
        cheque = self.env['eh.cheque'].create({
            'direction': 'incoming',
            'partner_id': self.partner_a.id,
            'journal_id': self.bank_journal.id,
            'cheque_number': 'CUST-NOINV-1',
            'amount': 500.0,
            'currency_id': self.company.currency_id.id,
            'company_id': self.company.id,
            'issue_date': self.today,
            'value_date': self.today,
        })
        cheque.action_register()
        cheque.action_present()

        invoice.invalidate_recordset()
        self.assertAlmostEqual(invoice.amount_residual, 500.0, places=2)
        self.assertEqual(invoice.payment_state, 'not_paid')

    def test_bounce_reopens_linked_invoice_on_same_accounting_date(self):
        invoice = self._customer_invoice(self.partner_a, 500.0)
        cheque = self.env['eh.cheque'].create({
            'direction': 'incoming',
            'partner_id': self.partner_a.id,
            'journal_id': self.bank_journal.id,
            'cheque_number': 'CUST-INV-BOUNCE',
            'issuer_bank_name': 'ABC Bank',
            'amount': 500.0,
            'currency_id': self.company.currency_id.id,
            'company_id': self.company.id,
            'issue_date': self.today,
            'value_date': self.today,
            'invoice_id': invoice.id,
        })
        cheque.action_register()
        cheque.action_present()
        cheque._mark_bounced(
            self.reason_funds,
            dishonour_date=self.today,
        )

        invoice.invalidate_recordset(['amount_residual', 'payment_state'])
        self.assertAlmostEqual(invoice.amount_residual, 500.0, places=2)
        self.assertEqual(invoice.payment_state, 'not_paid')
        self.assertEqual(cheque.bounce_move_id.date, self.today)

    def test_cross_partner_invoice_link_is_rejected_server_side(self):
        """A shared receivable account cannot make another customer's
        invoice a valid cheque source."""
        invoice = self._customer_invoice(self.partner_b, 500.0)
        with self.assertRaises(UserError):
            self.env['eh.cheque'].create({
                'direction': 'incoming',
                'partner_id': self.partner_a.id,
                'journal_id': self.bank_journal.id,
                'cheque_number': 'CUST-WRONG-PARTNER',
                'amount': 500.0,
                'currency_id': self.company.currency_id.id,
                'company_id': self.company.id,
                'issue_date': self.today,
                'value_date': self.today,
                'invoice_id': invoice.id,
            })

    def test_refund_link_is_rejected_for_incoming_cheque(self):
        """A customer refund has the opposite open-item sign and must not be
        silently ignored or reconciled as a receipt."""
        refund = self.env['account.move'].create({
            'move_type': 'out_refund',
            'partner_id': self.partner_a.id,
            'invoice_date': self.today,
            'invoice_line_ids': [(0, 0, {
                'name': 'PDC test credit note',
                'quantity': 1,
                'price_unit': 500.0,
                'account_id': self.account_revenue.id,
            })],
        })
        refund.action_post()
        with self.assertRaises(UserError):
            self.env['eh.cheque'].create({
                'direction': 'incoming',
                'partner_id': self.partner_a.id,
                'journal_id': self.bank_journal.id,
                'cheque_number': 'CUST-WRONG-DIRECTION',
                'amount': 500.0,
                'currency_id': self.company.currency_id.id,
                'company_id': self.company.id,
                'issue_date': self.today,
                'value_date': self.today,
                'invoice_id': refund.id,
            })
