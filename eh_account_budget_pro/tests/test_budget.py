# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Budget Pro tests.

Covers model constraints (code format, unique, date range), lifecycle
(draft to confirmed to closed and back), batch actual computation
correctness, variance math, and the version copy action.
"""

from datetime import timedelta
from unittest.mock import patch

import odoo
from lxml import etree
from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

try:  # Odoo 18+ re-exports freeze_time; 16/17 pull it from freezegun
    from odoo.tests import freeze_time
except ImportError:  # pragma: no cover - version shim
    from freezegun import freeze_time

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase
from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery


@tagged('eh_account_budget_pro', 'integration', 'post_install', '-at_install')
class TestBudgetModel(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Budget = cls.env['eh.budget.budget']
        cls.Line = cls.env['eh.budget.line']

    def _make_budget(self, code='annual_2026', name='Test Budget', lines=None):
        return self.Budget.create({
            'code': code,
            'name': name,
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [(0, 0, line) for line in (lines or [])],
        })

    # ---- constraints ----

    def test_code_format_constraint(self):
        with self.assertRaises(UserError):
            self._make_budget(code='Bad-Code')
        with self.assertRaises(UserError):
            self._make_budget(code='123_starts_with_number')
        # valid codes pass
        self._make_budget(code='good_code_one', name='one')

    def test_unique_code_per_company(self):
        self._make_budget(code='unique_one', name='First')
        with self.assertRaises(Exception):
            self._make_budget(code='unique_one', name='Dup')

    def test_date_range_constraint(self):
        with self.assertRaises(Exception):
            self.Budget.create({
                'code': 'reversed_dates',
                'name': 'Bad',
                'date_from': fields.Date.from_string('2026-12-31'),
                'date_to': fields.Date.from_string('2026-01-01'),
            })

    def test_child_branch_accepts_parent_owned_dimensions(self):
        Account = self.env['account.account']
        if not callable(getattr(Account, '_check_company_domain', None)):
            self.skipTest(
                "This Odoo series has no hierarchical company domain hook."
            )

        parent = self.company
        branch = self.env['res.company'].create({
            'name': 'Budget Child Branch',
            'parent_id': parent.id,
            'currency_id': parent.currency_id.id,
        })
        Plan = self.env['account.analytic.plan'].with_company(parent)
        plan_vals = {'name': 'Budget Parent Plan'}
        if 'company_id' in Plan._fields:
            plan_vals['company_id'] = parent.id
        plan = Plan.create(plan_vals)
        analytic = self.env[
            'account.analytic.account'
        ].with_company(parent).create({
            'name': 'Budget Parent Analytic',
            'plan_id': plan.id,
            'company_id': parent.id,
        })

        self.assertTrue(self.account_expense.filtered_domain(
            self.account_expense._check_company_domain(branch),
        ))
        self.assertTrue(analytic.filtered_domain(
            analytic._check_company_domain(branch),
        ))

        budget = self.Budget.with_company(branch).create({
            'code': 'child_branch_dimensions',
            'name': 'Child Branch Budget',
            'company_id': branch.id,
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
        })
        line = self.Line.with_company(branch).create({
            'budget_id': budget.id,
            'account_id': self.account_expense.id,
            'analytic_account_id': analytic.id,
            'period_from': budget.date_from,
            'period_to': budget.date_to,
            'budgeted_amount': 1000.0,
        })

        self.assertEqual(line.budget_id.company_id, branch)
        self.assertEqual(line.account_id, self.account_expense)
        self.assertEqual(line.analytic_account_id, analytic)

    def test_kwd_precision_keeps_one_fils_monthly_split(self):
        self.company.currency_id.write({'rounding': 0.001})
        budget = self._make_budget(code='kwd_monthly_split', lines=[{
            'account_id': self.account_expense.id,
            'period_from': fields.Date.from_string('2026-01-01'),
            'period_to': fields.Date.from_string('2026-12-31'),
            'budgeted_amount': 0.012,
        }])
        budget.action_split_lines_by_month()
        self.assertEqual(len(budget.line_ids), 12)
        self.assertTrue(all(
            self.company.currency_id.compare_amounts(
                line.budgeted_amount, 0.001,
            ) == 0
            for line in budget.line_ids
        ))
        self.assertAlmostEqual(
            sum(budget.line_ids.mapped('budgeted_amount')),
            0.012, places=3,
        )

    def test_kwd_monthly_split_allocates_final_residual_exactly(self):
        currency = self.company.currency_id
        currency.write({'rounding': 0.001})
        budget = self._make_budget(code='kwd_split_residual', lines=[{
            'account_id': self.account_expense.id,
            'period_from': fields.Date.from_string('2026-01-01'),
            'period_to': fields.Date.from_string('2026-03-31'),
            'budgeted_amount': 0.010,
            'behaviour': 'semi_variable',
            'fixed_portion': 0.007,
        }])

        budget.action_split_lines_by_month()

        self.assertEqual(len(budget.line_ids), 3)
        ordered_lines = budget.line_ids.sorted('period_from')
        self.assertEqual(
            ordered_lines.mapped('budgeted_amount'),
            [0.003, 0.003, 0.004],
        )
        self.assertEqual(
            ordered_lines.mapped('fixed_portion'),
            [0.002, 0.002, 0.003],
        )
        self.assertEqual(
            currency.compare_amounts(
                sum(budget.line_ids.mapped('budgeted_amount')), 0.010,
            ),
            0,
        )
        self.assertEqual(
            currency.compare_amounts(
                sum(budget.line_ids.mapped('fixed_portion')), 0.007,
            ),
            0,
        )

    def test_monthly_fixed_residual_never_exceeds_slice_budget(self):
        currency = self.company.currency_id
        currency.write({'rounding': 0.01})
        budget = self._make_budget(code='fixed_slice_capacity', lines=[{
            'account_id': self.account_expense.id,
            'period_from': fields.Date.from_string('2026-01-01'),
            'period_to': fields.Date.from_string('2026-03-31'),
            'budgeted_amount': 0.02,
            'behaviour': 'semi_variable',
            'fixed_portion': 0.01,
        }])

        budget.action_split_lines_by_month()

        self.assertEqual(len(budget.line_ids), 3)
        self.assertTrue(all(
            currency.compare_amounts(
                line.fixed_portion, line.budgeted_amount,
            ) <= 0
            for line in budget.line_ids
        ))
        self.assertEqual(
            currency.compare_amounts(
                sum(budget.line_ids.mapped('budgeted_amount')), 0.02,
            ),
            0,
        )
        self.assertEqual(
            currency.compare_amounts(
                sum(budget.line_ids.mapped('fixed_portion')), 0.01,
            ),
            0,
        )

    def test_jpy_monthly_split_allocates_whole_units_exactly(self):
        currency = self.company.currency_id
        currency.write({'rounding': 1.0})
        budget = self._make_budget(code='jpy_split_residual', lines=[{
            'account_id': self.account_expense.id,
            'period_from': fields.Date.from_string('2026-01-01'),
            'period_to': fields.Date.from_string('2026-03-31'),
            'budgeted_amount': 100.0,
        }])

        budget.action_split_lines_by_month()

        amounts = budget.line_ids.sorted('period_from').mapped(
            'budgeted_amount'
        )
        self.assertEqual(len(amounts), 3)
        self.assertEqual(amounts, [33.0, 33.0, 34.0])
        self.assertEqual(currency.compare_amounts(sum(amounts), 100.0), 0)
        self.assertTrue(all(amount == int(amount) for amount in amounts))

    def test_jpy_header_totals_reconcile_visible_line_precision(self):
        currency = self.company.currency_id
        currency.write({'rounding': 1.0})
        budget = self._make_budget(code='jpy_visible_totals', lines=[{
            'account_id': self.account_expense.id,
            'period_from': fields.Date.from_string('2026-01-01'),
            'period_to': fields.Date.from_string('2026-06-30'),
            'budgeted_amount': 33.4,
        }, {
            'account_id': self.account_expense.id,
            'period_from': fields.Date.from_string('2026-07-01'),
            'period_to': fields.Date.from_string('2026-12-31'),
            'budgeted_amount': 33.4,
        }])

        self.assertEqual(budget.total_budgeted, 66.0)
        self.assertEqual(budget.total_actual, 0.0)
        self.assertEqual(budget.total_variance, -66.0)
        self.assertEqual(
            budget.total_variance,
            sum(budget.line_ids.mapped('variance_amount')),
        )

    @freeze_time('2026-01-02')
    def test_jpy_theoretical_total_sums_visible_line_precision(self):
        currency = self.company.currency_id
        currency.write({'rounding': 1.0})
        budget = self.Budget.create({
            'code': 'jpy_visible_theoretical',
            'name': 'JPY visible theoretical',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-01-03'),
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'period_from': fields.Date.from_string('2026-01-01'),
                'period_to': fields.Date.from_string('2026-01-03'),
                'budgeted_amount': 1.0,
            }) for _index in range(2)],
        })
        monetary_handler = self.env['eh.account.dynamic.report.handler']
        visible_values = [
            monetary_handler._eh_round_monetary(
                line.theoretical_amount, currency=currency,
            )
            for line in budget.line_ids
        ]

        self.assertEqual(visible_values, [1.0, 1.0])
        self.assertEqual(budget.total_theoretical, 2.0)

    def test_monthly_split_fails_closed_for_quantity_basis(self):
        budget = self._make_budget(code='split_qty_guard', lines=[{
            'account_id': self.account_expense.id,
            'period_from': fields.Date.from_string('2026-01-01'),
            'period_to': fields.Date.from_string('2026-12-31'),
            'budgeted_amount': 120.0,
            'behaviour': 'variable',
            'budgeted_qty': 12.0,
            'budgeted_unit_price': 10.0,
        }])

        with self.assertRaises(UserError):
            budget.action_split_lines_by_month()

        self.assertEqual(len(budget.line_ids), 1)
        self.assertEqual(budget.line_ids.budgeted_qty, 12.0)

    def test_monthly_split_fails_closed_for_revenue_proxy_driver(self):
        common = {
            'period_from': fields.Date.from_string('2026-01-01'),
            'period_to': fields.Date.from_string('2026-12-31'),
            'budgeted_amount': 120.0,
        }
        budget = self._make_budget(code='split_driver_guard', lines=[
            dict(common, account_id=self.account_revenue.id),
            dict(common, account_id=self.account_expense.id),
        ])
        revenue_line = budget.line_ids.filtered(
            lambda line: line.account_id == self.account_revenue,
        )
        driven_line = budget.line_ids - revenue_line
        driven_line.write({
            'driver': 'revenue_line',
            'driver_line_id': revenue_line.id,
        })

        with self.assertRaises(UserError):
            budget.action_split_lines_by_month()

        self.assertEqual(len(budget.line_ids), 2)
        self.assertEqual(driven_line.driver_line_id, revenue_line)

    def test_currency_rounded_flex_components_reconcile(self):
        currency = self.company.currency_id
        currency.write({'rounding': 0.01})
        budget = self.Budget.create({
            'code': 'flex_rounding_tie',
            'name': 'Flex rounding tie',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'period_from': fields.Date.from_string('2026-01-01'),
                'period_to': fields.Date.from_string('2026-12-31'),
                'budgeted_amount': 1.0,
                'behaviour': 'variable',
            })],
            'activity_period_ids': [(0, 0, {
                'period_from': fields.Date.from_string('2026-01-01'),
                'period_to': fields.Date.from_string('2026-12-31'),
                'budgeted_activity': 200.0,
                'actual_activity': 67.0,
            })],
        })
        line = budget.line_ids

        self.assertEqual(currency.compare_amounts(line.flexed_amount, 0.34), 0)
        self.assertEqual(currency.compare_amounts(line.volume_variance, -0.66), 0)
        self.assertEqual(currency.compare_amounts(line.flexed_variance, -0.34), 0)
        self.assertEqual(
            currency.compare_amounts(
                line.variance_amount,
                line.flexed_variance + line.volume_variance,
            ),
            0,
        )

    def test_zero_actual_quantity_can_be_explicit_evidence(self):
        budget = self.Budget.create({
            'code': 'zero_qty_evidence',
            'name': 'Zero quantity evidence',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'period_from': fields.Date.from_string('2026-01-01'),
                'period_to': fields.Date.from_string('2026-12-31'),
                'budgeted_amount': 50.0,
                'behaviour': 'variable',
                'budgeted_qty': 10.0,
                'budgeted_unit_price': 5.0,
                'actual_qty': 0.0,
                'actual_unit_price': 0.0,
                'quantity_evidence_provided': True,
            })],
            'activity_period_ids': [(0, 0, {
                'period_from': fields.Date.from_string('2026-01-01'),
                'period_to': fields.Date.from_string('2026-12-31'),
                'budgeted_activity': 10.0,
                'actual_activity': 10.0,
            })],
        })
        line = budget.line_ids

        self.assertAlmostEqual(line.price_variance, 0.0, places=2)
        self.assertAlmostEqual(line.efficiency_variance, -50.0, places=2)
        self.assertAlmostEqual(line.split_residual, 0.0, places=2)
        self.assertAlmostEqual(line.flexed_variance, -50.0, places=2)

    def test_kwd_fixed_portion_rejects_one_fils_over_budget(self):
        self.company.currency_id.write({'rounding': 0.001})
        with self.assertRaises(ValidationError):
            self._make_budget(code='kwd_fixed_overrun', lines=[{
                'account_id': self.account_expense.id,
                'period_from': fields.Date.from_string('2026-01-01'),
                'period_to': fields.Date.from_string('2026-12-31'),
                'budgeted_amount': 1.0,
                'behaviour': 'semi_variable',
                'fixed_portion': 1.001,
            }])

    # ---- lifecycle ----

    def test_action_confirm_requires_lines(self):
        budget = self._make_budget(code='no_lines')
        with self.assertRaises(UserError):
            budget.action_confirm()

    def test_action_confirm_with_lines(self):
        budget = self._make_budget(code='with_lines', lines=[
            {
                'account_id': self.account_revenue.id,
                'period_from': fields.Date.from_string('2026-01-01'),
                'period_to': fields.Date.from_string('2026-12-31'),
                'budgeted_amount': 10000.0,
            },
        ])
        budget.action_confirm()
        self.assertEqual(budget.state, 'confirmed')

    def test_action_close(self):
        budget = self._make_budget(code='close_test', lines=[
            {
                'account_id': self.account_revenue.id,
                'period_from': fields.Date.from_string('2026-01-01'),
                'period_to': fields.Date.from_string('2026-12-31'),
                'budgeted_amount': 5000.0,
            },
        ])
        budget.action_confirm()
        budget.action_close()
        self.assertEqual(budget.state, 'closed')

    def test_obsolete_reset_draft_surface_is_removed(self):
        budget = self._make_budget(code='no_reset_surface')
        self.assertFalse(callable(getattr(budget, 'action_reset_draft', None)))


@tagged('eh_account_budget_pro', 'integration', 'post_install', '-at_install')
class TestBudgetActuals(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Budget = cls.env['eh.budget.budget']
        # Seed posted activity used by all tests.
        cls.post_balanced_move(
            [
                {'account': cls.account_revenue, 'credit': 1000.0},
                {'account': cls.account_cash, 'debit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-15'),
        )
        cls.post_balanced_move(
            [
                {'account': cls.account_expense, 'debit': 300.0},
                {'account': cls.account_cash, 'credit': 300.0},
            ],
            date=fields.Date.from_string('2026-07-01'),
        )

    def test_actual_amount_reflects_posted_balance(self):
        budget = self.Budget.create({
            'code': 'actual_test',
            'name': 'Actual Test',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'period_from': fields.Date.from_string('2026-01-01'),
                    'period_to': fields.Date.from_string('2026-12-31'),
                    'budgeted_amount': 1500.0,
                }),
            ],
        })
        revenue_line = budget.line_ids[0]
        # Revenue is credited 1000 (balance = -1000).
        self.assertAlmostEqual(revenue_line.actual_amount, -1000.0, places=2)
        self.assertAlmostEqual(
            revenue_line.normalised_actual_amount, 1000.0, places=2,
        )

    def test_report_view_exposes_normalised_income_actual_and_variance(self):
        budget = self.Budget.create({
            'code': 'income_report_sign',
            'name': 'Income report sign',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [(0, 0, {
                'account_id': self.account_revenue.id,
                'period_from': fields.Date.from_string('2026-01-01'),
                'period_to': fields.Date.from_string('2026-12-31'),
                'budgeted_amount': 1500.0,
            })],
        })
        line = budget.line_ids
        report = self.env['eh.budget.report'].search([
            ('line_id', '=', line.id),
        ])

        self.assertEqual(len(report), 1)
        self.assertAlmostEqual(
            report.actual_amount, line.normalised_actual_amount, places=2,
        )
        self.assertAlmostEqual(
            report.variance_amount, line.variance_amount, places=2,
        )
        self.assertAlmostEqual(report.actual_amount, 1000.0, places=2)
        self.assertAlmostEqual(report.variance_amount, -500.0, places=2)

    def test_variance_math(self):
        budget = self.Budget.create({
            'code': 'variance_test',
            'name': 'Variance Test',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_expense.id,
                    'period_from': fields.Date.from_string('2026-01-01'),
                    'period_to': fields.Date.from_string('2026-12-31'),
                    'budgeted_amount': 200.0,
                }),
            ],
        })
        line = budget.line_ids[0]
        # Expense actual = 300 (debit). Budgeted = 200. Variance = 100.
        self.assertAlmostEqual(line.actual_amount, 300.0, places=2)
        self.assertAlmostEqual(line.variance_amount, 100.0, places=2)
        self.assertAlmostEqual(line.variance_pct, 50.0, places=2)

    def test_period_filter_excludes_out_of_range(self):
        # Add an entry outside the line's period.
        self.post_balanced_move(
            [
                {'account': self.account_expense, 'debit': 9999.0},
                {'account': self.account_cash, 'credit': 9999.0},
            ],
            date=fields.Date.from_string('2027-02-01'),
        )
        budget = self.Budget.create({
            'code': 'period_filter_test',
            'name': 'Period Filter',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_expense.id,
                    'period_from': fields.Date.from_string('2026-01-01'),
                    'period_to': fields.Date.from_string('2026-12-31'),
                    'budgeted_amount': 200.0,
                }),
            ],
        })
        # Should still be 300 (the 2027 entry is excluded).
        self.assertAlmostEqual(
            budget.line_ids[0].actual_amount, 300.0, places=2,
        )

    def test_total_aggregates_lines(self):
        budget = self.Budget.create({
            'code': 'total_test',
            'name': 'Total Test',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'period_from': fields.Date.from_string('2026-01-01'),
                    'period_to': fields.Date.from_string('2026-12-31'),
                    'budgeted_amount': 1500.0,
                }),
                (0, 0, {
                    'account_id': self.account_expense.id,
                    'period_from': fields.Date.from_string('2026-01-01'),
                    'period_to': fields.Date.from_string('2026-12-31'),
                    'budgeted_amount': 500.0,
                }),
            ],
        })
        self.assertAlmostEqual(budget.total_budgeted, 2000.0, places=2)
        # Revenue is normalised to +1,000 and expense remains +300.
        self.assertAlmostEqual(budget.total_actual, 1300.0, places=2)
        self.assertAlmostEqual(budget.total_variance, -700.0, places=2)
        self.assertAlmostEqual(
            budget.total_variance,
            sum(budget.line_ids.mapped('variance_amount')),
            places=2,
        )

    def test_forecast_history_includes_other_expense_on_odoo_19(self):
        account_type_field = self.env[
            'account.account']._fields['account_type']
        if 'expense_other' not in dict(
                account_type_field._description_selection(self.env)):
            self.skipTest("'expense_other' account type not on this Odoo")

        other_expense = self._ensure_account(
            self.env, '5903', 'Forecast Other Expenses', 'expense_other')
        self.post_balanced_move(
            [
                {'account': other_expense, 'debit': 175.0},
                {'account': self.account_cash, 'credit': 175.0},
            ],
            date=fields.Date.from_string('2026-08-01'),
        )
        history = self.Budget._fetch_monthly_history_per_account(
            self.company.id,
            fields.Date.from_string('2026-01-01'),
            fields.Date.from_string('2026-12-31'),
        )

        self.assertIn(other_expense.id, history)
        self.assertAlmostEqual(sum(history[other_expense.id]), 175.0, places=2)

    def test_forecast_history_rejects_nonfinite_and_malformed_balances(self):
        rows = [
            {
                'account_id': self.account_expense.id,
                'month_start': fields.Date.from_string(month),
                'balance': balance,
            }
            for month, balance in (
                ('2026-01-01', 25.0),
                ('2026-02-01', float('nan')),
                ('2026-03-01', float('inf')),
                ('2026-04-01', 'not-a-number'),
                ('2026-05-01', True),
            )
        ]
        with patch.object(MoveLineQuery, 'execute', return_value=rows):
            history = self.Budget._fetch_monthly_history_per_account(
                self.company.id,
                fields.Date.from_string('2026-01-01'),
                fields.Date.from_string('2026-12-31'),
            )

        self.assertEqual(
            history[self.account_expense.id],
            [25.0] + [0.0] * 11,
        )

    def test_forecast_seed_sanitizes_nonfinite_projection_output(self):
        budget = self.Budget.create({
            'code': 'forecast_nonfinite_output',
            'name': 'Forecast non-finite output',
            'date_from': fields.Date.from_string('2027-01-01'),
            'date_to': fields.Date.from_string('2027-12-31'),
        })
        projections = [
            value
            for _index in range(3)
            for value in (float('nan'), float('inf'), 'not-a-number', True)
        ]
        model_class = type(self.Budget)
        with patch.object(
            model_class,
            '_fetch_monthly_history_per_account',
            return_value={self.account_expense.id: [1.0]},
        ), patch.object(
            model_class,
            '_project_account_series',
            return_value=(projections, 'malformed'),
        ):
            budget.action_seed_from_forecast()

        self.assertEqual(len(budget.line_ids), 12)
        self.assertEqual(set(budget.line_ids.mapped('budgeted_amount')), {0.0})

    def test_forecast_seed_trims_empty_prehistory_and_clamps_negative_output(self):
        budget = self.Budget.create({
            'code': 'forecast_effective_history',
            'name': 'Forecast effective history',
            'date_from': fields.Date.from_string('2027-01-01'),
            'date_to': fields.Date.from_string('2027-12-31'),
        })
        captured = []

        def _project(*args):
            series = next(arg for arg in args if isinstance(arg, list))
            captured.append(list(series))
            return ([-25.0] * 12, 'test_negative')

        model_class = type(self.Budget)
        with patch.object(
            model_class,
            '_fetch_monthly_history_per_account',
            return_value={
                self.account_expense.id: [0.0] * 18 + [10.0] * 6,
            },
        ), patch.object(
            model_class,
            '_project_account_series',
            side_effect=_project,
        ):
            budget.action_seed_from_forecast()

        self.assertEqual(captured, [[10.0] * 6])
        self.assertEqual(len(budget.line_ids), 12)
        self.assertEqual(set(budget.line_ids.mapped('budgeted_amount')), {0.0})

    def test_forecast_seed_normalises_revenue_credit_history_once(self):
        budget = self.Budget.create({
            'code': 'forecast_revenue_sign',
            'name': 'Forecast revenue sign',
            'date_from': fields.Date.from_string('2027-01-01'),
            'date_to': fields.Date.from_string('2027-12-31'),
        })
        captured = []

        def _project(*args):
            series = next(arg for arg in args if isinstance(arg, list))
            captured.append(list(series))
            return ([series[-1]] * 12, 'test_revenue')

        model_class = type(self.Budget)
        with patch.object(
            model_class,
            '_fetch_monthly_history_per_account',
            return_value={
                self.account_revenue.id: [0.0] * 23 + [-125.0],
            },
        ), patch.object(
            model_class,
            '_project_account_series',
            side_effect=_project,
        ):
            budget.action_seed_from_forecast()

        self.assertEqual(captured, [[125.0]])
        self.assertEqual(
            set(budget.line_ids.mapped('budgeted_amount')), {125.0},
        )

    def test_approved_evidence_master_data_uses_restrict_foreign_keys(self):
        protected_fields = (
            ('eh.budget.line', 'account_id'),
            ('eh.budget.line', 'analytic_account_id'),
            ('eh.budget.forecast.revision.line', 'account_id'),
            ('eh.budget.forecast.revision.line', 'analytic_account_id'),
        )
        for model_name, field_name in protected_fields:
            self.assertEqual(
                self.env[model_name]._fields[field_name].ondelete,
                'restrict',
                '%s.%s must retain approved evidence' % (
                    model_name, field_name,
                ),
            )

    def test_activity_register_rejects_overlapping_periods(self):
        budget = self.Budget.create({
            'code': 'activity_no_overlap',
            'name': 'Activity no overlap',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
        })
        Activity = self.env['eh.budget.activity']
        Activity.create({
            'budget_id': budget.id,
            'name': 'January',
            'period_from': '2026-01-01',
            'period_to': '2026-01-31',
            'budgeted_activity': 100.0,
        })
        with self.assertRaisesRegex(ValidationError, 'overlaps'):
            with self.env.cr.savepoint():
                Activity.create({
                    'budget_id': budget.id,
                    'name': 'January overlap',
                    'period_from': '2026-01-15',
                    'period_to': '2026-02-15',
                    'budgeted_activity': 50.0,
                })
        february = Activity.create({
            'budget_id': budget.id,
            'name': 'February',
            'period_from': '2026-02-01',
            'period_to': '2026-02-28',
            'budgeted_activity': 100.0,
        })
        with self.assertRaisesRegex(ValidationError, 'overlaps'):
            with self.env.cr.savepoint():
                february.write({'period_from': '2026-01-31'})

    def test_budget_forms_expose_only_model_editable_fields(self):
        budget_arch = etree.fromstring(self.env.ref(
            'eh_account_budget_pro.view_eh_budget_form',
        ).arch_db.encode())
        for field_name in (
            'version_label', 'version_root_code', 'version_sequence',
            'company_id',
        ):
            nodes = budget_arch.xpath(
                ".//field[@name='%s' and @readonly='1' "
                "and not(@invisible='1')]" % field_name,
            )
            self.assertEqual(len(nodes), 1)
        close = budget_arch.xpath(
            ".//button[@name='action_close']",
        )[0]
        reforecast = budget_arch.xpath(
            ".//button[@name='action_reforecast']",
        )[0]
        self.assertIn(
            'revised', close.get('invisible') or close.get('attrs', ''),
        )
        self.assertIn(
            'revised',
            reforecast.get('invisible') or reforecast.get('attrs', ''),
        )

        revision_arch = etree.fromstring(self.env.ref(
            'eh_account_budget_pro.view_eh_budget_forecast_revision_form',
        ).arch_db.encode())
        for field_name in ('name', 'notes'):
            nodes = revision_arch.xpath(
                ".//field[@name='%s' and @readonly='1']" % field_name,
            )
            self.assertEqual(len(nodes), 1)

    def test_prior_year_seed_skips_nonfinite_and_malformed_balances(self):
        budget = self.Budget.create({
            'code': 'prior_year_nonfinite_output',
            'name': 'Prior-year non-finite output',
            'date_from': fields.Date.from_string('2027-01-01'),
            'date_to': fields.Date.from_string('2027-12-31'),
        })
        rows = [
            {'account_id': self.account_expense.id, 'balance': value}
            for value in (
                float('nan'), float('inf'), 'not-a-number', True,
            )
        ] + [{
            'account_id': self.account_revenue.id,
            'balance': -125.0,
        }]
        with patch.object(MoveLineQuery, 'execute', return_value=rows):
            budget.action_seed_from_prior_year()

        self.assertEqual(len(budget.line_ids), 1)
        self.assertEqual(budget.line_ids.account_id, self.account_revenue)
        self.assertEqual(budget.line_ids.budgeted_amount, 125.0)


@tagged('eh_account_budget_pro', 'integration', 'post_install', '-at_install')
class TestBudgetVersioning(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Budget = cls.env['eh.budget.budget']

    def test_create_version_copies_with_parent_link(self):
        original = self.Budget.create({
            'code': 'version_test',
            'name': 'Original',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'version_label': 'v1',
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'period_from': fields.Date.from_string('2026-01-01'),
                    'period_to': fields.Date.from_string('2026-12-31'),
                    'budgeted_amount': 1500.0,
                }),
            ],
        })
        original.action_confirm()
        action = original.action_create_version()
        new_id = action['res_id']
        self.assertTrue(new_id)
        new_budget = self.Budget.browse(new_id)
        self.assertEqual(new_budget.parent_id, original)
        self.assertEqual(new_budget.state, 'draft')
        self.assertEqual(original.state, 'confirmed')
        self.assertEqual(original.version_root_code, 'version_test')
        self.assertEqual(original.version_sequence, 1)
        self.assertEqual(new_budget.version_label, 'v2')
        self.assertEqual(new_budget.version_root_code, 'version_test')
        self.assertEqual(new_budget.version_sequence, 2)
        self.assertEqual(new_budget.code, 'version_test_v2')
        self.assertEqual(len(new_budget.line_ids), 1)
        with self.assertRaises(UserError):
            original.action_create_version()

        new_budget.action_confirm()
        self.assertEqual(original.state, 'revised')
        self.assertEqual(new_budget.state, 'confirmed')

        third = self.Budget.browse(
            new_budget.action_create_version()['res_id'],
        )
        self.assertEqual(third.version_label, 'v3')
        self.assertEqual(third.version_sequence, 3)
        self.assertEqual(third.version_root_code, 'version_test')
        self.assertEqual(third.code, 'version_test_v3')
        self.assertNotIn('_v2_v3', third.code)

    def test_version_label_and_code_derive_from_stable_ordinal(self):
        self.assertEqual(self.Budget._version_label(2), 'v2')
        self.assertEqual(self.Budget._version_label(17), 'v17')
        self.assertEqual(
            self.Budget._next_version_code('annual_2026', 17),
            'annual_2026_v17',
        )

    def test_version_identity_is_server_owned(self):
        with self.assertRaisesRegex(AccessError, 'version root and sequence'):
            self.Budget.create({
                'code': 'forged_version_root',
                'name': 'Forged version root',
                'date_from': '2026-01-01',
                'date_to': '2026-12-31',
                'version_root_code': 'other_root',
                'version_sequence': 99,
            })
        budget = self.Budget.create({
            'code': 'protected_version_root',
            'name': 'Protected version root',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
        })
        with self.assertRaisesRegex(AccessError, 'version identity'):
            budget.sudo().write({'version_sequence': 99})

    def test_root_draft_rename_updates_canonical_chain_root_once(self):
        source = self.Budget.create({
            'code': 'rename_before_approval',
            'name': 'Rename before approval',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'line_ids': [(0, 0, {
                'account_id': self.account_revenue.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 1.0,
            })],
        })
        source.write({'code': 'renamed_canonical_root'})
        self.assertEqual(source.version_root_code, 'renamed_canonical_root')
        source.action_confirm()
        child = self.Budget.browse(
            source.action_create_version()['res_id'],
        )
        self.assertEqual(child.code, 'renamed_canonical_root_v2')
        with self.assertRaisesRegex(UserError, 'chain code is immutable'):
            child.write({'code': 'forged_child_code'})

    def test_canonical_successor_code_collision_is_user_facing_and_atomic(self):
        source = self.Budget.create({
            'code': 'canonical_collision',
            'name': 'Canonical collision source',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'line_ids': [(0, 0, {
                'account_id': self.account_revenue.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 1.0,
            })],
        })
        source.action_confirm()
        self.Budget.create({
            'code': 'canonical_collision_v2',
            'name': 'Unrelated collision',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
        })
        with self.assertRaisesRegex(UserError, 'Canonical successor code'):
            source.action_create_version()
        source.invalidate_recordset(['revised_budget_id', 'state'])
        self.assertFalse(source.revised_budget_id)
        self.assertEqual(source.state, 'confirmed')

    def test_create_version_rejects_multiple(self):
        b1 = self.Budget.create({
            'code': 'multi_test_one',
            'name': 'First',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
        })
        b2 = self.Budget.create({
            'code': 'multi_test_two',
            'name': 'Second',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
        })
        with self.assertRaises(UserError):
            (b1 | b2).action_create_version()

    # ---- regression: zero-budget variance must signal the overrun ----

    def test_safe_variance_pct_zero_budget_zero_actual(self):
        """A budget with zero baseline and no activity is genuinely 0%."""
        self.assertEqual(self.Budget._safe_variance_pct(0.0, 0.0), 0.0)

    def test_safe_variance_pct_zero_budget_with_actual(self):
        """A budget with zero baseline and actual spend is a 100% overrun.

        Regression: the prior code returned 0% in this case, hiding
        unbudgeted spend in variance reports.
        """
        self.assertEqual(
            self.Budget._safe_variance_pct(0.0, 1500.0), 100.0,
        )

    def test_safe_variance_pct_normal_overrun(self):
        """A 10% over-budget run reads as +10."""
        self.assertAlmostEqual(
            self.Budget._safe_variance_pct(1000.0, 1100.0), 10.0,
        )

    def test_safe_variance_pct_under_budget(self):
        """A 5% under-budget run reads as -5."""
        self.assertAlmostEqual(
            self.Budget._safe_variance_pct(1000.0, 950.0), -5.0,
        )


@tagged('eh_account_budget_pro', 'integration', 'post_install', '-at_install')
class TestBudgetAnalyticParity(EhAccountIntegrationTestCase):
    """The SQL report view (the source for the graph and pivot measures)
    must report the same analytic-weighted actual as the ORM compute on
    the budget line. The view previously summed the full balance for any
    journal item touching the analytic account, diverging from the
    percentage-weighted compute for analytic-split lines."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Budget = cls.env['eh.budget.budget']
        plan = cls.env['account.analytic.plan'].create({'name': 'Test Plan'})
        second_plan = cls.env['account.analytic.plan'].create({
            'name': 'Second Test Plan',
        })
        cls.analytic = cls.env['account.analytic.account'].create({
            'name': 'Project X',
            'plan_id': plan.id,
        })
        cls.other_plan_analytic = cls.env['account.analytic.account'].create({
            'name': 'Department Y',
            'plan_id': second_plan.id,
        })
        composite_key = '%s,%s' % (
            cls.analytic.id, cls.other_plan_analytic.id,
        )
        # Post an expense of 1,000 with 30% allocated to the analytic
        # account, so the analytic-weighted actual is 300 and the full
        # balance is 1,000.
        move = cls.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': cls.journal_misc.id,
            'date': fields.Date.from_string('2026-06-15'),
            'line_ids': [
                (0, 0, {
                    'account_id': cls.account_expense.id,
                    'debit': 1000.0,
                    'analytic_distribution': {
                        (
                            composite_key
                            if odoo.release.version_info[0] >= 17
                            else str(cls.analytic.id)
                        ): 30.0,
                    },
                }),
                (0, 0, {
                    'account_id': cls.account_cash.id,
                    'credit': 1000.0,
                }),
            ],
        })
        move.action_post()
        cls.analytic_line = move.line_ids.filtered(
            lambda line: line.account_id == cls.account_expense,
        ).ensure_one()
        if odoo.release.version_info[0] == 16:
            cls.env.cr.execute(
                "UPDATE account_move_line "
                "SET analytic_distribution = jsonb_build_object(%s, %s) "
                "WHERE id = %s",
                [composite_key, 30.0, cls.analytic_line.id],
            )

    def _make_line_budget(self, code, **line_overrides):
        line = {
            'account_id': self.account_expense.id,
            'period_from': fields.Date.from_string('2026-01-01'),
            'period_to': fields.Date.from_string('2026-12-31'),
            'budgeted_amount': 1000.0,
        }
        line.update(line_overrides)
        budget = self.Budget.create({
            'code': code,
            'name': code,
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [(0, 0, line)],
        })
        return budget.line_ids[0]

    def _make_scope_budget(self, code, lines):
        return self.Budget.create({
            'code': code,
            'name': code,
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [(0, 0, dict(
                {
                    'account_id': self.account_expense.id,
                    'period_from': fields.Date.from_string('2026-01-01'),
                    'period_to': fields.Date.from_string('2026-12-31'),
                    'budgeted_amount': 100.0,
                },
                **line,
            )) for line in lines],
        })

    def test_confirmation_rejects_overlapping_identical_actual_scope(self):
        budget = self._make_scope_budget(
            'overlap_same_scope', [{}, {'budgeted_amount': 200.0}],
        )

        with self.assertRaisesRegex(ValidationError, 'overlapping periods'):
            budget.action_confirm()

    def test_confirmation_rejects_global_and_analytic_overlap(self):
        budget = self._make_scope_budget('overlap_global_analytic', [
            {},
            {'analytic_account_id': self.analytic.id},
        ])

        with self.assertRaisesRegex(ValidationError, 'double count actuals'):
            budget.action_confirm()

    def test_confirmation_allows_disjoint_or_partitioned_scopes(self):
        same_plan = self.env['account.analytic.account'].create({
            'name': 'Project Z',
            'plan_id': self.analytic.plan_id.id,
        })
        partitioned = self._make_scope_budget('partitioned_scopes', [
            {'analytic_account_id': self.analytic.id},
            {'analytic_account_id': same_plan.id},
        ])
        partitioned.action_confirm()
        self.assertEqual(partitioned.state, 'confirmed')

        disjoint = self._make_scope_budget('disjoint_same_scope', [
            {
                'period_from': fields.Date.from_string('2026-01-01'),
                'period_to': fields.Date.from_string('2026-06-30'),
            },
            {
                'period_from': fields.Date.from_string('2026-07-01'),
                'period_to': fields.Date.from_string('2026-12-31'),
            },
        ])
        disjoint.action_confirm()
        self.assertEqual(disjoint.state, 'confirmed')

    def test_report_view_matches_weighted_compute(self):
        line = self._make_line_budget(
            'analytic_parity', analytic_account_id=self.analytic.id,
        )
        # ORM compute weights by the 30% allocation: 1,000 * 0.30 = 300.
        self.assertAlmostEqual(line.actual_amount, 300.0, places=2)
        report = self.env['eh.budget.report'].search([
            ('line_id', '=', line.id),
        ])
        self.assertEqual(len(report), 1)
        # The view must report the same 300, not the full 1,000 balance.
        self.assertAlmostEqual(report.actual_amount, 300.0, places=2)
        self.assertAlmostEqual(
            report.actual_amount, line.actual_amount, places=2,
        )

    def test_non_analytic_line_view_matches_full_balance(self):
        line = self._make_line_budget('non_analytic_parity')
        # A non-analytic line counts the full 1,000 balance.
        self.assertAlmostEqual(line.actual_amount, 1000.0, places=2)
        report = self.env['eh.budget.report'].search([
            ('line_id', '=', line.id),
        ])
        self.assertAlmostEqual(report.actual_amount, 1000.0, places=2)
        self.assertAlmostEqual(
            report.actual_amount, line.actual_amount, places=2,
        )

    def test_actual_view_and_drilldown_use_authoritative_move_state(self):
        # Partial/legacy data can carry a stale denormalised parent_state.
        # Move state remains authoritative across compute, SQL view, and click.
        self.env.cr.execute(
            "UPDATE account_move_line SET parent_state = 'draft' WHERE id = %s",
            [self.analytic_line.id],
        )
        self.analytic_line.invalidate_recordset(['parent_state'])
        line = self._make_line_budget('authoritative_move_state')

        self.assertAlmostEqual(line.actual_amount, 1000.0, places=2)
        report = self.env['eh.budget.report'].search([
            ('line_id', '=', line.id),
        ])
        self.assertAlmostEqual(report.actual_amount, 1000.0, places=2)
        action = line.action_view_actuals()
        visible_ids = set(self.env['account.move.line'].search(
            action['domain'],
        ).ids)
        self.assertIn(self.analytic_line.id, visible_ids)

    def test_analytic_actual_drilldown_matches_weighted_scope(self):
        line = self._make_line_budget(
            'analytic_drilldown', analytic_account_id=self.analytic.id,
        )
        unrelated_move = self.post_balanced_move([
            {'account': self.account_expense, 'debit': 75.0},
            {'account': self.account_cash, 'credit': 75.0},
        ], date=fields.Date.from_string('2026-06-16'))
        unrelated_line = unrelated_move.line_ids.filtered(
            lambda item: item.account_id == self.account_expense,
        ).ensure_one()

        action = line.action_view_actuals()
        visible_ids = set(self.env['account.move.line'].search(
            action['domain'],
        ).ids)

        self.assertIn(self.analytic_line.id, visible_ids)
        self.assertNotIn(unrelated_line.id, visible_ids)

    def test_whitespace_composite_key_has_form_view_drilldown_parity(self):
        whitespace_key = ' %s , %s ' % (
            self.analytic.id, self.other_plan_analytic.id,
        )
        self.env.cr.execute(
            "UPDATE account_move_line "
            "SET analytic_distribution = jsonb_build_object(%s, %s) "
            "WHERE id = %s",
            [whitespace_key, 30.0, self.analytic_line.id],
        )
        self.analytic_line.invalidate_recordset(['analytic_distribution'])
        line = self._make_line_budget(
            'analytic_whitespace_composite',
            analytic_account_id=self.analytic.id,
        )

        self.assertEqual(line.normalised_actual_amount, 300.0)
        report = self.env['eh.budget.report'].search([
            ('line_id', '=', line.id),
        ])
        self.assertEqual(report.actual_amount, 300.0)
        visible_ids = set(self.env['account.move.line'].search(
            line.action_view_actuals()['domain'],
        ).ids)
        self.assertIn(self.analytic_line.id, visible_ids)

    def test_internal_whitespace_analytic_key_fails_closed_everywhere(self):
        plan = self.env['account.analytic.plan'].create({
            'name': 'Malformed analytic key plan',
        })
        analytic = self.env['account.analytic.account'].create({
            'name': 'Malformed analytic key seed',
            'plan_id': plan.id,
        })
        while len(str(analytic.id)) < 2:
            analytic = self.env['account.analytic.account'].create({
                'name': 'Malformed analytic key %s' % analytic.id,
                'plan_id': plan.id,
            })
        analytic_token = str(analytic.id)
        malformed_key = '%s %s' % (
            analytic_token[:-1], analytic_token[-1:],
        )
        self.env.cr.execute(
            "UPDATE account_move_line "
            "SET analytic_distribution = jsonb_build_object(%s, %s) "
            "WHERE id = %s",
            [malformed_key, 30.0, self.analytic_line.id],
        )
        self.analytic_line.invalidate_recordset(['analytic_distribution'])
        line = self._make_line_budget(
            'analytic_internal_whitespace',
            analytic_account_id=analytic.id,
        )

        self.assertEqual(line.normalised_actual_amount, 0.0)
        report = self.env['eh.budget.report'].search([
            ('line_id', '=', line.id),
        ])
        self.assertEqual(report.actual_amount, 0.0)
        visible_ids = set(self.env['account.move.line'].search(
            line.action_view_actuals()['domain'],
        ).ids)
        self.assertNotIn(self.analytic_line.id, visible_ids)

    def test_json_boolean_and_string_percentages_contribute_nowhere(self):
        for index, raw_value in enumerate((True, '30.0'), start=1):
            self.env.cr.execute(
                "UPDATE account_move_line "
                "SET analytic_distribution = jsonb_build_object(%s, %s) "
                "WHERE id = %s",
                [str(self.analytic.id), raw_value, self.analytic_line.id],
            )
            self.analytic_line.invalidate_recordset([
                'analytic_distribution',
            ])
            line = self._make_line_budget(
                'analytic_non_numeric_json_%s' % index,
                analytic_account_id=self.analytic.id,
            )
            self.assertEqual(line.normalised_actual_amount, 0.0)
            report = self.env['eh.budget.report'].search([
                ('line_id', '=', line.id),
            ])
            self.assertEqual(report.actual_amount, 0.0)
            visible_ids = set(self.env['account.move.line'].search(
                line.action_view_actuals()['domain'],
            ).ids)
            self.assertNotIn(self.analytic_line.id, visible_ids)

    def test_view_rounds_row_components_before_variance(self):
        currency = self.company.currency_id
        # A used accounting currency correctly rejects coarser rounding via
        # the business API. This transaction-scoped fixture exercises the
        # legacy/imported precision the SQL view must nevertheless honour.
        self.env.cr.execute(
            "UPDATE res_currency SET rounding = %s WHERE id = %s",
            [0.1, currency.id],
        )
        currency.invalidate_recordset(['rounding'])
        self.env.cr.execute(
            "UPDATE account_move_line SET balance = %s WHERE id = %s",
            [0.25, self.analytic_line.id],
        )
        self.analytic_line.invalidate_recordset(['balance'])
        line = self._make_line_budget('report_currency_rounding')
        self.env.cr.execute(
            "UPDATE eh_budget_line SET budgeted_amount = %s WHERE id = %s",
            [0.15, line.id],
        )
        line.invalidate_recordset()

        report = self.env['eh.budget.report'].search([
            ('line_id', '=', line.id),
        ])
        self.assertEqual(
            currency.compare_amounts(
                report.budgeted_amount, currency.round(0.15),
            ),
            0,
        )
        self.assertEqual(
            currency.compare_amounts(
                report.actual_amount, line.normalised_actual_amount,
            ),
            0,
        )
        self.assertEqual(
            currency.compare_amounts(
                report.variance_amount, line.variance_amount,
            ),
            0,
        )
        self.assertEqual(
            currency.compare_amounts(
                report.variance_amount,
                currency.round(
                    currency.round(0.25) - currency.round(0.15),
                ),
            ),
            0,
        )

    def test_analytic_drilldown_excludes_non_contributing_percentages(self):
        line = self._make_line_budget(
            'analytic_drilldown_invalid',
            analytic_account_id=self.analytic.id,
        )

        def _posted_line(amount, posting_date):
            move = self.env['account.move'].create({
                'move_type': 'entry',
                'journal_id': self.journal_misc.id,
                'date': posting_date,
                'line_ids': [
                    (0, 0, {
                        'account_id': self.account_expense.id,
                        'debit': amount,
                        'analytic_distribution': {
                            str(self.analytic.id): 100.0,
                        },
                    }),
                    (0, 0, {
                        'account_id': self.account_cash.id,
                        'credit': amount,
                    }),
                ],
            })
            move.action_post()
            return move.line_ids.filtered(
                lambda item: item.account_id == self.account_expense,
            ).ensure_one()

        zero_line = _posted_line(11.0, '2026-06-17')
        malformed_line = _posted_line(12.0, '2026-06-18')
        overflow_line = _posted_line(13.0, '2026-06-19')
        self.env.cr.execute(
            "UPDATE account_move_line "
            "SET analytic_distribution = jsonb_build_object(%s, %s::numeric) "
            "WHERE id = %s",
            [str(self.analytic.id), '0', zero_line.id],
        )
        self.env.cr.execute(
            "UPDATE account_move_line "
            "SET analytic_distribution = jsonb_build_object(%s, %s) "
            "WHERE id = %s",
            [str(self.analytic.id), 'bad', malformed_line.id],
        )
        self.env.cr.execute(
            "UPDATE account_move_line "
            "SET analytic_distribution = jsonb_build_object(%s, %s::numeric) "
            "WHERE id = %s",
            [str(self.analytic.id), '1e309', overflow_line.id],
        )

        action = line.action_view_actuals()
        visible_ids = set(self.env['account.move.line'].search(
            action['domain'],
        ).ids)

        self.assertIn(self.analytic_line.id, visible_ids)
        self.assertNotIn(zero_line.id, visible_ids)
        self.assertNotIn(malformed_line.id, visible_ids)
        self.assertNotIn(overflow_line.id, visible_ids)

    def test_cross_plan_budget_lines_fail_closed(self):
        with self.assertRaises(ValidationError):
            self.Budget.create({
                'code': 'cross_plan_rejected',
                'name': 'Cross-plan rejected',
                'date_from': fields.Date.from_string('2026-01-01'),
                'date_to': fields.Date.from_string('2026-12-31'),
                'line_ids': [(0, 0, {
                    'account_id': self.account_expense.id,
                    'analytic_account_id': analytic.id,
                    'period_from': fields.Date.from_string('2026-01-01'),
                    'period_to': fields.Date.from_string('2026-12-31'),
                    'budgeted_amount': 1000.0,
                }) for analytic in (
                    self.analytic, self.other_plan_analytic,
                )],
            })

    def test_actual_fetch_query_count_is_independent_of_analytic_ids(self):
        analytics = self.env['account.analytic.account'].create([
            {
                'name': 'Budget query scope %s' % index,
                'plan_id': self.analytic.plan_id.id,
            }
            for index in range(1, 5)
        ])
        percentages = (10.0, 20.0, 30.0, 40.0)
        posting_date = fields.Date.from_string('2026-09-23')
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': posting_date,
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_expense.id,
                    'debit': 1000.0,
                    'analytic_distribution': {
                        str(analytic.id): percentage
                        for analytic, percentage in zip(
                            analytics, percentages,
                        )
                    },
                }),
                (0, 0, {
                    'account_id': self.account_cash.id,
                    'credit': 1000.0,
                }),
            ],
        })
        move.action_post()
        budget = self.Budget.create({
            'code': 'actual_query_count',
            'name': 'Actual query count',
            'date_from': posting_date,
            'date_to': posting_date,
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_expense.id,
                    'analytic_account_id': analytic.id,
                    'period_from': posting_date,
                    'period_to': posting_date,
                    'budgeted_amount': 1000.0,
                })
                for analytic in analytics
            ] + [(0, 0, {
                'account_id': self.account_expense.id,
                'period_from': posting_date,
                'period_to': posting_date,
                'budgeted_amount': 1000.0,
            })],
        })
        analytic_lines = budget.line_ids.filtered('analytic_account_id')
        original_execute = MoveLineQuery.execute

        one_calls = []

        def _count_one(query):
            one_calls.append(1)
            return original_execute(query)

        with patch.object(MoveLineQuery, 'execute', _count_one):
            one_actual = self.env['eh.budget.line']._fetch_actuals_batch(
                budget, analytic_lines[:1],
            )
        self.assertEqual(len(one_calls), 1)

        many_calls = []

        def _count_many(query):
            many_calls.append(1)
            return original_execute(query)

        with patch.object(MoveLineQuery, 'execute', _count_many):
            many_actual = self.env['eh.budget.line']._fetch_actuals_batch(
                budget, budget.line_ids,
            )
        self.assertEqual(
            len(many_calls), 1,
            "actual fetching must stay one SQL pass as analytic IDs grow",
        )

        for analytic, percentage in zip(analytics, percentages):
            key = (
                self.account_expense.id, analytic.id,
                posting_date, posting_date,
            )
            self.assertAlmostEqual(
                many_actual[key], 1000.0 * percentage / 100.0, places=2,
            )
        self.assertAlmostEqual(
            many_actual[(
                self.account_expense.id, False,
                posting_date, posting_date,
            )],
            1000.0,
            places=2,
        )
        self.assertAlmostEqual(
            next(iter(one_actual.values())), percentages[0] * 10.0,
            places=2,
        )

    def test_reforecast_history_skips_malformed_partial_distributions(self):
        posting_date = fields.Date.from_string('2026-06-01')
        rows = [
            {
                'account_id': self.account_expense.id,
                'month_start': posting_date,
                'analytic_distribution': ['not', 'an', 'object'],
                'balance': 100.0,
            },
            {
                'account_id': self.account_expense.id,
                'month_start': posting_date,
                'analytic_distribution': {str(self.analytic.id): 'bad'},
                'balance': 100.0,
            },
            {
                'account_id': self.account_expense.id,
                'month_start': posting_date,
                'analytic_distribution': {str(self.analytic.id): True},
                'balance': 100.0,
            },
            {
                'account_id': self.account_expense.id,
                'month_start': posting_date,
                'analytic_distribution': {str(self.analytic.id): '50.0'},
                'balance': 100.0,
            },
            {
                'account_id': self.account_expense.id,
                'month_start': posting_date,
                'analytic_distribution': {str(self.analytic.id): 50.0},
                'balance': 100.0,
            },
            {
                'account_id': self.account_expense.id,
                'month_start': posting_date,
                'analytic_distribution': {str(self.analytic.id): 200.0},
                'balance': float.fromhex('0x1.fffffffffffffp+1023'),
            },
            {
                'account_id': self.account_expense.id,
                'month_start': posting_date,
                'analytic_distribution': {str(self.analytic.id): 50.0},
                'balance': float('nan'),
            },
        ]
        with patch.object(MoveLineQuery, 'execute', return_value=rows):
            result = self.Budget._fetch_monthly_history_per_scope(
                self.company.id,
                posting_date,
                posting_date,
                [(self.account_expense.id, self.analytic.id)],
            )
        self.assertEqual(
            result[(self.account_expense.id, self.analytic.id)], [50.0],
        )

    def test_nonanalytic_actual_and_reforecast_queries_skip_json_grouping(self):
        posting_date = fields.Date.from_string('2026-06-01')
        line = self._make_line_budget('nonanalytic_fast_path')
        captured = []

        def _capture(query):
            captured.append(query.build().code.lower())
            return []

        with patch.object(MoveLineQuery, 'execute', _capture):
            self.env['eh.budget.line']._fetch_actuals_batch(
                line.budget_id, line,
            )
            self.Budget._fetch_monthly_history_per_scope(
                self.company.id,
                posting_date,
                posting_date,
                [(self.account_expense.id, False)],
            )
        self.assertEqual(len(captured), 2)
        self.assertTrue(all(
            'analytic_distribution' not in statement
            for statement in captured
        ))

    def test_actual_batch_rejects_non_json_numeric_percentages(self):
        posting_date = fields.Date.from_string('2026-06-01')
        line = self._make_line_budget(
            'actual_json_numeric_only',
            analytic_account_id=self.analytic.id,
            period_from=posting_date,
            period_to=posting_date,
        )
        rows = [
            {
                'account_id': self.account_expense.id,
                'date': posting_date,
                'analytic_distribution': {str(self.analytic.id): value},
                'balance': balance,
            }
            for value, balance in (
                (True, 100.0),
                ('25.0', 100.0),
                (25, 100.0),
                (200.0, float.fromhex('0x1.fffffffffffffp+1023')),
            )
        ]
        with patch.object(MoveLineQuery, 'execute', return_value=rows):
            actuals = self.env['eh.budget.line']._fetch_actuals_batch(
                line.budget_id, line,
            )
        self.assertEqual(
            actuals[(
                self.account_expense.id,
                self.analytic.id,
                posting_date,
                posting_date,
            )],
            25.0,
        )

    def test_reforecast_mean_fails_closed_on_float_overflow(self):
        maximum = float.fromhex('0x1.fffffffffffffp+1023')
        projected, algorithm = self.Budget._project_account_series(
            [maximum, maximum],
            2,
            lambda *_args: [],
            lambda *_args: (0.0, 0.0),
            lambda *_args: 0.0,
            ValueError,
        )
        self.assertEqual(algorithm, 'mean')
        self.assertEqual(projected, [0.0, 0.0])

    @freeze_time('2027-01-15')
    def test_reforecast_carries_non_profit_and_loss_account_scope(self):
        asset_account = self._ensure_account(
            self.env, '159901', 'Budget Reforecast Asset',
            'asset_current',
        )
        self.post_balanced_move([
            {'account': asset_account, 'debit': 250.0},
            {'account': self.account_cash, 'credit': 250.0},
        ], date=fields.Date.from_string('2026-06-15'))
        line = self._make_line_budget(
            'asset_reforecast', account_id=asset_account.id,
        )
        budget = line.budget_id
        budget.action_confirm()

        budget.action_reforecast()

        revision = budget.active_revision_id
        self.assertEqual(len(revision.line_ids), 12)
        self.assertAlmostEqual(revision.total_amount, 250.0, places=2)
        self.assertTrue(all(
            revision_line.account_id == asset_account
            for revision_line in revision.line_ids
        ))

    @freeze_time('2027-01-15')
    def test_revision_compute_query_count_does_not_scale_per_budget_line(self):
        plan = self.env['account.analytic.plan'].create({
            'name': 'Revision compute scale plan',
        })
        analytic_accounts = self.env['account.analytic.account'].create([
            {
                'name': 'Revision compute scale %s' % index,
                'plan_id': plan.id,
            }
            for index in range(1, 81)
        ])
        budget = self.Budget.create({
            'code': 'revision_compute_scale',
            'name': 'Revision compute scale',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [(0, 0, {
                'sequence': index,
                'account_id': self.account_expense.id,
                'analytic_account_id': analytic_account.id,
                'period_from': fields.Date.from_string('2026-01-01'),
                'period_to': fields.Date.from_string('2026-12-31'),
                'budgeted_amount': 1000.0,
            }) for index, analytic_account in enumerate(
                analytic_accounts, start=1,
            )],
        })
        budget.action_confirm()
        budget.action_reforecast()
        lines = budget.line_ids
        # Warm unrelated actual/variance fields so measurement isolates
        # revision interval lookup rather than ledger aggregation.
        lines.mapped('normalised_actual_amount')

        def _measure(records):
            records.invalidate_recordset([
                'revision_amount', 'revision_variance',
            ])
            budget.invalidate_recordset([
                'active_revision_id', 'revision_count',
            ])
            start = records.env.cr.sql_log_count
            # Let ORM enter its protected compute context. Calling a compute
            # method directly makes assignment to an existing non-stored
            # field look like a public write, which correctly trips the
            # confirmed-budget immutability guard.
            records.mapped('revision_amount')
            return records.env.cr.sql_log_count - start

        single_queries = _measure(lines[:1])
        large_queries = _measure(lines)
        self.assertLessEqual(
            large_queries,
            single_queries + 3,
            'revision lookup must stay batched as budget lines grow',
        )
        self.assertTrue(all(
            line.revision_amount == lines[0].revision_amount
            for line in lines
        ))

    @freeze_time('2027-01-15')
    def test_revision_prorates_adjacent_partial_periods_exactly(self):
        split_account = self._ensure_account(
            self.env, '589902', 'Split Reforecast Expense', 'expense',
        )
        full_account = self._ensure_account(
            self.env, '589903', 'Full Reforecast Expense', 'expense',
        )
        self.post_balanced_move([
            {'account': split_account, 'debit': 100.01},
            {'account': self.account_cash, 'credit': 100.01},
        ], date=fields.Date.from_string('2026-01-31'))
        self.post_balanced_move([
            {'account': full_account, 'debit': 73.27},
            {'account': self.account_cash, 'credit': 73.27},
        ], date=fields.Date.from_string('2026-01-31'))
        budget = self.Budget.create({
            'code': 'revision_partial_periods',
            'name': 'Revision partial periods',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-01-31'),
            'line_ids': [(0, 0, values) for values in (
                {
                    'sequence': 10,
                    'account_id': split_account.id,
                    'period_from': fields.Date.from_string('2026-01-01'),
                    'period_to': fields.Date.from_string('2026-01-15'),
                    'budgeted_amount': 50.0,
                },
                {
                    'sequence': 20,
                    'account_id': split_account.id,
                    'period_from': fields.Date.from_string('2026-01-16'),
                    'period_to': fields.Date.from_string('2026-01-31'),
                    'budgeted_amount': 50.01,
                },
                {
                    'sequence': 30,
                    'account_id': full_account.id,
                    'period_from': fields.Date.from_string('2026-01-01'),
                    'period_to': fields.Date.from_string('2026-01-31'),
                    'budgeted_amount': 73.27,
                },
            )],
        })
        budget.action_confirm()
        budget.action_reforecast()

        split_lines = budget.line_ids.filtered(
            lambda line: line.account_id == split_account,
        ).sorted(lambda line: (line.period_from, line.id))
        full_line = budget.line_ids.filtered(
            lambda line: line.account_id == full_account,
        ).ensure_one()
        split_revision = budget.active_revision_id.line_ids.filtered(
            lambda line: line.account_id == split_account,
        ).ensure_one()
        currency = budget.currency_id

        self.assertEqual(
            [
                currency.compare_amounts(actual, expected)
                for actual, expected in zip(
                    split_lines.mapped('revision_amount'),
                    [48.39, 51.62],
                )
            ],
            [0, 0],
        )
        self.assertEqual(
            currency.compare_amounts(
                sum(split_lines.mapped('revision_amount')),
                split_revision.amount,
            ),
            0,
        )
        self.assertEqual(
            currency.compare_amounts(full_line.revision_amount, 73.27),
            0,
        )

    @freeze_time('2027-01-15')
    def test_reforecast_keeps_composite_analytic_scope(self):
        line = self._make_line_budget(
            'analytic_reforecast', analytic_account_id=self.analytic.id,
        )
        budget = line.budget_id
        budget.action_confirm()

        budget.action_reforecast()

        revision = budget.active_revision_id
        self.assertEqual(len(revision.line_ids), 12)
        self.assertTrue(all(
            rev_line.analytic_account_id == self.analytic
            for rev_line in revision.line_ids
        ))
        self.assertAlmostEqual(revision.total_amount, 300.0, places=2)
        self.assertAlmostEqual(line.revision_amount, 300.0, places=2)
        self.assertAlmostEqual(line.revision_variance, 0.0, places=2)


@tagged('eh_account_budget_pro', 'integration', 'post_install', '-at_install')
class TestBudgetRollForwardIsolation(EhAccountIntegrationTestCase):

    def _make_rolling(self, code):
        today = fields.Date.context_today(self.env['eh.budget.budget'])
        near = today + timedelta(days=5)
        budget = self.env['eh.budget.budget'].create({
            'code': code,
            'name': code,
            'date_from': today.replace(day=1),
            'date_to': near,
            'is_rolling': True,
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'period_from': today.replace(day=1),
                'period_to': near,
                'budgeted_amount': 100.0,
            })],
        })
        budget.action_confirm()
        return budget, near

    def test_cron_roll_forward_isolates_failures(self):
        # Two due rolling budgets; line creation raises for the first.
        # The per-budget savepoint (shared batch mixin) must isolate the
        # failure so the second budget still rolls forward. Before the
        # mixin the bare savepoint let the error abort the whole cron.
        b1, near1 = self._make_rolling('roll_iso_1')
        b2, near2 = self._make_rolling('roll_iso_2')
        Line = type(self.env['eh.budget.line'])
        real_create = Line.create

        def flaky(self2, vals):
            rows = vals if isinstance(vals, list) else [vals]
            if any(r.get('budget_id') == b1.id for r in rows):
                raise ValueError("forced failure")
            return real_create(self2, vals)

        with patch.object(Line, 'create', flaky):
            self.env['eh.budget.budget'].cron_roll_forward()

        b1.invalidate_recordset()
        b2.invalidate_recordset()
        self.assertEqual(
            b1.date_to, near1,
            "failed budget must roll back and keep its original date_to",
        )
        self.assertGreater(
            b2.date_to, near2,
            "second budget must roll forward despite the first failing",
        )

    def test_roll_forward_cron_is_private(self):
        method = type(self.env['eh.budget.budget']).cron_roll_forward
        self.assertTrue(getattr(method, '_api_private', False))

    def test_roll_forward_skips_concurrently_claimed_budget(self):
        budget, original_end = self._make_rolling('roll_claimed')
        Model = type(self.env['eh.budget.budget'])
        with patch.object(
                Model, '_eh_try_lock_roll_forward', return_value=False):
            self.env['eh.budget.budget'].cron_roll_forward()
        budget.invalidate_recordset()
        self.assertEqual(budget.date_to, original_end)
