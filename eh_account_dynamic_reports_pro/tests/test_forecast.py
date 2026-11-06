# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Forecast tests.

Covers calendar-month validation and boundaries, growth factor math, semantic
monetary-only scaling, option propagation with forced company/date scope, and
the owner-scoped transient JSON result returned by the form action.
"""

import base64
import json
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import new_test_user, tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_dynamic_reports_pro', 'integration', 'post_install', '-at_install')
class TestForecast(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        DynRep = cls.env['eh.account.dynamic.report']
        cls.report = DynRep.search([('code', '=', 'profit_and_loss')], limit=1)
        if not cls.report:
            cls.report = DynRep.create({
                'code': 'profit_and_loss',
                'name': 'Profit and Loss',
                'handler_model':
                    'eh.account.dynamic.report.handler.profit_and_loss',
            })
        cls.Forecast = cls.env['eh.report.forecast']
        # Seed activity in the baseline period.
        cls.post_balanced_move(
            [
                {'account': cls.account_revenue, 'credit': 1000.0},
                {'account': cls.account_cash, 'debit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-15'),
        )

    def _make_forecast(self, **overrides):
        vals = {
            'name': 'Test Forecast',
            'base_report_id': self.report.id,
            'base_date_from': fields.Date.from_string('2026-06-01'),
            'base_date_to': fields.Date.from_string('2026-06-30'),
            'horizon_months': 6,
            'growth_method': 'compound',
            'monthly_growth_pct': 5.0,
        }
        vals.update(overrides)
        return self.Forecast.create(vals)

    @staticmethod
    def _projected_line_value(period, line_id, expression='amount'):
        line = next(
            line for line in period['lines'] if line.get('id') == line_id
        )
        return next(
            cell.get('value')
            for cell in line.get('columns') or []
            if cell.get('expression_label') == expression
        )

    # ---- shape and counts ----

    def test_project_returns_baseline_periods_meta(self):
        forecast = self._make_forecast(horizon_months=3)
        result = forecast.project()
        self.assertIn('baseline', result)
        self.assertIn('periods', result)
        self.assertIn('meta', result)
        self.assertEqual(len(result['periods']), 3)

    def test_period_carries_label_and_dates(self):
        forecast = self._make_forecast(horizon_months=2)
        result = forecast.project()
        first = result['periods'][0]
        self.assertEqual(first['period_index'], 1)
        self.assertEqual(first['date_from'], '2026-07-01')
        self.assertEqual(first['date_to'], '2026-07-31')
        self.assertEqual(first['period_label'], '2026-07')

    def test_calendar_month_boundaries_cover_short_months(self):
        forecast = self._make_forecast(
            base_date_from=fields.Date.from_string('2024-01-01'),
            base_date_to=fields.Date.from_string('2024-01-31'),
            horizon_months=2,
        )
        periods = forecast.project()['periods']
        self.assertEqual(periods[0]['date_from'], '2024-02-01')
        self.assertEqual(periods[0]['date_to'], '2024-02-29')
        self.assertEqual(periods[1]['date_from'], '2024-03-01')
        self.assertEqual(periods[1]['date_to'], '2024-03-31')

    def test_partial_or_multi_month_baseline_fails_before_render(self):
        for date_from, date_to in (
            ('2026-06-02', '2026-06-30'),
            ('2026-06-01', '2026-07-31'),
        ):
            forecast = self._make_forecast(
                base_date_from=fields.Date.from_string(date_from),
                base_date_to=fields.Date.from_string(date_to),
            )
            with patch.object(
                type(forecast.base_report_id), 'render',
            ) as render:
                with self.assertRaises(UserError):
                    forecast.project()
                render.assert_not_called()

    def test_oversized_baseline_fails_before_period_amplification(self):
        forecast = self._make_forecast(horizon_months=24)
        baseline = {
            'columns': [
                {'expression_label': 'label', 'figure_type': 'string'},
                {'expression_label': 'amount', 'figure_type': 'monetary'},
            ],
            'lines': [
                {
                    'id': 'line-%s' % index,
                    'name': 'Line %s' % index,
                    'columns': [{
                        'expression_label': 'amount', 'value': 1.0,
                    }],
                }
                for index in range(2001)
            ],
            'totals': {'amount': 2001.0},
        }
        with patch.object(
            type(forecast.base_report_id), 'render', return_value=baseline,
        ), patch.object(type(forecast), '_project_periods') as project_periods:
            with self.assertRaisesRegex(UserError, 'too large'):
                forecast.project()
            project_periods.assert_not_called()

    def test_trial_balance_fails_closed_before_render(self):
        trial_balance = self.env['eh.account.dynamic.report'].search([
            ('code', '=', 'trial_balance'),
        ], limit=1)
        if not trial_balance:
            trial_balance = self.env['eh.account.dynamic.report'].create({
                'code': 'trial_balance',
                'name': 'Trial Balance',
                'handler_model':
                    'eh.account.dynamic.report.handler.trial_balance',
            })
        forecast = self._make_forecast(base_report_id=trial_balance.id)
        with patch.object(type(trial_balance), 'render') as render:
            with self.assertRaisesRegex(UserError, 'cannot be used'):
                forecast.project()
            render.assert_not_called()

    def test_analytic_balance_fails_closed_before_render(self):
        analytic_balance = self.env['eh.account.dynamic.report'].search([
            ('handler_model', '=',
             'eh.account.dynamic.report.handler.analytic_balance'),
        ], limit=1)
        if not analytic_balance:
            analytic_balance = self.env['eh.account.dynamic.report'].create({
                'code': 'analytic_balance',
                'name': 'Analytic Balance',
                'handler_model':
                    'eh.account.dynamic.report.handler.analytic_balance',
            })
        forecast = self._make_forecast(base_report_id=analytic_balance.id)
        with patch.object(type(analytic_balance), 'render') as render:
            with self.assertRaisesRegex(UserError, 'scalar monetary growth'):
                forecast.project()
            render.assert_not_called()

    def test_unsafe_handler_fails_closed_after_report_code_rename(self):
        unsafe_alias = self.env['eh.account.dynamic.report'].create({
            'code': 'forecast_renamed_analytic',
            'name': 'Renamed Aggregate',
            'handler_model':
                'eh.account.dynamic.report.handler.analytic_balance',
        })
        forecast = self._make_forecast(base_report_id=unsafe_alias.id)
        with patch.object(type(unsafe_alias), 'render') as render:
            with self.assertRaisesRegex(UserError, 'cannot be used'):
                forecast.project()
            render.assert_not_called()

    # ---- growth factors ----

    def test_flat_method_returns_factor_one(self):
        forecast = self._make_forecast(
            growth_method='flat', monthly_growth_pct=5.0,
        )
        for period_n in range(1, 7):
            self.assertAlmostEqual(
                forecast._growth_factor(period_n), 1.0, places=6,
            )

    def test_compound_method_compounds_monthly(self):
        forecast = self._make_forecast(
            growth_method='compound', monthly_growth_pct=10.0,
        )
        # 1.10^1 = 1.10
        self.assertAlmostEqual(forecast._growth_factor(1), 1.10, places=6)
        # 1.10^3 = 1.331
        self.assertAlmostEqual(forecast._growth_factor(3), 1.331, places=6)

    def test_legacy_linear_key_is_normalized_to_truthful_compound_method(self):
        forecast = self._make_forecast(growth_method='linear')
        self.assertEqual(forecast.growth_method, 'compound')

    def test_growth_rejects_non_finite_and_impractical_values(self):
        for value in (float('inf'), float('-inf'), float('nan'), 1001, -101):
            with self.assertRaisesRegex(UserError, 'growth'):
                self._make_forecast(monthly_growth_pct=value)
        forecast = self._make_forecast(monthly_growth_pct=10.0)
        with self.assertRaisesRegex(UserError, 'growth'):
            forecast.write({'monthly_growth_pct': 1e308})
        self.assertEqual(forecast.monthly_growth_pct, 10.0)

    # ---- value scaling ----

    def test_baseline_numeric_cells_scaled(self):
        forecast = self._make_forecast(
            growth_method='compound', monthly_growth_pct=10.0,
            horizon_months=2,
        )
        result = forecast.project()
        baseline = result['baseline']
        period1 = result['periods'][0]
        period2 = result['periods'][1]

        # Find the same line in baseline and projected periods. Use the
        # first numeric cell as the comparison.
        def find_first_value(payload):
            for line in payload.get('lines') or []:
                for col in line.get('columns') or []:
                    if isinstance(col.get('value'), (int, float)) and col['value']:
                        return col['value']
            return None

        b = find_first_value(baseline)
        p1 = find_first_value(period1)
        p2 = find_first_value(period2)
        if b is None or p1 is None or p2 is None:
            self.skipTest("Baseline produced no numeric cells")
        self.assertAlmostEqual(p1, round(b * 1.10, 2), places=2)
        self.assertAlmostEqual(p2, round(b * 1.21, 2), places=2)

    def test_only_semantically_monetary_values_are_scaled(self):
        baseline = {
            'columns': [
                {'expression_label': 'label', 'figure_type': 'string'},
                {'expression_label': 'amount', 'figure_type': 'monetary'},
                {'expression_label': 'margin', 'figure_type': 'percentage'},
                {'expression_label': 'days', 'figure_type': 'integer'},
                {'expression_label': 'ratio', 'figure_type': 'float'},
            ],
            'lines': [
                {
                    'id': 'section-income-total',
                    'name': 'Income',
                    'columns': [
                        {'expression_label': 'amount', 'value': 100.0},
                        {'expression_label': 'margin', 'value': 0.25},
                        {'expression_label': 'days', 'value': 30},
                        {'expression_label': 'ratio', 'value': 1.5},
                    ],
                    'meta': {
                        'kind': 'section_total', 'section_id': 'income',
                    },
                },
                {
                    'id': 'override-percentage',
                    'name': 'Override',
                    'columns': [{
                        'expression_label': 'amount',
                        'figure_type': 'percentage',
                        'value': 0.4,
                    }],
                },
            ],
            'totals': {
                'amount': 100.0,
                'income': 100.0,
                'margin': 0.25,
                'days': 30,
                'ratio': 1.5,
                'untyped_count': 7,
            },
        }
        projected = self.Forecast._apply_factor(baseline, 1.10)
        values = projected['lines'][0]['columns']
        self.assertEqual(values[0]['value'], 110.0)
        self.assertEqual(values[1]['value'], 0.25)
        self.assertEqual(values[2]['value'], 30)
        self.assertEqual(values[3]['value'], 1.5)
        self.assertEqual(projected['lines'][1]['columns'][0]['value'], 0.4)
        self.assertEqual(projected['totals']['amount'], 110.0)
        self.assertEqual(projected['totals']['income'], 110.0)
        self.assertEqual(projected['totals']['margin'], 0.25)
        self.assertEqual(projected['totals']['days'], 30)
        self.assertEqual(projected['totals']['ratio'], 1.5)
        self.assertEqual(projected['totals']['untyped_count'], 7)
        # Projection must not mutate cached baseline payload.
        self.assertEqual(baseline['lines'][0]['columns'][0]['value'], 100.0)

    def test_comparison_history_stays_fixed_and_variance_is_recomputed(self):
        baseline = {
            'columns': [
                {'expression_label': 'account', 'figure_type': 'string'},
                {'expression_label': 'amount', 'figure_type': 'monetary'},
                {
                    'expression_label': 'prior_amount',
                    'figure_type': 'monetary',
                },
                {'expression_label': 'variance', 'figure_type': 'monetary'},
                {
                    'expression_label': 'variance_pct',
                    'figure_type': 'percentage',
                },
            ],
            'lines': [{
                # Match the real P&L payload so total semantics can be
                # inferred without treating every numeric total as money.
                'id': 'net_profit',
                'columns': [
                    {'expression_label': 'amount', 'value': 110.0},
                    {'expression_label': 'prior_amount', 'value': 100.0},
                    {'expression_label': 'variance', 'value': 10.0},
                    {'expression_label': 'variance_pct', 'value': 0.10},
                ],
            }],
            'totals': {
                'amount': 110.0,
                'net_profit': 110.0,
                'prior_net_profit': 100.0,
            },
        }
        projected = self.Forecast._apply_factor(baseline, 1.10)
        cells = {
            cell['expression_label']: cell['value']
            for cell in projected['lines'][0]['columns']
        }
        self.assertEqual(cells['amount'], 121.0)
        self.assertEqual(cells['prior_amount'], 100.0)
        self.assertEqual(cells['variance'], 21.0)
        self.assertAlmostEqual(cells['variance_pct'], 0.21)
        self.assertEqual(projected['totals']['amount'], 121.0)
        self.assertEqual(projected['totals']['net_profit'], 121.0)
        self.assertEqual(projected['totals']['prior_net_profit'], 100.0)

    def test_nested_monetary_disclosure_totals_match_projected_lines(self):
        baseline = {
            'columns': [
                {'expression_label': 'label', 'figure_type': 'string'},
                {'expression_label': 'amount', 'figure_type': 'monetary'},
            ],
            'lines': [{
                'id': 'disclosure-interest_paid',
                'columns': [{
                    'expression_label': 'amount', 'value': -100.0,
                }],
            }],
            'totals': {
                'disclosures': {'interest_paid': -100.0},
            },
            'meta': {
                'total_figure_types': {'disclosures': 'monetary'},
            },
        }
        projected = self.Forecast._apply_factor(baseline, 1.10)
        self.assertEqual(
            projected['lines'][0]['columns'][0]['value'], -110.0,
        )
        self.assertEqual(
            projected['totals']['disclosures']['interest_paid'], -110.0,
        )

    def test_currency_is_resolved_once_for_many_projected_cells(self):
        baseline = {
            'currency': {'id': self.company.currency_id.id},
            'columns': [{
                'expression_label': 'amount',
                'figure_type': 'monetary',
            }],
            'lines': [
                {
                    'id': 'line-%s' % index,
                    'columns': [{
                        'expression_label': 'amount',
                        'value': float(index + 1),
                    }],
                }
                for index in range(200)
            ],
            'totals': {'amount': 20100.0},
        }
        ForecastClass = type(self.Forecast)
        original = ForecastClass._forecast_currency
        calls = []

        def counted(recordset, payload):
            calls.append(1)
            return original(recordset, payload)

        with patch.object(
            ForecastClass, '_forecast_currency', counted,
        ):
            projected = self.Forecast._apply_factor(baseline, 1.10)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(projected['lines']), 200)

    def test_projection_uses_payload_currency_precision(self):
        currency = self.env.ref('base.KWD')
        self.assertEqual(currency.decimal_places, 3)
        baseline = {
            'currency': {
                'id': currency.id,
                'decimal_places': currency.decimal_places,
            },
            'columns': [
                {'expression_label': 'label', 'figure_type': 'string'},
                {'expression_label': 'amount', 'figure_type': 'monetary'},
                {
                    'expression_label': 'prior_amount',
                    'figure_type': 'monetary',
                },
                {'expression_label': 'variance', 'figure_type': 'monetary'},
            ],
            'lines': [{
                'id': 'amount',
                'columns': [
                    {'expression_label': 'amount', 'value': 1.234},
                    {'expression_label': 'prior_amount', 'value': 1.0},
                    {'expression_label': 'variance', 'value': 0.234},
                ],
            }],
            'totals': {'amount': 1.234},
        }
        projected = self.Forecast._apply_factor(baseline, 1.10)
        cells = projected['lines'][0]['columns']
        self.assertEqual(cells[0]['value'], 1.357)
        self.assertEqual(cells[2]['value'], 0.357)
        self.assertEqual(projected['totals']['amount'], 1.357)

    def test_projected_additive_sections_foot_at_currency_precision(self):
        for currency_xmlid in ('base.USD', 'base.JPY', 'base.KWD'):
            currency = self.env.ref(currency_xmlid)
            unit = float(currency.rounding)
            baseline = {
                'currency': {'id': currency.id},
                'columns': [{
                    'expression_label': 'amount',
                    'figure_type': 'monetary',
                }],
                'lines': [
                    {
                        'id': 'section-rounding-header',
                        'columns': [{
                            'expression_label': 'amount', 'value': '',
                        }],
                        'meta': {
                            'kind': 'section_header',
                            'section_id': 'rounding',
                        },
                    },
                    {
                        'id': 'rounding-a',
                        'columns': [{
                            'expression_label': 'amount', 'value': unit,
                        }],
                        'meta': {'kind': 'section_line'},
                    },
                    {
                        'id': 'rounding-b',
                        'columns': [{
                            'expression_label': 'amount', 'value': unit,
                        }],
                        'meta': {'kind': 'section_line'},
                    },
                    {
                        'id': 'section-rounding-total',
                        'columns': [{
                            'expression_label': 'amount',
                            'value': 2.0 * unit,
                        }],
                        'meta': {
                            'kind': 'section_total',
                            'section_id': 'rounding',
                        },
                    },
                ],
                'totals': {'rounding': 2.0 * unit},
            }
            projected = self.Forecast._apply_factor(baseline, 0.5)
            leaves = [
                projected['lines'][index]['columns'][0]['value']
                for index in (1, 2)
            ]
            section_total = projected['lines'][3]['columns'][0]['value']
            expected_total = currency.round(unit)
            self.assertEqual(section_total, expected_total, currency_xmlid)
            self.assertEqual(
                sorted(leaves), [currency.round(0.0), expected_total],
                currency_xmlid,
            )
            self.assertTrue(
                currency.is_zero(sum(leaves) - section_total),
                currency_xmlid,
            )
            self.assertEqual(
                projected['totals']['rounding'], section_total,
                currency_xmlid,
            )

    def test_real_profit_and_loss_statement_foots_after_projection(self):
        """Cross-section Net Profit keeps the displayed P&L identity."""
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 0.01},
                {'account': self.account_revenue, 'credit': 0.01},
            ],
            date=fields.Date.from_string('2026-07-10'),
        )
        self.post_balanced_move(
            [
                {'account': self.account_expense, 'debit': 0.02},
                {'account': self.account_cash, 'credit': 0.02},
            ],
            date=fields.Date.from_string('2026-07-11'),
        )
        forecast = self._make_forecast(
            base_date_from=fields.Date.from_string('2026-07-01'),
            base_date_to=fields.Date.from_string('2026-07-31'),
            horizon_months=1,
            monthly_growth_pct=-50.0,
        )
        period = forecast.project()['periods'][0]
        income = self._projected_line_value(
            period, 'section-income-total',
        )
        expenses = self._projected_line_value(
            period, 'section-expenses-total',
        )
        net_profit = self._projected_line_value(period, 'net_profit')
        currency = self.company.currency_id

        self.assertTrue(currency.is_zero(net_profit - income + expenses))
        self.assertEqual(period['totals']['income'], income)
        self.assertEqual(period['totals']['expenses'], expenses)
        self.assertEqual(period['totals']['net_profit'], net_profit)

    def test_real_balance_sheet_statement_foots_after_projection(self):
        """Assets, Liabilities + Equity, and Balance Check stay aligned."""
        liability = self._ensure_account(
            self.env, '2201', 'Forecast Liability', 'liability_current',
        )
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 0.01},
                {'account': liability, 'credit': 0.01},
            ],
            date=fields.Date.from_string('2026-06-20'),
        )
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 0.01},
                {'account': self.account_equity, 'credit': 0.01},
            ],
            date=fields.Date.from_string('2026-06-21'),
        )
        report = self.env['eh.account.dynamic.report'].search(
            [('code', '=', 'balance_sheet')], limit=1,
        )
        forecast = self._make_forecast(
            base_report_id=report.id,
            horizon_months=1,
            monthly_growth_pct=-50.0,
        )
        period = forecast.project()['periods'][0]
        assets = self._projected_line_value(
            period, 'section-assets-total',
        )
        liabilities = self._projected_line_value(
            period, 'section-liabilities-total',
        )
        equity = self._projected_line_value(
            period, 'section-equity-total',
        )
        liabilities_equity = self._projected_line_value(
            period, 'total_equity_liabilities',
        )
        balance_check = self._projected_line_value(
            period, 'balance_check',
        )
        currency = self.company.currency_id

        self.assertTrue(
            currency.is_zero(assets - liabilities_equity),
        )
        self.assertTrue(
            currency.is_zero(liabilities_equity - liabilities - equity),
        )
        self.assertTrue(currency.is_zero(balance_check))
        self.assertEqual(period['totals']['assets'], assets)
        self.assertEqual(
            period['totals']['total_equity_liabilities'],
            liabilities_equity,
        )
        self.assertEqual(period['totals']['balance_check'], balance_check)
        previous_earnings = self._projected_line_value(
            period, 'previous_year_earnings',
        )
        current_earnings = self._projected_line_value(
            period, 'current_year_earnings',
        )
        self.assertEqual(
            period['totals']['equity'],
            currency.round(equity - previous_earnings - current_earnings),
        )

    def test_real_by_function_tax_sections_foot_after_projection(self):
        current_tax = self._ensure_account(
            self.env, '5911', 'Forecast Current Tax', 'expense',
        )
        deferred_tax = self._ensure_account(
            self.env, '5912', 'Forecast Deferred Tax', 'expense',
        )
        self.post_balanced_move(
            [
                {'account': current_tax, 'debit': 0.01},
                {'account': self.account_cash, 'credit': 0.01},
            ],
            date=fields.Date.from_string('2026-07-12'),
        )
        self.post_balanced_move(
            [
                {'account': deferred_tax, 'debit': 0.01},
                {'account': self.account_cash, 'credit': 0.01},
            ],
            date=fields.Date.from_string('2026-07-13'),
        )
        self.company.write({
            'eh_pnl_tax_expense_account_ids': [(6, 0, [
                current_tax.id, deferred_tax.id,
            ])],
            'eh_pnl_deferred_tax_account_ids': [(6, 0, [
                deferred_tax.id,
            ])],
        })
        forecast = self._make_forecast(
            base_date_from=fields.Date.from_string('2026-07-01'),
            base_date_to=fields.Date.from_string('2026-07-31'),
            horizon_months=1,
            monthly_growth_pct=-50.0,
        )
        currency = self.company.currency_id

        for hierarchical in (False, True):
            period = forecast.project({
                'pnl_presentation': 'by_function',
                'hierarchical_groups': hierarchical,
            })['periods'][0]
            current_total = self._projected_line_value(
                period, 'section-current_tax-total',
            )
            deferred_total = self._projected_line_value(
                period, 'section-deferred_tax-total',
            )
            tax_total = self._projected_line_value(
                period, 'section-tax_expense-total',
            )
            current_leaf = self._projected_line_value(
                period, 'account-%s' % current_tax.id,
            )
            deferred_leaf = self._projected_line_value(
                period, 'account-%s' % deferred_tax.id,
            )
            profit_before_tax = self._projected_line_value(
                period, 'profit_before_tax',
            )
            net_profit = self._projected_line_value(period, 'net_profit')

            self.assertTrue(currency.is_zero(current_total - current_leaf))
            self.assertTrue(
                currency.is_zero(deferred_total - deferred_leaf),
            )
            self.assertTrue(
                currency.is_zero(tax_total - current_total - deferred_total),
            )
            self.assertTrue(
                currency.is_zero(net_profit - profit_before_tax + tax_total),
            )
            cost_of_sales = self._projected_line_value(
                period, 'section-cost_of_sales-total',
            )
            operating_expenses = self._projected_line_value(
                period, 'section-operating_expenses-total',
            )
            finance_costs = self._projected_line_value(
                period, 'section-finance_costs-total',
            )
            self.assertEqual(
                period['totals']['expenses'],
                currency.round(
                    cost_of_sales + operating_expenses
                    + finance_costs + tax_total,
                ),
            )

    def test_real_cash_flow_statement_foots_after_projection(self):
        """Activity bridge, closing cash, and cash check remain additive."""
        fixed_asset = self._ensure_account(
            self.env, '1601', 'Forecast Fixed Asset', 'asset_fixed',
        )
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 0.01},
                {'account': self.account_revenue, 'credit': 0.01},
            ],
            date=fields.Date.from_string('2026-06-22'),
        )
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 0.01},
                {'account': fixed_asset, 'credit': 0.01},
            ],
            date=fields.Date.from_string('2026-06-23'),
        )
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 0.01},
                {'account': self.account_equity, 'credit': 0.01},
            ],
            date=fields.Date.from_string('2026-06-24'),
        )
        report = self.env['eh.account.dynamic.report'].search(
            [('code', '=', 'cash_flow')], limit=1,
        )
        forecast = self._make_forecast(
            base_report_id=report.id,
            horizon_months=2,
            monthly_growth_pct=-50.0,
        )
        result = forecast.project({
            'cash_flow_method': 'direct',
            'cash_flow_reconciled': False,
        })
        currency = self.company.currency_id
        previous_closing = self._projected_line_value(
            result['baseline'], 'closing_cash_balance',
        )
        for period in result['periods']:
            operating = self._projected_line_value(
                period, 'section-operating-total',
            )
            investing = self._projected_line_value(
                period, 'section-investing-total',
            )
            financing = self._projected_line_value(
                period, 'section-financing-total',
            )
            net_change = self._projected_line_value(
                period, 'net_change_in_cash',
            )
            opening = self._projected_line_value(
                period, 'opening_cash_balance',
            )
            closing = self._projected_line_value(
                period, 'closing_cash_balance',
            )
            cash_check = self._projected_line_value(
                period, 'cash_balance_check',
            )

            self.assertTrue(currency.is_zero(opening - previous_closing))
            self.assertTrue(currency.is_zero(
                net_change - operating - investing - financing,
            ))
            self.assertTrue(currency.is_zero(closing - opening - net_change))
            self.assertTrue(currency.is_zero(cash_check))
            self.assertEqual(period['totals']['net_change_in_cash'], net_change)
            self.assertEqual(
                period['totals']['opening_cash_balance'], opening,
            )
            self.assertEqual(
                period['totals']['closing_cash_balance'], closing,
            )
            self.assertEqual(period['totals']['balance_check'], cash_check)
            previous_closing = closing

    def test_cash_flow_nonzero_baseline_check_fails_closed(self):
        report = self.env['eh.account.dynamic.report'].search(
            [('code', '=', 'cash_flow')], limit=1,
        )
        forecast = self._make_forecast(
            base_report_id=report.id,
            horizon_months=1,
        )

        def line(line_id, value):
            return {
                'id': line_id,
                'columns': [{
                    'expression_label': 'amount',
                    'value': value,
                    'figure_type': 'monetary',
                }],
            }

        baseline = {
            'currency': {'id': self.company.currency_id.id},
            'columns': [{
                'expression_label': 'amount',
                'figure_type': 'monetary',
            }],
            'lines': [
                line('net_change_in_cash', 10.0),
                line('opening_cash_balance', 100.0),
                line('closing_cash_balance', 111.0),
                line('cash_balance_check', 1.0),
            ],
            'totals': {
                'net_change_in_cash': 10.0,
                'opening_cash_balance': 100.0,
                'closing_cash_balance': 111.0,
                'balance_check': 1.0,
            },
        }
        with self.assertRaisesRegex(UserError, 'does not reconcile'):
            forecast._project_periods(baseline)

    def test_real_builder_section_total_foots_after_projection(self):
        builder = self.env['eh.report.builder'].create({
            'code': 'forecast_rounding_builder',
            'name': 'Forecast Rounding Builder',
            'company_id': self.company.id,
            'line_ids': [
                (0, 0, {
                    'sequence': 10,
                    'name': 'Rounding',
                    'code': 'rounding_section',
                    'line_type': 'section_header',
                }),
                (0, 0, {
                    'sequence': 20,
                    'name': 'A',
                    'code': 'rounding_a',
                    'line_type': 'formula',
                    'formula': '0.01',
                }),
                (0, 0, {
                    'sequence': 30,
                    'name': 'B',
                    'code': 'rounding_b',
                    'line_type': 'formula',
                    'formula': '0.02',
                }),
                (0, 0, {
                    'sequence': 40,
                    'name': 'Subtotal',
                    'code': 'rounding_subtotal',
                    'line_type': 'formula',
                    'formula': 'rounding_a - rounding_b',
                }),
                (0, 0, {
                    'sequence': 50,
                    'name': 'Total',
                    'code': 'rounding_total',
                    'line_type': 'formula',
                    'formula': 'rounding_subtotal',
                    'is_section_total': True,
                }),
            ],
        })
        builder.action_publish()
        forecast = self._make_forecast(
            base_report_id=builder.published_report_id.id,
            horizon_months=1,
            monthly_growth_pct=-50.0,
        )
        result = forecast.project()
        period = result['periods'][0]
        values = {
            (line.get('meta') or {}).get('builder_line_code'):
                line['columns'][0]['value']
            for line in period['lines']
            if (line.get('meta') or {}).get('builder_line_code')
        }
        self.assertEqual(
            values['rounding_a'] - values['rounding_b'],
            values['rounding_subtotal'],
        )
        self.assertEqual(
            values['rounding_subtotal'],
            values['rounding_total'],
        )
        self.assertEqual(values['rounding_total'], -0.01)
        for code, value in values.items():
            self.assertEqual(period['totals'][code], value)

        baseline_meta = {
            (line.get('meta') or {}).get('builder_line_code'):
                line.get('meta') or {}
            for line in result['baseline']['lines']
            if (line.get('meta') or {}).get('builder_line_code')
        }
        self.assertEqual(
            baseline_meta['rounding_subtotal']['forecast_additive_terms'],
            [
                {'code': 'rounding_a', 'coefficient': 1},
                {'code': 'rounding_b', 'coefficient': -1},
            ],
        )
        self.assertEqual(
            baseline_meta['rounding_total']['forecast_additive_terms'],
            [{'code': 'rounding_subtotal', 'coefficient': 1}],
        )
        self.assertNotIn('formula', baseline_meta['rounding_total'])

    def test_builder_additive_metadata_is_signed_and_fails_closed(self):
        handler = self.env[
            'eh.account.dynamic.report.handler.builder'
        ]
        self.assertEqual(
            handler._forecast_additive_terms('revenue - cogs'),
            [
                {'code': 'revenue', 'coefficient': 1},
                {'code': 'cogs', 'coefficient': -1},
            ],
        )
        self.assertEqual(
            handler._forecast_additive_terms('-(revenue - cogs) + other'),
            [
                {'code': 'revenue', 'coefficient': -1},
                {'code': 'cogs', 'coefficient': 1},
                {'code': 'other', 'coefficient': 1},
            ],
        )
        for formula in (
            'revenue * 1',
            'revenue / 1',
            'revenue + 0.01',
            'revenue + revenue',
        ):
            self.assertIsNone(
                handler._forecast_additive_terms(formula), formula,
            )

    def test_rounding_allocation_recomputes_fixed_reference_variances(self):
        currency = self.env.ref('base.USD')
        baseline = {
            'currency': {'id': currency.id},
            'columns': [
                {'expression_label': 'amount', 'figure_type': 'monetary'},
                {
                    'expression_label': 'prior_amount',
                    'figure_type': 'monetary',
                },
                {'expression_label': 'variance', 'figure_type': 'monetary'},
                {'expression_label': 'budget', 'figure_type': 'monetary'},
                {
                    'expression_label': 'budget_variance',
                    'figure_type': 'monetary',
                },
            ],
            'lines': [
                {
                    'id': 'section-reference-header',
                    'columns': [],
                    'meta': {
                        'kind': 'section_header',
                        'section_id': 'reference',
                    },
                },
                *[
                    {
                        'id': 'reference-%s' % index,
                        'columns': [
                            {'expression_label': 'amount', 'value': 0.01},
                            {
                                'expression_label': 'prior_amount',
                                'value': 0.02,
                            },
                            {'expression_label': 'variance', 'value': -0.01},
                            {'expression_label': 'budget', 'value': 0.03},
                            {
                                'expression_label': 'budget_variance',
                                'value': -0.02,
                            },
                        ],
                        'meta': {'kind': 'section_line'},
                    }
                    for index in range(2)
                ],
                {
                    'id': 'section-reference-total',
                    'columns': [
                        {'expression_label': 'amount', 'value': 0.02},
                        {'expression_label': 'prior_amount', 'value': 0.04},
                        {'expression_label': 'variance', 'value': -0.02},
                        {'expression_label': 'budget', 'value': 0.06},
                        {
                            'expression_label': 'budget_variance',
                            'value': -0.04,
                        },
                    ],
                    'meta': {
                        'kind': 'section_total',
                        'section_id': 'reference',
                    },
                },
            ],
            'totals': {'reference': 0.02},
        }
        projected = self.Forecast._apply_factor(baseline, 0.5)
        leaf_cells = [
            {
                cell['expression_label']: cell['value']
                for cell in projected['lines'][index]['columns']
            }
            for index in (1, 2)
        ]
        self.assertEqual(
            sorted(cells['amount'] for cells in leaf_cells), [0.0, 0.01],
        )
        self.assertEqual(
            [cells['prior_amount'] for cells in leaf_cells], [0.02, 0.02],
        )
        self.assertEqual(
            [cells['budget'] for cells in leaf_cells], [0.03, 0.03],
        )
        for cells in leaf_cells:
            self.assertEqual(
                cells['variance'],
                currency.round(cells['amount'] - cells['prior_amount']),
            )
            self.assertEqual(
                cells['budget_variance'],
                currency.round(cells['amount'] - cells['budget']),
            )

    def test_hierarchical_section_reconciles_each_visible_level(self):
        currency = self.env.ref('base.USD')
        baseline = {
            'currency': {'id': currency.id},
            'columns': [{
                'expression_label': 'amount', 'figure_type': 'monetary',
            }],
            'lines': [
                {
                    'id': 'section-tree-header',
                    'columns': [],
                    'meta': {
                        'kind': 'section_header', 'section_id': 'tree',
                    },
                },
                {
                    'id': 'tree-group',
                    'parent_id': 'section-tree-header',
                    'level': 1,
                    'columns': [{
                        'expression_label': 'amount', 'value': 0.02,
                    }],
                    'meta': {'kind': 'account_group'},
                },
                *[
                    {
                        'id': 'tree-leaf-%s' % index,
                        'parent_id': 'tree-group',
                        'level': 2,
                        'columns': [{
                            'expression_label': 'amount', 'value': 0.01,
                        }],
                    }
                    for index in range(2)
                ],
                {
                    'id': 'section-tree-total',
                    'columns': [{
                        'expression_label': 'amount', 'value': 0.02,
                    }],
                    'meta': {
                        'kind': 'section_total', 'section_id': 'tree',
                    },
                },
            ],
            'totals': {'tree': 0.02},
        }
        projected = self.Forecast._apply_factor(baseline, 0.5)
        group_value = projected['lines'][1]['columns'][0]['value']
        leaf_total = sum(
            projected['lines'][index]['columns'][0]['value']
            for index in (2, 3)
        )
        section_total = projected['lines'][4]['columns'][0]['value']
        self.assertEqual(group_value, 0.01)
        self.assertTrue(currency.is_zero(leaf_total - group_value))
        self.assertTrue(currency.is_zero(group_value - section_total))

    def test_nested_sections_and_subtotal_relationships_stay_footed(self):
        currency = self.env.ref('base.USD')

        def line(line_id, value, kind, section_id=None):
            meta = {'kind': kind}
            if section_id:
                meta['section_id'] = section_id
            return {
                'id': line_id,
                'columns': [{
                    'expression_label': 'amount', 'value': value,
                }],
                'meta': meta,
            }

        baseline = {
            'currency': {'id': currency.id},
            'columns': [{
                'expression_label': 'amount', 'figure_type': 'monetary',
            }],
            'lines': [
                line(
                    'section-outer-header', '', 'section_header', 'outer',
                ),
                line(
                    'section-child-a-header', '', 'section_header',
                    'child-a',
                ),
                line('child-a-leaf', 0.01, 'section_line'),
                line(
                    'section-child-a-total', 0.01, 'section_total',
                    'child-a',
                ),
                line(
                    'section-child-b-header', '', 'section_header',
                    'child-b',
                ),
                line('child-b-leaf', 0.01, 'section_line'),
                line(
                    'section-child-b-total', 0.01, 'section_total',
                    'child-b',
                ),
                line(
                    'section-outer-total', 0.02, 'section_total', 'outer',
                ),
                line(
                    'section-cash-header', '', 'section_header', 'cash',
                ),
                line('cash-component-a', 0.01, 'section_line'),
                line('cash-component-b', 0.01, 'section_line'),
                line('cash-subtotal', 0.02, 'section_subtotal'),
                line('cash-disclosure', 0.01, 'disclosure_line'),
                line('cash-subtotal-later', 0.03, 'section_subtotal'),
                line('cash-disclosure-later', 0.01, 'disclosure_line'),
                line(
                    'section-cash-total', 0.04, 'section_total', 'cash',
                ),
            ],
            'totals': {'outer': 0.02, 'cash': 0.04},
        }
        projected = self.Forecast._apply_factor(baseline, 0.5)

        values = {
            line['id']: line['columns'][0]['value']
            for line in projected['lines']
            if line['columns'] and isinstance(
                line['columns'][0].get('value'), (int, float),
            )
        }

        self.assertTrue(currency.is_zero(
            values['child-a-leaf'] - values['section-child-a-total'],
        ))
        self.assertTrue(currency.is_zero(
            values['child-b-leaf'] - values['section-child-b-total'],
        ))
        self.assertTrue(currency.is_zero(
            values['section-child-a-total']
            + values['section-child-b-total']
            - values['section-outer-total'],
        ))
        self.assertTrue(currency.is_zero(
            values['cash-component-a'] + values['cash-component-b']
            - values['cash-subtotal'],
        ))
        self.assertTrue(currency.is_zero(
            values['cash-subtotal'] + values['cash-disclosure']
            - values['cash-subtotal-later'],
        ))
        self.assertTrue(currency.is_zero(
            values['cash-subtotal-later']
            + values['cash-disclosure-later']
            - values['section-cash-total'],
        ))

    def test_budget_reference_stays_fixed_and_variance_is_recomputed(self):
        baseline = {
            'columns': [
                {'expression_label': 'label', 'figure_type': 'string'},
                {'expression_label': 'amount', 'figure_type': 'monetary'},
                {'expression_label': 'budget', 'figure_type': 'monetary'},
                {
                    'expression_label': 'budget_variance',
                    'figure_type': 'monetary',
                },
            ],
            'lines': [{
                'id': 'net_profit',
                'columns': [
                    {'expression_label': 'amount', 'value': 1200.0},
                    {'expression_label': 'budget', 'value': 1000.0},
                    {'expression_label': 'budget_variance', 'value': 200.0},
                ],
            }],
            'totals': {
                'amount': 1200.0,
                'net_budget': 1000.0,
            },
        }
        projected = self.Forecast._apply_factor(baseline, 1.10)
        cells = {
            cell['expression_label']: cell['value']
            for cell in projected['lines'][0]['columns']
        }
        self.assertEqual(cells['amount'], 1320.0)
        self.assertEqual(cells['budget'], 1000.0)
        self.assertEqual(cells['budget_variance'], 320.0)
        self.assertEqual(projected['totals']['net_budget'], 1000.0)

    def test_axis_forecast_scales_current_money_not_historical_or_ratios(self):
        current = 'amount__period_current'
        comparison = 'amount__period_comparison_1'
        baseline = {
            'columns': [
                {'expression_label': current, 'figure_type': 'monetary'},
                {
                    'expression_label': comparison,
                    'figure_type': 'monetary',
                },
            ],
            'lines': [
                {
                    'id': 'exec-revenue',
                    'meta': {'metric': 'revenue'},
                    'columns': [
                        {
                            'expression_label': current,
                            'figure_type': 'monetary',
                            'value': 120.0,
                        },
                        {
                            'expression_label': comparison,
                            'figure_type': 'monetary',
                            'value': 80.0,
                        },
                    ],
                },
                {
                    'id': 'exec-current-ratio',
                    'meta': {'metric': 'current_ratio'},
                    'columns': [
                        {
                            'expression_label': current,
                            'figure_type': 'float',
                            'value': 2.0,
                        },
                        {
                            'expression_label': comparison,
                            'figure_type': 'float',
                            'value': 1.5,
                        },
                    ],
                },
            ],
            'totals': {
                current: 120.0,
                comparison: 80.0,
                'column_scopes': {
                    current: {'revenue': 120.0, 'current_ratio': 2.0},
                    comparison: {'revenue': 80.0, 'current_ratio': 1.5},
                },
            },
        }
        projected = self.Forecast._apply_factor(baseline, 1.10)
        values = {
            line['meta']['metric']: {
                cell['expression_label']: cell['value']
                for cell in line['columns']
            }
            for line in projected['lines']
        }
        self.assertEqual(values['revenue'][current], 132.0)
        self.assertEqual(values['revenue'][comparison], 80.0)
        self.assertEqual(values['current_ratio'][current], 2.0)
        self.assertEqual(values['current_ratio'][comparison], 1.5)
        self.assertEqual(projected['totals'][current], 132.0)
        self.assertEqual(projected['totals'][comparison], 80.0)
        self.assertEqual(
            projected['totals']['column_scopes'][current],
            {'revenue': 132.0, 'current_ratio': 2.0},
        )
        self.assertEqual(
            projected['totals']['column_scopes'][comparison],
            {'revenue': 80.0, 'current_ratio': 1.5},
        )

    def test_axis_rounding_reconciles_visible_and_semantic_totals(self):
        expression = 'amount__period_current'
        baseline = {
            'columns': [{
                'expression_label': expression,
                'figure_type': 'monetary',
            }],
            'lines': [
                {
                    'id': 'section-income-total',
                    'meta': {
                        'kind': 'section_total', 'section_id': 'income',
                    },
                    'columns': [{
                        'expression_label': expression,
                        'figure_type': 'monetary', 'value': 0.01,
                    }],
                },
                {
                    'id': 'section-expenses-total',
                    'meta': {
                        'kind': 'section_total', 'section_id': 'expenses',
                    },
                    'columns': [{
                        'expression_label': expression,
                        'figure_type': 'monetary', 'value': 0.02,
                    }],
                },
                {
                    'id': 'net_profit',
                    'columns': [{
                        'expression_label': expression,
                        'figure_type': 'monetary', 'value': -0.01,
                    }],
                },
            ],
            'totals': {
                'income': 0.01,
                'expenses': 0.02,
                'net_profit': -0.01,
                'amount': -0.01,
                expression: -0.01,
                'column_scopes': {
                    expression: {
                        'income': 0.01,
                        'expenses': 0.02,
                        'net_profit': -0.01,
                    },
                },
            },
        }
        projected = self.Forecast._apply_factor(baseline, 0.5)
        visible = {
            line['id']: line['columns'][0]['value']
            for line in projected['lines']
        }
        scoped = projected['totals']['column_scopes'][expression]
        self.assertEqual(scoped['income'], visible['section-income-total'])
        self.assertEqual(
            scoped['expenses'], visible['section-expenses-total'],
        )
        self.assertEqual(scoped['net_profit'], visible['net_profit'])
        self.assertEqual(projected['totals']['income'], scoped['income'])
        self.assertEqual(projected['totals']['expenses'], scoped['expenses'])
        self.assertEqual(projected['totals']['net_profit'], scoped['net_profit'])
        self.assertEqual(projected['totals'][expression], scoped['net_profit'])

    def test_supported_overrides_survive_but_scope_is_forced(self):
        forecast = self._make_forecast()
        options = forecast._build_base_options({
            'date': {'date_from': '1900-01-01', 'date_to': '1900-01-02'},
            'company_ids': [999999],
            'primary_company_id': 999999,
            'comparison': 'previous_year',
            'comparison_number': 2,
            'pnl_presentation': 'by_function',
            'account_type_ids': ['income'],
            'analytic_account_ids': [11],
            'presentation_currency_id': 7,
            'show_annotations': False,
            'lazy_expand': True,
            '_cache_context': {'uid': 1},
        })
        self.assertEqual(options['company_ids'], [forecast.company_id.id])
        self.assertEqual(
            options['primary_company_id'], forecast.company_id.id,
        )
        self.assertEqual(options['date'], {
            'mode': 'range',
            'date_from': '2026-06-01',
            'date_to': '2026-06-30',
        })
        self.assertEqual(options['comparison'], 'previous_year')
        self.assertEqual(options['comparison_number'], 2)
        self.assertEqual(options['pnl_presentation'], 'by_function')
        self.assertEqual(options['account_type_ids'], ['income'])
        self.assertEqual(options['analytic_account_ids'], [11])
        self.assertEqual(options['presentation_currency_id'], 7)
        self.assertFalse(options['show_annotations'])
        self.assertTrue(options['eager_expand'])
        self.assertNotIn('lazy_expand', options)
        self.assertNotIn('_cache_context', options)

    # ---- meta ----

    def test_meta_carries_input_parameters(self):
        forecast = self._make_forecast(
            scenario_label="Optimistic",
            growth_method='compound',
            monthly_growth_pct=7.5,
            horizon_months=4,
        )
        meta = forecast.project()['meta']
        self.assertEqual(meta['scenario_label'], "Optimistic")
        self.assertEqual(meta['horizon_months'], 4)
        self.assertEqual(meta['growth_method'], 'compound')
        self.assertEqual(meta['monthly_growth_pct'], 7.5)
        self.assertEqual(meta['period_granularity'], 'calendar_month')
        self.assertEqual(meta['growth_scope'], 'monetary_only')

    def test_last_run_stamped(self):
        forecast = self._make_forecast(horizon_months=1)
        self.assertFalse(forecast.last_run)
        forecast.project()
        self.assertTrue(forecast.last_run)

    def test_action_returns_transient_json_without_attachment_blob(self):
        forecast = self._make_forecast(horizon_months=1)
        payload = {
            'baseline': {'columns': [], 'lines': [], 'totals': {}},
            'periods': [{'period_index': 1}],
            'meta': {'forecast_id': forecast.id},
        }
        Attachment = self.env['ir.attachment']
        domain = [('res_model', '=', 'eh.report.forecast.result')]
        before = Attachment.search_count(domain)
        with patch.object(type(forecast), 'project', return_value=payload):
            action = forecast.action_project_now()
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], 'eh.report.forecast.result')
        self.assertEqual(action['target'], 'new')
        result_wizard = self.env['eh.report.forecast.result'].browse(
            action['res_id'],
        )
        self.assertTrue(result_wizard.exists())
        decoded = json.loads(base64.b64decode(result_wizard.file_data))
        self.assertEqual(decoded, payload)
        self.assertEqual(Attachment.search_count(domain), before)

        other = new_test_user(
            self.env,
            login='eh_forecast_result_other',
            groups='base.group_user,eh_account_base.group_eh_user',
            company_id=self.company.id,
        )
        with self.assertRaises(AccessError):
            result_wizard.with_user(other)._eh_check_access('read')

    def test_horizon_above_resource_limit_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._make_forecast(horizon_months=25)

    def test_legacy_unsafe_horizon_fails_before_render(self):
        forecast = self._make_forecast(horizon_months=1)
        self.env.cr.execute(
            "ALTER TABLE eh_report_forecast DROP CONSTRAINT "
            "eh_report_forecast_bounded_horizon"
        )
        self.env.cr.execute(
            "UPDATE eh_report_forecast SET horizon_months = 25 "
            "WHERE id = %s",
            [forecast.id],
        )
        forecast.invalidate_recordset(['horizon_months'])
        with patch.object(
            type(forecast.base_report_id),
            'render',
        ) as render:
            with self.assertRaises(UserError):
                forecast.project()
            render.assert_not_called()
