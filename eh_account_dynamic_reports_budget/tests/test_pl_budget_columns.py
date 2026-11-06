# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Budget-vs-actual columns on the P&L report."""

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import new_test_user, tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_dynamic_reports_budget', 'integration', 'post_install',
        '-at_install')
class TestPlBudgetColumns(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env[
            'eh.account.dynamic.report.handler.profit_and_loss']
        cls.budget = cls.env['eh.budget.budget'].create({
            'code': 'pl_budget', 'name': 'P&L budget',
            'date_from': '2026-01-01', 'date_to': '2026-12-31',
            'line_ids': [
                (0, 0, {'account_id': cls.account_revenue.id,
                        'period_from': '2026-01-01',
                        'period_to': '2026-12-31',
                        'budgeted_amount': 1000.0}),
                (0, 0, {'account_id': cls.account_expense.id,
                        'period_from': '2026-01-01',
                        'period_to': '2026-12-31',
                        'budgeted_amount': 300.0}),
            ],
        })
        account_type_field = cls.env[
            'account.account']._fields['account_type']
        cls.has_expense_other = 'expense_other' in dict(
            account_type_field._description_selection(cls.env))
        cls.account_other_expense = None
        if cls.has_expense_other:
            cls.account_other_expense = cls._ensure_account(
                cls.env, '5902', 'Budgeted Other Expenses',
                'expense_other')

    def _options(self):
        return {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True, 'show_zero': False,
            'budget_id': self.budget.id,
        }

    @staticmethod
    def _line(result, line_id):
        for line in result['lines']:
            if line['id'] == line_id:
                return line
        return None

    @staticmethod
    def _col(line, label):
        for col in line['columns']:
            if col['expression_label'] == label:
                return col['value']
        return None

    def test_budget_picker_is_empty_without_budget_read_access(self):
        auditor = new_test_user(
            self.env,
            login='pl_budget_auditor',
            groups='eh_account_base.group_eh_auditor',
        )
        Budget = self.env['eh.budget.budget'].with_user(auditor)
        with self.assertRaises(AccessError):
            Budget._eh_check_access('read')
        self.assertEqual(
            Budget.eh_report_budget_choices('profit_and_loss'),
            {'supported': True, 'choices': []},
        )

    def test_budget_picker_exposes_state_version_and_company_scope(self):
        closed_budget = self.env['eh.budget.budget'].create({
            'code': 'pl_budget_closed',
            'name': 'Closed P&L budget',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'line_ids': [(0, 0, {
                'account_id': self.account_revenue.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 500.0,
            })],
        })
        closed_budget.action_confirm()
        closed_budget.action_close()
        choices = self.env['eh.budget.budget'].eh_report_budget_choices(
            'profit_and_loss', [self.company.id],
        )
        self.assertTrue(choices['supported'])
        selected = next(
            choice for choice in choices['choices']
            if choice['id'] == self.budget.id
        )
        self.assertEqual(selected['company_id'], self.company.id)
        self.assertEqual(selected['state'], 'draft')
        self.assertEqual(selected['version_label'], 'v1')
        self.assertIn('Draft', selected['label'])
        self.assertIn('v1', selected['label'])
        self.assertNotIn(
            closed_budget.id,
            {choice['id'] for choice in choices['choices']},
        )
        all_choices = self.env[
            'eh.budget.budget'
        ].eh_report_budget_choices(
            'profit_and_loss', [self.company.id], 'all',
        )
        closed_choice = next(
            choice for choice in all_choices['choices']
            if choice['id'] == closed_budget.id
        )
        self.assertEqual(closed_choice['state'], 'closed')
        self.assertIn('Closed', closed_choice['label'])
        self.assertEqual(
            self.env['eh.budget.budget'].eh_report_budget_choices(
                'balance_sheet', [self.company.id],
            ),
            {'supported': False, 'choices': []},
        )

    def test_budget_columns_present_and_correct(self):
        # Actuals: revenue 1200, expense 250.
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 1200.0},
             {'account': self.account_cash, 'debit': 1200.0}],
            date=fields.Date.from_string('2026-06-15'))
        self.post_balanced_move(
            [{'account': self.account_expense, 'debit': 250.0},
             {'account': self.account_cash, 'credit': 250.0}],
            date=fields.Date.from_string('2026-06-15'))

        result = self.handler.compute(self._options())
        col_keys = [c['expression_label'] for c in result['columns']]
        self.assertIn('budget', col_keys)
        self.assertIn('budget_variance', col_keys)
        self.assertIn('budget_variance_pct', col_keys)

        rev = self._line(result, 'account-%d' % self.account_revenue.id)
        self.assertAlmostEqual(self._col(rev, 'budget'), 1000.0, places=2)
        self.assertAlmostEqual(self._col(rev, 'amount'), 1200.0, places=2)
        self.assertAlmostEqual(
            self._col(rev, 'budget_variance'), 200.0, places=2)
        self.assertAlmostEqual(
            self._col(rev, 'budget_variance_pct'), 0.20, places=4)

        net = self._line(result, 'net_profit')
        # net actual 1200-250=950; net budget 1000-300=700; var 250.
        self.assertAlmostEqual(self._col(net, 'amount'), 950.0, places=2)
        self.assertAlmostEqual(self._col(net, 'budget'), 700.0, places=2)
        self.assertAlmostEqual(
            self._col(net, 'budget_variance'), 250.0, places=2)
        self.assertAlmostEqual(result['totals']['net_budget'], 700.0, places=2)
        self.assertEqual(
            result['meta']['budget_variance_convention'],
            'actual_minus_budget',
        )

    def test_full_year_budget_apportioned_over_quarter(self):
        # The full-year revenue budget line is 1000 over 365 days.
        # Reported over Q1 (Jan 1 - Mar 31, 90 days) it must contribute
        # roughly a quarter (1000 * 90 / 365 ~= 246.58), not the whole
        # 1000, so a Q1 actual is not compared against a full-year budget.
        # Post small Q1 actuals so the account and net lines are present
        # (show_zero is False, so accounts without activity are omitted).
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 100.0},
             {'account': self.account_cash, 'debit': 100.0}],
            date=fields.Date.from_string('2026-02-10'))
        self.post_balanced_move(
            [{'account': self.account_expense, 'debit': 50.0},
             {'account': self.account_cash, 'credit': 50.0}],
            date=fields.Date.from_string('2026-02-10'))

        options = self._options()
        options['date'] = {'date_from': '2026-01-01', 'date_to': '2026-03-31'}
        result = self.handler.compute(options)

        rev = self._line(result, 'account-%d' % self.account_revenue.id)
        expected = 1000.0 * 90.0 / 365.0
        self.assertAlmostEqual(
            self._col(rev, 'budget'), round(expected, 2), places=2)
        # Sanity: strictly between zero and the full-year amount.
        self.assertGreater(self._col(rev, 'budget'), 0.0)
        self.assertLess(self._col(rev, 'budget'), 1000.0)

        exp = self._line(result, 'account-%d' % self.account_expense.id)
        expected_exp = 300.0 * 90.0 / 365.0
        self.assertAlmostEqual(
            self._col(exp, 'budget'), round(expected_exp, 2), places=2)

        net = self._line(result, 'net_profit')
        expected_net = round(expected, 2) - round(expected_exp, 2)
        self.assertAlmostEqual(
            self._col(net, 'budget'), round(expected_net, 2), places=2)
        self.assertEqual(result['meta']['budget_apportionment'], 'daily')

    def test_period_exact_policy_rejects_partial_budget_period(self):
        options = self._options()
        options['date'] = {
            'date_from': '2026-01-01', 'date_to': '2026-03-31',
        }
        options['budget_apportionment'] = 'period_exact'
        with self.assertRaisesRegex(UserError, 'partly overlaps'):
            self.handler.compute(options)

        exact_budget = self.env['eh.budget.budget'].create({
            'code': 'pl_budget_exact_period',
            'name': 'Exact-period P&L budget',
            'date_from': '2026-01-01',
            'date_to': '2026-03-31',
            'line_ids': [(0, 0, {
                'account_id': self.account_revenue.id,
                'period_from': '2026-01-01',
                'period_to': '2026-03-31',
                'budgeted_amount': 250.0,
            })],
        })
        result = self.handler.compute(dict(
            options, budget_id=exact_budget.id,
        ))
        revenue = self._line(
            result, 'account-%d' % self.account_revenue.id,
        )
        self.assertEqual(self._col(revenue, 'budget'), 250.0)
        self.assertEqual(result['meta']['budget_apportionment'], 'period_exact')

    def test_no_budget_columns_without_budget_id(self):
        result = self.handler.compute({
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True, 'show_zero': False,
        })
        col_keys = [c['expression_label'] for c in result['columns']]
        self.assertNotIn('budget', col_keys)

    def test_missing_or_malformed_budget_falls_back_to_standard_report(self):
        for stale_id in ('not-an-id', self.budget.id + 999999):
            result = self.handler.compute(dict(
                self._options(), budget_id=stale_id,
            ))
            expressions = {
                column['expression_label']
                for column in result['columns']
            }
            self.assertNotIn('budget', expressions)
            self.assertNotIn('budget_variance', expressions)

    def test_budget_edit_invalidates_cached_report_payload(self):
        report = self.env['eh.account.dynamic.report'].search([
            ('code', '=', 'profit_and_loss'),
        ], limit=1)
        self.assertTrue(report)
        options = self._options()
        first = report.render(options, use_cache=True)
        first_revenue = self._line(
            first, 'account-%d' % self.account_revenue.id,
        )
        self.assertEqual(self._col(first_revenue, 'budget'), 1000.0)

        version_before = self.company.eh_move_version
        revenue_line = self.budget.line_ids.filtered(
            lambda line: line.account_id == self.account_revenue,
        ).ensure_one()
        revenue_line.write({'budgeted_amount': 2000.0})
        self.company.invalidate_recordset(['eh_move_version'])
        self.assertEqual(self.company.eh_move_version, version_before)

        second = report.render(options, use_cache=True)
        second_revenue = self._line(
            second, 'account-%d' % self.account_revenue.id,
        )
        self.assertEqual(self._col(second_revenue, 'budget'), 2000.0)
        self.assertFalse(second['from_cache'])

        third = report.render(options, use_cache=True)
        self.assertTrue(third['from_cache'])

    def test_analytic_scope_edit_invalidates_cached_validation(self):
        plan = self.env['account.analytic.plan'].create({
            'name': 'P&L budget cache scope plan',
        })
        _analytic_a, _analytic_b, analytic_c = self.env[
            'account.analytic.account'
        ].create([
            {'name': name, 'plan_id': plan.id}
            for name in ('Cache scope A', 'Cache scope B', 'Cache scope C')
        ])
        budget = self.env['eh.budget.budget'].create({
            'code': 'pl_budget_analytic_cache',
            'name': 'P&L analytic cache validation',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'period_from': '2026-01-01',
                    'period_to': '2026-12-31',
                    'budgeted_amount': 100.0,
                }),
                (0, 0, {
                    'account_id': self.account_expense.id,
                    'period_from': '2026-01-01',
                    'period_to': '2026-12-31',
                    'budgeted_amount': 200.0,
                }),
            ],
        })
        report = self.env['eh.account.dynamic.report'].search([
            ('code', '=', 'profit_and_loss'),
        ], limit=1)
        options = dict(self._options(), budget_id=budget.id)
        first = report.render(options, use_cache=True)
        self.assertFalse(first['from_cache'])
        self.assertTrue(report.render(options, use_cache=True)['from_cache'])

        expense_line = budget.line_ids.filtered(
            lambda line: line.account_id == self.account_expense,
        ).ensure_one()
        version = budget.eh_report_cache_version
        expense_line.write({'analytic_account_id': analytic_c.id})
        self.assertEqual(budget.eh_report_cache_version, version + 1)
        with self.assertRaisesRegex(UserError, 'cannot mix'):
            report.render(options, use_cache=True)

        expense_line.write({'analytic_account_id': False})
        refreshed = report.render(options, use_cache=True)
        self.assertFalse(refreshed['from_cache'])
        self.assertTrue(report.render(options, use_cache=True)['from_cache'])

    def test_budget_edit_does_not_invalidate_balance_sheet_cache(self):
        report = self.env['eh.account.dynamic.report'].search([
            ('code', '=', 'balance_sheet'),
        ], limit=1)
        self.assertTrue(report)
        options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
        }
        report.render(options, use_cache=True)
        version_before = self.company.eh_move_version

        self.budget.line_ids[0].write({'budgeted_amount': 4321.0})
        self.company.invalidate_recordset(['eh_move_version'])

        self.assertEqual(self.company.eh_move_version, version_before)
        self.assertTrue(report.render(options, use_cache=True)['from_cache'])

    def test_nested_budget_create_does_not_bump_ledger_version(self):
        version_before = self.company.eh_move_version
        nested = self.env['eh.budget.budget'].create({
            'code': 'pl_budget_nested_cache',
            'name': 'Nested cache budget',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'line_ids': [(0, 0, {
                'account_id': self.account_revenue.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 321.0,
            })],
        })
        self.company.invalidate_recordset(['eh_move_version'])
        self.assertEqual(self.company.eh_move_version, version_before)
        self.assertEqual(nested.eh_report_cache_version, 1)

    def test_budget_cache_version_tracks_line_lifecycle_only(self):
        extra_account = self._ensure_account(
            self.env, '4976', 'Cache lifecycle revenue', 'income',
        )
        report = self.env['eh.account.dynamic.report'].search([
            ('code', '=', 'profit_and_loss'),
        ], limit=1)
        options = self._options()
        report.render(options, use_cache=True)
        version_before = self.company.eh_move_version
        budget_version = self.budget.eh_report_cache_version

        # Header notes do not affect report output, so they retain the exact
        # selected-budget cache identity.
        self.budget.write({'notes': '<p>audit note only</p>'})
        self.assertTrue(report.render(options, use_cache=True)['from_cache'])
        self.assertEqual(
            self.budget.eh_report_cache_version, budget_version,
        )

        extra = self.env['eh.budget.line'].create({
            'budget_id': self.budget.id,
            'account_id': extra_account.id,
            'period_from': '2026-01-01',
            'period_to': '2026-12-31',
            'budgeted_amount': 25.0,
        })
        self.assertFalse(report.render(options, use_cache=True)['from_cache'])
        budget_version += 1
        self.assertEqual(
            self.budget.eh_report_cache_version, budget_version,
        )

        extra.write({'budgeted_amount': 50.0})
        self.assertFalse(report.render(options, use_cache=True)['from_cache'])
        budget_version += 1
        self.assertEqual(
            self.budget.eh_report_cache_version, budget_version,
        )

        extra.unlink()
        restored = report.render(options, use_cache=True)
        self.assertFalse(restored['from_cache'])
        self.assertTrue(report.render(options, use_cache=True)['from_cache'])
        budget_version += 1
        self.assertEqual(
            self.budget.eh_report_cache_version, budget_version,
        )
        self.company.invalidate_recordset(['eh_move_version'])
        self.assertEqual(self.company.eh_move_version, version_before)

    def test_budget_cache_normalization_is_constant_query_count(self):
        many = self.env['eh.budget.budget'].create({
            'code': 'pl_budget_query_count',
            'name': 'Cache query-count budget',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'line_ids': [(0, 0, {
                'account_id': self.account_revenue.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': float(index + 1),
            }) for index in range(80)],
        })

        def _warm_and_count(budget):
            options = dict(self._options(), budget_id=budget.id)
            self.handler.normalize_options(options)
            before = self.env.cr.sql_log_count
            normalized = self.handler.normalize_options(options)
            return self.env.cr.sql_log_count - before, normalized

        small_count, small = _warm_and_count(self.budget)
        many_count, large = _warm_and_count(many)
        self.assertEqual(small_count, many_count)
        self.assertLessEqual(many_count, 4)
        self.assertEqual(
            small['_budget_cache_version'],
            self.budget.eh_report_cache_version,
        )
        self.assertEqual(
            large['_budget_cache_version'], many.eh_report_cache_version,
        )

    def test_budget_state_filter_is_not_report_cache_identity(self):
        live = self.handler.normalize_options(dict(
            self._options(), budget_state_filter='live',
        ))
        all_states = self.handler.normalize_options(dict(
            self._options(), budget_state_filter='all',
        ))
        self.assertNotIn('budget_state_filter', live)
        self.assertNotIn('budget_state_filter', all_states)
        self.assertEqual(live, all_states)

    def test_legacy_null_budget_cache_version_upgrades_lazily(self):
        self.env.cr.execute(
            "UPDATE eh_budget_budget SET eh_report_cache_version = NULL "
            "WHERE id = %s",
            (self.budget.id,),
        )
        self.budget.invalidate_recordset(['eh_report_cache_version'])
        normalized = self.handler.normalize_options(self._options())
        self.assertEqual(normalized['_budget_cache_version'], 0)

        self.budget.line_ids[0].write({'budgeted_amount': 1234.0})
        self.assertEqual(self.budget.eh_report_cache_version, 1)

    def test_deleted_budget_cannot_serve_old_budget_cache_payload(self):
        report = self.env['eh.account.dynamic.report'].search([
            ('code', '=', 'profit_and_loss'),
        ], limit=1)
        doomed = self.env['eh.budget.budget'].create({
            'code': 'pl_budget_delete_cache',
            'name': 'Delete cache budget',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'line_ids': [(0, 0, {
                'account_id': self.account_revenue.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 777.0,
            })],
        })
        options = dict(self._options(), budget_id=doomed.id)
        cached_budget = report.render(options, use_cache=True)
        self.assertIn(
            'budget',
            {column['expression_label'] for column in cached_budget['columns']},
        )
        version_before = self.company.eh_move_version

        doomed.unlink()
        fallback = report.render(options, use_cache=True)

        self.assertNotIn(
            'budget',
            {column['expression_label'] for column in fallback['columns']},
        )
        self.assertNotIn('budget_id', fallback.get('meta', {}))
        self.company.invalidate_recordset(['eh_move_version'])
        self.assertEqual(self.company.eh_move_version, version_before)

    def test_budget_cache_token_is_server_owned_and_access_checked(self):
        normalized = self.handler.normalize_options(dict(
            self._options(), _budget_cache_version=-999,
        ))
        self.assertNotEqual(
            normalized['_budget_cache_version'], -999,
        )

        stale = self.handler.normalize_options(dict(
            self._options(),
            budget_id=self.budget.id + 999999,
            _budget_cache_version=-999,
        ))
        self.assertNotIn('budget_id', stale)
        self.assertNotIn('_budget_cache_version', stale)

        with self.assertRaises(AccessError):
            self.budget.sudo().write({'eh_report_cache_version': 0})
        with self.assertRaises(AccessError):
            self.env['eh.budget.budget'].sudo().create({
                'code': 'pl_budget_forged_cache_version',
                'name': 'Forged cache version',
                'date_from': '2026-01-01',
                'date_to': '2026-12-31',
                'eh_report_cache_version': 999,
            })
        with self.assertRaises(AccessError):
            self.env['eh.budget.budget'].sudo().with_context(
                default_eh_report_cache_version=0,
            ).create({
                'code': 'pl_budget_default_cache_version',
                'name': 'Default cache version',
                'date_from': '2026-01-01',
                'date_to': '2026-12-31',
            })

        auditor = new_test_user(
            self.env,
            login='budget_cache_auditor',
            groups='eh_account_base.group_eh_auditor',
        )
        with self.assertRaises(AccessError):
            self.handler.with_user(auditor).normalize_options(self._options())

    def test_budget_only_accounts_are_visible_and_foot_to_sections(self):
        result = self.handler.compute(self._options())
        revenue = self._line(
            result, 'account-%d' % self.account_revenue.id,
        )
        expense = self._line(
            result, 'account-%d' % self.account_expense.id,
        )
        self.assertTrue(revenue['meta']['budget_only'])
        self.assertTrue(expense['meta']['budget_only'])
        self.assertEqual(self._col(revenue, 'amount'), 0.0)
        self.assertEqual(self._col(expense, 'amount'), 0.0)
        self.assertEqual(self._col(revenue, 'budget'), 1000.0)
        self.assertEqual(self._col(expense, 'budget'), 300.0)
        self.assertEqual(
            self._col(
                self._line(result, 'section-income-total'), 'budget',
            ),
            1000.0,
        )
        self.assertEqual(
            self._col(
                self._line(result, 'section-expenses-total'), 'budget',
            ),
            300.0,
        )

    def test_hierarchical_groups_include_actual_and_budget_only_accounts(self):
        Group = self.env['account.group']
        parent_group = Group.create({
            'name': 'Budget income',
            'code_prefix_start': '4',
            'code_prefix_end': '4',
            'company_id': self.company.id,
        })
        child_group = Group.create({
            'name': 'Budget operating income',
            'code_prefix_start': '40',
            'code_prefix_end': '49',
            'parent_id': parent_group.id,
            'company_id': self.company.id,
        })
        self.account_revenue.group_id = child_group.id
        budget_only = self._ensure_account(
            self.env, '4898', 'Grouped budget-only income', 'income',
        )
        budget_only.group_id = child_group.id
        self.env['eh.budget.line'].create({
            'budget_id': self.budget.id,
            'account_id': budget_only.id,
            'period_from': fields.Date.from_string('2026-01-01'),
            'period_to': fields.Date.from_string('2026-12-31'),
            'budgeted_amount': 250.0,
        })
        self.post_balanced_move(
            [
                {'account': self.account_revenue, 'credit': 100.0},
                {'account': self.account_cash, 'debit': 100.0},
            ],
            date=fields.Date.from_string('2026-06-15'),
        )

        result = self.handler.compute(dict(
            self._options(), hierarchical_groups=True,
        ))
        parent_id = 'section-income-group-%s' % parent_group.id
        child_id = '%s_%s' % (parent_id, child_group.id)
        parent = self._line(result, parent_id)
        child = self._line(result, child_id)
        revenue = self._line(
            result, 'account-%s' % self.account_revenue.id,
        )
        missing = self._line(result, 'account-%s' % budget_only.id)

        self.assertTrue(parent)
        self.assertTrue(child)
        self.assertEqual(child['parent_id'], parent_id)
        self.assertEqual(revenue['parent_id'], child_id)
        self.assertEqual(missing['parent_id'], child_id)
        self.assertTrue(missing['meta']['budget_only'])
        for line in (parent, child):
            self.assertEqual(self._col(line, 'amount'), 100.0)
            self.assertEqual(self._col(line, 'budget'), 1250.0)
            self.assertEqual(self._col(line, 'budget_variance'), -1150.0)
        self.assertEqual(self._col(missing, 'amount'), 0.0)
        self.assertEqual(self._col(missing, 'budget'), 250.0)

    def test_prior_only_budget_account_stays_inside_income_hierarchy(self):
        self.post_balanced_move(
            [
                {'account': self.account_revenue, 'credit': 100.0},
                {'account': self.account_cash, 'debit': 100.0},
            ],
            date=fields.Date.from_string('2025-06-15'),
        )
        result = self.handler.compute(dict(
            self._options(),
            comparison='previous_year',
            hierarchical_groups=True,
        ))
        revenue_id = 'account-%s' % self.account_revenue.id
        revenue = self._line(result, revenue_id)
        ids = [line['id'] for line in result['lines']]

        self.assertLess(ids.index('section-income-header'), ids.index(
            revenue_id,
        ))
        self.assertLess(ids.index(revenue_id), ids.index(
            'section-income-total',
        ))
        self.assertLess(ids.index(revenue_id), ids.index('net_profit'))
        self.assertEqual(revenue['parent_id'], 'section-income-header')
        self.assertEqual(self._col(revenue, 'amount'), 0.0)
        self.assertEqual(self._col(revenue, 'prior_amount'), 100.0)
        self.assertEqual(self._col(revenue, 'budget'), 1000.0)

    def test_unsupported_budget_dimensions_fail_closed(self):
        with self.assertRaisesRegex(UserError, 'by-nature'):
            self.handler.compute(dict(
                self._options(), pnl_presentation='by_function',
            ))
        with self.assertRaisesRegex(UserError, 'one report company'):
            self.handler.compute(dict(
                self._options(),
                company_ids=[self.company.id, self.company.id + 999999],
            ))
        with self.assertRaisesRegex(UserError, 'horizontal'):
            self.handler.compute(dict(
                self._options(), horizontal_group_by='company',
            ))
        with self.assertRaisesRegex(UserError, 'cannot apply'):
            self.handler.compute(dict(
                self._options(), partner_ids=[self.partner_a.id],
            ))
        with self.assertRaisesRegex(UserError, 'cannot be combined'):
            self.handler.compute(dict(
                self._options(), analytic_account_ids=[999999],
            ))
        for axis_options in (
            {'analytic_column_account_ids': [999999]},
            {'analytic_column_plan_ids': [999999]},
            {
                'comparison': 'custom',
                'comparison_custom_date_from': '2025-01-01',
                'comparison_custom_date_to': '2025-12-31',
            },
            {'comparison': 'previous_year', 'comparison_number': 2},
            {
                'comparison': 'previous_year',
                'comparison_order': 'ascending',
            },
        ):
            with self.subTest(axis_options=axis_options), self.assertRaisesRegex(
                    UserError, 'does not yet support column-axis'):
                self.handler.compute(dict(self._options(), **axis_options))
        with self.assertRaisesRegex(
                UserError, 'Comparison period count must be a whole number'):
            self.handler.compute(dict(
                self._options(), comparison='previous_year',
                comparison_number=2.5,
            ))

    def test_analytic_scoped_budget_uses_allocation_weighted_actual(self):
        plan = self.env['account.analytic.plan'].create({
            'name': 'P&L Budget Department Plan',
        })
        department_a, department_b = self.env[
            'account.analytic.account'
        ].create([{
            'name': name,
            'plan_id': plan.id,
        } for name in ('P&L Budget Department A', 'P&L Budget Department B')])
        analytic_budget = self.env['eh.budget.budget'].create({
            'code': 'pl_budget_analytic_scope',
            'name': 'Analytic P&L Budget',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'analytic_account_id': department_a.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 100.0,
            })],
        })
        for analytic, amount in (
                (department_a, 30.0), (department_b, 70.0)):
            move = self.env['account.move'].create({
                'move_type': 'entry',
                'journal_id': self.journal_misc.id,
                'date': '2026-06-15',
                'line_ids': [(0, 0, {
                    'account_id': self.account_expense.id,
                    'debit': amount,
                    'credit': 0.0,
                    'name': 'Analytic budget actual',
                    'analytic_distribution': {str(analytic.id): 100.0},
                }), (0, 0, {
                    'account_id': self.account_cash.id,
                    'debit': 0.0,
                    'credit': amount,
                    'name': 'Analytic budget counterpart',
                })],
            })
            move.action_post()

        result = self.handler.compute(dict(
            self._options(), budget_id=analytic_budget.id,
        ))
        expense = self._line(
            result, 'account-%d' % self.account_expense.id,
        )
        self.assertEqual(self._col(expense, 'amount'), 30.0)
        self.assertEqual(self._col(expense, 'budget'), 100.0)
        self.assertEqual(self._col(expense, 'budget_variance'), -70.0)
        self.assertEqual(self._col(expense, 'budget_variance_pct'), -0.70)
        self.assertEqual(result['totals']['expenses'], 30.0)
        self.assertEqual(result['totals']['net_profit'], -30.0)
        self.assertEqual(
            result['meta']['budget_actual_scope'], 'analytic_allocation',
        )
        self.assertEqual(
            result['meta']['budget_analytic_account_ids'],
            [department_a.id],
        )

    def test_budget_section_headers_keep_structural_cells_blank(self):
        result = self.handler.compute(self._options())
        for line_id in ('section-income-header', 'section-expenses-header'):
            line = self._line(result, line_id)
            self.assertEqual(self._col(line, 'budget'), '')
            self.assertEqual(self._col(line, 'budget_variance'), '')
            self.assertEqual(self._col(line, 'budget_variance_pct'), '')

    def test_budget_values_use_same_presentation_currency_as_actuals(self):
        target = self.env['res.currency'].create({
            'name': 'PBX', 'symbol': 'PBX', 'rounding': 0.001,
        })
        rate_company = (
            self.company.root_id
            if 'root_id' in self.company._fields else self.company
        )
        self.env['res.currency.rate'].create({
            'currency_id': target.id,
            'company_id': rate_company.id,
            # Flow reports require a valid presentation rate from the first
            # day of the requested period; an end-date-only rate correctly
            # fails closed for earlier ledger dates.
            'name': '2026-01-01',
            'rate': 2.0,
        })
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 1200.0},
             {'account': self.account_cash, 'debit': 1200.0}],
            date=fields.Date.from_string('2026-06-15'),
        )
        options = dict(
            self._options(), presentation_currency_id=target.id,
        )
        result = self.handler.compute(options)
        revenue = self._line(
            result, 'account-%d' % self.account_revenue.id,
        )
        expected_budget = target.round(
            self.company.currency_id._convert(
                1000.0, target, self.company,
                fields.Date.from_string('2026-12-31'),
            )
        )
        expected_actual = target.round(
            self.company.currency_id._convert(
                1200.0, target, self.company,
                fields.Date.from_string('2026-12-31'),
            )
        )
        self.assertEqual(self._col(revenue, 'budget'), expected_budget)
        self.assertEqual(self._col(revenue, 'amount'), expected_actual)
        self.assertEqual(
            self._col(revenue, 'budget_variance'),
            target.round(expected_actual - expected_budget),
        )
        self.assertEqual(result['meta']['budget_currency_id'], target.id)
        self.assertEqual(
            result['meta']['budget_currency_translation_policy'],
            'period_average',
        )

    def test_budget_and_actual_share_day_weighted_average_fx_rate(self):
        target = self.env['res.currency'].create({
            'name': 'PBA', 'symbol': 'PBA', 'rounding': 0.01,
        })
        rate_company = (
            self.company.root_id
            if 'root_id' in self.company._fields else self.company
        )
        self.env['res.currency.rate'].create([
            {
                'currency_id': target.id,
                'company_id': rate_company.id,
                'name': '2026-01-01',
                'rate': 1.0,
            },
            {
                'currency_id': target.id,
                'company_id': rate_company.id,
                'name': '2026-07-01',
                'rate': 2.0,
            },
        ])
        self.post_balanced_move(
            [
                {'account': self.account_revenue, 'credit': 1000.0},
                {'account': self.account_cash, 'debit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-15'),
        )
        result = self.handler.compute(dict(
            self._options(), presentation_currency_id=target.id,
        ))
        revenue = self._line(
            result, 'account-%d' % self.account_revenue.id,
        )
        self.assertEqual(
            self._col(revenue, 'amount'), self._col(revenue, 'budget'),
        )
        self.assertEqual(self._col(revenue, 'budget_variance'), 0.0)
        self.assertEqual(self._col(revenue, 'budget_variance_pct'), 0.0)
        self.assertEqual(
            result['meta']['budget_currency_translation_policy'],
            'period_average',
        )

    def test_rounded_account_budgets_drive_section_total(self):
        second_revenue = self._ensure_account(
            self.env, '4907', 'Budget Rounding Revenue', 'income',
        )
        budget = self.env['eh.budget.budget'].create({
            'code': 'pl_budget_rounding',
            'name': 'P&L budget rounding',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'line_ids': [
                (0, 0, {
                    'account_id': account.id,
                    'period_from': '2026-01-01',
                    'period_to': '2026-12-31',
                    'budgeted_amount': 0.005,
                })
                for account in (self.account_revenue, second_revenue)
            ],
        })
        result = self.handler.compute(dict(
            self._options(), budget_id=budget.id,
        ))
        rows = [
            self._line(result, 'account-%d' % account.id)
            for account in (self.account_revenue, second_revenue)
        ]
        row_total = sum(self._col(row, 'budget') for row in rows)
        section_total = self._col(
            self._line(result, 'section-income-total'), 'budget',
        )
        self.assertEqual(section_total, row_total)
        self.assertFalse(self.company.currency_id.is_zero(section_total))

    def test_other_expense_budget_reconciles_to_expense_section(self):
        if not self.has_expense_other:
            self.skipTest("'expense_other' account type not on this Odoo")
        self.post_balanced_move(
            [{'account': self.account_other_expense, 'debit': 100.0},
             {'account': self.account_cash, 'credit': 100.0}],
            date=fields.Date.from_string('2026-06-15'))
        budget = self.env['eh.budget.budget'].create({
            'code': 'pl_budget_other',
            'name': 'Other-expense P&L budget',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'line_ids': [(0, 0, {
                'account_id': self.account_other_expense.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 125.0,
            })],
        })

        result = self.handler.compute(dict(
            self._options(), budget_id=budget.id))
        other = self._line(
            result, 'account-%d' % self.account_other_expense.id)
        expense_total = self._line(result, 'section-expenses-total')
        net = self._line(result, 'net_profit')

        self.assertAlmostEqual(self._col(other, 'amount'), 100.0, places=2)
        self.assertAlmostEqual(self._col(other, 'budget'), 125.0, places=2)
        self.assertAlmostEqual(
            self._col(expense_total, 'budget'), 125.0, places=2)
        self.assertAlmostEqual(self._col(net, 'budget'), -125.0, places=2)
        self.assertAlmostEqual(
            result['totals']['expense_budget'], 125.0, places=2)
