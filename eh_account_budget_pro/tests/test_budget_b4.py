# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
B4 budget depth tests: budget_type, time-prorated pacing, revision
supersede, and the account x analytic x period split wizard.
"""

from datetime import date

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_budget_pro', 'integration', 'post_install', '-at_install')
class TestBudgetB4(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Budget = cls.env['eh.budget.budget']
        plan = cls.env['account.analytic.plan'].create({'name': 'B4 Plan'})
        cls.an1 = cls.env['account.analytic.account'].create({
            'name': 'B4 A1', 'plan_id': plan.id})
        cls.an2 = cls.env['account.analytic.account'].create({
            'name': 'B4 A2', 'plan_id': plan.id})

    def _budget(self, code='b4', lines=None):
        return self.Budget.create({
            'code': code, 'name': 'B4 budget',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [(0, 0, line) for line in (lines or [])],
        })

    # ---- budget_type ----

    def test_budget_type_defaults_both(self):
        self.assertEqual(self._budget().budget_type, 'both')

    # ---- pacing ----

    def test_elapsed_fraction_bounds(self):
        budget = self._budget(lines=[{
            'account_id': self.account_expense.id,
            'period_from': '2026-01-01', 'period_to': '2026-12-31',
            'budgeted_amount': 1200.0,
        }])
        line = budget.line_ids
        self.assertEqual(
            line._elapsed_fraction(date(2025, 12, 31)), 0.0)
        self.assertEqual(
            line._elapsed_fraction(date(2027, 1, 1)), 1.0)
        mid = line._elapsed_fraction(date(2026, 7, 2))
        self.assertTrue(0.45 < mid < 0.55, "midpoint ~50%%, got %s" % mid)

    def test_theoretical_amount_tracks_fraction(self):
        budget = self._budget(lines=[{
            'account_id': self.account_expense.id,
            'period_from': '2026-01-01', 'period_to': '2026-12-31',
            'budgeted_amount': 1200.0,
        }])
        line = budget.line_ids
        today = fields.Date.context_today(self.env['res.users'])
        expected = 1200.0 * line._elapsed_fraction(today)
        self.assertAlmostEqual(line.theoretical_amount, expected, places=2)

    # ---- revision supersede ----

    def test_revision_supersedes_prior(self):
        budget = self._budget(code='rev_src', lines=[{
            'account_id': self.account_expense.id,
            'period_from': '2026-01-01', 'period_to': '2026-12-31',
            'budgeted_amount': 500.0,
        }])
        budget.action_confirm()
        self.assertEqual(budget.state, 'confirmed')
        budget.action_create_version()
        self.assertEqual(budget.state, 'confirmed')
        self.assertTrue(budget.revised_budget_id)
        new = budget.revised_budget_id
        self.assertEqual(new.parent_id, budget)
        self.assertEqual(new.state, 'draft')
        new.action_confirm()
        self.assertEqual(budget.state, 'revised')
        self.assertEqual(new.state, 'confirmed')

    # ---- split wizard ----

    def test_slice_periods_counts(self):
        Wiz = self.env['eh.budget.split.wizard']
        d0 = date(2026, 1, 1)
        d1 = date(2026, 12, 31)
        self.assertEqual(len(Wiz._slice_periods(d0, d1, 'month')), 12)
        self.assertEqual(len(Wiz._slice_periods(d0, d1, 'quarter')), 4)
        self.assertEqual(len(Wiz._slice_periods(d0, d1, 'year')), 1)

    def test_split_wizard_cartesian(self):
        budget = self._budget(code='split_grid')
        wizard = self.env['eh.budget.split.wizard'].create({
            'budget_id': budget.id,
            'granularity': 'quarter',
            'account_ids': [(6, 0, [self.account_revenue.id,
                                    self.account_expense.id])],
            'analytic_account_ids': [(6, 0, [self.an1.id, self.an2.id])],
            'amount_per_line': 100.0,
        })
        wizard.action_generate()
        # 2 accounts x 2 analytic x 4 quarters = 16 lines.
        self.assertEqual(len(budget.line_ids), 16)
        self.assertTrue(all(
            l.budgeted_amount == 100.0 for l in budget.line_ids))
        self.assertEqual(
            len(budget.line_ids.filtered(
                lambda l: l.analytic_account_id == self.an1)), 8)

    def test_split_wizard_no_analytic(self):
        budget = self._budget(code='split_plain')
        self.env['eh.budget.split.wizard'].create({
            'budget_id': budget.id,
            'granularity': 'month',
            'account_ids': [(6, 0, [self.account_expense.id])],
            'amount_per_line': 50.0,
        }).action_generate()
        # 1 account x no analytic x 12 months = 12 lines.
        self.assertEqual(len(budget.line_ids), 12)
