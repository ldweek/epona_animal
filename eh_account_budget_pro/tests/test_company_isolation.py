# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Multi-company record-rule regression for eh.budget.commitment.

eh.budget.commitment carries a stored company_id but had no ir.rule, so a
plain user scoped to one company could search_read every other company's
encumbrance amounts (and, via write, distort another company's budget
availability). The added global rule
``[('company_id', 'in', company_ids)]`` isolates the model per company.

The test env is superuser (env.su True), which bypasses record rules, so
the negative path runs as a NON-superuser via with_user whose company_ids
exclude company B.
"""

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_budget_pro', 'post_install', '-at_install')
class TestCommitmentCompanyIsolation(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Budget = cls.env['eh.budget.budget']
        cls.Commitment = cls.env['eh.budget.commitment']

        cls.company_a = cls.company
        cls.company_b = cls.env['res.company'].create({
            'name': 'Isolation Company B',
            'currency_id': cls.company_a.currency_id.id,
        })

        # An expense account belonging to company B for its budget line.
        # account.account is multi-company (company_ids, Many2many) from
        # Odoo 18 and single company_id before, so resolve the field.
        Account = cls.env['account.account']
        multi = 'company_ids' in Account._fields
        acct_vals = {
            'code': 'ISOB5000',
            'name': 'Company B Expense',
            'account_type': 'expense',
        }
        acct_vals['company_ids' if multi else 'company_id'] = (
            [(6, 0, cls.company_b.ids)] if multi else cls.company_b.id
        )
        cls.account_expense_b = Account.create(acct_vals)

        # A confirmed budget with a reserved commitment, all in company B.
        budget_b = cls.Budget.create({
            'code': 'iso_budget_b',
            'name': 'Isolation Budget B',
            'company_id': cls.company_b.id,
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'line_ids': [(0, 0, {
                'account_id': cls.account_expense_b.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 10000.0,
            })],
        })
        budget_b.action_confirm()
        cls.commitment_b = cls.Commitment.create({
            'budget_line_id': budget_b.line_ids[0].id,
            'amount': 4000.0,
        })
        cls.commitment_b.action_reserve()

        # A plain accounting user scoped to company A only. Use singular
        # company_id on create (the 16/17 backport transform mangles a
        # two-element company_ids list) and grant the module's user group so
        # access rights are not the reason a read is refused.
        cls.user_a = cls.env['res.users'].create({
            'name': 'Budget Company A User',
            'login': 'eh_budget_iso_user_a',
            'email': 'eh_budget_iso_user_a@example.com',
            'company_id': cls.company_a.id,
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })

    def test_company_b_commitment_not_readable_from_company_a(self):
        """A company-A user must not see company B's commitment rows."""
        if not self.user_a:
            self.skipTest("No plain user available in this environment.")
        visible = self.Commitment.with_user(self.user_a).search([])
        self.assertNotIn(
            self.commitment_b, visible,
            "company B commitment leaked to a company A user")

    def test_company_b_commitment_not_writable_from_company_a(self):
        """The rule also blocks write to another company's commitment."""
        if not self.user_a:
            self.skipTest("No plain user available in this environment.")
        with self.assertRaises(AccessError):
            self.commitment_b.with_user(self.user_a).write({'amount': 1.0})

    def test_own_company_commitment_still_visible(self):
        """The rule must not over-block: same-company rows stay visible."""
        if not self.user_a:
            self.skipTest("No plain user available in this environment.")
        budget_a = self.Budget.create({
            'code': 'iso_budget_a',
            'name': 'Isolation Budget A',
            'company_id': self.company_a.id,
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 10000.0,
            })],
        })
        budget_a.action_confirm()
        commitment_a = self.Commitment.create({
            'budget_line_id': budget_a.line_ids[0].id,
            'amount': 2000.0,
        })
        commitment_a.action_reserve()
        visible = self.Commitment.with_user(self.user_a).search([])
        self.assertIn(
            commitment_a, visible,
            "same-company commitment must remain visible")
