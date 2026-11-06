# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Balance Sheet handler tests.

Covers:

* Assets section sums asset accounts (signed positive).
* Liabilities section flips credit balances to positive display.
* Equity section flips credit balances to positive display.
* Current Year Earnings reflects net of income + expense up to date_to.
* Balance Check is zero for a balanced ledger across multiple scenarios.
* Cumulative date filter: only entries with date <= date_to count.
* Future entries excluded.
* Zero balance accounts hidden by default.
* posted_only excludes draft entries; setting it false includes them.
* Cancelled entries excluded.
* Account, journal, partner filters narrow the result set.
* Missing date_to raises a UserError.
* Orchestrator render works and respects the cache.
* Drill down works for account lines, returns None for section/computed
  markers.
* XLSX export produces a valid workbook.
"""

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_dynamic_reports', 'integration', 'post_install', '-at_install')
class TestBalanceSheetHandler(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env[
            'eh.account.dynamic.report.handler.balance_sheet'
        ]
        cls.report = cls.env['eh.account.dynamic.report'].search(
            [('code', '=', 'balance_sheet')], limit=1,
        )
        if not cls.report:
            cls.report = cls.env['eh.account.dynamic.report'].create({
                'code': 'balance_sheet',
                'name': 'Balance Sheet',
                'handler_model':
                    'eh.account.dynamic.report.handler.balance_sheet',
            })

    def setUp(self):
        super().setUp()
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
        }

    def _post_in_period(self, lines):
        return self.post_balanced_move(
            lines, date=fields.Date.from_string('2026-06-15'),
        )

    def test_column_axis_capability_contract_owns_both_axes(self):
        self.assertEqual(
            self.handler._EH_COLUMN_AXIS_CAPABILITIES,
            frozenset({'comparison', 'analytic_columns'}),
        )
        normalized = self.handler.normalize_options(dict(
            self.options,
            comparison='previous_period',
            comparison_number=2,
        ))
        self.assertEqual(normalized['comparison_number'], 2)

    def _set_presentation_rate(self, currency, date, rate):
        existing = self.env['res.currency.rate'].search([
            ('currency_id', '=', currency.id),
            ('company_id', '=', self.company.id),
            ('name', '=', date),
        ], limit=1)
        if existing:
            existing.rate = rate
            return existing
        return self.env['res.currency.rate'].create({
            'currency_id': currency.id,
            'company_id': self.company.id,
            'name': date,
            'rate': rate,
        })

    @staticmethod
    def _line_by_id(result, line_id):
        for line in result['lines']:
            if line['id'] == line_id:
                return line
        return None

    @staticmethod
    def _amount(line):
        if line is None:
            return None
        for col in line['columns']:
            if col['expression_label'] == 'amount':
                return col['value']
        return None

    # ---- presentation currency (central translation) ----

    def test_presentation_currency_translates_balance_sheet(self):
        currency = self.env['res.currency'].create({
            'name': 'ZZB', 'symbol': 'B', 'rounding': 0.01})
        self.env['res.currency.rate'].create({
            'currency_id': currency.id, 'name': '2026-01-01',
            'rate': 2.0, 'company_id': self.company.id})
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 1000.0},
            {'account': self.account_cash, 'debit': 1000.0},
        ])
        result = self.report.render(
            dict(self.options, presentation_currency_id=currency.id),
            use_cache=False)
        # Assets 1000 company * rate 2.0 = 2000 presentation currency.
        self.assertAlmostEqual(result['totals']['assets'], 2000.0, places=2)
        self.assertEqual(result['currency']['id'], currency.id)

    def test_comparison_uses_each_snapshot_closing_spot_rate(self):
        currency = self.env['res.currency'].create({
            'name': 'ZYE', 'symbol': 'E', 'rounding': 0.01,
        })
        self._set_presentation_rate(currency, '2025-01-01', 2.0)
        self._set_presentation_rate(currency, '2026-01-01', 4.0)
        for date in ('2025-06-15', '2026-06-15'):
            self.post_balanced_move([
                {'account': self.account_equity, 'credit': 100.0},
                {'account': self.account_cash, 'debit': 100.0},
            ], date=fields.Date.from_string(date))

        result = self.handler.compute(dict(
            self.options,
            presentation_currency_id=currency.id,
            comparison='previous_year',
        ))
        assets = self._line_by_id(result, 'section-assets-total')
        values = {
            column['expression_label']: column['value']
            for column in assets['columns']
        }
        # Snapshot policy translates full as-of balances at each close:
        # current 200 * 4, prior 100 * 2.
        self.assertEqual(values['amount'], 800.0)
        self.assertEqual(values['prior_amount'], 200.0)
        periods = result['meta']['currency_translation_periods']
        self.assertEqual(
            [period['rate_dates'][str(self.company.id)] for period in periods],
            ['2026-01-01', '2025-01-01'],
        )
        self.assertEqual(
            result['meta']['currency_translation_policy'], 'closing_spot',
        )

    def test_previous_period_is_prior_period_end_not_yesterday(self):
        self.post_balanced_move([
            {'account': self.account_equity, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ], date=fields.Date.from_string('2025-12-31'))
        self.post_balanced_move([
            {'account': self.account_equity, 'credit': 50.0},
            {'account': self.account_cash, 'debit': 50.0},
        ], date=fields.Date.from_string('2026-06-30'))

        result = self.handler.compute(dict(
            self.options,
            comparison='previous_period',
        ))
        assets = self._line_by_id(result, 'section-assets-total')
        values = {
            column['expression_label']: column['value']
            for column in assets['columns']
        }
        self.assertEqual(result['meta']['prior_date_to'], '2025-12-31')
        self.assertEqual(values['amount'], 150.0)
        self.assertEqual(values['prior_amount'], 100.0)

    def test_custom_comparison_is_cumulative_as_of_custom_end(self):
        self.post_balanced_move([
            {'account': self.account_equity, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ], date=fields.Date.from_string('2024-01-15'))
        self.post_balanced_move([
            {'account': self.account_equity, 'credit': 50.0},
            {'account': self.account_cash, 'debit': 50.0},
        ], date=fields.Date.from_string('2024-06-15'))
        self.post_balanced_move([
            {'account': self.account_equity, 'credit': 25.0},
            {'account': self.account_cash, 'debit': 25.0},
        ], date=fields.Date.from_string('2026-06-15'))

        result = self.handler.compute(dict(
            self.options,
            comparison='custom',
            comparison_custom_date_from='2024-06-01',
            comparison_custom_date_to='2024-06-30',
        ))
        value_columns = result['columns'][1:]
        self.assertEqual(len(value_columns), 2)
        self.assertTrue(all(column.get('scope') for column in value_columns))
        self.assertEqual(
            [column['scope']['date_to'] for column in value_columns],
            ['2026-12-31', '2024-06-30'],
        )
        assets = self._line_by_id(result, 'section-assets-total')
        # Snapshot ignores custom date_from. January + June both contribute
        # to the custom as-of column; current additionally includes 2026.
        self.assertEqual(
            [column['value'] for column in assets['columns']],
            [175.0, 150.0],
        )

    def test_n_period_snapshot_order_can_be_ascending(self):
        for date, amount in (
            ('2024-06-15', 100.0),
            ('2025-06-15', 50.0),
            ('2026-06-15', 25.0),
        ):
            self.post_balanced_move([
                {'account': self.account_equity, 'credit': amount},
                {'account': self.account_cash, 'debit': amount},
            ], date=fields.Date.from_string(date))
        result = self.handler.compute(dict(
            self.options,
            comparison='previous_year',
            comparison_number=2,
            comparison_order='ascending',
        ))
        value_columns = result['columns'][1:]
        self.assertEqual(
            [column['scope']['date_to'] for column in value_columns],
            ['2024-12-31', '2025-12-31', '2026-12-31'],
        )
        assets = self._line_by_id(result, 'section-assets-total')
        self.assertEqual(
            [column['value'] for column in assets['columns']],
            [100.0, 150.0, 175.0],
        )

    def test_analytic_account_columns_keep_balance_and_independent_total(self):
        if 'account.analytic.plan' not in self.env:
            self.skipTest("Analytic addon not installed in this build.")
        plan = self.env['account.analytic.plan'].create({
            'name': 'Balance Sheet column plan',
        })
        analytic_a = self.env['account.analytic.account'].create({
            'name': 'BS Analytic A', 'plan_id': plan.id,
        })
        analytic_b = self.env['account.analytic.account'].create({
            'name': 'BS Analytic B', 'plan_id': plan.id,
        })
        distribution = {
            str(analytic_a.id): 60.0,
            str(analytic_b.id): 40.0,
        }
        self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': fields.Date.from_string('2026-06-15'),
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_cash.id,
                    'debit': 100.0,
                    'analytic_distribution': distribution,
                }),
                (0, 0, {
                    'account_id': self.account_equity.id,
                    'credit': 100.0,
                    'analytic_distribution': distribution,
                }),
            ],
        }).action_post()
        axis_options = dict(
            self.options,
            hierarchical_groups=False,
            analytic_column_account_ids=[analytic_a.id, analytic_b.id],
        )
        result = self.report.render(axis_options, use_cache=False)
        assets = self._line_by_id(result, 'section-assets-total')
        balance_check = self._line_by_id(result, 'balance_check')
        self.assertEqual(
            [column['value'] for column in assets['columns']],
            [60.0, 40.0, 100.0],
        )
        self.assertEqual(
            [column['value'] for column in balance_check['columns']],
            [0.0, 0.0, 0.0],
        )

        cash = self._line_by_id(
            result, 'account-%d' % self.account_cash.id,
        )
        value_columns = result['columns'][1:]
        clicked_cell = cash['columns'][0]['value']
        detail = self.report.get_analytic_column_drilldown_page(
            axis_options,
            cash['id'],
            value_columns[0]['expression_label'],
            offset=0,
            limit=80,
            execution_id=result['execution_id'],
            displayed_amount=clicked_cell,
        )
        self.assertEqual(detail['scope']['date_from'], '0001-01-01')
        self.assertEqual(detail['scope'], value_columns[0]['scope'])
        self.assertAlmostEqual(detail['total'], clicked_cell, places=2)
        self.assertAlmostEqual(
            sum(row['values']['allocated_amount'] for row in detail['rows']),
            clicked_cell,
            places=2,
        )
        self.assertEqual(detail['total_count'], 1)
        presentation = self.env['res.currency'].create({
            'name': 'ZBD', 'symbol': 'B', 'rounding': 0.01,
        })
        self._set_presentation_rate(presentation, '2026-01-01', 2.0)
        converted_options = dict(
            axis_options, presentation_currency_id=presentation.id,
        )
        converted = self.report.render(converted_options, use_cache=False)
        converted_cash = self._line_by_id(
            converted, 'account-%d' % self.account_cash.id,
        )
        converted_detail = self.report.get_analytic_column_drilldown_page(
            converted_options,
            converted_cash['id'],
            converted['columns'][1]['expression_label'],
            execution_id=converted['execution_id'],
            displayed_amount=converted_cash['columns'][0]['value'],
        )
        self.assertEqual(converted_detail['currency']['id'], presentation.id)
        self.assertAlmostEqual(
            converted_detail['total'],
            converted_cash['columns'][0]['value'],
            places=2,
        )
        self.assertAlmostEqual(converted_detail['total'], 120.0, places=2)
        with self.assertRaises(UserError):
            self.report.get_analytic_column_drilldown_page(
                axis_options,
                'balance_check',
                value_columns[0]['expression_label'],
            )

    def test_analytic_columns_weight_current_year_earnings(self):
        if 'account.analytic.plan' not in self.env:
            self.skipTest("Analytic addon not installed in this build.")
        plan = self.env['account.analytic.plan'].create({
            'name': 'Earnings column plan',
        })
        analytic_a = self.env['account.analytic.account'].create({
            'name': 'Earnings Analytic A', 'plan_id': plan.id,
        })
        analytic_b = self.env['account.analytic.account'].create({
            'name': 'Earnings Analytic B', 'plan_id': plan.id,
        })
        distribution = {
            str(analytic_a.id): 60.0,
            str(analytic_b.id): 40.0,
        }
        income_account = getattr(self, 'account_revenue', None)
        if income_account is None:
            income_account = self.account_income
        self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': fields.Date.from_string('2026-06-16'),
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_cash.id,
                    'debit': 100.0,
                    'analytic_distribution': distribution,
                }),
                (0, 0, {
                    'account_id': income_account.id,
                    'credit': 100.0,
                    'analytic_distribution': distribution,
                }),
            ],
        }).action_post()
        result = self.handler.compute(dict(
            self.options,
            hierarchical_groups=False,
            analytic_column_account_ids=[analytic_a.id, analytic_b.id],
        ))
        earnings = self._line_by_id(result, 'current_year_earnings')
        balance_check = self._line_by_id(result, 'balance_check')
        self.assertEqual(
            [column['value'] for column in earnings['columns']],
            [60.0, 40.0, 100.0],
        )
        self.assertEqual(
            [column['value'] for column in balance_check['columns']],
            [0.0, 0.0, 0.0],
        )

    # ---- core math ----

    def test_kwd_precision_preserves_lines_totals_and_half_step_tie(self):
        kwd = self.env.ref('base.KWD')
        self.assertEqual(kwd.decimal_places, 3)
        self._set_presentation_rate(kwd, '2025-01-01', 1.0)

        def fake_totals(handler, account_types=None, **kwargs):
            account_types = tuple(account_types or ())
            if account_types == handler.CURRENT_ASSET_TYPES:
                return [{
                    'account_id': self.account_cash.id,
                    'account_code': self.account_cash.code,
                    'account_name': self.account_cash.name,
                    'amount': 1.2345,
                }]
            if account_types == handler.CURRENT_LIABILITY_TYPES:
                return [{
                    'account_id': self.account_payable.id,
                    'account_code': self.account_payable.code,
                    'account_name': self.account_payable.name,
                    'amount': 1.2335,
                }]
            return []

        self.patch(
            type(self.handler), '_fetch_grouped_account_totals',
            fake_totals,
        )
        self.patch(
            type(self.handler), '_fetch_fiscal_earnings',
            lambda handler, **kwargs: (0.0005, 0.0),
        )
        result = self.handler.compute(dict(
            self.options,
            hierarchical_groups=False,
            presentation_currency_id=kwd.id,
        ))

        cash = self._line_by_id(
            result, 'account-%d' % self.account_cash.id)
        assets = self._line_by_id(result, 'section-assets-total')
        current_earnings = self._line_by_id(
            result, 'current_year_earnings')
        balance_check = self._line_by_id(result, 'balance_check')
        self.assertEqual(self._amount(cash), 1.235)
        self.assertEqual(self._amount(assets), 1.235)
        self.assertEqual(self._amount(current_earnings), 0.001)
        self.assertEqual(self._amount(balance_check), 0.0)
        self.assertEqual(result['totals']['assets'], 1.235)
        self.assertEqual(result['totals']['total_equity'], 0.001)
        self.assertEqual(result['totals']['balance_check'], 0.0)

    def test_income_post_yields_assets_and_current_year_earnings(self):
        # Cash debited (asset up), revenue credited (income up). The Balance
        # Sheet should show cash in Assets and the same value in Current
        # Year Earnings.
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 1000.0},
            {'account': self.account_cash, 'debit': 1000.0},
        ])
        result = self.handler.compute(self.options)
        self.assertAlmostEqual(result['totals']['assets'], 1000.0, places=2)
        self.assertAlmostEqual(
            result['totals']['current_year_earnings'], 1000.0, places=2,
        )
        self.assertAlmostEqual(result['totals']['balance_check'], 0.0, places=2)

    def test_cash_basis_partial_settlement_keeps_balance_check_zero(self):
        invoice = self.post_balanced_move([
            {'account': self.account_receivable, 'debit': 100.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 100.0},
        ], date=fields.Date.from_string('2026-05-01'))
        payment = self.post_balanced_move([
            {'account': self.account_cash, 'debit': 40.0},
            {'account': self.account_receivable, 'credit': 40.0,
             'partner': self.partner_a},
        ], date=fields.Date.from_string('2026-06-01'))
        (invoice.line_ids | payment.line_ids).filtered(
            lambda line: line.account_id == self.account_receivable
        ).reconcile()

        result = self.handler.compute(dict(self.options, cash_basis=True))
        self.assertAlmostEqual(
            result['totals']['current_year_earnings'], 40.0, places=2)
        self.assertAlmostEqual(result['totals']['assets'], 40.0, places=2)
        self.assertAlmostEqual(result['totals']['balance_check'], 0.0, places=2)

    def test_loan_creates_liabilities(self):
        # Cash debited (asset up), payable credited (liability up).
        self._post_in_period([
            {'account': self.account_payable, 'credit': 500.0},
            {'account': self.account_cash, 'debit': 500.0},
        ])
        result = self.handler.compute(self.options)
        self.assertAlmostEqual(result['totals']['assets'], 500.0, places=2)
        self.assertAlmostEqual(
            result['totals']['liabilities'], 500.0, places=2,
        )
        self.assertAlmostEqual(result['totals']['balance_check'], 0.0, places=2)

    def test_equity_injection(self):
        # Cash debited (asset up), equity credited (owner contribution).
        self._post_in_period([
            {'account': self.account_equity, 'credit': 2000.0},
            {'account': self.account_cash, 'debit': 2000.0},
        ])
        result = self.handler.compute(self.options)
        self.assertAlmostEqual(result['totals']['assets'], 2000.0, places=2)
        self.assertAlmostEqual(result['totals']['equity'], 2000.0, places=2)
        self.assertAlmostEqual(result['totals']['balance_check'], 0.0, places=2)

    def test_expense_post_reduces_current_year_earnings(self):
        self._post_in_period([
            {'account': self.account_expense, 'debit': 250.0},
            {'account': self.account_cash, 'credit': 250.0},
        ])
        result = self.handler.compute(self.options)
        self.assertAlmostEqual(
            result['totals']['assets'], -250.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['current_year_earnings'], -250.0, places=2,
        )
        self.assertAlmostEqual(result['totals']['balance_check'], 0.0, places=2)

    def test_balance_check_zero_for_complex_ledger(self):
        # A combination of income, expense, equity, and liability postings.
        self._post_in_period([
            {'account': self.account_equity, 'credit': 5000.0},
            {'account': self.account_cash, 'debit': 5000.0},
        ])
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 2000.0},
            {'account': self.account_cash, 'debit': 2000.0},
        ])
        self._post_in_period([
            {'account': self.account_expense, 'debit': 500.0},
            {'account': self.account_cash, 'credit': 500.0},
        ])
        self._post_in_period([
            {'account': self.account_payable, 'credit': 1000.0},
            {'account': self.account_cash, 'debit': 1000.0},
        ])
        result = self.handler.compute(self.options)
        self.assertAlmostEqual(result['totals']['balance_check'], 0.0, places=2)
        # Sanity: each section is the value we expect.
        self.assertAlmostEqual(result['totals']['assets'], 7500.0, places=2)
        self.assertAlmostEqual(result['totals']['liabilities'], 1000.0, places=2)
        self.assertAlmostEqual(result['totals']['equity'], 5000.0, places=2)
        self.assertAlmostEqual(
            result['totals']['current_year_earnings'], 1500.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['total_equity_liabilities'], 7500.0, places=2,
        )

    # ---- date filtering ----

    def test_cumulative_date_filter_includes_prior_periods(self):
        # An entry from before the period should still contribute, because
        # the Balance Sheet is cumulative up to date_to.
        self.post_balanced_move(
            [
                {'account': self.account_equity, 'credit': 1000.0},
                {'account': self.account_cash, 'debit': 1000.0},
            ],
            date=fields.Date.from_string('2025-12-15'),
        )
        result = self.handler.compute(self.options)
        self.assertAlmostEqual(result['totals']['assets'], 1000.0, places=2)
        self.assertAlmostEqual(result['totals']['equity'], 1000.0, places=2)

    def test_prior_and_current_fiscal_earnings_are_separate_and_balance(self):
        self.post_balanced_move(
            [
                {'account': self.account_revenue, 'credit': 1000.0},
                {'account': self.account_cash, 'debit': 1000.0},
            ],
            date=fields.Date.from_string('2025-12-15'),
        )
        self.post_balanced_move(
            [
                {'account': self.account_revenue, 'credit': 250.0},
                {'account': self.account_cash, 'debit': 250.0},
            ],
            date=fields.Date.from_string('2026-06-15'),
        )
        result = self.handler.compute(self.options)
        self.assertAlmostEqual(
            result['totals']['previous_year_earnings'], 1000.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['current_year_earnings'], 250.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['total_equity'], 1250.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['total_equity_liabilities'], 1250.0, places=2,
        )
        self.assertAlmostEqual(result['totals']['balance_check'], 0.0, places=2)

    def test_future_entries_excluded(self):
        self.post_balanced_move(
            [
                {'account': self.account_equity, 'credit': 9999.0},
                {'account': self.account_cash, 'debit': 9999.0},
            ],
            date=fields.Date.from_string('2027-01-15'),
        )
        result = self.handler.compute(self.options)
        self.assertAlmostEqual(result['totals']['assets'], 0.0, places=2)
        self.assertAlmostEqual(result['totals']['equity'], 0.0, places=2)

    # ---- structural / display ----

    def test_section_structure_present(self):
        self._post_in_period([
            {'account': self.account_equity, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        result = self.handler.compute(self.options)
        kinds = [
            (line.get('meta') or {}).get('kind')
            for line in result['lines']
        ]
        # IAS 1.60/66 current vs non-current presentation:
        # Headers: Assets, Current Assets, Non-Current Assets, Liabilities,
        #   Current Liabilities, Non-Current Liabilities, Equity = 7.
        # Totals: Total Current Assets, Total Non-Current Assets,
        #   Total Assets, Total Current Liabilities,
        #   Total Non-Current Liabilities, Total Liabilities,
        #   Total Equity = 7.
        self.assertEqual(kinds.count('section_header'), 7)
        self.assertEqual(kinds.count('section_total'), 7)
        self.assertIn('current_year_earnings', kinds)
        self.assertIn('balance_check', kinds)

    def test_current_non_current_split_lines_present(self):
        # The new IAS 1 subsections and subtotals must be emitted with
        # their fresh stable line ids, and each subtotal must equal the
        # matching totals[] entry.
        self._post_in_period([
            {'account': self.account_equity, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        result = self.handler.compute(self.options)
        for line_id in (
            'section-assets_current-header',
            'section-assets_current-total',
            'section-assets_non_current-header',
            'section-assets_non_current-total',
            'section-liabilities_current-header',
            'section-liabilities_current-total',
            'section-liabilities_non_current-header',
            'section-liabilities_non_current-total',
        ):
            self.assertIsNotNone(
                self._line_by_id(result, line_id),
                "missing line %s" % line_id,
            )
        # The grand-total lids that tests / downstream rely on stay put.
        self.assertIsNotNone(
            self._line_by_id(result, 'section-assets-total'))
        self.assertIsNotNone(
            self._line_by_id(result, 'section-liabilities-total'))

    def test_current_plus_non_current_equals_grand_total(self):
        # Current Assets sit in cash/receivable; a fixed asset lands in
        # Non-Current. Both subtotals must sum to the grand Total Assets,
        # and the sheet must still balance.
        self._post_in_period([
            {'account': self.account_equity, 'credit': 3000.0},
            {'account': self.account_cash, 'debit': 3000.0},
        ])
        self._post_in_period([
            {'account': self.account_payable, 'credit': 800.0},
            {'account': self.account_cash, 'debit': 800.0},
        ])
        fixed_asset = self._ensure_account(
            self.env, '1605', 'Reporting Fixture Fixed Asset', 'asset_fixed')
        self._post_in_period([
            {'account': fixed_asset, 'debit': 1200.0},
            {'account': self.account_equity, 'credit': 1200.0},
        ])
        result = self.handler.compute(self.options)
        totals = result['totals']
        self.assertAlmostEqual(totals['non_current_assets'], 1200.0, places=2)
        # Total Current Assets + Total Non-Current Assets == Total Assets.
        self.assertAlmostEqual(
            totals['current_assets'] + totals['non_current_assets'],
            totals['assets'], places=2,
        )
        self.assertAlmostEqual(
            totals['current_liabilities'] + totals['non_current_liabilities'],
            totals['liabilities'], places=2,
        )
        # The displayed subtotal lines agree with the totals dict.
        self.assertAlmostEqual(
            self._amount(self._line_by_id(
                result, 'section-assets_current-total')),
            totals['current_assets'], places=2,
        )
        self.assertAlmostEqual(
            self._amount(self._line_by_id(
                result, 'section-assets_non_current-total')),
            totals['non_current_assets'], places=2,
        )
        self.assertAlmostEqual(
            self._amount(self._line_by_id(result, 'section-assets-total')),
            totals['assets'], places=2,
        )
        # Sheet still balances.
        self.assertAlmostEqual(totals['balance_check'], 0.0, places=2)

    def test_zero_balance_account_hidden_by_default(self):
        self._post_in_period([
            {'account': self.account_equity, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        result = self.handler.compute(self.options)
        # Receivable account had no activity, so should be absent.
        codes = {
            line['meta'].get('account_code')
            for line in result['lines']
            if (line.get('meta') or {}).get('account_code')
        }
        self.assertNotIn('1100', codes)

    # ---- state filtering ----

    def test_posted_only_excludes_draft(self):
        self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': '2026-06-15',
            'line_ids': [
                (0, 0, {'account_id': self.account_equity.id, 'credit': 999.0}),
                (0, 0, {'account_id': self.account_cash.id, 'debit': 999.0}),
            ],
        })
        result = self.handler.compute(self.options)
        self.assertAlmostEqual(result['totals']['assets'], 0.0, places=2)
        self.assertAlmostEqual(result['totals']['equity'], 0.0, places=2)

    def test_posted_only_false_includes_draft(self):
        self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': '2026-06-15',
            'line_ids': [
                (0, 0, {'account_id': self.account_equity.id, 'credit': 333.0}),
                (0, 0, {'account_id': self.account_cash.id, 'debit': 333.0}),
            ],
        })
        opts = dict(self.options)
        opts['posted_only'] = False
        result = self.handler.compute(opts)
        self.assertAlmostEqual(result['totals']['assets'], 333.0, places=2)
        self.assertAlmostEqual(result['totals']['equity'], 333.0, places=2)

    def test_cancelled_excluded(self):
        move = self._post_in_period([
            {'account': self.account_equity, 'credit': 444.0},
            {'account': self.account_cash, 'debit': 444.0},
        ])
        move.button_cancel()
        result = self.handler.compute(self.options)
        self.assertAlmostEqual(result['totals']['assets'], 0.0, places=2)
        self.assertAlmostEqual(result['totals']['equity'], 0.0, places=2)

    # ---- error handling ----

    def test_missing_date_to_raises(self):
        bad = dict(self.options)
        bad['date'] = {'date_from': '2026-01-01'}
        with self.assertRaises(UserError):
            self.handler.compute(bad)

    def test_missing_date_block_raises(self):
        bad = dict(self.options)
        bad.pop('date')
        with self.assertRaises(UserError):
            self.handler.compute(bad)

    # ---- orchestrator wiring ----

    def test_orchestrator_renders(self):
        self._post_in_period([
            {'account': self.account_equity, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        result = self.report.render(self.options)
        self.assertFalse(result['from_cache'])
        self.assertIn('execution_id', result)
        self.assertGreater(len(result['lines']), 0)

    def test_orchestrator_cache_hit_on_second_render(self):
        self._post_in_period([
            {'account': self.account_equity, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        first = self.report.render(self.options)
        second = self.report.render(self.options)
        self.assertFalse(first['from_cache'])
        self.assertTrue(second['from_cache'])
        self.assertEqual(first['totals'], second['totals'])

    # ---- drill down ----

    def test_drilldown_for_account_line(self):
        move = self.post_balanced_move([
            {'account': self.account_equity, 'credit': 75.0},
            {'account': self.account_cash, 'debit': 75.0},
        ], date=fields.Date.from_string('2025-12-15'))
        action = self.handler.get_drilldown_action(
            self.options, "account-%s" % self.account_cash.id,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action['res_model'], 'account.move.line')
        items = self.env['account.move.line'].search(action['domain'])
        cash_line = move.line_ids.filtered(
            lambda line: line.account_id == self.account_cash
        )
        self.assertIn(cash_line.id, items.ids)
        self.assertIn(('date', '>=', '0001-01-01'), action['domain'])

    def test_drilldown_returns_none_for_section_marker(self):
        self.assertIsNone(self.handler.get_drilldown_action(
            self.options, 'section-assets-header',
        ))
        self.assertIsNone(self.handler.get_drilldown_action(
            self.options, 'section-liabilities-total',
        ))
        self.assertIsNone(self.handler.get_drilldown_action(
            self.options, 'current_year_earnings',
        ))
        self.assertIsNone(self.handler.get_drilldown_action(
            self.options, 'balance_check',
        ))

    # ---- XLSX export ----

    def test_xlsx_export_renders_workbook(self):
        self._post_in_period([
            {'account': self.account_equity, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        content = self.report.render_xlsx(self.options)
        self.assertEqual(content[:2], b'PK')
        self.assertGreater(len(content), 1000,
                           "XLSX should contain meaningful content")
