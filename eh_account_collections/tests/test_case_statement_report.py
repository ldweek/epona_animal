# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Accounting and render guards for the collections case-statement PDF.

The template used ``fields.Date.context_today`` / ``fields.Datetime.now``,
but ``fields`` is not in the QWeb render context, so printing a case
statement failed with KeyError: 'fields'. This test renders the report the
way the Print button does and fails if that context regression returns.
"""

from datetime import timedelta

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_collections', 'integration', 'post_install',
        '-at_install')
class TestCaseStatementReport(EhAccountIntegrationTestCase):

    def _post_customer_document(self, move_type, amount, currency=None,
                                partner=None):
        currency = currency or self.company.currency_id
        partner = partner or self.partner_a
        document = self.env['account.move'].create({
            'move_type': move_type,
            'partner_id': partner.id,
            'journal_id': self.journal_sale.id,
            'company_id': self.company.id,
            'currency_id': currency.id,
            'invoice_date': fields.Date.today() - timedelta(days=45),
            'invoice_date_due': fields.Date.today() - timedelta(days=30),
            'invoice_line_ids': [(0, 0, {
                'name': '%s statement item' % move_type,
                'quantity': 1.0,
                'price_unit': amount,
                'account_id': self.account_revenue.id,
            })],
        })
        document.action_post()
        return document

    def test_case_statement_report_renders(self):
        case = self.env['eh.collections.case'].create({
            'partner_id': self.partner_a.id})
        report = self.env.ref(
            'eh_account_collections.action_report_case_statement')
        html, ftype = report._render_qweb_html(report.report_name, case.ids)
        self.assertEqual(ftype, 'html')
        self.assertTrue(html)
        self.assertIn(b'Statement of Account', html)

    def test_statement_nets_credits_and_subtotals_each_currency(self):
        eur = self.env.ref('base.EUR')
        if not eur.active:
            eur.sudo().active = True
        self.env['res.currency.rate'].create({
            'currency_id': eur.id,
            'company_id': self.company.id,
            'name': fields.Date.today() - timedelta(days=60),
            'rate': 2.0,
        })
        invoice = self._post_customer_document('out_invoice', 100.0)
        credit = self._post_customer_document('out_refund', 25.0)
        child = self.env['res.partner'].create({
            'name': 'Statement billing contact',
            'parent_id': self.partner_a.id,
            'type': 'invoice',
        })
        foreign_invoice = self._post_customer_document(
            'out_invoice', 40.0, currency=eur, partner=child,
        )
        case = self.env['eh.collections.case'].create({
            'partner_id': self.partner_a.id,
        })

        statement = case._eh_statement_data_for_report()
        by_move = {
            item['move'].id: item for item in statement['items']
        }
        self.assertEqual(
            set(by_move),
            {invoice.id, credit.id, foreign_invoice.id},
            "the commercial-partner statement must include child contacts",
        )
        self.assertAlmostEqual(by_move[invoice.id]['residual'], 100.0)
        self.assertAlmostEqual(by_move[credit.id]['residual'], -25.0)
        self.assertAlmostEqual(by_move[foreign_invoice.id]['residual'], 40.0)
        subtotals = {
            total['currency'].id: total['amount']
            for total in statement['currency_totals']
        }
        self.assertAlmostEqual(
            subtotals[self.company.currency_id.id],
            75.0,
            msg="customer credits must reduce, not increase, the subtotal",
        )
        self.assertAlmostEqual(subtotals[eur.id], 40.0)
        self.assertAlmostEqual(
            statement['company_total'],
            sum(item['company_residual'] for item in statement['items']),
        )
        self.assertTrue(statement['show_company_total'])
        action = case.action_view_open_invoices()
        visible_moves = self.env['account.move.line'].search(
            action['domain'],
        ).move_id
        self.assertIn(
            foreign_invoice,
            visible_moves,
            "the Open Invoices drilldown must match commercial-partner totals",
        )

        report = self.env.ref(
            'eh_account_collections.action_report_case_statement')
        html, ftype = report._render_qweb_html(report.report_name, case.ids)
        self.assertEqual(ftype, 'html')
        self.assertIn(b'Open subtotal', html)
        self.assertIn(b'USD', html)
        self.assertIn(b'EUR', html)
        self.assertIn(b'Ledger total', html)
