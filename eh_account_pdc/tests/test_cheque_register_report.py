# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Render regression for the cheque register PDF.

The register report shipped without a render test (unlike the physical
cheque print, covered by test_cheque_print). This proves the register
template renders for a real cheque recordset so a missing field or QWeb
break surfaces in CI rather than at print time.
"""

from odoo.tests import tagged

from odoo.addons.eh_account_pdc.tests.common import EhPdcTestCase


@tagged('eh_account_pdc', 'integration', 'post_install', '-at_install')
class TestChequeRegisterReport(EhPdcTestCase):

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
        return self.env['eh.cheque'].create(vals)

    def test_register_report_renders_without_error(self):
        cheque = self._make_outgoing()
        report = self.env.ref('eh_account_pdc.action_report_cheque_register')
        html, ftype = self.env['ir.actions.report']._render_qweb_html(
            report.report_name, cheque.ids)
        self.assertEqual(ftype, 'html')
        self.assertTrue(html)
        self.assertIn(b'Cheque Register', html)

    def test_register_uses_registration_name_snapshot(self):
        partner = self.env['res.partner'].create({
            'name': 'Immutable Register Counterparty Alpha',
        })
        cheque = self._make_outgoing(partner_id=partner.id)
        cheque.action_register()
        partner.name = 'Mutated Register Counterparty Omega'

        report = self.env.ref('eh_account_pdc.action_report_cheque_register')
        html, _dummy = self.env['ir.actions.report']._render_qweb_html(
            report.report_name, cheque.ids,
        )
        self.assertIn(b'Immutable Register Counterparty Alpha', html)
        self.assertNotIn(b'Mutated Register Counterparty Omega', html)
