# -*- encoding: utf-8 -*-
##############################################################################
# ERP Heritage  -  Copyright (C) 2026 (https://www.erpheritage.com.au/)
##############################################################################
"""
Variance commenter tests.
"""

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_ai_agent.tools.variance_commenter import (
    comment, BudgetLineSnapshot,
)


@tagged('post_install', '-at_install')
class VarianceCommenterTest(TransactionCase):

    def _snap(self):
        return [
            BudgetLineSnapshot(label='Salaries', budget=100000, actual=110000),
            BudgetLineSnapshot(label='Rent', budget=24000, actual=24000),
            BudgetLineSnapshot(label='Marketing', budget=15000, actual=22000),
        ]

    def test_overrun_in_headline(self):
        out = comment(self._snap(), period_label='Q3 2026')
        self.assertIn('overran', out.lower())
        self.assertIn('Q3 2026', out)

    def test_underrun_in_headline(self):
        snap = [
            BudgetLineSnapshot(label='Travel', budget=50000, actual=30000),
        ]
        out = comment(snap, period_label='Q3 2026')
        self.assertIn('underran', out.lower())

    def test_match_in_headline(self):
        snap = [
            BudgetLineSnapshot(label='Rent', budget=24000, actual=24000),
        ]
        out = comment(snap)
        self.assertIn('matched', out.lower())

    def test_top_n_largest_listed(self):
        out = comment(self._snap(), top_n=3)
        self.assertIn('Salaries', out)
        self.assertIn('Marketing', out)

    def test_empty_snapshot_returns_placeholder(self):
        out = comment([])
        self.assertIn('No budgeted', out)

    def test_zero_budget_with_actual(self):
        snap = [
            BudgetLineSnapshot(label='Surprise expense',
                              budget=0.0, actual=5000.0),
        ]
        out = comment(snap)
        # Should not crash on the divide-by-zero; should produce
        # *something* meaningful.
        self.assertTrue(out)
        self.assertIn('Surprise expense', out)

    def test_mixed_income_and_expense_headline_uses_net_result(self):
        snap = [
            BudgetLineSnapshot(
                label='Revenue', budget=100.0, actual=90.0, is_income=True,
            ),
            BudgetLineSnapshot(
                label='Expense', budget=60.0, actual=50.0, is_income=False,
            ),
        ]
        out = comment(snap)
        self.assertIn('Net result', out)
        self.assertIn('matched budget', out)
        self.assertNotIn('Period total', out)

    def test_income_and_expense_direction_are_not_inverted(self):
        income = comment([
            BudgetLineSnapshot(
                label='Revenue', budget=100.0, actual=120.0, is_income=True,
            ),
        ])
        expense = comment([
            BudgetLineSnapshot(
                label='Expense', budget=100.0, actual=120.0, is_income=False,
            ),
        ])
        self.assertIn('exceeded budget', income)
        self.assertIn('overran budget', expense)

    def test_llm_provider_failure_safe(self):
        """A failing LLM provider returns the deterministic template."""
        out_manual = comment(self._snap(), period_label='Q3 2026')
        out_with_stub = comment(
            self._snap(), period_label='Q3 2026',
            provider_key='claude',
            provider_config={'api_key': 'x', 'model': 'y'},
        )
        # Stub raises; deterministic template should be returned
        # identically.
        self.assertEqual(out_manual, out_with_stub)
