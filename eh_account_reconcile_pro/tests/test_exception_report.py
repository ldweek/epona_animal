# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Render guard for the reconciliation-exception PDF.

The report's ``_get_report_values`` lived on the wizard model, which Odoo
never calls: it resolves report data through a ``report.<report_name>``
AbstractModel. Without that model the template's ``data`` key was missing and
the render failed with KeyError: 'data'. This test renders the report the way
the wizard's Print button does and fails if that wiring regresses.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_reconcile_pro.tests.common import (
    EhReconcileIntegrationTestCase,
)


@tagged('eh_account_reconcile_pro', 'integration', 'post_install',
        '-at_install')
class TestExceptionReport(EhReconcileIntegrationTestCase):

    def test_exception_report_renders(self):
        wizard = self.env['eh.reconciliation.exception.wizard'].create({
            'date_from': '2026-01-01', 'date_to': '2026-12-31'})
        report = self.env.ref(
            'eh_account_reconcile_pro.action_report_reconciliation_exception')
        html, ftype = report._render_qweb_html(report.report_name, wizard.ids)
        self.assertEqual(ftype, 'html')
        self.assertTrue(html)
        self.assertIn(b'Reconciliation Exception Report', html)

    def test_exception_report_rejects_cross_company_or_wrong_type_journal(self):
        other_company = self.env['res.company'].create({
            'name': 'Exception Report Other Company',
        })
        other_bank = self.env['account.journal'].create({
            'name': 'Other Company Bank',
            'code': 'OCBK',
            'type': 'bank',
            'company_id': other_company.id,
        })
        Wizard = self.env['eh.reconciliation.exception.wizard']
        base_vals = {
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'company_id': self.company.id,
        }
        with self.assertRaisesRegex(UserError, 'selected company'):
            Wizard.create({
                **base_vals,
                'journal_ids': [(6, 0, other_bank.ids)],
            })
        with self.assertRaisesRegex(UserError, 'bank or cash'):
            Wizard.create({
                **base_vals,
                'journal_ids': [(6, 0, self.journal_misc.ids)],
            })
