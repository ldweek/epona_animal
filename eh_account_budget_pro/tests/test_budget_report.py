# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Render regression test for the Budget vs Actual PDF report.

The two shipped report bugs were caught only because a render test was
missing. This exercises the QWeb template end to end (HTML render, so no
wkhtmltopdf dependency) and proves the report produces non-empty output
for a valid budget record with lines.
"""

from unittest.mock import patch

from odoo import fields, release
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)
from odoo.addons.eh_account_budget_pro.models import budget_report as report_model


@tagged('eh_account_budget_pro', 'integration', 'post_install', '-at_install')
class TestBudgetReport(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Budget = cls.env['eh.budget.budget']

    def _budget(self, code='rpt', lines=None):
        return self.Budget.create({
            'code': code,
            'name': 'Report render budget',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [(0, 0, line) for line in (lines or [])],
        })

    def test_report_renders_with_lines(self):
        budget = self._budget(lines=[{
            'account_id': self.account_expense.id,
            'period_from': '2026-01-01',
            'period_to': '2026-12-31',
            'budgeted_amount': 1200.0,
        }])
        report = self.env.ref('eh_account_budget_pro.action_report_budget')
        html, ftype = report._render_qweb_html(
            report.report_name, budget.ids)
        self.assertEqual(ftype, 'html')
        # Non-empty output proves no KeyError / render failure.
        self.assertTrue(html)
        # Stable string: the seeded budget name always appears in the header.
        self.assertIn(b'Report render budget', html)

    def test_report_renders_without_lines(self):
        # The "no lines" branch is a separate template path; render it too.
        budget = self._budget(code='rpt_empty')
        report = self.env.ref('eh_account_budget_pro.action_report_budget')
        html, ftype = report._render_qweb_html(
            report.report_name, budget.ids)
        self.assertEqual(ftype, 'html')
        self.assertTrue(html)
        self.assertIn(b'This budget has no lines.', html)


@tagged('eh_account_budget_pro', 'integration', 'post_install', '-at_install')
class TestBudgetAnalyticsView(EhAccountIntegrationTestCase):
    """Set-based view shape and currency-safe aggregate regressions."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Budget = cls.env['eh.budget.budget']
        cls.Line = cls.env['eh.budget.line']
        cls.Report = cls.env['eh.budget.report']

    def _company_account(self, company, code):
        Account = self.env['account.account']
        vals = {
            'code': code,
            'name': '%s Budget Expense' % company.name,
            'account_type': 'expense',
        }
        if 'company_ids' in Account._fields:
            vals['company_ids'] = [(6, 0, company.ids)]
        else:
            vals['company_id'] = company.id
        return Account.create(vals)

    def _company_budget(self, company, account, code, amount):
        return self.Budget.create({
            'code': code,
            'name': code,
            'company_id': company.id,
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [(0, 0, {
                'account_id': account.id,
                'period_from': fields.Date.from_string('2026-01-01'),
                'period_to': fields.Date.from_string('2026-12-31'),
                'budgeted_amount': amount,
            })],
        })

    @staticmethod
    def _walk_plan(node):
        yield node
        for child in node.get('Plans', []):
            for nested in TestBudgetAnalyticsView._walk_plan(child):
                yield nested

    def test_view_uses_one_set_based_analytic_expansion(self):
        self.env.cr.execute(
            "SELECT pg_get_viewdef(%s::regclass, true)",
            [self.Report._table],
        )
        definition = self.env.cr.fetchone()[0].lower()
        self.assertIn('union all', definition)
        self.assertEqual(
            definition.count('jsonb_each('), 1,
            "analytic_distribution must be expanded once, not per budget line",
        )
        self.assertNotIn('jsonb_each_text', definition)
        self.assertNotIn('jsonb_object_keys', definition)
        self.assertNotIn(
            'double precision', definition,
            "analytic percentages must not overflow a float cast",
        )
        self.assertNotIn(
            'left join lateral (', definition,
            "posted AML aggregation must not be a correlated subquery",
        )

    def test_huge_analytic_percentage_is_ignored_without_view_error(self):
        plan = self.env['account.analytic.plan'].create({
            'name': 'Budget report huge percentage plan',
        })
        analytic = self.env['account.analytic.account'].create({
            'name': 'Budget report huge percentage analytic',
            'plan_id': plan.id,
        })
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': fields.Date.from_string('2026-05-01'),
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_expense.id,
                    'debit': 10.0,
                    'analytic_distribution': {str(analytic.id): 100.0},
                }),
                (0, 0, {
                    'account_id': self.account_cash.id,
                    'credit': 10.0,
                }),
            ],
        })
        move.action_post()
        analytic_line = move.line_ids.filtered(
            lambda line: line.account_id == self.account_expense,
        ).ensure_one()
        self.env.cr.execute(
            "UPDATE account_move_line "
            "SET analytic_distribution = jsonb_build_object(%s, %s::numeric) "
            "WHERE id = %s",
            [str(analytic.id), '1e309', analytic_line.id],
        )
        budget = self.Budget.create({
            'code': 'report_huge_analytic_pct',
            'name': 'Report huge analytic percentage',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'analytic_account_id': analytic.id,
                'period_from': fields.Date.from_string('2026-01-01'),
                'period_to': fields.Date.from_string('2026-12-31'),
                'budgeted_amount': 10.0,
            })],
        })

        report = self.Report.search([('line_id', '=', budget.line_ids.id)])

        self.assertEqual(len(report), 1)
        self.assertEqual(report.actual_amount, 0.0)
        self.assertEqual(report.variance_amount, -10.0)

    def test_view_plan_has_fixed_move_line_scan_branches(self):
        plan = self.env['account.analytic.plan'].create({
            'name': 'Budget report plan shape',
        })
        analytic = self.env['account.analytic.account'].create({
            'name': 'Budget report analytic shape',
            'plan_id': plan.id,
        })
        budget = self.Budget.create({
            'code': 'report_plan_shape',
            'name': 'Report plan shape',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_expense.id,
                    'analytic_account_id': analytic.id if index % 2 else False,
                    'period_from': fields.Date.from_string('2026-01-01'),
                    'period_to': fields.Date.from_string('2026-12-31'),
                    'budgeted_amount': 100.0 + index,
                })
                for index in range(12)
            ],
        })
        self.env.cr.execute(
            "EXPLAIN (FORMAT JSON, COSTS FALSE) "
            "SELECT actual_amount FROM eh_budget_report "
            "WHERE line_id = ANY(%s)",
            [budget.line_ids.ids],
        )
        plan_nodes = list(self._walk_plan(self.env.cr.fetchone()[0][0]['Plan']))
        self.assertFalse(
            [node for node in plan_nodes if node.get('Subplan Name')],
            "budget lines must not drive a correlated AML subplan",
        )
        aml_scans = [
            node for node in plan_nodes
            if node.get('Relation Name') == 'account_move_line'
        ]
        self.assertLessEqual(
            len(aml_scans), 2,
            "AML access must stay fixed at analytic/non-analytic branches",
        )
        line_scans = [
            node for node in plan_nodes
            if node.get('Relation Name') == 'eh_budget_line'
        ]
        self.assertLessEqual(len(line_scans), 2)
        for line_scan in line_scans:
            pushed_condition = ' '.join(
                str(line_scan.get(key, ''))
                for key in ('Index Cond', 'Recheck Cond', 'Filter')
            )
            self.assertIn(
                'ANY', pushed_condition,
                "outer line domain must reach every aggregate branch",
            )

    def test_mixed_company_currencies_fail_closed(self):
        company_same = self.env['res.company'].create({
            'name': 'Budget Same Currency Company',
            'currency_id': self.company.currency_id.id,
        })
        account_same = self._company_account(company_same, 'BCS5000')
        budget_current = self._company_budget(
            self.company, self.account_expense, 'budget_currency_current', 100.0,
        )
        budget_same = self._company_budget(
            company_same, account_same, 'budget_currency_same', 200.0,
        )
        same_currency_context = {
            'allowed_company_ids': [self.company.id, company_same.id],
        }
        report_domain = [
            ('budget_id', 'in', (budget_current | budget_same).ids),
        ]
        groups = self.Report.with_context(**same_currency_context).read_group(
            report_domain, ['budgeted_amount:sum'], [],
        )
        self.assertAlmostEqual(groups[0]['budgeted_amount'], 300.0, places=2)

        foreign_currency = self.env['res.currency'].with_context(
            active_test=False,
        ).search([('id', '!=', self.company.currency_id.id)], limit=1)
        if not foreign_currency:
            self.skipTest("No second currency available for isolation proof.")
        foreign_currency.active = True
        company_foreign = self.env['res.company'].create({
            'name': 'Budget Foreign Currency Company',
            'currency_id': foreign_currency.id,
        })
        account_foreign = self._company_account(company_foreign, 'BCF5000')
        budget_foreign = self._company_budget(
            company_foreign, account_foreign, 'budget_currency_foreign', 900.0,
        )
        mixed_context = {
            'allowed_company_ids': [self.company.id, company_foreign.id],
        }
        mixed_domain = [
            ('budget_id', 'in', (budget_current | budget_foreign).ids),
        ]
        current_only_domain = [('budget_id', '=', budget_current.id)]
        report_group = self.Report.with_context(**mixed_context).read_group(
            current_only_domain, ['budgeted_amount:sum'], [],
        )
        line_group = self.Line.with_context(**mixed_context).read_group(
            current_only_domain, ['budgeted_amount:sum'], [],
        )
        self.assertAlmostEqual(
            report_group[0]['budgeted_amount'], 100.0, places=2,
        )
        self.assertAlmostEqual(
            line_group[0]['budgeted_amount'], 100.0, places=2,
        )
        with self.assertRaises(UserError):
            self.Report.with_context(**mixed_context).read_group(
                mixed_domain, ['budgeted_amount:sum'], [],
            )
        with self.assertRaises(UserError):
            self.Line.with_context(**mixed_context).read_group(
                [('budget_id', 'in', (budget_current | budget_foreign).ids)],
                ['budgeted_amount:sum'], [],
            )

        mixed_report = self.Report.with_context(**mixed_context)
        mixed_lines = self.Line.with_context(**mixed_context)
        mixed_line_domain = [
            ('budget_id', 'in', (budget_current | budget_foreign).ids),
        ]
        with patch.object(
            report_model,
            '_assert_single_currency_aggregate',
            wraps=report_model._assert_single_currency_aggregate,
        ) as guard:
            # Counts never combine money; grouped company totals each retain
            # one currency. Neither operation should spend currency probes.
            self.assertEqual(
                mixed_report.read_group(
                    mixed_domain, ['__count'], [],
                )[0]['__count'],
                2,
            )
            mixed_lines.read_group(mixed_line_domain, ['__count'], [])
            grouped = mixed_report.read_group(
                mixed_domain,
                ['company_id', 'budgeted_amount:sum'],
                ['company_id'],
                lazy=False,
            )
            self.assertEqual(len(grouped), 2)
            guard.assert_not_called()

        with patch.object(
            report_model,
            '_assert_single_currency_aggregate',
            wraps=report_model._assert_single_currency_aggregate,
        ) as guard:
            with self.assertRaises(UserError):
                mixed_report.read_group(
                    mixed_domain, ['budgeted_amount:sum'], [],
                )
            self.assertEqual(
                guard.call_count, 1,
                'public read_group and internal _read_group must guard once',
            )

        if release.version_info[0] >= 19:
            # Currency-safe leaf grouping is allowed, but adding an empty
            # grouping set creates a cross-currency grand total and must fail.
            mixed_report._read_grouping_sets(
                mixed_domain,
                [['company_id']],
                ['budgeted_amount:sum'],
            )
            with self.assertRaises(UserError):
                mixed_report._read_grouping_sets(
                    mixed_domain,
                    [['company_id'], []],
                    ['budgeted_amount:sum'],
                )
