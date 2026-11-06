# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Integration tests for the live budget variance commentary.

These prove the deterministic commentary helper is wired live onto
eh.budget.budget: the field renders a non-empty narrative, includes the
period label, maps the income flag from the account type, and refreshes
when the lines change.
"""

import ast
from datetime import date
from pathlib import Path

from odoo import fields
from odoo.modules.module import get_module_path
from odoo.tests import tagged
from odoo.tools.misc import format_amount

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)
from odoo.addons.eh_account_ai_budget.models import budget as ai_budget_model
from odoo.addons.eh_account_budget_pro.models import budget_report


@tagged('eh_account_ai_budget', 'integration', 'post_install', '-at_install')
class TestVarianceCommentary(EhAccountIntegrationTestCase):

    def _budget(self, lines=True, name="FY Test Budget"):
        today = fields.Date.context_today(self.env['res.users'])
        date_from = date(today.year, 1, 1)
        date_to = date(today.year, 12, 31)
        vals = {
            'name': name,
            'code': 'FYTEST',
            'company_id': self.company.id,
            'date_from': date_from,
            'date_to': date_to,
        }
        if lines:
            vals['line_ids'] = [
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'period_from': date_from, 'period_to': date_to,
                    'budgeted_amount': 10000.0,
                }),
                (0, 0, {
                    'account_id': self.account_expense.id,
                    'period_from': date_from, 'period_to': date_to,
                    'budgeted_amount': 5000.0,
                }),
            ]
        return self.env['eh.budget.budget'].create(vals)

    def test_commentary_non_empty_with_period_label(self):
        budget = self._budget()
        text = budget.eh_ai_variance_commentary
        self.assertTrue(text)
        self.assertIn('budget', text.lower())
        self.assertIn('FY Test Budget', text)

    def test_income_flag_mapped_from_account_type(self):
        budget = self._budget()
        snapshot = budget._eh_build_budget_snapshot()
        by_income = {s.is_income for s in snapshot}
        self.assertIn(True, by_income, "revenue line must map is_income=True")
        self.assertIn(False, by_income, "expense line must map is_income=False")
        revenue_snap = next(
            s for s in snapshot
            if s.label == self.account_revenue.display_name
        )
        self.assertTrue(revenue_snap.is_income)
        self.assertEqual(
            set(ai_budget_model._INCOME_TYPES),
            set(budget_report._INCOME_LIKE_ACCOUNT_TYPES),
        )

    def test_mixed_budget_headline_reports_net_result(self):
        budget = self._budget()
        text = budget.eh_ai_variance_commentary
        self.assertIn('Net result', text)
        # 10,000 income less 5,000 expense, not an invalid 15,000 blend.
        self.assertIn(
            format_amount(self.env, 5000.0, budget.currency_id), text,
        )
        self.assertNotIn(
            format_amount(self.env, 15000.0, budget.currency_id), text,
        )

    def test_posted_revenue_actual_keeps_credit_sign_and_currency_format(self):
        budget = self._budget()
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': budget.date_from,
            'line_ids': [
                (0, 0, {
                    'name': 'Revenue actual',
                    'account_id': self.account_cash.id,
                    'debit': 8000.0,
                }),
                (0, 0, {
                    'name': 'Revenue actual',
                    'account_id': self.account_revenue.id,
                    'credit': 8000.0,
                }),
            ],
        })
        move.action_post()
        budget.line_ids.invalidate_recordset(['actual_amount'])

        snapshot = budget._eh_build_budget_snapshot()
        revenue = next(
            line for line in snapshot
            if line.label == self.account_revenue.display_name
        )
        self.assertEqual(revenue.actual, 8000.0)
        budget.invalidate_recordset(['eh_ai_variance_commentary'])
        self.assertIn(
            format_amount(self.env, 8000.0, budget.currency_id),
            budget.eh_ai_variance_commentary,
        )

    def test_empty_budget_returns_placeholder(self):
        budget = self._budget(lines=False)
        self.assertTrue(budget.eh_ai_variance_commentary)

    def test_commentary_recomputes_on_line_change(self):
        budget = self._budget()
        budget.line_ids.filtered(
            lambda line: line.account_id == self.account_revenue
        ).budgeted_amount = 99999.0
        budget.invalidate_recordset(['eh_ai_variance_commentary'])
        # New net-result budget = 99,999 income - 5,000 expense = 94,999.
        self.assertIn(
            format_amount(self.env, 94999.0, budget.currency_id),
            budget.eh_ai_variance_commentary,
        )

    def test_listing_and_translation_template_match_live_contract(self):
        root = Path(get_module_path('eh_account_ai_budget'))
        manifest = ast.literal_eval((root / '__manifest__.py').read_text())
        listing = (root / 'static/description/index.html').read_text()
        template = (
            root / 'i18n/eh_account_ai_budget.pot'
        ).read_text()

        self.assertIn('v%s' % manifest['version'], listing)
        self.assertIn('No duplicate provider charge', listing)
        self.assertIn(
            'Form and list reads remain deterministic and network-free',
            listing,
        )
        self.assertNotIn('failures are caught silently', listing)
        self.assertNotIn('parent company', listing)
        self.assertIn('AI narrative generated at', template)
        self.assertIn('AI narrative source fingerprint', template)
        self.assertIn('Variance commentary', template)
