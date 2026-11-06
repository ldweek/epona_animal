# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Multi-company record-rule regression for eh.budget.report.

eh.budget.report is an _auto=False SQL VIEW backing the Budget vs Actual
graph/pivot. Record rules on the base tables (eh.budget.budget /
eh.budget.line) never propagate to a separate SQL-view model, so before the
fix a plain user scoped to one company could search_read every other
company's budgeted and posted-actual GL figures grouped by account. The
added global rule ``[('company_id', 'in', company_ids)]`` (the view selects
b.company_id) isolates the report per company.

The test env is superuser (env.su True), which bypasses record rules, so the
negative path runs as a NON-superuser via with_user whose company_ids
exclude company B.
"""

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_budget_pro', 'post_install', '-at_install')
class TestReportCompanyIsolation(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Budget = cls.env['eh.budget.budget']
        cls.Report = cls.env['eh.budget.report']

        cls.company_a = cls.company
        cls.company_b = cls.env['res.company'].create({
            'name': 'Report Isolation Company B',
            'currency_id': cls.company_a.currency_id.id,
        })

        # An expense account belonging to company B for its budget line.
        # account.account became multi-company (company_ids, Many2many) in
        # Odoo 18; before that it carries a single company_id.
        Account = cls.env['account.account']
        multi = 'company_ids' in Account._fields
        acct_vals = {
            'code': 'RPTISOB5000',
            'name': 'Company B Expense (report)',
            'account_type': 'expense',
        }
        acct_vals['company_ids' if multi else 'company_id'] = (
            [(6, 0, cls.company_b.ids)] if multi else cls.company_b.id
        )
        cls.account_expense_b = Account.create(acct_vals)

        # A budget with one line in company B: this produces exactly one
        # eh.budget.report row carrying company_id = company B.
        cls.budget_b = cls.Budget.create({
            'code': 'rpt_iso_budget_b',
            'name': 'Report Isolation Budget B',
            'company_id': cls.company_b.id,
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'line_ids': [(0, 0, {
                'account_id': cls.account_expense_b.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 50000.0,
            })],
        })

        # A plain accounting user scoped to company A only. Singular
        # company_id on create (the 16/17 backport transform mangles a
        # two-element company_ids list) and grant the module's user group so
        # access rights are not the reason a read is refused.
        cls.user_a = cls.env['res.users'].create({
            'name': 'Budget Report Company A User',
            'login': 'eh_budget_rpt_iso_user_a',
            'email': 'eh_budget_rpt_iso_user_a@example.com',
            'company_id': cls.company_a.id,
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })

    def test_company_b_report_row_not_readable_from_company_a(self):
        """A company-A user must not see company B's report rows."""
        if not self.user_a:
            self.skipTest("No plain user available in this environment.")
        rows = self.Report.with_user(self.user_a).search(
            [('budget_id', '=', self.budget_b.id)])
        self.assertFalse(
            rows,
            "company B budget-report rows leaked to a company A user")
        # A blanket search must not surface any foreign-company row either.
        visible = self.Report.with_user(self.user_a).search([])
        self.assertFalse(
            visible.filtered(lambda r: r.company_id == self.company_b),
            "company B rows reachable via an unfiltered report search")

    def test_own_company_report_row_still_visible(self):
        """The rule must not over-block: same-company rows stay visible."""
        if not self.user_a:
            self.skipTest("No plain user available in this environment.")
        budget_a = self.Budget.create({
            'code': 'rpt_iso_budget_a',
            'name': 'Report Isolation Budget A',
            'company_id': self.company_a.id,
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 30000.0,
            })],
        })
        rows = self.Report.with_user(self.user_a).search(
            [('budget_id', '=', budget_a.id)])
        self.assertTrue(
            rows,
            "same-company budget-report row must remain visible")
