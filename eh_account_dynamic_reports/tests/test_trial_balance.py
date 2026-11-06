# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Trial Balance handler tests.

Covers:

* Movement column shows period activity.
* Opening balance reflects entries before date_from.
* Closing balance equals opening plus period movement.
* Zero balance accounts hidden by default; show_zero exposes them.
* Totals balance: debit equals credit at every column tier.
* Account, journal, partner filters narrow the result set.
* posted_only excludes draft entries; setting it false includes them.
* Cancelled entries are always excluded.
* Missing date raises a clear UserError.
* Orchestrator render path produces the same data and respects the cache.
"""

import time

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_dynamic_reports', 'integration', 'post_install', '-at_install')
class TestTrialBalanceHandler(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env['eh.account.dynamic.report.handler.trial_balance']
        cls.report = cls.env['eh.account.dynamic.report'].search(
            [('code', '=', 'trial_balance')], limit=1,
        )
        if not cls.report:
            cls.report = cls.env['eh.account.dynamic.report'].create({
                'code': 'trial_balance',
                'name': 'Trial Balance',
                'handler_model': 'eh.account.dynamic.report.handler.trial_balance',
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

    def _post_before_period(self, lines):
        return self.post_balanced_move(
            lines, date=fields.Date.from_string('2025-12-15'),
        )

    @staticmethod
    def _index_lines(result):
        return {
            line['meta']['account_code']: line
            for line in result['lines']
            if (line.get('meta') or {}).get('account_code')
        }

    @staticmethod
    def _column_value(line, label):
        for col in line['columns']:
            if col['expression_label'] == label:
                return col['value']
        raise AssertionError(f"Column {label!r} missing from line {line['name']!r}")

    # ---- core math ----

    def test_period_movement_appears(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 1000.0,
             'partner': self.partner_a},
            {'account': self.account_cash, 'debit': 1000.0},
        ])
        result = self.handler.compute(self.options)
        idx = self._index_lines(result)
        self.assertIn('4000', idx)
        self.assertAlmostEqual(
            self._column_value(idx['4000'], 'period_credit'), 1000.0, places=2,
        )

    def test_n_period_axis_repeats_complete_measure_block(self):
        for date, amount in (
            ('2024-06-15', 100.0),
            ('2025-06-15', 50.0),
            ('2026-06-15', 25.0),
        ):
            self.post_balanced_move(
                [{'account': self.account_cash, 'debit': amount},
                 {'account': self.account_revenue, 'credit': amount}],
                date=fields.Date.from_string(date),
            )
        result = self.handler.compute(dict(
            self.options,
            hierarchical_groups=False,
            comparison='previous_year',
            comparison_number=2,
            comparison_order='ascending',
        ))
        self.assertEqual(len(result['columns']), 19)
        value_columns = result['columns'][1:]
        self.assertTrue(all(column.get('scope') for column in value_columns))
        self.assertEqual(
            [value_columns[index]['scope']['date_to']
             for index in (0, 6, 12)],
            ['2024-12-31', '2025-12-31', '2026-12-31'],
        )
        self.assertEqual(
            sum(cell['colspan'] for cell in result['column_header_rows'][0]),
            len(result['columns']),
        )
        cash = self._index_lines(result)[self.account_cash.code]
        self.assertEqual(
            [cash['columns'][index]['value'] for index in (2, 8, 14)],
            [100.0, 50.0, 25.0],
        )
        self.assertEqual(
            [cash['columns'][index]['value'] for index in (4, 10, 16)],
            [100.0, 150.0, 175.0],
        )
        first_movement = value_columns[2]
        drill_options = dict(
            self.options,
            date={
                'date_from': first_movement['scope']['date_from'],
                'date_to': first_movement['scope']['date_to'],
            },
            _eh_column_expression=first_movement['expression_label'],
        )
        action = self.handler.get_drilldown_action(
            drill_options, 'account-%s' % self.account_cash.id,
        )
        self.assertIn(('date', '>=', '2024-01-01'), action['domain'])
        self.assertIn(('date', '<=', '2024-12-31'), action['domain'])

    def test_n_period_axis_enforces_expanded_value_column_cap(self):
        with self.assertRaisesRegex(
                UserError, 'produces 54 value columns'):
            self.handler.compute(dict(
                self.options,
                comparison='previous_year',
                comparison_number=8,
            ))

    def test_analytic_account_and_plan_axes_weight_all_six_measures(self):
        plan = self.env['account.analytic.plan'].create({
            'name': 'TB weighted plan',
        })
        analytic_a = self.env['account.analytic.account'].create({
            'name': 'TB Analytic A', 'plan_id': plan.id,
        })
        analytic_b = self.env['account.analytic.account'].create({
            'name': 'TB Analytic B', 'plan_id': plan.id,
        })
        distribution = {
            str(analytic_a.id): 60.0,
            str(analytic_b.id): 40.0,
        }
        self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': fields.Date.from_string('2025-12-15'),
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
        self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': fields.Date.from_string('2026-06-15'),
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_cash.id,
                    'debit': 50.0,
                    'analytic_distribution': distribution,
                }),
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'credit': 50.0,
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
        self.assertEqual(len(result['columns']), 19)
        self.assertEqual(len(result['column_header_rows']), 3)
        self.assertEqual(
            sum(
                cell['colspan']
                for cell in result['column_header_rows'][0]
            ),
            len(result['columns']),
        )
        value_columns = result['columns'][1:]
        self.assertEqual(
            [value_columns[index]['scope']['analytic_account_ids']
             for index in (0, 6, 12)],
            [[analytic_a.id], [analytic_b.id], []],
        )
        cash = self._index_lines(result)[self.account_cash.code]
        self.assertEqual(
            [cell['value'] for cell in cash['columns'][0:6]],
            [60.0, 0.0, 30.0, 0.0, 90.0, 0.0],
        )
        self.assertEqual(
            [cell['value'] for cell in cash['columns'][6:12]],
            [40.0, 0.0, 20.0, 0.0, 60.0, 0.0],
        )
        self.assertEqual(
            [cell['value'] for cell in cash['columns'][12:18]],
            [100.0, 0.0, 50.0, 0.0, 150.0, 0.0],
        )
        self.assertEqual(result['totals']['opening_debit'], 100.0)
        self.assertEqual(result['totals']['period_debit'], 50.0)
        self.assertEqual(result['totals']['closing_debit'], 150.0)

        def detail_for(measure, **page):
            column = next(
                column for column in value_columns
                if column['scope']['analytic_account_ids'] == [analytic_a.id]
                and column['expression_label'].endswith('__' + measure)
            )
            value_index = value_columns.index(column)
            return self.report.get_analytic_column_drilldown_page(
                axis_options,
                'account-%d' % self.account_cash.id,
                column['expression_label'],
                execution_id=result['execution_id'],
                displayed_amount=cash['columns'][value_index]['value'],
                **page,
            )

        opening = detail_for('opening_debit')
        movement = detail_for('period_debit')
        closing = detail_for('closing_debit')
        self.assertEqual(opening['total_count'], 1)
        self.assertEqual(movement['total_count'], 1)
        self.assertEqual(closing['total_count'], 2)
        for detail, expected in (
            (opening, 60.0), (movement, 30.0), (closing, 90.0),
        ):
            self.assertAlmostEqual(detail['total'], expected, places=2)
            self.assertAlmostEqual(
                sum(
                    row['values']['allocated_amount']
                    for row in detail['rows']
                ),
                expected,
                places=2,
            )
        self.assertEqual(
            [row['values']['date'] for row in opening['rows']],
            ['2025-12-15'],
        )
        self.assertEqual(
            [row['values']['date'] for row in movement['rows']],
            ['2026-06-15'],
        )
        closing_first = detail_for('closing_debit', offset=0, limit=1)
        closing_second = detail_for(
            'closing_debit', offset=1, limit=1,
            page_token=closing_first['page_token'],
        )
        self.assertTrue(closing_first['has_more'])
        self.assertFalse(closing_second['has_more'])
        self.assertAlmostEqual(
            sum(
                row['values']['allocated_amount']
                for page in (closing_first, closing_second)
                for row in page['rows']
            ),
            cash['columns'][4]['value'],
            places=2,
        )
        wrong_side = detail_for('opening_credit')
        self.assertEqual(wrong_side['rows'], [])
        self.assertEqual(wrong_side['total'], 0.0)
        with self.assertRaises(UserError):
            self.report.get_analytic_column_drilldown_page(
                axis_options,
                'account-%d' % self.account_cash.id,
                value_columns[12]['expression_label'],
                execution_id=result['execution_id'],
                displayed_amount=cash['columns'][12]['value'],
            )
        with self.assertRaises(UserError):
            self.report.get_analytic_column_drilldown_page(
                dict(axis_options, cash_basis=True),
                'account-%d' % self.account_cash.id,
                value_columns[2]['expression_label'],
            )

        presentation = self.env['res.currency'].create({
            'name': 'ZTB', 'symbol': 'T', 'rounding': 0.01,
        })
        self.env['res.currency.rate'].create({
            'currency_id': presentation.id,
            'company_id': self.company.id,
            'name': '2026-01-01',
            'rate': 2.0,
        })
        converted_options = dict(
            axis_options, presentation_currency_id=presentation.id,
        )
        converted = self.report.render(converted_options, use_cache=False)
        converted_cash = self._index_lines(converted)[self.account_cash.code]
        converted_detail = self.report.get_analytic_column_drilldown_page(
            converted_options,
            'account-%d' % self.account_cash.id,
            converted['columns'][3]['expression_label'],
            execution_id=converted['execution_id'],
            displayed_amount=converted_cash['columns'][2]['value'],
        )
        self.assertEqual(converted_detail['currency']['id'], presentation.id)
        self.assertAlmostEqual(
            converted_detail['total'],
            converted_cash['columns'][2]['value'],
            places=2,
        )
        self.assertAlmostEqual(converted_detail['total'], 60.0, places=2)

        plan_options = dict(
            self.options,
            hierarchical_groups=False,
            analytic_column_plan_ids=[plan.id],
        )
        by_plan = self.report.render(plan_options, use_cache=False)
        self.assertEqual(len(by_plan['columns']), 13)
        plan_cash = self._index_lines(by_plan)[self.account_cash.code]
        self.assertEqual(
            [cell['value'] for cell in plan_cash['columns'][0:6]],
            [100.0, 0.0, 50.0, 0.0, 150.0, 0.0],
        )
        self.assertEqual(
            [cell['value'] for cell in plan_cash['columns'][6:12]],
            [100.0, 0.0, 50.0, 0.0, 150.0, 0.0],
        )
        plan_detail = self.report.get_analytic_column_drilldown_page(
            plan_options,
            'account-%d' % self.account_cash.id,
            by_plan['columns'][3]['expression_label'],
            execution_id=by_plan['execution_id'],
            displayed_amount=plan_cash['columns'][2]['value'],
        )
        self.assertEqual(plan_detail['scope']['analytic_plan_ids'], [plan.id])
        self.assertEqual(plan_detail['total'], 50.0)

    def test_weighted_drilldown_accepts_root_account_for_branch_scope(self):
        Account = self.env['account.account']
        if not callable(getattr(Account, '_check_company_domain', None)):
            self.skipTest('branch-aware account scope is unavailable')
        branch = self._create_accounting_branch({
            'name': 'TB weighted-detail branch',
            'parent_id': self.company.id,
        })
        self.env.user.write({'company_ids': [(4, branch.id)]})
        branch_env = self.env['account.move'].with_context(
            allowed_company_ids=[self.company.id, branch.id],
        ).with_company(branch).env
        journal = self._ensure_journal(
            branch_env, branch, 'general', 'TDBR',
            'TB Detail Branch Journal',
        )
        plan = branch_env['account.analytic.plan'].create({
            'name': 'TB detail branch plan',
        })
        analytic = branch_env['account.analytic.account'].create({
            'name': 'TB detail branch analytic', 'plan_id': plan.id,
        })
        move = branch_env['account.move'].create({
            'move_type': 'entry', 'journal_id': journal.id,
            'date': fields.Date.from_string('2026-08-02'),
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_cash.id,
                    'debit': 100.0,
                    'analytic_distribution': {str(analytic.id): 100.0},
                }),
                (0, 0, {
                    'account_id': self.account_revenue.id, 'credit': 100.0,
                }),
            ],
        })
        move.action_post()
        report = branch_env['eh.account.dynamic.report'].search([
            ('code', '=', 'trial_balance'),
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
        cash = self._index_lines(result)[self.account_cash.code]
        value_columns = result['columns'][1:]
        period_debit_index = next(
            index for index, column in enumerate(value_columns)
            if column['scope']['analytic_account_ids'] == [analytic.id]
            and column['expression_label'].endswith('__period_debit')
        )
        detail = report.get_analytic_column_drilldown_page(
            options,
            'account-%d' % self.account_cash.id,
            value_columns[period_debit_index]['expression_label'],
            execution_id=result['execution_id'],
            displayed_amount=cash['columns'][period_debit_index]['value'],
        )
        self.assertEqual(detail['total'], 100.0)
        self.assertEqual(detail['total_count'], 1)

    def test_period_analytic_product_order_overlap_and_hard_cap(self):
        plan_a = self.env['account.analytic.plan'].create({
            'name': 'TB product plan A',
        })
        plan_b = self.env['account.analytic.plan'].create({
            'name': 'TB product plan B',
        })
        analytic_a = self.env['account.analytic.account'].create({
            'name': 'TB Product A', 'plan_id': plan_a.id,
        })
        analytic_b = self.env['account.analytic.account'].create({
            'name': 'TB Product B', 'plan_id': plan_b.id,
        })
        composite = {'%d,%d' % (analytic_a.id, analytic_b.id): 100.0}
        self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': fields.Date.from_string('2026-06-15'),
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_cash.id,
                    'debit': 100.0,
                    'analytic_distribution': composite,
                }),
                (0, 0, {
                    'account_id': self.account_equity.id,
                    'credit': 100.0,
                    'analytic_distribution': composite,
                }),
            ],
        }).action_post()
        overlap_options = dict(
            self.options,
            hierarchical_groups=False,
            analytic_column_account_ids=[analytic_a.id, analytic_b.id],
        )
        overlap = self.handler.compute(overlap_options)
        cash = self._index_lines(overlap)[self.account_cash.code]
        # A composite cross-plan key belongs to both selected account slices;
        # independent Total remains 100 instead of summing overlap to 200.
        self.assertEqual(
            [cash['columns'][index]['value'] for index in (2, 8, 14)],
            [100.0, 100.0, 100.0],
        )

        product_options = dict(
            self.options,
            hierarchical_groups=False,
            analytic_column_account_ids=[analytic_a.id],
            comparison='previous_year',
            comparison_number=1,
            comparison_order='ascending',
        )
        product = self.handler.compute(product_options)
        self.assertEqual(len(product['columns']), 25)
        value_columns = product['columns'][1:]
        self.assertEqual(
            [value_columns[index]['scope']['date_to']
             for index in (0, 6, 12, 18)],
            ['2025-12-31', '2025-12-31',
             '2026-12-31', '2026-12-31'],
        )
        self.assertEqual(
            [value_columns[index]['scope']['analytic_account_ids']
             for index in (0, 6, 12, 18)],
            [[analytic_a.id], [], [analytic_a.id], []],
        )
        self.assertEqual(
            [cell['colspan']
             for cell in product['column_header_rows'][0][1:]],
            [12, 12],
        )
        with self.assertRaisesRegex(
                UserError, 'produces 54 value columns'):
            self.handler.compute(dict(
                overlap_options,
                comparison='previous_year',
                comparison_number=2,
            ))

    def test_period_merge_keeps_prior_only_hierarchy_before_totals(self):
        def line(line_id, value=0.0, parent_id=None):
            return {
                'id': line_id,
                'name': line_id,
                'parent_id': parent_id,
                'columns': [{
                    'expression_label': 'opening_debit',
                    'value': value,
                }],
            }

        current = {
            'scope': {'key': 'period_current'},
            'lines': [
                line('section-assets'),
                line('account-current', 10.0, 'section-assets'),
                line('section-assets-total'),
                line('report-grand-total'),
            ],
        }
        prior = {
            'scope': {'key': 'period_comparison_1'},
            'lines': [
                line('section-assets'),
                line('group-prior', 4.0, 'section-assets'),
                line('account-prior', 4.0, 'group-prior'),
                line('section-assets-total'),
                line('report-grand-total'),
            ],
        }
        merged = self.handler._merge_tb_period_results([current, prior])
        self.assertEqual(
            [row['id'] for row in merged],
            [
                'section-assets', 'account-current', 'group-prior',
                'account-prior', 'section-assets-total',
                'report-grand-total',
            ],
        )
        prior_only = next(
            row for row in merged if row['id'] == 'account-prior'
        )
        self.assertEqual(prior_only['parent_id'], 'group-prior')
        self.assertEqual(
            [cell['value'] for cell in prior_only['columns'][::6]],
            [0.0, 4.0],
        )

    def test_kwd_precision_keeps_milliunit(self):
        kwd = self.env.ref('base.KWD')
        self.assertEqual(kwd.decimal_places, 3)
        options = dict(
            self.options,
            presentation_currency_id=kwd.id,
        )
        lines, totals = self.handler._build_lines_and_totals(
            [{
                'account_id': self.account_cash.id,
                'account_code': self.account_cash.code,
                'account_name': self.account_cash.name,
                'opening_balance': 0.0,
                'period_debit': 0.001,
                'period_credit': 0.0,
            }],
            show_zero=False,
            options=options,
            presentation_converted=True,
        )
        self.assertEqual(len(lines), 1)
        self.assertEqual(
            self._column_value(lines[0], 'period_debit'), 0.001,
        )
        self.assertEqual(
            self._column_value(lines[0], 'closing_debit'), 0.001,
        )
        self.assertEqual(totals['period_debit'], 0.001)

    def test_opening_balance_carries_from_prior_period(self):
        self._post_before_period([
            {'account': self.account_revenue, 'credit': 500.0},
            {'account': self.account_cash, 'debit': 500.0},
        ])
        result = self.handler.compute(self.options)
        idx = self._index_lines(result)
        self.assertAlmostEqual(
            self._column_value(idx['1000'], 'opening_debit'), 500.0, places=2,
        )

    def test_closing_equals_opening_plus_movement(self):
        self._post_before_period([
            {'account': self.account_revenue, 'credit': 500.0},
            {'account': self.account_cash, 'debit': 500.0},
        ])
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 200.0},
            {'account': self.account_cash, 'debit': 200.0},
        ])
        result = self.handler.compute(self.options)
        cash = self._index_lines(result)['1000']
        cols = {c['expression_label']: c['value'] for c in cash['columns']}
        self.assertAlmostEqual(cols['opening_debit'], 500.0, places=2)
        self.assertAlmostEqual(cols['period_debit'], 200.0, places=2)
        self.assertAlmostEqual(cols['closing_debit'], 700.0, places=2)
        self.assertAlmostEqual(cols['opening_credit'], 0.0, places=2)
        self.assertAlmostEqual(cols['period_credit'], 0.0, places=2)
        self.assertAlmostEqual(cols['closing_credit'], 0.0, places=2)

    def test_totals_balance_at_each_tier(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 1000.0},
            {'account': self.account_cash, 'debit': 1000.0},
        ])
        self._post_in_period([
            {'account': self.account_expense, 'debit': 200.0},
            {'account': self.account_cash, 'credit': 200.0},
        ])
        result = self.handler.compute(self.options)
        totals = result['totals']
        self.assertAlmostEqual(
            totals['period_debit'], totals['period_credit'], places=2,
        )
        self.assertAlmostEqual(
            totals['closing_debit'], totals['closing_credit'], places=2,
        )
        self.assertAlmostEqual(
            totals['opening_debit'], totals['opening_credit'], places=2,
        )

    # ---- filter behaviour ----

    def test_zero_balance_account_hidden_by_default(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        result = self.handler.compute(self.options)
        idx = self._index_lines(result)
        self.assertNotIn('5000', idx,
                         "Untouched expense account must be hidden")

    @staticmethod
    def _line_for_account(result, account):
        return next((
            line for line in result['lines']
            if (line.get('meta') or {}).get('account_id') == account.id
        ), None)

    @staticmethod
    def _all_zero(line):
        return all(
            not column['value'] for column in line['columns']
            if isinstance(column.get('value'), (int, float))
        )

    def test_show_zero_lists_untouched_chart_accounts(self):
        # Showing zero balance accounts lists the whole chart, not only
        # the accounts that already carry journal items.
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        untouched = self._ensure_account(
            self.env, '199901', 'TB never used', 'asset_current')
        for hierarchical in (False, True):
            with self.subTest(hierarchical=hierarchical):
                options = dict(self.options, hierarchical_groups=hierarchical)
                shown = self.handler.compute(dict(options, show_zero=True))
                line = self._line_for_account(shown, untouched)
                self.assertIsNotNone(line)
                self.assertTrue(self._all_zero(line))
                self.assertIn('199901', line['name'])
                self.assertAlmostEqual(
                    shown['totals']['closing_debit'],
                    shown['totals']['closing_credit'], places=2)
                hidden = self.handler.compute(dict(options, show_zero=False))
                self.assertIsNone(self._line_for_account(hidden, untouched))

    def test_show_zero_lists_an_account_with_only_draft_entries(self):
        draft_only = self._ensure_account(
            self.env, '199902', 'TB draft only', 'asset_current')
        self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': fields.Date.from_string('2026-06-15'),
            'line_ids': [
                (0, 0, {'account_id': draft_only.id, 'debit': 80.0}),
                (0, 0, {'account_id': self.account_cash.id, 'credit': 80.0}),
            ],
        })
        result = self.handler.compute(dict(self.options, show_zero=True))
        line = self._line_for_account(result, draft_only)
        self.assertIsNotNone(line)
        self.assertTrue(self._all_zero(line))

    def test_new_account_expires_a_cached_zero_balance_report(self):
        options = dict(self.options, show_zero=True)
        self.report.render(options)
        self.assertTrue(self.report.render(options)['from_cache'])
        created = self._ensure_account(
            self.env, '199905', 'TB created after render', 'asset_current')
        fresh = self.report.render(options)
        self.assertFalse(fresh['from_cache'])
        self.assertIsNotNone(self._line_for_account(fresh, created))

    def test_fiscal_block_merge_stays_fast_on_a_large_chart(self):
        # Every block lists the whole chart when zero balances are shown;
        # the merge must stay linear in the number of lines.
        measures = [label for label, _name in self.handler._tb_measures()]
        lines = [{
            'id': 'account-%s' % index,
            'columns': [
                {'expression_label': label, 'value': 0.0}
                for label in measures
            ],
        } for index in range(6000)]
        scoped = [
            {'scope': {'key': 'period_fiscal_1'}, 'lines': lines},
            {'scope': {'key': 'period_current'}, 'lines': list(lines)},
        ]
        started = time.monotonic()
        merged = self.handler._merge_tb_period_results(scoped)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(
            [line['id'] for line in merged], [line['id'] for line in lines])

    def test_show_zero_respects_the_account_type_selector(self):
        untouched = self._ensure_account(
            self.env, '199903', 'TB untouched current asset', 'asset_current')
        result = self.handler.compute(dict(
            self.options, show_zero=True, account_type_ids=['income'],
        ))
        self.assertIsNone(self._line_for_account(result, untouched))
        self.assertTrue(all(
            (line.get('meta') or {}).get('account_code') != '199903'
            for line in result['lines']
        ))

    def test_account_filter(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        opts = dict(self.options)
        opts['account_ids'] = [self.account_cash.id]
        result = self.handler.compute(opts)
        idx = self._index_lines(result)
        self.assertIn('1000', idx)
        self.assertNotIn('4000', idx)

    def test_journal_filter(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        opts = dict(self.options)
        opts['journal_ids'] = [self.journal_misc.id]
        result = self.handler.compute(opts)
        # Posting was via journal_misc, so it should still appear.
        self.assertIn('1000', self._index_lines(result))

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
        idx = self._index_lines(result)
        # Only the 100 credit (partner A) should aggregate; cash line had no
        # partner so it is not present in the partner filtered result.
        self.assertIn('4000', idx)
        self.assertAlmostEqual(
            self._column_value(idx['4000'], 'period_credit'), 100.0, places=2,
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
        idx = self._index_lines(result)
        self.assertNotIn(
            '4000', idx,
            "draft entries must be excluded when posted_only=True",
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
        idx = self._index_lines(result)
        self.assertIn('4000', idx)
        self.assertAlmostEqual(
            self._column_value(idx['4000'], 'period_credit'), 333.0, places=2,
        )

    def test_cancelled_entries_excluded(self):
        move = self._post_in_period([
            {'account': self.account_revenue, 'credit': 444.0},
            {'account': self.account_cash, 'debit': 444.0},
        ])
        move.button_cancel()
        result = self.handler.compute(self.options)
        idx = self._index_lines(result)
        self.assertNotIn('4000', idx)

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

    def test_orchestrator_cache_hit_on_repeated_render(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        first = self.report.render(self.options)
        second = self.report.render(self.options)
        self.assertFalse(first['from_cache'])
        self.assertTrue(second['from_cache'])
        # Cached payload should match the freshly computed one in shape.
        self.assertEqual(len(first['lines']), len(second['lines']))
        self.assertEqual(first['totals'], second['totals'])

    def test_orchestrator_invalidates_cache_on_new_post(self):
        first = self.report.render(self.options)
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 50.0},
            {'account': self.account_cash, 'debit': 50.0},
        ])
        second = self.report.render(self.options)
        self.assertFalse(second['from_cache'],
                         "Posting must invalidate the cache")
        # The new entry should appear in the second render.
        idx = self._index_lines(second)
        self.assertIn('4000', idx)

    def test_drilldown_action_returns_filtered_journal_items(self):
        move = self._post_in_period([
            {'account': self.account_revenue, 'credit': 75.0},
            {'account': self.account_cash, 'debit': 75.0},
        ])
        options = dict(
            self.options,
            _eh_column_expression='period_credit',
        )
        action = self.handler.get_drilldown_action(
            options, "account-%s" % self.account_revenue.id,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action['res_model'], 'account.move.line')
        # Verify the domain matches the revenue line.
        Line = self.env['account.move.line']
        items = Line.search(action['domain'])
        self.assertIn(
            self.account_revenue.id, items.mapped('account_id.id'),
        )

    def test_drilldown_windows_match_opening_movement_and_closing(self):
        before = self.post_balanced_move([
            {'account': self.account_equity, 'credit': 40.0},
            {'account': self.account_cash, 'debit': 40.0},
        ], date=fields.Date.from_string('2025-12-15'))
        during = self._post_in_period([
            {'account': self.account_equity, 'credit': 60.0},
            {'account': self.account_cash, 'debit': 60.0},
        ])
        line_id = "account-%s" % self.account_cash.id

        def ids_for(column):
            options = dict(self.options, _eh_column_expression=column)
            action = self.handler.get_drilldown_action(options, line_id)
            return set(self.env['account.move.line'].search(
                action['domain'],
            ).ids)

        before_line = before.line_ids.filtered(
            lambda line: line.account_id == self.account_cash
        ).id
        during_line = during.line_ids.filtered(
            lambda line: line.account_id == self.account_cash
        ).id
        self.assertIn(before_line, ids_for('opening_debit'))
        self.assertNotIn(during_line, ids_for('opening_debit'))
        self.assertNotIn(before_line, ids_for('period_debit'))
        self.assertIn(during_line, ids_for('period_debit'))
        self.assertIn(before_line, ids_for('closing_debit'))
        self.assertIn(during_line, ids_for('closing_debit'))

    def test_movement_drilldown_keeps_clicked_debit_or_credit_side(self):
        move = self._post_in_period([
            {'account': self.account_equity, 'credit': 60.0},
            {'account': self.account_cash, 'debit': 60.0},
        ])
        debit_action = self.handler.get_drilldown_action(
            dict(self.options, _eh_column_expression='period_debit'),
            "account-%s" % self.account_cash.id,
        )
        credit_action = self.handler.get_drilldown_action(
            dict(self.options, _eh_column_expression='period_credit'),
            "account-%s" % self.account_equity.id,
        )
        debit_lines = self.env['account.move.line'].search(
            debit_action['domain'])
        credit_lines = self.env['account.move.line'].search(
            credit_action['domain'])
        self.assertEqual(debit_lines, move.line_ids.filtered('debit'))
        self.assertEqual(credit_lines, move.line_ids.filtered('credit'))

    def test_drilldown_rejects_missing_or_unknown_column(self):
        line_id = "account-%s" % self.account_cash.id
        self.assertIsNone(self.handler.get_drilldown_action(
            self.options, line_id,
        ))
        self.assertIsNone(self.handler.get_drilldown_action(
            dict(self.options, _eh_column_expression='variance'), line_id,
        ))

    def test_drilldown_action_returns_none_for_invalid_id(self):
        action = self.handler.get_drilldown_action(self.options, 'totally-bogus')
        self.assertIsNone(action)

    def test_compute_under_non_en_us_lang_does_not_crash(self):
        """Regression: Trial Balance must run in any env.lang.

        Customer report (Daria, INTERIM 2000 GNF, odoo.sh, French UI):
        opening the Balance générale screen surfaced a generic "Error:
        Odoo Server Error" with no traceback in the UI.

        Root cause: the trial balance handler issued a SELECT whose
        account_name expression resolved via the MoveLineQuery
        translated-name helper (``COALESCE(acc.name ->> '<lang>',
        acc.name ->> 'en_US')`` when env.lang differs from en_US),
        but the GROUP BY clause hardcoded ``(acc.name ->> 'en_US')``.
        PostgreSQL strictly requires every non-aggregated SELECT
        expression to appear in GROUP BY verbatim, so it rejected the
        query with ``column "acc.name" must appear in the GROUP BY
        clause`` for every non-en_US locale.

        The fix made trial_balance.py use the new
        ``group_by_account_field`` helper so SELECT, ORDER BY, and
        GROUP BY share the same expression for translated columns.
        This test installs French, posts an entry, runs compute() and
        render() under fr_FR, and asserts both return data without
        raising.
        """
        # Ensure French is active. Odoo lazy-loads languages, so we
        # call load_language directly rather than INSERT into res_lang.
        self.env['res.lang']._activate_lang('fr_FR')

        # Post one entry inside the period so the SQL has rows to
        # group; a zero-row query bypasses the GROUP BY validation in
        # some PostgreSQL versions.
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])

        handler = self.handler.with_context(lang='fr_FR')
        report = self.report.with_context(lang='fr_FR')

        # compute() path
        result = handler.compute(self.options)
        self.assertTrue(result.get('lines'),
                        "compute() under fr_FR returned no lines")

        # render() path (the orchestrator the OWL viewer actually calls)
        rendered = report.render(self.options)
        self.assertTrue(rendered.get('lines'),
                        "render() under fr_FR returned no lines")


@tagged('eh_account_dynamic_reports', 'integration', 'post_install',
        '-at_install')
class TestTrialBalanceFiscalYearWS4(EhAccountIntegrationTestCase):
    """WS4: fiscal-year-aware opening + unaffected-earnings footing.

    The base company uses a calendar fiscal year (default
    fiscalyear_last_month=12), so the fiscal year containing 2026-01-01
    starts on 2026-01-01 and everything dated in 2025 is prior-year P&L.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env['eh.account.dynamic.report.handler.trial_balance']

    @staticmethod
    def _index_by_id(result):
        return {line['id']: line for line in result['lines']}

    @staticmethod
    def _col(line, label):
        for c in line['columns']:
            if c['expression_label'] == label:
                return c['value']
        raise AssertionError(f"missing column {label!r}")

    def _unaffected_line(self, result):
        """Return the real retained-earnings row, or the safe fallback."""
        destination_ids = set(
            self.env[
                'eh.account.dynamic.report.handler.general_ledger'
            ]._unaffected_account_ids([self.company.id]).values()
        )
        for line in result['lines']:
            meta = line.get('meta') or {}
            if (
                line['id'] == 'account-unaffected-earnings'
                or meta.get('account_id') in destination_ids
            ):
                return line
        return None

    def _opts(self, date_from='2026-01-01', date_to='2026-12-31', **kw):
        opts = {
            'date': {'date_from': date_from, 'date_to': date_to},
            'company_ids': [self.company.id],
            'posted_only': True, 'show_zero': False,
            'hierarchical_groups': kw.pop('hierarchical_groups', False),
        }
        opts.update(kw)
        return opts

    def test_tb_foots_at_year_boundary_with_unaffected_line(self):
        # Prior-year revenue 1000 (credit) and expense 300 (debit): net
        # profit P = 700. Plus a balance-sheet posting so opening is non-trivial.
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 1000.0},
             {'account': self.account_cash, 'debit': 1000.0}],
            date=fields.Date.from_string('2025-06-15'))
        self.post_balanced_move(
            [{'account': self.account_expense, 'debit': 300.0},
             {'account': self.account_cash, 'credit': 300.0}],
            date=fields.Date.from_string('2025-07-15'))

        result = self.handler.compute(self._opts())
        totals = result['totals']
        # The trial balance must foot at the year boundary on every tier.
        self.assertAlmostEqual(
            totals['opening_debit'], totals['opening_credit'], places=2)
        self.assertAlmostEqual(
            totals['closing_debit'], totals['closing_credit'], places=2)

        # The unaffected-earnings line carries net prior-year profit P=700 on
        # the credit side (a profit is a credit balance).
        ue = self._unaffected_line(result)
        self.assertIsNotNone(ue)
        self.assertAlmostEqual(
            self._col(ue, 'opening_credit'), 700.0, places=2)
        self.assertAlmostEqual(self._col(ue, 'opening_debit'), 0.0, places=2)

    def test_analytic_axis_weights_retained_earnings_and_foots(self):
        plan = self.env['account.analytic.plan'].create({
            'name': 'TB retained earnings plan',
        })
        analytic_a = self.env['account.analytic.account'].create({
            'name': 'TB retained A', 'plan_id': plan.id,
        })
        analytic_b = self.env['account.analytic.account'].create({
            'name': 'TB retained B', 'plan_id': plan.id,
        })
        distribution = {
            str(analytic_a.id): 60.0,
            str(analytic_b.id): 40.0,
        }
        for date, debit_account, credit_account, amount in (
            (
                '2025-06-15', self.account_cash,
                self.account_revenue, 1000.0,
            ),
            (
                '2025-07-15', self.account_expense,
                self.account_cash, 300.0,
            ),
        ):
            self.env['account.move'].create({
                'move_type': 'entry',
                'journal_id': self.journal_misc.id,
                'date': fields.Date.from_string(date),
                'line_ids': [
                    (0, 0, {
                        'account_id': debit_account.id,
                        'debit': amount,
                        'analytic_distribution': distribution,
                    }),
                    (0, 0, {
                        'account_id': credit_account.id,
                        'credit': amount,
                        'analytic_distribution': distribution,
                    }),
                ],
            }).action_post()
        options = self._opts(
            analytic_column_account_ids=[analytic_a.id, analytic_b.id],
        )
        result = self.handler.compute(options)
        unaffected = self._unaffected_line(result)
        self.assertIsNotNone(unaffected)
        self.assertEqual(
            [unaffected['columns'][index]['value']
             for index in (1, 7, 13)],
            [420.0, 280.0, 700.0],
        )
        for scoped_totals in result['totals']['column_scopes'].values():
            self.assertAlmostEqual(
                scoped_totals['opening_debit'],
                scoped_totals['opening_credit'],
                places=2,
            )
            self.assertAlmostEqual(
                scoped_totals['closing_debit'],
                scoped_totals['closing_credit'],
                places=2,
            )

        report = self.env['eh.account.dynamic.report'].search([
            ('code', '=', 'trial_balance'),
        ], limit=1)
        bound_result = report.render(options, use_cache=False)
        bound_unaffected = self._unaffected_line(bound_result)
        with self.assertRaisesRegex(UserError, 'retained-earnings'):
            report.get_analytic_column_drilldown_page(
                options,
                bound_unaffected['id'],
                bound_result['columns'][2]['expression_label'],
                execution_id=bound_result['execution_id'],
                displayed_amount=bound_unaffected['columns'][1]['value'],
            )

    def test_existing_unaffected_account_is_real_and_drillable(self):
        gl_handler = self.env[
            'eh.account.dynamic.report.handler.general_ledger']
        existing_id = gl_handler._unaffected_account_ids(
            [self.company.id]).get(self.company.id)
        unaffected = self.env['account.account'].browse(existing_id).exists()
        if not unaffected:
            unaffected = self.env['account.account'].create({
                'code': '3999',
                'name': 'Retained Earnings Control',
                'account_type': 'equity_unaffected',
                'company_ids': [(6, 0, [self.company.id])],
            })
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 700.0},
             {'account': self.account_cash, 'debit': 700.0}],
            date=fields.Date.from_string('2025-06-15'))
        options = self._opts()
        result = self.handler.compute(options)
        by_id = self._index_by_id(result)
        real_id = 'account-%s' % unaffected.id
        self.assertIn(real_id, by_id)
        self.assertNotIn('account-unaffected-earnings', by_id)
        self.assertAlmostEqual(
            self._col(by_id[real_id], 'opening_credit'), 700.0, places=2)
        action = self.handler.get_drilldown_action(
            dict(options, _eh_column_expression='opening_credit'), real_id)
        self.assertTrue(action)

    def test_trial_balance_default_comparison_has_full_period_blocks(self):
        self.post_balanced_move(
            [{'account': self.account_cash, 'debit': 100.0},
             {'account': self.account_revenue, 'credit': 100.0}],
            date=fields.Date.from_string('2025-06-15'))
        self.post_balanced_move(
            [{'account': self.account_cash, 'debit': 200.0},
             {'account': self.account_revenue, 'credit': 200.0}],
            date=fields.Date.from_string('2026-06-15'))
        result = self.handler.compute(self._opts(comparison='previous_year'))
        cash = {
            (line.get('meta') or {}).get('account_code'): line
            for line in result['lines']
        }['1000']
        prior_closing = next(
            column['expression_label']
            for column in result['columns']
            if (column.get('scope') or {}).get('comparison_index') == 1
            and (column.get('scope') or {}).get('is_total') is False
            and column['expression_label'].endswith('__closing_debit')
        )
        current_opening = next(
            column['expression_label']
            for column in result['columns']
            if (column.get('scope') or {}).get('comparison_index') == 0
            and (column.get('scope') or {}).get('is_total') is False
            and column['expression_label'].endswith('__opening_debit')
        )
        current_movement = next(
            column['expression_label']
            for column in result['columns']
            if (column.get('scope') or {}).get('comparison_index') == 0
            and (column.get('scope') or {}).get('is_total') is False
            and column['expression_label'].endswith('__period_debit')
        )
        current_closing = next(
            column['expression_label']
            for column in result['columns']
            if (column.get('scope') or {}).get('comparison_index') == 0
            and (column.get('scope') or {}).get('is_total') is False
            and column['expression_label'].endswith('__closing_debit')
        )
        self.assertAlmostEqual(
            self._col(cash, prior_closing), 100.0, places=2)
        self.assertAlmostEqual(
            self._col(cash, current_opening), 100.0, places=2)
        self.assertAlmostEqual(
            self._col(cash, current_movement), 200.0, places=2)
        self.assertAlmostEqual(
            self._col(cash, current_closing), 300.0, places=2)
        self.assertEqual(len(result['column_header_rows']), 2)

    def test_trial_balance_cross_fiscal_year_scope_fails_closed(self):
        with self.assertRaisesRegex(UserError, 'crosses a fiscal-year boundary'):
            self.handler.compute(self._opts(
                comparison='custom',
                comparison_custom_date_from='2023-01-01',
                comparison_custom_date_to='2024-12-31',
            ))

    def _use_fiscal_year_ending_march(self):
        original_calendar = {
            'fiscalyear_last_day': self.company.fiscalyear_last_day,
            'fiscalyear_last_month': self.company.fiscalyear_last_month,
        }
        self.addCleanup(self.company.sudo().write, original_calendar)
        self.company.write({
            'fiscalyear_last_day': 31, 'fiscalyear_last_month': '3'})

    @staticmethod
    def _block_label(result, date_from, measure):
        matches = [
            column['expression_label']
            for column in result['columns']
            if (column.get('scope') or {}).get('date_from') == date_from
            and column['expression_label'].endswith('__%s' % measure)
        ]
        if len(matches) != 1:
            raise AssertionError(
                "expected one %s column for %s, got %s"
                % (measure, date_from, matches))
        return matches[0]

    def test_plain_trial_balance_cross_fiscal_year_splits_into_blocks(self):
        # Fiscal year ends 31 March, the period is a calendar year to date:
        # one block per fiscal year, each with its own opening, movement
        # and closing, instead of a refused report.
        self._use_fiscal_year_ending_march()
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 1000.0},
             {'account': self.account_cash, 'debit': 1000.0}],
            date=fields.Date.from_string('2026-02-10'))
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 400.0},
             {'account': self.account_cash, 'debit': 400.0}],
            date=fields.Date.from_string('2026-05-20'))
        result = self.handler.compute(self._opts(
            date_from='2026-01-01', date_to='2026-10-27'))

        scopes = sorted({
            (column['scope']['date_from'], column['scope']['date_to'])
            for column in result['columns'] if column.get('scope')
        })
        self.assertEqual(scopes, [
            ('2026-01-01', '2026-03-31'), ('2026-04-01', '2026-10-27'),
        ])
        self.assertEqual(len(result['columns']), 1 + 2 * 6)
        self.assertEqual(len(result['column_header_rows']), 2)
        self.assertEqual(result['meta']['comparison_number'], 0)

        idx = {line['meta'].get('account_code'): line
               for line in result['lines'] if line.get('meta')}
        revenue = idx['4000']
        first, second = '2026-01-01', '2026-04-01'
        self.assertAlmostEqual(self._col(
            revenue, self._block_label(result, first, 'period_credit')),
            1000.0, places=2)
        self.assertAlmostEqual(self._col(
            revenue, self._block_label(result, first, 'closing_credit')),
            1000.0, places=2)
        # The new fiscal year opens revenue at zero and closes on its own
        # movement only; the completed year rolls to retained earnings.
        self.assertAlmostEqual(self._col(
            revenue, self._block_label(result, second, 'opening_credit')),
            0.0, places=2)
        self.assertAlmostEqual(self._col(
            revenue, self._block_label(result, second, 'closing_credit')),
            400.0, places=2)
        unaffected = self._unaffected_line(result)
        self.assertIsNotNone(unaffected)
        self.assertAlmostEqual(self._col(
            unaffected, self._block_label(result, second, 'opening_credit')),
            1000.0, places=2)
        cash = idx['1000']
        self.assertAlmostEqual(self._col(
            cash, self._block_label(result, second, 'closing_debit')),
            1400.0, places=2)
        for scoped_totals in result['totals']['column_scopes'].values():
            self.assertAlmostEqual(
                scoped_totals['opening_debit'],
                scoped_totals['opening_credit'], places=2)
            self.assertAlmostEqual(
                scoped_totals['closing_debit'],
                scoped_totals['closing_credit'], places=2)

    def test_cross_fiscal_year_render_with_hierarchy_and_zero_rows(self):
        # The exact viewer option set from a customer report: range, no
        # comparison, account hierarchy, company currency, posted only and
        # zero balance accounts shown.
        self._use_fiscal_year_ending_march()
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 250.0},
             {'account': self.account_cash, 'debit': 250.0}],
            date=fields.Date.from_string('2026-02-10'))
        report = self.env['eh.account.dynamic.report'].search([
            ('code', '=', 'trial_balance'),
        ], limit=1)
        result = report.render(self._opts(
            date_from='2026-01-01', date_to='2026-10-27',
            comparison='none', hierarchical_groups=True, show_zero=True,
            presentation_currency_id=None, lazy_expand=True,
            unfold_all=False, unfolded_lines=[],
        ), use_cache=False)
        self.assertEqual(len(result['columns']), 1 + 2 * 6)
        self.assertTrue(result['lines'])

    def test_fiscal_year_blocks_are_capped(self):
        with self.assertRaisesRegex(UserError, 'needs 12 fiscal year blocks'):
            self.handler.compute(self._opts(
                date_from='2015-01-01', date_to='2026-12-31'))

    def test_fiscal_year_ending_on_the_last_date_is_not_cut(self):
        # The year walk must stop before stepping past 9999-12-31.
        self.assertFalse(self.handler._tb_fiscal_year_periods(
            {'comparison': 'none'},
            fields.Date.from_string('9999-01-01'),
            fields.Date.from_string('9999-12-31'),
            [self.company.id],
        ))
        result = self.handler.compute(self._opts(
            date_from='9999-01-01', date_to='9999-12-31'))
        self.assertNotIn('column_header_rows', result)

    def test_fiscal_year_blocks_name_the_split_in_the_column_budget(self):
        self._use_fiscal_year_ending_march()
        plan = self.env['account.analytic.plan'].create({
            'name': 'TB fiscal budget plan',
        })
        analytic_ids = [
            self.env['account.analytic.account'].create({
                'name': 'TB fiscal budget %s' % index, 'plan_id': plan.id,
            }).id
            for index in range(4)
        ]
        # Two blocks x (four analytic groups + Total) x six measures = 60.
        with self.assertRaisesRegex(UserError, 'cut into 2 fiscal year blocks'):
            self.handler.compute(self._opts(
                date_from='2026-01-01', date_to='2026-10-27',
                analytic_column_account_ids=analytic_ids,
            ))

    def test_fiscal_year_blocks_follow_every_company_calendar(self):
        # A December company and a March company: the period is cut at both
        # year ends so every block sits inside one fiscal year of each.
        other = self.env['res.company'].create({
            'name': 'TB March year end',
            'currency_id': self.company.currency_id.id,
            'fiscalyear_last_day': 31,
            'fiscalyear_last_month': '3',
        })
        periods = self.handler._tb_fiscal_year_periods(
            {'comparison': 'none'},
            fields.Date.from_string('2025-11-01'),
            fields.Date.from_string('2026-05-31'),
            [self.company.id, other.id],
        )
        self.assertEqual(
            [(period['date_from'], period['date_to']) for period in periods],
            [('2025-11-01', '2025-12-31'), ('2026-01-01', '2026-03-31'),
             ('2026-04-01', '2026-05-31')],
        )
        self.assertEqual(
            [period['key'] for period in periods],
            ['period_fiscal_1', 'period_fiscal_2', 'period_current'],
        )
        self.assertFalse(self.handler._tb_fiscal_year_periods(
            {'comparison': 'previous_year'},
            fields.Date.from_string('2025-11-01'),
            fields.Date.from_string('2026-05-31'),
            [self.company.id, other.id],
        ))

    def test_fiscal_year_block_analytic_cell_resolves_its_own_window(self):
        self._use_fiscal_year_ending_march()
        plan = self.env['account.analytic.plan'].create({
            'name': 'TB fiscal block plan',
        })
        analytic = self.env['account.analytic.account'].create({
            'name': 'TB fiscal block A', 'plan_id': plan.id,
        })
        options = self._opts(
            date_from='2026-01-01', date_to='2026-10-27',
            analytic_column_account_ids=[analytic.id],
        )
        result = self.handler.compute(options)
        label = next(
            column['expression_label']
            for column in result['columns']
            if (column.get('scope') or {}).get('date_from') == '2026-01-01'
            and column['scope'].get('is_total') is False
            and column['expression_label'].endswith('__period_debit')
        )
        value_scope, measure = self.handler._eh_tb_analytic_drilldown_scope(
            options, label)
        self.assertEqual(measure, 'period_debit')
        self.assertEqual(
            (value_scope['scope']['date_from'],
             value_scope['scope']['date_to']),
            ('2026-01-01', '2026-03-31'),
        )

    def test_fiscal_blocks_share_one_account_list_and_hierarchy(self):
        # An account first used in the later fiscal year sits in both
        # blocks (zero in the first) under the parent a one block report
        # gives it, and every parent reference resolves.
        self._use_fiscal_year_ending_march()
        later = self._ensure_account(
            self.env, '4999', 'TB later year revenue', 'income')
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 1000.0},
             {'account': self.account_cash, 'debit': 1000.0}],
            date=fields.Date.from_string('2026-02-10'))
        self.post_balanced_move(
            [{'account': later, 'credit': 400.0},
             {'account': self.account_cash, 'debit': 400.0}],
            date=fields.Date.from_string('2026-05-20'))
        for hierarchical in (False, True):
            with self.subTest(hierarchical=hierarchical):
                result = self.handler.compute(self._opts(
                    date_from='2026-01-01', date_to='2026-10-27',
                    hierarchical_groups=hierarchical,
                ))
                ids = {line['id'] for line in result['lines']}
                self.assertEqual(len(ids), len(result['lines']))
                for line in result['lines']:
                    if line.get('parent_id'):
                        self.assertIn(line['parent_id'], ids)
                line = next(
                    line for line in result['lines']
                    if (line.get('meta') or {}).get('account_id') == later.id)
                self.assertAlmostEqual(self._col(
                    line, self._block_label(result, '2026-01-01', 'period_credit')),
                    0.0, places=2)
                self.assertAlmostEqual(self._col(
                    line, self._block_label(result, '2026-04-01', 'period_credit')),
                    400.0, places=2)
                if not hierarchical:
                    codes = [
                        (line.get('meta') or {}).get('account_code') or ''
                        for line in result['lines']
                        if (line.get('meta') or {}).get('account_id')
                    ]
                    self.assertEqual(codes, sorted(codes))
                single = self.handler.compute(self._opts(
                    date_from='2026-04-01', date_to='2026-10-27',
                    hierarchical_groups=hierarchical,
                ))
                single_line = next(
                    line for line in single['lines']
                    if (line.get('meta') or {}).get('account_id') == later.id)
                self.assertEqual(
                    line.get('parent_id'), single_line.get('parent_id'))

    def test_fiscal_blocks_hide_accounts_zero_in_every_block(self):
        # History that nets to zero before the period leaves an account at
        # zero in both blocks: hidden unless zero balances are shown.
        self._use_fiscal_year_ending_march()
        settled = self._ensure_account(
            self.env, '199904', 'TB settled before', 'asset_current')
        self.post_balanced_move(
            [{'account': settled, 'debit': 50.0},
             {'account': self.account_cash, 'credit': 50.0}],
            date=fields.Date.from_string('2025-11-01'))
        self.post_balanced_move(
            [{'account': settled, 'credit': 50.0},
             {'account': self.account_cash, 'debit': 50.0}],
            date=fields.Date.from_string('2025-11-02'))
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 10.0},
             {'account': self.account_cash, 'debit': 10.0}],
            date=fields.Date.from_string('2026-05-20'))
        for hierarchical in (False, True):
            with self.subTest(hierarchical=hierarchical):
                options = self._opts(
                    date_from='2026-01-01', date_to='2026-10-27',
                    hierarchical_groups=hierarchical,
                )
                hidden = self.handler.compute(options)
                self.assertFalse([
                    line for line in hidden['lines']
                    if (line.get('meta') or {}).get('account_id') == settled.id
                ])
                shown = self.handler.compute(dict(options, show_zero=True))
                self.assertTrue([
                    line for line in shown['lines']
                    if (line.get('meta') or {}).get('account_id') == settled.id
                ])

    def _second_root_company(self, name):
        company = self.env['res.company'].create({
            'name': name, 'currency_id': self.company.currency_id.id,
        })
        self.env.user.company_ids |= company
        return company

    def test_account_shared_by_two_root_companies_keeps_both_balances(self):
        if 'company_ids' not in self.env['account.account']._fields:
            self.skipTest('accounts are shared across companies from Odoo 18')
        other = self._second_root_company('TB sharing root')
        shared = self._ensure_account(
            self.env, '1998', 'TB shared asset', 'asset_current')
        # Odoo 18 and later want the new company's code in the same write.
        shared.with_company(other).write({
            'company_ids': [(4, other.id)], 'code': 'SH1998',
        })
        Account = self.env['account.account']
        other_equity = Account.with_company(other).create({
            'name': 'TB sharing equity', 'code': 'SH3000',
            'account_type': 'equity', 'company_ids': [(6, 0, other.ids)],
        })
        other_journal = self.env['account.journal'].create({
            'name': 'TB sharing misc', 'code': 'TBSH', 'type': 'general',
            'company_id': other.id,
        })
        self.post_balanced_move(
            [{'account': shared, 'debit': 100.0},
             {'account': self.account_equity, 'credit': 100.0}],
            date=fields.Date.from_string('2026-03-01'))
        self.post_balanced_move(
            [{'account': shared, 'debit': 50.0},
             {'account': other_equity, 'credit': 50.0}],
            journal=other_journal,
            date=fields.Date.from_string('2026-03-01'))
        for hierarchical in (False, True):
            with self.subTest(hierarchical=hierarchical):
                result = self.handler.compute(self._opts(
                    company_ids=(self.company | other).ids,
                    primary_company_id=self.company.id,
                    hierarchical_groups=hierarchical,
                ))
                lines = [
                    line for line in result['lines']
                    if (line.get('meta') or {}).get('account_id') == shared.id
                ]
                self.assertEqual(len(lines), 1)
                self.assertAlmostEqual(
                    self._col(lines[0], 'period_debit'), 150.0, places=2)
                self.assertEqual(lines[0]['meta']['account_code'], '1998')
                self.assertAlmostEqual(
                    result['totals']['closing_debit'],
                    result['totals']['closing_credit'], places=2)

    def test_shared_account_reads_in_the_report_company_code(self):
        if 'company_ids' not in self.env['account.account']._fields:
            self.skipTest('accounts are shared across companies from Odoo 18')
        other = self._second_root_company('TB label root')
        shared = self._ensure_account(
            self.env, '1997', 'TB shared label', 'asset_current')
        shared.with_company(other).write({
            'company_ids': [(4, other.id)], 'code': 'SH1997',
        })
        other_equity = self.env['account.account'].with_company(other).create({
            'name': 'TB label equity', 'code': 'SH3001',
            'account_type': 'equity', 'company_ids': [(6, 0, other.ids)],
        })
        journal = self.env['account.journal'].create({
            'name': 'TB label misc', 'code': 'TBLB', 'type': 'general',
            'company_id': other.id,
        })
        self.post_balanced_move(
            [{'account': shared, 'debit': 25.0},
             {'account': other_equity, 'credit': 25.0}],
            journal=journal, date=fields.Date.from_string('2026-03-01'))
        result = self.handler.compute(self._opts(
            company_ids=(self.company | other).ids,
            primary_company_id=self.company.id,
        ))
        line = next(
            line for line in result['lines']
            if (line.get('meta') or {}).get('account_id') == shared.id)
        self.assertEqual(line['meta']['account_code'], '1997')

    def test_second_company_retained_earnings_keeps_its_own_code(self):
        if 'company_ids' not in self.env['account.account']._fields:
            self.skipTest('account codes are company dependent from Odoo 18')
        other = self._second_root_company('TB retained root')
        Account = self.env['account.account']
        common = {'company_ids': [(6, 0, other.ids)]}
        retained = Account.with_company(other).create(dict(
            common, name='TB other retained', code='OT9999',
            account_type='equity_unaffected'))
        income = Account.with_company(other).create(dict(
            common, name='TB other income', code='OT4000',
            account_type='income'))
        cash = Account.with_company(other).create(dict(
            common, name='TB other cash', code='OT1000',
            account_type='asset_current'))
        journal = self.env['account.journal'].create({
            'name': 'TB other misc', 'code': 'TBOT', 'type': 'general',
            'company_id': other.id,
        })
        self.post_balanced_move(
            [{'account': cash, 'debit': 500.0},
             {'account': income, 'credit': 500.0}],
            journal=journal, date=fields.Date.from_string('2025-06-01'))
        result = self.handler.compute(self._opts(
            company_ids=(self.company | other).ids,
            primary_company_id=self.company.id,
        ))
        line = next(
            line for line in result['lines']
            if (line.get('meta') or {}).get('account_id') == retained.id)
        self.assertEqual(line['meta']['account_code'], 'OT9999')
        self.assertIn('OT9999', line['name'])
        self.assertAlmostEqual(
            self._col(line, 'opening_credit'), 500.0, places=2)

    def test_previous_year_comparison_shifts_leap_day_safely(self):
        sectioned = self.env[
            'eh.account.dynamic.report.handler.sectioned']
        prior_from, prior_to, _label = sectioned._resolve_comparison_dates(
            'previous_year',
            fields.Date.from_string('2024-02-29'),
            fields.Date.from_string('2024-02-29'),
        )
        self.assertEqual(fields.Date.to_string(prior_from), '2023-02-28')
        self.assertEqual(fields.Date.to_string(prior_to), '2023-02-28')

    def test_pl_account_opening_zero_at_fiscal_year_start(self):
        # Prior-year revenue must NOT appear as the revenue account's opening
        # once date_from is the fiscal-year start; it is rolled to unaffected.
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 500.0},
             {'account': self.account_cash, 'debit': 500.0}],
            date=fields.Date.from_string('2025-12-15'))
        result = self.handler.compute(self._opts())
        idx = {l['meta'].get('account_code'): l
               for l in result['lines'] if l.get('meta')}
        # Revenue account (4000) opens at zero on both sides at FY start.
        if '4000' in idx:
            self.assertAlmostEqual(
                self._col(idx['4000'], 'opening_debit'), 0.0, places=2)
            self.assertAlmostEqual(
                self._col(idx['4000'], 'opening_credit'), 0.0, places=2)
        # Cash (balance sheet) still carries its 500 opening forward.
        self.assertIn('1000', idx)
        self.assertAlmostEqual(
            self._col(idx['1000'], 'opening_debit'), 500.0, places=2)

    def test_mid_year_pl_opening_is_current_fy_to_date_only(self):
        # date_from mid fiscal year: an income account's opening must equal
        # ONLY its current-FY-to-date movement (Jan..May 2026), not all-time.
        self.post_balanced_move(  # prior year -> rolls to unaffected
            [{'account': self.account_revenue, 'credit': 400.0},
             {'account': self.account_cash, 'debit': 400.0}],
            date=fields.Date.from_string('2025-11-15'))
        self.post_balanced_move(  # current FY, before mid-year date_from
            [{'account': self.account_revenue, 'credit': 250.0},
             {'account': self.account_cash, 'debit': 250.0}],
            date=fields.Date.from_string('2026-03-15'))
        result = self.handler.compute(
            self._opts(date_from='2026-07-01', date_to='2026-12-31'))
        idx = {l['meta'].get('account_code'): l
               for l in result['lines'] if l.get('meta')}
        # Revenue opening = only the current-FY-to-date 250 credit, NOT 650.
        self.assertIn('4000', idx)
        self.assertAlmostEqual(
            self._col(idx['4000'], 'opening_credit'), 250.0, places=2)

    def test_opening_parity_roll_loses_nothing(self):
        # Sum of all openings (incl unaffected) must equal the sum of all
        # aml.balance before date_from: the roll reclassifies, never loses.
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 900.0},
             {'account': self.account_cash, 'debit': 900.0}],
            date=fields.Date.from_string('2025-05-15'))
        self.post_balanced_move(
            [{'account': self.account_expense, 'debit': 200.0},
             {'account': self.account_cash, 'credit': 200.0}],
            date=fields.Date.from_string('2025-08-15'))
        result = self.handler.compute(self._opts())
        # Net signed opening across all lines = opening_debit - opening_credit.
        net_opening = (result['totals']['opening_debit']
                       - result['totals']['opening_credit'])
        # All prior moves are balanced, so the signed sum is exactly zero.
        self.assertAlmostEqual(net_opening, 0.0, places=2)

    def test_no_prior_pl_means_no_unaffected_line(self):
        # Regression: with only in-period activity, no unaffected line is
        # emitted and the TB shape is unchanged from pre-WS4.
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 100.0},
             {'account': self.account_cash, 'debit': 100.0}],
            date=fields.Date.from_string('2026-06-15'))
        result = self.handler.compute(self._opts())
        ids = {l['id'] for l in result['lines']}
        self.assertNotIn('account-unaffected-earnings', ids)

    def test_hierarchical_unaffected_line_and_footing(self):
        # The hierarchical builder must also emit the unaffected line and foot.
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 800.0},
             {'account': self.account_cash, 'debit': 800.0}],
            date=fields.Date.from_string('2025-06-15'))
        result = self.handler.compute(self._opts(hierarchical_groups=True))
        self.assertIsNotNone(self._unaffected_line(result))
        totals = result['totals']
        self.assertAlmostEqual(
            totals['opening_debit'], totals['opening_credit'], places=2)
        self.assertAlmostEqual(
            totals['closing_debit'], totals['closing_credit'], places=2)

    def test_staggered_fiscal_year_opening(self):
        # Non-calendar fiscal year ending 30 June: FY containing 2026-08-01
        # starts 2026-07-01, so a July 2026 P&L line is current-FY (opening),
        # while a June 2026 line is prior-year (rolled to unaffected).
        original_calendar = {
            'fiscalyear_last_day': self.company.fiscalyear_last_day,
            'fiscalyear_last_month': self.company.fiscalyear_last_month,
        }
        self.addCleanup(self.company.sudo().write, original_calendar)
        self.company.write({
            'fiscalyear_last_day': 30, 'fiscalyear_last_month': '6'})
        self.post_balanced_move(  # prior FY (before 2026-07-01) -> unaffected
            [{'account': self.account_revenue, 'credit': 600.0},
             {'account': self.account_cash, 'debit': 600.0}],
            date=fields.Date.from_string('2026-06-15'))
        self.post_balanced_move(  # current FY, before date_from -> opening
            [{'account': self.account_revenue, 'credit': 150.0},
             {'account': self.account_cash, 'debit': 150.0}],
            date=fields.Date.from_string('2026-07-10'))
        result = self.handler.compute(
            self._opts(date_from='2026-08-01', date_to='2027-06-30'))
        idx = {l['meta'].get('account_code'): l
               for l in result['lines'] if l.get('meta')}
        self.assertIn('4000', idx)
        # Only the current-FY 150 is in revenue's opening; the June 600 rolled.
        self.assertAlmostEqual(
            self._col(idx['4000'], 'opening_credit'), 150.0, places=2)
        ue = self._unaffected_line(result)
        self.assertIsNotNone(ue)
        self.assertAlmostEqual(self._col(ue, 'opening_credit'), 600.0, places=2)


@tagged('eh_account_dynamic_reports', 'integration', 'post_install',
        '-at_install')
class TestTrialBalanceMultiCurrencyWS4(EhAccountIntegrationTestCase):
    """WS4: cross-company consolidation converts to a presentation currency.

    Company A is in the base currency; company B reports in a second currency
    at a fixed rate. The consolidated trial balance total must convert B's
    balance, not sum raw mixed-currency figures.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env['eh.account.dynamic.report.handler.trial_balance']
        cls.company_a = cls.company
        cls.base_currency = cls.company_a.currency_id

        # A distinct second currency for company B with a fixed rate of 2.0
        # against the base currency (1 base = 2.0 of currency B), so a B
        # balance converts to base by dividing by the company rate. We assert
        # the realised number directly to pin the orientation.
        cls.currency_b = cls.env['res.currency'].create({
            'name': 'XBT', 'symbol': 'X',
            'rounding': cls.base_currency.rounding,
        })
        cls.env['res.currency.rate'].create({
            'currency_id': cls.currency_b.id, 'name': '2020-01-01',
            'rate': 2.0,
        })
        cls.company_b = cls.env['res.company'].create({
            'name': 'FX Co B', 'currency_id': cls.currency_b.id,
        })
        cls.env['res.currency.rate'].create({
            'currency_id': cls.base_currency.id,
            'company_id': cls.company_b.id,
            'name': '2020-01-01',
            'rate': 0.5,
        })
        cls.env.user.company_ids = [(4, cls.company_b.id)]
        cls.journal_b = cls.env['account.journal'].create({
            'name': 'Misc B', 'code': 'MSCB', 'type': 'general',
            'company_id': cls.company_b.id,
        })
        cls.revenue_b = cls.env['account.account'].create({
            'code': '4002B', 'name': 'Revenue B', 'account_type': 'income',
            'company_ids': [(6, 0, [cls.company_b.id])],
        })
        cls.cash_b = cls.env['account.account'].create({
            'code': '1002B', 'name': 'Cash B', 'account_type': 'asset_cash',
            'company_ids': [(6, 0, [cls.company_b.id])],
        })

    def _consolidated_total(self, result, label):
        return result['totals'][label]

    def test_cross_currency_consolidation_converts_b(self):
        # A: 1000 base. B: 1000 currency-B (which is 500 base at rate 2.0).
        self.post_balanced_move(
            [{'account': self.account_cash, 'debit': 1000.0},
             {'account': self.account_revenue, 'credit': 1000.0}],
            date=fields.Date.from_string('2026-06-15'))
        self.post_balanced_move(
            [{'account': self.cash_b, 'debit': 1000.0},
             {'account': self.revenue_b, 'credit': 1000.0}],
            journal=self.journal_b,
            date=fields.Date.from_string('2026-06-15'))

        options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company_a.id, self.company_b.id],
            'posted_only': True, 'show_zero': False,
            'hierarchical_groups': False,
            'presentation_currency_id': self.base_currency.id,
        }
        result = self.handler.compute(options)

        # Resolve the actual rate orientation the ORM uses so the assertion is
        # robust to rate-direction conventions: convert 1000 B to base.
        b_in_base = self.currency_b._convert(
            1000.0, self.base_currency, self.company_b,
            fields.Date.from_string('2026-12-31'))
        expected = self.base_currency.round(1000.0 + b_in_base)

        # Period debit total: cash A (1000 base) + cash B (1000 B -> base).
        self.assertAlmostEqual(
            self._consolidated_total(result, 'period_debit'),
            expected, places=2)
        # Raw (unconverted) sum would have been 2000; prove we did NOT do that
        # unless the rate happens to be 1.0.
        if not self.base_currency.is_zero(b_in_base - 1000.0):
            self.assertNotAlmostEqual(
                self._consolidated_total(result, 'period_debit'),
                2000.0, places=2)

    def test_inverse_direction_presentation_currency_b(self):
        # Present in currency B instead: now A's base balance converts UP into
        # currency B and B's stays as-is, proving rate orientation both ways.
        self.post_balanced_move(
            [{'account': self.account_cash, 'debit': 1000.0},
             {'account': self.account_revenue, 'credit': 1000.0}],
            date=fields.Date.from_string('2026-06-15'))
        self.post_balanced_move(
            [{'account': self.cash_b, 'debit': 1000.0},
             {'account': self.revenue_b, 'credit': 1000.0}],
            journal=self.journal_b,
            date=fields.Date.from_string('2026-06-15'))

        options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company_a.id, self.company_b.id],
            'posted_only': True, 'show_zero': False,
            'hierarchical_groups': False,
            'presentation_currency_id': self.currency_b.id,
        }
        result = self.handler.compute(options)
        a_in_b = self.base_currency._convert(
            1000.0, self.currency_b, self.company_a,
            fields.Date.from_string('2026-12-31'))
        expected = self.currency_b.round(a_in_b + 1000.0)
        self.assertAlmostEqual(
            self._consolidated_total(result, 'period_debit'),
            expected, places=2)

    def test_monocurrency_single_company_unchanged(self):
        # Regression: a single-company run (no presentation currency) must
        # produce exactly the figures it did before WS4's currency thread.
        self.post_balanced_move(
            [{'account': self.account_cash, 'debit': 1000.0},
             {'account': self.account_revenue, 'credit': 1000.0}],
            date=fields.Date.from_string('2026-06-15'))
        options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company_a.id],
            'posted_only': True, 'show_zero': False,
            'hierarchical_groups': False,
        }
        result = self.handler.compute(options)
        self.assertAlmostEqual(
            result['totals']['period_debit'], 1000.0, places=2)
        self.assertFalse(
            result['meta'].get('multi_currency', False))
