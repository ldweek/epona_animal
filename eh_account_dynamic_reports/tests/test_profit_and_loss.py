# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Profit and Loss handler tests.

Covers:

* Income only entries: net profit equals income amount.
* Expense only entries: net profit equals minus expense amount.
* Mixed: net profit equals income minus expenses.
* Section structure: header, account lines, section total per section.
* Net Profit row sits at the bottom.
* Zero balance accounts hidden by default.
* posted_only excludes draft entries; setting it false includes them.
* Cancelled entries excluded.
* Out of period entries not included.
* Account, journal, partner filters narrow the result set.
* Missing date raises a UserError.
* Orchestrator render works and respects the cache.
* Drill down works for account lines, returns None for section markers.
"""

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_dynamic_reports', 'integration', 'post_install', '-at_install')
class TestProfitAndLossHandler(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env[
            'eh.account.dynamic.report.handler.profit_and_loss'
        ]
        cls.report = cls.env['eh.account.dynamic.report'].search(
            [('code', '=', 'profit_and_loss')], limit=1,
        )
        if not cls.report:
            cls.report = cls.env['eh.account.dynamic.report'].create({
                'code': 'profit_and_loss',
                'name': 'Profit and Loss',
                'handler_model':
                    'eh.account.dynamic.report.handler.profit_and_loss',
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

    def test_previous_period_preserves_complete_calendar_months(self):
        to_date = fields.Date.to_date
        self.assertEqual(
            self.handler._resolve_comparison_dates(
                'previous_period',
                to_date('2025-02-01'),
                to_date('2025-02-28'),
            )[:2],
            (to_date('2025-01-01'), to_date('2025-01-31')),
        )
        self.assertEqual(
            self.handler._resolve_comparison_dates(
                'previous_period',
                to_date('2026-04-01'),
                to_date('2026-06-30'),
            )[:2],
            (to_date('2026-01-01'), to_date('2026-03-31')),
        )
        # Partial windows remain adjacent equal-day comparisons.
        self.assertEqual(
            self.handler._resolve_comparison_dates(
                'previous_period',
                to_date('2025-02-10'),
                to_date('2025-02-20'),
            )[:2],
            (to_date('2025-01-30'), to_date('2025-02-09')),
        )

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
    def _line_by_meta_kind(result, kind):
        return [
            line for line in result['lines']
            if (line.get('meta') or {}).get('kind') == kind
        ]

    @staticmethod
    def _amount(line):
        for col in line['columns']:
            if col['expression_label'] == 'amount':
                return col['value']
        return None

    # ---- core math ----

    def test_kwd_precision_preserves_lines_totals_and_half_step_tie(self):
        kwd = self.env.ref('base.KWD')
        self.assertEqual(kwd.decimal_places, 3)
        self._set_presentation_rate(kwd, '2025-01-01', 1.0)

        def fake_totals(handler, account_types=None, **kwargs):
            if tuple(account_types or ()) == handler.INCOME_TYPES:
                date_to = kwargs.get('date_to')
                return [{
                    'account_id': self.account_revenue.id,
                    'account_code': self.account_revenue.code,
                    'account_name': self.account_revenue.name,
                    'amount': (
                        1.2345 if getattr(date_to, 'year', 0) == 2026
                        else 1.2335
                    ),
                }]
            if tuple(account_types or ()) == handler.EXPENSE_TYPES:
                return [{
                    'account_id': self.account_expense.id,
                    'account_code': self.account_expense.code,
                    'account_name': self.account_expense.name,
                    'amount': 1.2335,
                }]
            return []

        self.patch(
            type(self.handler), '_fetch_grouped_account_totals',
            fake_totals,
        )
        result = self.handler.compute(dict(
            self.options,
            hierarchical_groups=False,
            presentation_currency_id=kwd.id,
            comparison='previous_period',
        ))

        revenue = self._line_by_id(
            result, 'account-%d' % self.account_revenue.id)
        income_total = self._line_by_id(result, 'section-income-total')
        net_profit = self._line_by_id(result, 'net_profit')
        self.assertEqual(self._amount(revenue), 1.235)
        self.assertEqual(self._amount(income_total), 1.235)
        self.assertEqual(self._amount(net_profit), 0.001)
        variance = next(
            col['value'] for col in net_profit['columns']
            if col['expression_label'] == 'variance'
        )
        self.assertEqual(variance, 0.001)
        self.assertEqual(result['totals']['income'], 1.235)
        self.assertEqual(result['totals']['net_profit'], 0.001)

    def test_income_only_yields_positive_net_profit(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 1000.0},
            {'account': self.account_cash, 'debit': 1000.0},
        ])
        result = self.handler.compute(self.options)
        net = self._line_by_id(result, 'net_profit')
        self.assertIsNotNone(net)
        self.assertAlmostEqual(self._amount(net), 1000.0, places=2)
        self.assertAlmostEqual(result['totals']['net_profit'], 1000.0, places=2)

    def test_cash_basis_recognises_paid_portion(self):
        # Invoice: Dr AR 1000 / Cr Revenue 1000.
        inv = self.post_balanced_move(
            [{'account': self.account_receivable, 'debit': 1000.0,
              'partner': self.partner_a},
             {'account': self.account_revenue, 'credit': 1000.0}],
            date=fields.Date.from_string('2026-06-15'))
        # Partial payment 400: Dr Cash 400 / Cr AR 400.
        pay = self.post_balanced_move(
            [{'account': self.account_cash, 'debit': 400.0},
             {'account': self.account_receivable, 'credit': 400.0,
              'partner': self.partner_a}],
            date=fields.Date.from_string('2026-06-20'))
        ar_lines = (inv.line_ids + pay.line_ids).filtered(
            lambda l: l.account_id == self.account_receivable)
        ar_lines.reconcile()

        # Accrual: full 1000 recognised.
        accrual = self.handler.compute(self.options)
        rev_accrual = self._line_by_id(
            accrual, 'account-%d' % self.account_revenue.id)
        self.assertAlmostEqual(self._amount(rev_accrual), 1000.0, places=2)

        # Cash basis: only the paid 40% (400) recognised.
        cash = self.handler.compute(dict(self.options, cash_basis=True))
        rev_cash = self._line_by_id(
            cash, 'account-%d' % self.account_revenue.id)
        self.assertAlmostEqual(self._amount(rev_cash), 400.0, places=2)

    def test_cash_basis_credit_note_and_payment_net_to_cash_receipt(self):
        invoice = self.post_balanced_move([
            {'account': self.account_receivable, 'debit': 100.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 100.0},
        ], date=fields.Date.from_string('2026-05-01'))
        credit = self.post_balanced_move([
            {'account': self.account_revenue, 'debit': 30.0},
            {'account': self.account_receivable, 'credit': 30.0,
             'partner': self.partner_a},
        ], date=fields.Date.from_string('2026-05-15'))
        payment = self.post_balanced_move([
            {'account': self.account_cash, 'debit': 70.0},
            {'account': self.account_receivable, 'credit': 70.0,
             'partner': self.partner_a},
        ], date=fields.Date.from_string('2026-06-01'))
        (invoice.line_ids | credit.line_ids | payment.line_ids).filtered(
            lambda line: line.account_id == self.account_receivable
        ).reconcile()

        result = self.handler.compute(dict(self.options, cash_basis=True))
        revenue = self._line_by_id(
            result, 'account-%d' % self.account_revenue.id)
        self.assertAlmostEqual(self._amount(revenue), 70.0, places=2)
        self.assertAlmostEqual(result['totals']['net_profit'], 70.0, places=2)

    def test_cash_basis_recognises_prior_period_invoice_when_paid(self):
        invoice = self.post_balanced_move(
            [
                {
                    'account': self.account_receivable,
                    'debit': 1000.0,
                    'partner': self.partner_a,
                },
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2025-12-20'),
        )
        payment = self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 400.0},
                {
                    'account': self.account_receivable,
                    'credit': 400.0,
                    'partner': self.partner_a,
                },
            ],
            date=fields.Date.from_string('2026-01-10'),
        )
        (invoice.line_ids + payment.line_ids).filtered(
            lambda line: line.account_id == self.account_receivable
        ).reconcile()

        january = dict(
            self.options,
            date={'date_from': '2026-01-01', 'date_to': '2026-01-31'},
            cash_basis=True,
        )
        cash = self.handler.compute(january)
        revenue = self._line_by_id(
            cash, 'account-%d' % self.account_revenue.id,
        )
        self.assertIsNotNone(revenue)
        self.assertAlmostEqual(self._amount(revenue), 400.0, places=2)

    def test_presentation_currency_translation(self):
        currency = self.env['res.currency'].create({
            'name': 'ZZT', 'symbol': 'Z', 'rounding': 0.01})
        self.env['res.currency.rate'].create({
            'currency_id': currency.id, 'name': '2026-01-01',
            'rate': 2.0, 'company_id': self.company.id})
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 1000.0},
            {'account': self.account_cash, 'debit': 1000.0},
        ])
        # Translation is applied centrally in render(), not in the handler.
        result = self.report.render(
            dict(self.options, presentation_currency_id=currency.id),
            use_cache=False)
        net = self._line_by_id(result, 'net_profit')
        # 1000 company currency * rate 2.0 = 2000 presentation currency.
        self.assertAlmostEqual(self._amount(net), 2000.0, places=2)
        self.assertEqual(result['currency']['id'], currency.id)

    def test_comparison_uses_each_period_average_rate(self):
        currency = self.env['res.currency'].create({
            'name': 'ZXA', 'symbol': 'A', 'rounding': 0.01,
        })
        self._set_presentation_rate(currency, '2025-01-01', 2.0)
        self._set_presentation_rate(currency, '2026-01-01', 4.0)
        for date in ('2025-06-15', '2026-06-15'):
            self.post_balanced_move([
                {'account': self.account_revenue, 'credit': 100.0},
                {'account': self.account_cash, 'debit': 100.0},
            ], date=fields.Date.from_string(date))

        result = self.handler.compute(dict(
            self.options,
            presentation_currency_id=currency.id,
            comparison='previous_year',
        ))
        net = self._line_by_id(result, 'net_profit')
        values = {
            column['expression_label']: column['value']
            for column in net['columns']
        }
        self.assertEqual(values['amount'], 400.0)
        self.assertEqual(values['prior_amount'], 200.0)
        self.assertEqual(
            result['meta']['currency_translation_policy'], 'period_average',
        )
        periods = result['meta']['currency_translation_periods']
        self.assertEqual(
            [(period['label'], period['date_to']) for period in periods],
            [('current', '2026-12-31'), ('prior_1', '2025-12-31')],
        )
        self.assertEqual(
            periods[0]['rate_components'][str(self.company.id)][0]['rate'],
            4.0,
        )
        self.assertEqual(
            periods[1]['rate_components'][str(self.company.id)][0]['rate'],
            2.0,
        )

    def test_n_period_comparison_uses_each_period_average_rate(self):
        currency = self.env['res.currency'].create({
            'name': 'ZXB', 'symbol': 'B', 'rounding': 0.01,
        })
        for year, rate in ((2024, 1.0), (2025, 2.0), (2026, 4.0)):
            self._set_presentation_rate(
                currency, '%d-01-01' % year, rate,
            )
            self.post_balanced_move([
                {'account': self.account_revenue, 'credit': 100.0},
                {'account': self.account_cash, 'debit': 100.0},
            ], date=fields.Date.from_string('%d-06-15' % year))

        result = self.handler.compute(dict(
            self.options,
            presentation_currency_id=currency.id,
            comparison='previous_year',
            comparison_number=2,
        ))
        net = self._line_by_id(result, 'net_profit')
        self.assertEqual(
            [column['value'] for column in net['columns']],
            [400.0, 200.0, 100.0],
        )
        periods = result['meta']['currency_translation_periods']
        self.assertEqual(
            [period['label'] for period in periods],
            ['period_current', 'period_comparison_1',
             'period_comparison_2'],
        )
        self.assertEqual(
            [period['rate_components'][str(self.company.id)][0]['rate']
             for period in periods],
            [4.0, 2.0, 1.0],
        )

    def test_presentation_currency_uses_day_weighted_average(self):
        currency = self.env['res.currency'].create({
            'name': 'ZXE', 'symbol': 'E', 'rounding': 0.01,
        })
        self._set_presentation_rate(currency, '2026-01-01', 2.0)
        self._set_presentation_rate(currency, '2026-07-01', 4.0)
        self.post_balanced_move([
            {'account': self.account_revenue, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ], date=fields.Date.from_string('2026-06-15'))

        result = self.handler.compute(dict(
            self.options, presentation_currency_id=currency.id,
        ))
        net = self._line_by_id(result, 'net_profit')
        # Independently calculated: 181 days at 2.0 plus 184 days at 4.0.
        expected_rate = (181.0 * 2.0 + 184.0 * 4.0) / 365.0
        self.assertAlmostEqual(
            self._amount(net), 100.0 * expected_rate, places=2)

    def test_presentation_currency_missing_or_future_rate_fails_closed(self):
        missing = self.env['res.currency'].create({
            'name': 'ZXC', 'symbol': 'C', 'rounding': 0.01,
        })
        future = self.env['res.currency'].create({
            'name': 'ZXD', 'symbol': 'D', 'rounding': 0.01,
        })
        self._set_presentation_rate(future, '2027-01-01', 2.0)
        for currency in (missing, future):
            with self.subTest(currency=currency.name), self.assertRaisesRegex(
                    UserError, "No valid .* exchange rate"):
                self.handler.compute(dict(
                    self.options,
                    presentation_currency_id=currency.id,
                ))

    def test_drilldown_folds_report_filters(self):
        journal = self.env['account.journal'].search(
            [('company_id', '=', self.company.id)], limit=1)
        opts = dict(self.options, journal_ids=[journal.id],
                    partner_ids=[self.partner_a.id])
        action = self.handler.get_drilldown_action(
            opts, 'account-%d' % self.account_revenue.id)
        self.assertIsNotNone(action)
        domain = action['domain']
        self.assertIn(('account_id', '=', self.account_revenue.id), domain)
        self.assertIn(('journal_id', 'in', [journal.id]), domain)
        self.assertIn(('partner_id', 'in', [self.partner_a.id]), domain)

    def test_annotation_injected_into_payload(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 1000.0},
            {'account': self.account_cash, 'debit': 1000.0},
        ])
        # Row-level annotation.
        self.report.add_annotation('net_profit', 'Reviewed by CFO')
        payload = self.report.render(self.options, use_cache=False)
        net = self._line_by_id(payload, 'net_profit')
        self.assertIn('annotations', net.get('meta', {}))
        self.assertEqual(
            net['meta']['annotations'][0]['text'], 'Reviewed by CFO')
        # Cell-level annotation on the amount column.
        self.report.add_annotation(
            'net_profit', 'Strong quarter', expression_label='amount')
        payload2 = self.report.render(self.options, use_cache=False)
        net2 = self._line_by_id(payload2, 'net_profit')
        amount_col = next(
            c for c in net2['columns']
            if c['expression_label'] == 'amount')
        self.assertIn('annotations', amount_col)
        self.assertEqual(
            amount_col['annotations'][0]['text'], 'Strong quarter')

    def test_n_period_previous_year_columns(self):
        # Revenue in three successive years.
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 1000.0},
             {'account': self.account_cash, 'debit': 1000.0}],
            date=fields.Date.from_string('2026-06-15'))
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 800.0},
             {'account': self.account_cash, 'debit': 800.0}],
            date=fields.Date.from_string('2025-06-15'))
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 600.0},
             {'account': self.account_cash, 'debit': 600.0}],
            date=fields.Date.from_string('2024-06-15'))
        options = dict(self.options, comparison='previous_year',
                       comparison_number=2)
        result = self.handler.compute(options)
        value_columns = result['columns'][1:]
        self.assertEqual(len(value_columns), 3)
        self.assertTrue(all(column.get('scope') for column in value_columns))
        self.assertEqual(
            [column['scope']['date_to'] for column in value_columns],
            ['2026-12-31', '2025-12-31', '2024-12-31'],
        )
        net = self._line_by_id(result, 'net_profit')
        self.assertEqual(
            [column['expression_label'] for column in net['columns']],
            [column['expression_label'] for column in value_columns],
        )
        self.assertEqual(
            [column['value'] for column in net['columns']],
            [1000.0, 800.0, 600.0],
        )

    def test_custom_comparison_uses_explicit_scoped_period(self):
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 125.0},
             {'account': self.account_cash, 'debit': 125.0}],
            date=fields.Date.from_string('2026-06-15'))
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 75.0},
             {'account': self.account_cash, 'debit': 75.0}],
            date=fields.Date.from_string('2024-04-15'))
        result = self.handler.compute(dict(
            self.options,
            comparison='custom',
            comparison_custom_date_from='2024-04-01',
            comparison_custom_date_to='2024-04-30',
        ))
        value_columns = result['columns'][1:]
        self.assertEqual(
            [(column['scope']['date_from'], column['scope']['date_to'])
             for column in value_columns],
            [
                ('2026-01-01', '2026-12-31'),
                ('2024-04-01', '2024-04-30'),
            ],
        )
        net = self._line_by_id(result, 'net_profit')
        self.assertEqual(
            [column['value'] for column in net['columns']],
            [125.0, 75.0],
        )

    def test_analytic_columns_use_weighted_cells_and_independent_total(self):
        if 'account.analytic.plan' not in self.env:
            self.skipTest("Analytic addon not installed in this build.")
        plan = self.env['account.analytic.plan'].create({
            'name': 'P&L column plan',
        })
        analytic_a = self.env['account.analytic.account'].create({
            'name': 'P&L Analytic A', 'plan_id': plan.id,
        })
        analytic_b = self.env['account.analytic.account'].create({
            'name': 'P&L Analytic B', 'plan_id': plan.id,
        })
        for day in (15, 16):
            self.env['account.move'].create({
                'move_type': 'entry',
                'journal_id': self.journal_misc.id,
                'date': fields.Date.from_string('2026-06-%02d' % day),
                'line_ids': [
                    (0, 0, {
                        'account_id': self.account_revenue.id,
                        'credit': 50.0,
                        'analytic_distribution': {
                            str(analytic_a.id): 60.0,
                            str(analytic_b.id): 40.0,
                        },
                    }),
                    (0, 0, {
                        'account_id': self.account_cash.id,
                        'debit': 50.0,
                    }),
                ],
            }).action_post()
        axis_options = dict(
            self.options,
            hierarchical_groups=False,
            analytic_column_account_ids=[analytic_a.id, analytic_b.id],
        )
        result = self.report.render(axis_options, use_cache=False)
        value_columns = result['columns'][1:]
        self.assertEqual(len(value_columns), 3)
        self.assertEqual(
            [column['scope']['is_total'] for column in value_columns],
            [False, False, True],
        )
        self.assertEqual(
            value_columns[-1]['scope']['analytic_account_ids'], [],
        )
        net = self._line_by_id(result, 'net_profit')
        self.assertEqual(
            [column['value'] for column in net['columns']],
            [60.0, 40.0, 100.0],
        )
        self.assertEqual(
            sum(cell['colspan'] for cell in result['column_header_rows'][0]),
            len(result['columns']),
        )

        revenue = self._line_by_id(
            result, 'account-%d' % self.account_revenue.id,
        )
        clicked_cell = revenue['columns'][0]['value']
        # Private overlay keys are deliberately forged toward B.  The public
        # RPC must discard them, rebuild A from expression_label, and return
        # the same signed 60% contribution as the clicked revenue cell.
        forged_options = dict(
            axis_options,
            _eh_analytic_column_active=True,
            _eh_analytic_column_account_ids=[analytic_b.id],
        )
        detail = self.report.get_analytic_column_drilldown_page(
            forged_options,
            revenue['id'],
            value_columns[0]['expression_label'],
            offset=0,
            limit=1,
            execution_id=result['execution_id'],
            displayed_amount=clicked_cell,
        )
        self.assertEqual(detail['total_count'], 2)
        self.assertTrue(detail['has_more'])
        self.assertEqual(detail['scope'], value_columns[0]['scope'])
        self.assertAlmostEqual(detail['total'], clicked_cell, places=2)
        next_detail = self.report.get_analytic_column_drilldown_page(
            axis_options,
            revenue['id'],
            value_columns[0]['expression_label'],
            offset=1,
            limit=1,
            execution_id=result['execution_id'],
            displayed_amount=clicked_cell,
            page_token=detail['page_token'],
        )
        self.assertFalse(next_detail['has_more'])
        self.assertEqual(next_detail['total'], detail['total'])
        self.assertAlmostEqual(
            sum(
                row['values']['allocated_amount']
                for page in (detail, next_detail)
                for row in page['rows']
            ),
            clicked_cell,
            places=2,
        )
        self.assertEqual(
            [column['key'] for column in detail['columns']],
            ['date', 'move', 'partner', 'label', 'allocated_amount'],
        )
        with self.assertRaisesRegex(UserError, 'selected amount'):
            self.report.get_analytic_column_drilldown_page(
                axis_options,
                revenue['id'],
                value_columns[0]['expression_label'],
                execution_id=result['execution_id'],
                displayed_amount=clicked_cell + 1.0,
            )
        with self.assertRaisesRegex(UserError, 'options changed'):
            self.report.get_analytic_column_drilldown_page(
                dict(axis_options, show_zero=True),
                revenue['id'],
                value_columns[0]['expression_label'],
                execution_id=result['execution_id'],
                displayed_amount=clicked_cell,
            )
        with self.assertRaisesRegex(UserError, 'opened in sequence'):
            self.report.get_analytic_column_drilldown_page(
                axis_options,
                revenue['id'],
                value_columns[0]['expression_label'],
                offset=1,
                limit=1,
                execution_id=result['execution_id'],
                displayed_amount=clicked_cell,
            )
        other_actor = new_test_user(
            self.env,
            login='weighted_detail_foreign_execution_user',
            groups='eh_account_base.group_eh_user',
            company_id=self.company.id,
            company_ids=[(6, 0, [self.company.id])],
        )
        with self.assertRaisesRegex(AccessError, 'does not belong'):
            self.report.with_user(
                other_actor,
            ).get_analytic_column_drilldown_page(
                axis_options,
                revenue['id'],
                value_columns[0]['expression_label'],
                execution_id=result['execution_id'],
                displayed_amount=clicked_cell,
            )
        presentation = self.env['res.currency'].create({
            'name': 'ZAD', 'symbol': 'D', 'rounding': 0.01,
        })
        self._set_presentation_rate(presentation, '2026-01-01', 2.0)
        converted_options = dict(
            axis_options, presentation_currency_id=presentation.id,
        )
        converted = self.report.render(converted_options, use_cache=False)
        converted_revenue = self._line_by_id(
            converted, 'account-%d' % self.account_revenue.id,
        )
        converted_detail = self.report.get_analytic_column_drilldown_page(
            converted_options,
            converted_revenue['id'],
            converted['columns'][1]['expression_label'],
            execution_id=converted['execution_id'],
            displayed_amount=converted_revenue['columns'][0]['value'],
        )
        self.assertEqual(converted_detail['currency']['id'], presentation.id)
        self.assertAlmostEqual(
            converted_detail['total'],
            converted_revenue['columns'][0]['value'],
            places=2,
        )
        self.assertAlmostEqual(converted_detail['total'], 120.0, places=2)
        with self.assertRaises(UserError):
            self.report.get_analytic_column_drilldown_page(
                axis_options,
                revenue['id'],
                value_columns[-1]['expression_label'],
            )
        with self.assertRaises(UserError):
            self.report.get_analytic_column_drilldown_page(
                axis_options,
                revenue['id'],
                value_columns[0]['expression_label'],
                limit=0,
            )
        with self.assertRaises(UserError):
            self.report.get_analytic_column_drilldown_page(
                dict(axis_options, cash_basis=True),
                revenue['id'],
                value_columns[0]['expression_label'],
            )

        plan_options = dict(
            self.options,
            hierarchical_groups=False,
            analytic_column_plan_ids=[plan.id],
        )
        by_plan = self.report.render(plan_options, use_cache=False)
        plan_columns = by_plan['columns'][1:]
        self.assertEqual(len(plan_columns), 2)
        self.assertEqual(
            [column['scope']['analytic_plan_ids']
             for column in plan_columns],
            [[plan.id], []],
        )
        self.assertEqual(
            [column['value'] for column in
             self._line_by_id(by_plan, 'net_profit')['columns']],
            [100.0, 100.0],
        )
        plan_revenue = self._line_by_id(
            by_plan, 'account-%d' % self.account_revenue.id,
        )
        plan_detail = self.report.get_analytic_column_drilldown_page(
            plan_options,
            plan_revenue['id'],
            plan_columns[0]['expression_label'],
            execution_id=by_plan['execution_id'],
            displayed_amount=plan_revenue['columns'][0]['value'],
        )
        self.assertAlmostEqual(plan_detail['total'], 100.0, places=2)
        self.assertAlmostEqual(
            sum(
                row['values']['allocated_amount']
                for row in plan_detail['rows']
            ),
            100.0,
            places=2,
        )

        combined = self.handler.compute(dict(
            self.options,
            hierarchical_groups=False,
            analytic_column_account_ids=[analytic_a.id, analytic_b.id],
            comparison='previous_year',
            comparison_number=2,
        ))
        self.assertEqual(len(combined['columns']), 10)
        self.assertEqual(
            [cell['colspan']
             for cell in combined['column_header_rows'][0][1:]],
            [3, 3, 3],
        )

        hidden_move_line_id = detail['rows'][0]['move_line_id']
        self.env['ir.rule'].create({
            'name': 'Weighted detail hidden AML test rule',
            'model_id': self.env['ir.model']._get(
                'account.move.line',
            ).id,
            'domain_force': [('id', '!=', hidden_move_line_id)],
        })
        restricted = new_test_user(
            self.env,
            login='weighted_detail_record_rule_user',
            groups='eh_account_base.group_eh_user',
            company_id=self.company.id,
            company_ids=[(6, 0, [self.company.id])],
        )
        restricted_report = self.report.with_user(restricted)
        restricted_result = restricted_report.render(
            axis_options, use_cache=False,
        )
        restricted_revenue = self._line_by_id(
            restricted_result, revenue['id'],
        )
        with self.assertRaisesRegex(AccessError, 'every journal item'):
            restricted_report.get_analytic_column_drilldown_page(
                axis_options,
                revenue['id'],
                value_columns[0]['expression_label'],
                execution_id=restricted_result['execution_id'],
                displayed_amount=restricted_revenue['columns'][0]['value'],
            )

    def test_weighted_drilldown_rounds_rows_and_reconciles_across_pages(self):
        if 'account.analytic.plan' not in self.env:
            self.skipTest("Analytic addon not installed in this build.")
        plan = self.env['account.analytic.plan'].create({
            'name': 'P&L fractional drill-down plan',
        })
        analytic = self.env['account.analytic.account'].create({
            'name': 'P&L fractional analytic', 'plan_id': plan.id,
        })
        for day in (20, 21, 22):
            self.env['account.move'].create({
                'move_type': 'entry',
                'journal_id': self.journal_misc.id,
                'date': fields.Date.from_string('2026-06-%02d' % day),
                'line_ids': [
                    (0, 0, {
                        'account_id': self.account_revenue.id,
                        'credit': 0.03,
                        'analytic_distribution': {str(analytic.id): 10.0},
                    }),
                    (0, 0, {
                        'account_id': self.account_cash.id,
                        'debit': 0.03,
                    }),
                ],
            }).action_post()

        options = dict(
            self.options,
            hierarchical_groups=False,
            analytic_column_account_ids=[analytic.id],
        )
        result = self.report.render(options, use_cache=False)
        revenue = self._line_by_id(
            result, 'account-%d' % self.account_revenue.id,
        )
        expression = result['columns'][1]['expression_label']
        first = self.report.get_analytic_column_drilldown_page(
            options, revenue['id'], expression, offset=0, limit=1,
            execution_id=result['execution_id'],
            displayed_amount=revenue['columns'][0]['value'],
        )
        pages = [first]
        for offset in (1, 2):
            pages.append(self.report.get_analytic_column_drilldown_page(
                options, revenue['id'], expression, offset=offset, limit=1,
                execution_id=result['execution_id'],
                displayed_amount=revenue['columns'][0]['value'],
                page_token=first['page_token'],
            ))
        amounts_by_offset = {
            page['offset']: page['rows'][0]['values']['allocated_amount']
            for page in pages
        }
        amounts = [amounts_by_offset[index] for index in range(3)]
        currency = self.company.currency_id
        self.assertEqual(amounts, [0.0, 0.0, 0.01])
        self.assertTrue(all(
            currency.round(amount) == amount for amount in amounts
        ))
        self.assertEqual(sum(amounts), pages[0]['total'])
        self.assertEqual(pages[0]['total'], revenue['columns'][0]['value'])

        repeated_last = self.report.get_analytic_column_drilldown_page(
            options, revenue['id'], expression, offset=2, limit=1,
            execution_id=result['execution_id'],
            displayed_amount=revenue['columns'][0]['value'],
            page_token=first['page_token'],
        )
        self.assertEqual(repeated_last['rows'], pages[2]['rows'])

    def test_weighted_drilldown_rejects_changed_total_after_render(self):
        plan = self.env['account.analytic.plan'].create({
            'name': 'P&L stale-total detail plan',
        })
        analytic = self.env['account.analytic.account'].create({
            'name': 'P&L stale-total analytic', 'plan_id': plan.id,
        })
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': fields.Date.from_string('2026-07-01'),
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'credit': 100.0,
                    'analytic_distribution': {str(analytic.id): 50.0},
                }),
                (0, 0, {
                    'account_id': self.account_cash.id, 'debit': 100.0,
                }),
            ],
        })
        move.action_post()
        options = dict(
            self.options,
            hierarchical_groups=False,
            analytic_column_account_ids=[analytic.id],
        )
        result = self.report.render(options, use_cache=False)
        revenue = self._line_by_id(
            result, 'account-%d' % self.account_revenue.id,
        )
        expression = result['columns'][1]['expression_label']
        displayed = revenue['columns'][0]['value']
        revenue_line = move.line_ids.filtered(
            lambda line: line.account_id == self.account_revenue
        )
        revenue_line.with_context(check_move_validity=False).write({
            'analytic_distribution': {str(analytic.id): 60.0},
        })
        with self.assertRaisesRegex(
                UserError, 'no longer match the displayed amount'):
            self.report.get_analytic_column_drilldown_page(
                options, revenue['id'], expression,
                execution_id=result['execution_id'],
                displayed_amount=displayed,
            )

    def test_weighted_drilldown_detects_equal_total_candidate_page_drift(self):
        plan = self.env['account.analytic.plan'].create({
            'name': 'P&L equal-total detail plan',
        })
        analytic = self.env['account.analytic.account'].create({
            'name': 'P&L equal-total analytic', 'plan_id': plan.id,
        })
        moves = self.env['account.move']
        for day in (2, 3):
            move = self.env['account.move'].create({
                'move_type': 'entry',
                'journal_id': self.journal_misc.id,
                'date': fields.Date.from_string(
                    '2026-07-%02d' % day,
                ),
                'line_ids': [
                    (0, 0, {
                        'account_id': self.account_revenue.id,
                        'credit': 100.0,
                        'analytic_distribution': {str(analytic.id): 50.0},
                    }),
                    (0, 0, {
                        'account_id': self.account_cash.id, 'debit': 100.0,
                    }),
                ],
            })
            move.action_post()
            moves |= move
        options = dict(
            self.options,
            hierarchical_groups=False,
            analytic_column_account_ids=[analytic.id],
        )
        result = self.report.render(options, use_cache=False)
        revenue = self._line_by_id(
            result, 'account-%d' % self.account_revenue.id,
        )
        expression = result['columns'][1]['expression_label']
        displayed = revenue['columns'][0]['value']
        first = self.report.get_analytic_column_drilldown_page(
            options, revenue['id'], expression, offset=0, limit=1,
            execution_id=result['execution_id'],
            displayed_amount=displayed,
        )
        revenue_lines = moves.line_ids.filtered(
            lambda line: line.account_id == self.account_revenue
        ).sorted('date')
        revenue_lines[0].with_context(check_move_validity=False).write({
            'analytic_distribution': {str(analytic.id): 40.0},
        })
        revenue_lines[1].with_context(check_move_validity=False).write({
            'analytic_distribution': {str(analytic.id): 60.0},
        })
        with self.assertRaisesRegex(UserError, 'changed while paging'):
            self.report.get_analytic_column_drilldown_page(
                options, revenue['id'], expression, offset=1, limit=1,
                execution_id=result['execution_id'],
                displayed_amount=displayed,
                page_token=first['page_token'],
            )

    def test_weighted_drilldown_accepts_root_account_for_branch_scope(self):
        Account = self.env['account.account']
        if not callable(getattr(Account, '_check_company_domain', None)):
            self.skipTest('branch-aware account scope is unavailable')
        branch = self._create_accounting_branch({
            'name': 'P&L weighted-detail branch',
            'parent_id': self.company.id,
        })
        unrelated = self.env['res.company'].create({
            'name': 'P&L weighted-detail unrelated company',
        })
        detail_helper = self.env[
            'eh.account.dynamic.report.handler.sectioned'
        ]
        self.assertTrue(
            detail_helper._eh_analytic_drilldown_account_matches_companies(
                self.account_revenue, [branch.id],
            )
        )
        self.assertFalse(
            detail_helper._eh_analytic_drilldown_account_matches_companies(
                self.account_revenue, [unrelated.id],
            )
        )
        self.env.user.write({'company_ids': [(4, branch.id)]})
        branch_env = self.env['account.move'].with_context(
            allowed_company_ids=[self.company.id, branch.id],
        ).with_company(branch).env
        journal = self._ensure_journal(
            branch_env, branch, 'general', 'PDBR',
            'P&L Detail Branch Journal',
        )
        plan = branch_env['account.analytic.plan'].create({
            'name': 'P&L detail branch plan',
        })
        analytic = branch_env['account.analytic.account'].create({
            'name': 'P&L detail branch analytic', 'plan_id': plan.id,
        })
        move = branch_env['account.move'].create({
            'move_type': 'entry', 'journal_id': journal.id,
            'date': fields.Date.from_string('2026-08-01'),
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'credit': 100.0,
                    'analytic_distribution': {str(analytic.id): 100.0},
                }),
                (0, 0, {
                    'account_id': self.account_cash.id, 'debit': 100.0,
                }),
            ],
        })
        move.action_post()
        report = branch_env['eh.account.dynamic.report'].search([
            ('code', '=', 'profit_and_loss'),
        ], limit=1)
        options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [branch.id],
            'posted_only': True,
            'show_zero': False,
            'hierarchical_groups': False,
            'analytic_column_account_ids': [analytic.id],
        }
        result = report.render(options, use_cache=False)
        revenue = self._line_by_id(
            result, 'account-%d' % self.account_revenue.id,
        )
        if 'company_ids' in self.account_revenue._fields:
            self.assertEqual(self.account_revenue.company_ids, self.company)
        else:
            self.assertEqual(self.account_revenue.company_id, self.company)
        detail = report.get_analytic_column_drilldown_page(
            options,
            revenue['id'],
            result['columns'][1]['expression_label'],
            execution_id=result['execution_id'],
            displayed_amount=revenue['columns'][0]['value'],
        )
        self.assertEqual(detail['total'], 100.0)
        self.assertEqual(detail['total_count'], 1)

    def test_comparison_period_count_is_bounded_before_sql(self):
        with self.assertRaises(UserError):
            self.handler.compute(dict(
                self.options,
                comparison='previous_year',
                comparison_number=25,
            ))

    def test_comparison_period_count_rejects_fractional_and_boolean(self):
        for invalid in (3.7, True):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                    UserError, 'whole number'):
                self.handler.compute(dict(
                    self.options,
                    comparison='previous_year',
                    comparison_number=invalid,
                ))

    def test_expense_only_yields_negative_net_profit(self):
        self._post_in_period([
            {'account': self.account_expense, 'debit': 300.0},
            {'account': self.account_cash, 'credit': 300.0},
        ])
        result = self.handler.compute(self.options)
        net = self._line_by_id(result, 'net_profit')
        self.assertAlmostEqual(self._amount(net), -300.0, places=2)

    def test_mixed_income_minus_expenses(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 1000.0},
            {'account': self.account_cash, 'debit': 1000.0},
        ])
        self._post_in_period([
            {'account': self.account_expense, 'debit': 300.0},
            {'account': self.account_cash, 'credit': 300.0},
        ])
        result = self.handler.compute(self.options)
        self.assertAlmostEqual(
            result['totals']['income'], 1000.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['expenses'], 300.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['net_profit'], 700.0, places=2,
        )

    def test_section_structure_present(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        result = self.handler.compute(self.options)
        headers = self._line_by_meta_kind(result, 'section_header')
        totals = self._line_by_meta_kind(result, 'section_total')
        self.assertEqual(len(headers), 2,
                         "Income and Expenses section headers must appear")
        self.assertEqual(len(totals), 2,
                         "Income and Expenses section totals must appear")
        self.assertEqual({h['name'] for h in headers},
                         {'Income', 'Expenses'})
        # Net Profit always at the bottom.
        self.assertEqual(result['lines'][-1]['id'], 'net_profit')

    def test_section_totals_match_account_sum(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 600.0},
            {'account': self.account_cash, 'debit': 600.0},
        ])
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 400.0},
            {'account': self.account_cash, 'debit': 400.0},
        ])
        result = self.handler.compute(self.options)
        income_total = next(
            self._amount(l) for l in result['lines']
            if l['id'] == 'section-income-total'
        )
        self.assertAlmostEqual(income_total, 1000.0, places=2)

    # ---- filter behaviour ----

    def test_zero_balance_account_hidden_by_default(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        result = self.handler.compute(self.options)
        # Expense account untouched: should not appear as a sub line.
        sub_account_lines = [
            l for l in result['lines']
            if (l.get('meta') or {}).get('account_code')
        ]
        codes = {l['meta']['account_code'] for l in sub_account_lines}
        self.assertNotIn('5000', codes)

    def test_account_filter(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        self._post_in_period([
            {'account': self.account_expense, 'debit': 50.0},
            {'account': self.account_cash, 'credit': 50.0},
        ])
        opts = dict(self.options)
        opts['account_ids'] = [self.account_revenue.id]
        result = self.handler.compute(opts)
        # Only revenue account contributes; net profit = income.
        self.assertAlmostEqual(
            result['totals']['net_profit'], 100.0, places=2,
        )

    def test_partner_filter(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 100.0,
             'partner': self.partner_a},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 200.0,
             'partner': self.partner_b},
            {'account': self.account_cash, 'debit': 200.0},
        ])
        opts = dict(self.options)
        opts['partner_ids'] = [self.partner_a.id]
        result = self.handler.compute(opts)
        self.assertAlmostEqual(
            result['totals']['net_profit'], 100.0, places=2,
        )

    # ---- state filtering ----

    def test_posted_only_excludes_draft(self):
        self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': '2026-06-15',
            'line_ids': [
                (0, 0, {'account_id': self.account_revenue.id, 'credit': 999.0}),
                (0, 0, {'account_id': self.account_cash.id, 'debit': 999.0}),
            ],
        })
        result = self.handler.compute(self.options)
        self.assertAlmostEqual(
            result['totals']['net_profit'], 0.0, places=2,
        )

    def test_posted_only_false_includes_draft(self):
        self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': '2026-06-15',
            'line_ids': [
                (0, 0, {'account_id': self.account_revenue.id, 'credit': 333.0}),
                (0, 0, {'account_id': self.account_cash.id, 'debit': 333.0}),
            ],
        })
        opts = dict(self.options)
        opts['posted_only'] = False
        result = self.handler.compute(opts)
        self.assertAlmostEqual(
            result['totals']['net_profit'], 333.0, places=2,
        )

    def test_cancelled_excluded(self):
        move = self._post_in_period([
            {'account': self.account_revenue, 'credit': 444.0},
            {'account': self.account_cash, 'debit': 444.0},
        ])
        move.button_cancel()
        result = self.handler.compute(self.options)
        self.assertAlmostEqual(
            result['totals']['net_profit'], 0.0, places=2,
        )

    def test_out_of_period_excluded(self):
        # Entry before the period.
        self.post_balanced_move(
            [
                {'account': self.account_revenue, 'credit': 1000.0},
                {'account': self.account_cash, 'debit': 1000.0},
            ],
            date=fields.Date.from_string('2025-12-15'),
        )
        # Entry after the period.
        self.post_balanced_move(
            [
                {'account': self.account_revenue, 'credit': 2000.0},
                {'account': self.account_cash, 'debit': 2000.0},
            ],
            date=fields.Date.from_string('2027-01-15'),
        )
        result = self.handler.compute(self.options)
        self.assertAlmostEqual(
            result['totals']['net_profit'], 0.0, places=2,
        )

    # ---- error handling ----

    def test_missing_date_raises(self):
        bad = dict(self.options)
        bad.pop('date')
        with self.assertRaises(UserError):
            self.handler.compute(bad)

    def test_missing_date_from_raises(self):
        bad = dict(self.options)
        bad['date'] = {'date_to': '2026-12-31'}
        with self.assertRaises(UserError):
            self.handler.compute(bad)

    # ---- orchestrator wiring ----

    def test_orchestrator_renders(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        result = self.report.render(self.options)
        self.assertFalse(result['from_cache'])
        self.assertIn('execution_id', result)
        self.assertGreater(len(result['lines']), 0)

    def test_orchestrator_cache_hit_on_second_render(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        first = self.report.render(self.options)
        second = self.report.render(self.options)
        self.assertFalse(first['from_cache'])
        self.assertTrue(second['from_cache'])
        self.assertEqual(first['totals'], second['totals'])

    # ---- drill down ----

    def test_drilldown_action_for_account_line(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 75.0},
            {'account': self.account_cash, 'debit': 75.0},
        ])
        action = self.handler.get_drilldown_action(
            self.options, "account-%s" % self.account_revenue.id,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action['res_model'], 'account.move.line')
        items = self.env['account.move.line'].search(action['domain'])
        self.assertIn(
            self.account_revenue.id, items.mapped('account_id.id'),
        )

    def test_drilldown_returns_none_for_section_marker(self):
        self.assertIsNone(self.handler.get_drilldown_action(
            self.options, 'section-income-header',
        ))
        self.assertIsNone(self.handler.get_drilldown_action(
            self.options, 'section-expenses-total',
        ))
        self.assertIsNone(self.handler.get_drilldown_action(
            self.options, 'net_profit',
        ))

    # ---- XLSX export ----

    def test_xlsx_export_renders_workbook(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        content = self.report.render_xlsx(self.options)
        self.assertEqual(content[:2], b'PK')
        self.assertGreater(len(content), 1000,
                           "XLSX should contain meaningful content")


@tagged('eh_account_dynamic_reports', 'integration', 'post_install',
        '-at_install')
class TestProfitAndLossHorizontal(EhAccountIntegrationTestCase):
    """Horizontal column groups: one amount column per company."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env[
            'eh.account.dynamic.report.handler.profit_and_loss']
        cls.company_a = cls.company
        cls.company_b = cls.env['res.company'].create({
            'name': 'Horiz Co B',
            'currency_id': cls.company_a.currency_id.id,
        })
        cls.env.user.company_ids = [(4, cls.company_b.id)]
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.journal_b = cls.env['account.journal'].create({
            'name': 'Misc B', 'code': 'MSCB', 'type': 'general',
            'company_id': cls.company_b.id,
        })
        cls.revenue_b = cls.env['account.account'].create({
            'code': '4001B', 'name': 'Revenue B', 'account_type': 'income',
            'company_ids': [(6, 0, [cls.company_b.id])],
        })
        cls.cash_b = cls.env['account.account'].create({
            'code': '1001B', 'name': 'Cash B', 'account_type': 'asset_cash',
            'company_ids': [(6, 0, [cls.company_b.id])],
        })

    def test_horizontal_by_company(self):
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 1000.0},
             {'account': self.account_cash, 'debit': 1000.0}],
            date=fields.Date.from_string('2026-06-15'))
        self.post_balanced_move(
            [{'account': self.revenue_b, 'credit': 600.0},
             {'account': self.cash_b, 'debit': 600.0}],
            journal=self.journal_b,
            date=fields.Date.from_string('2026-06-15'))
        options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company_a.id, self.company_b.id],
            'posted_only': True, 'show_zero': False,
            'horizontal_group_by': 'company',
        }
        result = self.handler.compute(options)
        col_keys = [c['expression_label'] for c in result['columns']]
        self.assertEqual(col_keys, ['account', 'group_1', 'group_2', 'total'])
        net = next(l for l in result['lines'] if l['id'] == 'net_profit')
        cols = {c['expression_label']: c['value'] for c in net['columns']}
        self.assertAlmostEqual(cols['group_1'], 1000.0, places=2)
        self.assertAlmostEqual(cols['group_2'], 600.0, places=2)
        self.assertAlmostEqual(cols['total'], 1600.0, places=2)


@tagged('eh_account_dynamic_reports', 'integration', 'post_install',
        '-at_install')
class TestProfitAndLossByFunction(EhAccountIntegrationTestCase):
    """IAS 1.82/85 by-function presentation election.

    * The by-nature default is unaffected (asserted by every test in the
      primary class, which never sets pnl_presentation).
    * by_function yields Gross Profit / Operating Profit / Profit Before
      Tax / Profit for the Period subtotals that tie to the by-nature
      Net Profit.
    * With a finance-cost account mapped, Finance Costs is non-zero and
      Profit Before Tax = Operating Profit - Finance Costs.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env[
            'eh.account.dynamic.report.handler.profit_and_loss']
        # Direct-cost account -> Cost of Sales bucket.
        cls.account_direct_cost = cls._ensure_account(
            cls.env, '5100', 'Direct Materials', 'expense_direct_cost')
        # Overhead expense -> Operating Expenses bucket (account_expense at
        # 5000 is type 'expense' and also lands in Operating Expenses).
        cls.account_overhead = cls._ensure_account(
            cls.env, '5200', 'Administration', 'expense')
        # Finance-cost account: still an ordinary 'expense' account, but we
        # map it to Finance Costs so it is carved out of Operating Expenses.
        cls.account_interest = cls._ensure_account(
            cls.env, '5300', 'Interest Expense', 'expense')
        # Two tax-expense accounts so the deferred-tax split has something on
        # each side: current tax and deferred tax.
        cls.account_current_tax = cls._ensure_account(
            cls.env, '5400', 'Current Income Tax', 'expense')
        cls.account_deferred_tax = cls._ensure_account(
            cls.env, '5500', 'Deferred Income Tax', 'expense')

    def setUp(self):
        super().setUp()
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
        }

    def _post(self, lines):
        return self.post_balanced_move(
            lines, date=fields.Date.from_string('2026-06-15'))

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
        for col in line['columns']:
            if col['expression_label'] == 'amount':
                return col['value']
        return None

    def _seed_full_pnl(self):
        # Revenue 1000, Cost of Sales 300, Overhead 200, Interest 50.
        self._post([
            {'account': self.account_revenue, 'credit': 1000.0},
            {'account': self.account_cash, 'debit': 1000.0},
        ])
        self._post([
            {'account': self.account_direct_cost, 'debit': 300.0},
            {'account': self.account_cash, 'credit': 300.0},
        ])
        self._post([
            {'account': self.account_overhead, 'debit': 200.0},
            {'account': self.account_cash, 'credit': 200.0},
        ])
        self._post([
            {'account': self.account_interest, 'debit': 50.0},
            {'account': self.account_cash, 'credit': 50.0},
        ])

    def test_kwd_by_function_subtotals_keep_three_decimal_precision(self):
        kwd = self.env.ref('base.KWD')
        self.assertEqual(kwd.decimal_places, 3)
        self._set_presentation_rate(kwd, '2025-01-01', 1.0)

        def fake_totals(handler, account_types=None, **kwargs):
            account_types = tuple(account_types or ())
            if account_types == handler.INCOME_TYPES:
                return [{
                    'account_id': self.account_revenue.id,
                    'account_code': self.account_revenue.code,
                    'account_name': self.account_revenue.name,
                    'amount': 2.4705,
                }]
            if account_types == handler.COST_OF_SALES_TYPES:
                return [{
                    'account_id': self.account_direct_cost.id,
                    'account_code': self.account_direct_cost.code,
                    'account_name': self.account_direct_cost.name,
                    'amount': 1.2345,
                }]
            if account_types == handler.OPERATING_EXPENSE_TYPES:
                return [{
                    'account_id': self.account_overhead.id,
                    'account_code': self.account_overhead.code,
                    'account_name': self.account_overhead.name,
                    'amount': 1.2345,
                }]
            return []

        self.patch(
            type(self.handler), '_fetch_grouped_account_totals',
            fake_totals,
        )
        result = self.handler.compute(dict(
            self.options,
            pnl_presentation='by_function',
            hierarchical_groups=False,
            presentation_currency_id=kwd.id,
        ))

        cos = self._line_by_id(result, 'section-cost_of_sales-total')
        operating_profit = self._line_by_id(result, 'operating_profit')
        net_profit = self._line_by_id(result, 'net_profit')
        self.assertEqual(self._amount(cos), 1.235)
        self.assertEqual(self._amount(operating_profit), 0.001)
        self.assertEqual(self._amount(net_profit), 0.001)
        self.assertEqual(result['totals']['expenses'], 2.47)
        self.assertEqual(result['totals']['net_profit'], 0.001)

    def test_default_presentation_is_by_nature(self):
        # No pnl_presentation key: classic Income / Expenses / Net Profit.
        self._seed_full_pnl()
        result = self.handler.compute(self.options)
        headers = [
            l['name'] for l in result['lines']
            if (l.get('meta') or {}).get('kind') == 'section_header']
        self.assertEqual(set(headers), {'Income', 'Expenses'})
        self.assertIsNone(self._line_by_id(result, 'gross_profit'))
        # Net Profit = 1000 - (300 + 200 + 50) = 450.
        net = self._line_by_id(result, 'net_profit')
        self.assertAlmostEqual(self._amount(net), 450.0, places=2)
        self.assertEqual(net['name'], 'Net Profit')

    def test_by_function_subtotals_tie_to_net_profit(self):
        # No account mapping: Finance Costs and Tax Expense are zero, so the
        # 50 interest stays in Operating Expenses.
        self._seed_full_pnl()
        opts = dict(self.options, pnl_presentation='by_function')
        result = self.handler.compute(opts)

        revenue = self._line_by_id(result, 'section-income-total')
        cos = self._line_by_id(result, 'section-cost_of_sales-total')
        gross = self._line_by_id(result, 'gross_profit')
        opex = self._line_by_id(result, 'section-operating_expenses-total')
        operating = self._line_by_id(result, 'operating_profit')
        finance = self._line_by_id(result, 'section-finance_costs-total')
        pbt = self._line_by_id(result, 'profit_before_tax')
        tax = self._line_by_id(result, 'section-tax_expense-total')
        net = self._line_by_id(result, 'net_profit')

        self.assertAlmostEqual(self._amount(revenue), 1000.0, places=2)
        self.assertAlmostEqual(self._amount(cos), 300.0, places=2)
        # Gross Profit = 1000 - 300 = 700.
        self.assertAlmostEqual(self._amount(gross), 700.0, places=2)
        # Operating Expenses = overhead 200 + interest 50 (unmapped) = 250.
        self.assertAlmostEqual(self._amount(opex), 250.0, places=2)
        # Operating Profit = 700 - 250 = 450.
        self.assertAlmostEqual(self._amount(operating), 450.0, places=2)
        # Finance / Tax unmapped -> zero.
        self.assertAlmostEqual(self._amount(finance), 0.0, places=2)
        self.assertAlmostEqual(self._amount(tax), 0.0, places=2)
        # PBT = Operating Profit - 0 = 450; Profit for Period = 450.
        self.assertAlmostEqual(self._amount(pbt), 450.0, places=2)
        self.assertAlmostEqual(self._amount(net), 450.0, places=2)
        self.assertEqual(net['name'], 'Profit for the Period')

        # Ties to the by-nature Net Profit exactly.
        by_nature = self.handler.compute(self.options)
        self.assertAlmostEqual(
            self._amount(net),
            self._amount(self._line_by_id(by_nature, 'net_profit')),
            places=2)
        self.assertAlmostEqual(
            result['totals']['net_profit'],
            by_nature['totals']['net_profit'], places=2)

    def test_by_function_finance_cost_mapping_splits_out_interest(self):
        # Map the interest account to Finance Costs.
        self.company.sudo().write({
            'eh_pnl_finance_cost_account_ids': [(6, 0, [
                self.account_interest.id])],
        })
        self._seed_full_pnl()
        opts = dict(self.options, pnl_presentation='by_function')
        result = self.handler.compute(opts)

        opex = self._line_by_id(result, 'section-operating_expenses-total')
        operating = self._line_by_id(result, 'operating_profit')
        finance = self._line_by_id(result, 'section-finance_costs-total')
        pbt = self._line_by_id(result, 'profit_before_tax')
        net = self._line_by_id(result, 'net_profit')

        # Interest now carved out: Operating Expenses = overhead 200 only.
        self.assertAlmostEqual(self._amount(opex), 200.0, places=2)
        # Operating Profit = 700 - 200 = 500.
        self.assertAlmostEqual(self._amount(operating), 500.0, places=2)
        # Finance Costs non-zero = 50.
        self.assertAlmostEqual(self._amount(finance), 50.0, places=2)
        # PBT = Operating Profit - Finance Costs = 500 - 50 = 450.
        self.assertAlmostEqual(self._amount(pbt), 450.0, places=2)
        # Profit for the Period still ties to Net Profit (450).
        self.assertAlmostEqual(self._amount(net), 450.0, places=2)

        # The interest account must appear under Finance Costs, not Opex.
        finance_leaf = self._line_by_id(
            result, 'account-%d' % self.account_interest.id)
        self.assertIsNotNone(finance_leaf)
        self.assertAlmostEqual(self._amount(finance_leaf), 50.0, places=2)

    def test_by_function_tax_mapping_splits_out_tax(self):
        # Map the interest account to Tax Expense instead.
        self.company.sudo().write({
            'eh_pnl_tax_expense_account_ids': [(6, 0, [
                self.account_interest.id])],
        })
        self._seed_full_pnl()
        opts = dict(self.options, pnl_presentation='by_function')
        result = self.handler.compute(opts)

        pbt = self._line_by_id(result, 'profit_before_tax')
        tax = self._line_by_id(result, 'section-tax_expense-total')
        net = self._line_by_id(result, 'net_profit')

        # Tax carved out: Operating Expenses = overhead 200 -> Operating
        # Profit 500 -> PBT 500 (no finance) -> Tax 50 -> Profit 450.
        self.assertAlmostEqual(self._amount(pbt), 500.0, places=2)
        self.assertAlmostEqual(self._amount(tax), 50.0, places=2)
        self.assertAlmostEqual(self._amount(net), 450.0, places=2)

    def test_by_function_account_in_both_mappings_counted_once(self):
        # Same account mapped to BOTH Finance Costs and Tax Expense must not
        # be subtracted twice, double counted in expenses, or emit a duplicate
        # 'account-<id>' line. Finance Costs wins; Tax Expense drops it, and
        # Profit for the Period still ties to the by-nature Net Profit.
        self.company.sudo().write({
            'eh_pnl_finance_cost_account_ids': [
                (6, 0, [self.account_interest.id])],
            'eh_pnl_tax_expense_account_ids': [
                (6, 0, [self.account_interest.id])],
        })
        self._seed_full_pnl()
        opts = dict(self.options, pnl_presentation='by_function')
        result = self.handler.compute(opts)

        finance = self._line_by_id(result, 'section-finance_costs-total')
        tax = self._line_by_id(result, 'section-tax_expense-total')
        opex = self._line_by_id(result, 'section-operating_expenses-total')
        pbt = self._line_by_id(result, 'profit_before_tax')
        net = self._line_by_id(result, 'net_profit')

        # Interest 50 is carved into Finance Costs only; Tax Expense is zero.
        self.assertAlmostEqual(self._amount(finance), 50.0, places=2)
        self.assertAlmostEqual(self._amount(tax), 0.0, places=2)
        # Operating Expenses = overhead 200 only (interest carved out once).
        self.assertAlmostEqual(self._amount(opex), 200.0, places=2)
        # PBT = 700 - 200 - 50 = 450; Profit for the Period = 450 (not 400).
        self.assertAlmostEqual(self._amount(pbt), 450.0, places=2)
        self.assertAlmostEqual(self._amount(net), 450.0, places=2)

        # The 'account-<id>' line for the shared account appears exactly once.
        leaf_id = 'account-%d' % self.account_interest.id
        leaf_count = sum(1 for l in result['lines'] if l['id'] == leaf_id)
        self.assertEqual(leaf_count, 1)

        # Ties to the by-nature Net Profit exactly.
        by_nature = self.handler.compute(self.options)
        self.assertAlmostEqual(
            self._amount(net),
            self._amount(self._line_by_id(by_nature, 'net_profit')),
            places=2)
        self.assertAlmostEqual(
            result['totals']['net_profit'],
            by_nature['totals']['net_profit'], places=2)

    def test_by_function_deferred_tax_splits_current_and_deferred(self):
        # IAS 1.82 / IAS 12.81(c): with a deferred-tax account mapped, the
        # by-function Tax Expense splits into distinct Current Tax and
        # Deferred Tax lines that sum to the total tax. Without the split,
        # only 'section-tax_expense-total' exists and these assertions fail.
        #
        # Revenue 1000, Cost of Sales 300, Overhead 200; then current tax 80
        # and deferred tax 20 (total tax 100). Both tax accounts are mapped to
        # Tax Expense; only the deferred account is marked deferred.
        self._post([
            {'account': self.account_revenue, 'credit': 1000.0},
            {'account': self.account_cash, 'debit': 1000.0},
        ])
        self._post([
            {'account': self.account_direct_cost, 'debit': 300.0},
            {'account': self.account_cash, 'credit': 300.0},
        ])
        self._post([
            {'account': self.account_overhead, 'debit': 200.0},
            {'account': self.account_cash, 'credit': 200.0},
        ])
        self._post([
            {'account': self.account_current_tax, 'debit': 80.0},
            {'account': self.account_cash, 'credit': 80.0},
        ])
        self._post([
            {'account': self.account_deferred_tax, 'debit': 20.0},
            {'account': self.account_cash, 'credit': 20.0},
        ])
        self.company.sudo().write({
            'eh_pnl_tax_expense_account_ids': [(6, 0, [
                self.account_current_tax.id,
                self.account_deferred_tax.id])],
            'eh_pnl_deferred_tax_account_ids': [(6, 0, [
                self.account_deferred_tax.id])],
        })
        opts = dict(self.options, pnl_presentation='by_function')
        result = self.handler.compute(opts)

        current = self._line_by_id(result, 'section-current_tax-total')
        deferred = self._line_by_id(result, 'section-deferred_tax-total')
        tax_total = self._line_by_id(result, 'section-tax_expense-total')
        net = self._line_by_id(result, 'net_profit')

        # Distinct Current Tax and Deferred Tax lines are present.
        self.assertIsNotNone(current, "Current Tax line must be present")
        self.assertIsNotNone(deferred, "Deferred Tax line must be present")
        self.assertEqual(current['name'], 'Current Tax')
        self.assertEqual(deferred['name'], 'Deferred Tax')

        self.assertAlmostEqual(self._amount(current), 80.0, places=2)
        self.assertAlmostEqual(self._amount(deferred), 20.0, places=2)
        # Current + Deferred sum to the total Tax Expense.
        self.assertAlmostEqual(self._amount(tax_total), 100.0, places=2)
        self.assertAlmostEqual(
            self._amount(current) + self._amount(deferred),
            self._amount(tax_total), places=2)

        # Each tax account appears exactly once, under its own sub-line.
        for acc in (self.account_current_tax, self.account_deferred_tax):
            leaf_id = 'account-%d' % acc.id
            self.assertEqual(
                sum(1 for l in result['lines'] if l['id'] == leaf_id), 1)

        # Profit for the Period still ties to the by-nature Net Profit:
        # 1000 - 300 - 200 - 80 - 20 = 400.
        self.assertAlmostEqual(self._amount(net), 400.0, places=2)
        by_nature = self.handler.compute(self.options)
        self.assertAlmostEqual(
            result['totals']['net_profit'],
            by_nature['totals']['net_profit'], places=2)

    def test_by_function_empty_deferred_map_shows_single_tax_line(self):
        # Regression guard: with tax accounts mapped but NO deferred-tax
        # account, a single Tax Expense line is shown and the split sub-lines
        # are absent, exactly as before.
        self._post([
            {'account': self.account_revenue, 'credit': 1000.0},
            {'account': self.account_cash, 'debit': 1000.0},
        ])
        self._post([
            {'account': self.account_current_tax, 'debit': 100.0},
            {'account': self.account_cash, 'credit': 100.0},
        ])
        self.company.sudo().write({
            'eh_pnl_tax_expense_account_ids': [(6, 0, [
                self.account_current_tax.id])],
        })
        opts = dict(self.options, pnl_presentation='by_function')
        result = self.handler.compute(opts)

        self.assertIsNone(
            self._line_by_id(result, 'section-current_tax-total'))
        self.assertIsNone(
            self._line_by_id(result, 'section-deferred_tax-total'))
        tax_total = self._line_by_id(result, 'section-tax_expense-total')
        self.assertAlmostEqual(self._amount(tax_total), 100.0, places=2)


@tagged('eh_account_dynamic_reports', 'integration', 'post_install',
        '-at_install')
class TestProfitAndLossVarianceSemantics(EhAccountIntegrationTestCase):
    """WS5 contract: the comparative layout exposes the variance columns the
    viewer colours by, and the payload carries the per-line directional hint
    (income higher_is_better=True, expense False) so favourable/unfavourable
    colouring is correct on the very first column rather than sign-only.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env[
            'eh.account.dynamic.report.handler.profit_and_loss']

    def _base_options(self):
        return {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
        }

    def test_comparative_layout_exposes_variance_labels(self):
        # Current-period and prior-year revenue so a variance is produced.
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 1000.0},
             {'account': self.account_cash, 'debit': 1000.0}],
            date=fields.Date.from_string('2026-06-15'))
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 800.0},
             {'account': self.account_cash, 'debit': 800.0}],
            date=fields.Date.from_string('2025-06-15'))
        options = dict(self._base_options(), comparison='previous_year')
        result = self.handler.compute(options)
        col_keys = [c['expression_label'] for c in result['columns']]
        # The UI colouring contract: these two labels MUST be present, or
        # cellClass() can never tell a change column from a balance column.
        self.assertIn('variance', col_keys)
        self.assertIn('variance_pct', col_keys)

    def test_higher_is_better_hint_by_section(self):
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 1000.0},
             {'account': self.account_expense, 'debit': 300.0},
             {'account': self.account_cash, 'debit': 700.0}],
            date=fields.Date.from_string('2026-06-15'))
        result = self.handler.compute(self._base_options())
        income_total = next(
            l for l in result['lines'] if l['id'] == 'section-income-total')
        expense_total = next(
            l for l in result['lines'] if l['id'] == 'section-expenses-total')
        net = next(l for l in result['lines'] if l['id'] == 'net_profit')
        # Income rows: a rise is favourable.
        self.assertIs(
            income_total['meta'].get('higher_is_better'), True,
            "income total must hint higher_is_better=True")
        # Expense rows: a rise is UNfavourable.
        self.assertIs(
            expense_total['meta'].get('higher_is_better'), False,
            "expense total must hint higher_is_better=False")
        # A higher net profit is favourable.
        self.assertIs(net['meta'].get('higher_is_better'), True)
        # Account leaves inherit their section's directionality.
        income_accounts = [
            l for l in result['lines']
            if (l.get('meta') or {}).get('account_code')
            and l['meta'].get('higher_is_better') is True]
        self.assertTrue(
            income_accounts,
            "income account leaves must carry higher_is_better=True")
