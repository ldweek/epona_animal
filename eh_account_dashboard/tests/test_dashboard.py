# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Dashboard KPI computation tests.

Builds a small set of receivable / payable / income / expense entries
and verifies each KPI computation aggregates them correctly. Also
exercises the period switcher and the open_for_current_user pattern
that the menu action calls.
"""

import ast
from datetime import timedelta
from pathlib import Path

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_dashboard', 'integration', 'post_install', '-at_install')
class TestDashboardKpis(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Dashboard = cls.env['eh.account.dashboard']
        cls.today = fields.Date.context_today(cls.env['res.users'])

    def _post_invoice(self, partner, amount, days_ago_due=0):
        """Post a customer invoice with the given residual."""
        post_date = self.today - timedelta(days=days_ago_due + 30)
        due_date = self.today - timedelta(days=days_ago_due)
        return self.post_balanced_move(
            [
                {
                    'account': self.account_receivable,
                    'debit': amount,
                    'partner': partner,
                    'date_maturity': due_date,
                },
                {'account': self.account_revenue, 'credit': amount},
            ],
            date=post_date,
        )

    def _make_dashboard(self, **overrides):
        vals = {
            'name': 'Test dashboard',
            'period_mode': 'mtd',
            'posted_only': True,
        }
        vals.update(overrides)
        return self.Dashboard.create(vals)

    # ---- optional collections KPI ----

    def test_collections_kpi_aggregates_via_read_group(self):
        """The collections KPI is computed with a single SQL aggregation;
        the count and total still match the open cases. Runs only when the
        collections module is installed alongside the dashboard."""
        if 'eh.collections.case' not in self.env:
            self.skipTest("eh_account_collections not installed")
        self.env['eh.collections.case'].create([
            {
                'partner_id': self.partner_a.id,
                'company_id': self.company.id,
                'total_overdue_amount': 300.0,
            },
            {
                'partner_id': self.partner_b.id,
                'company_id': self.company.id,
                'total_overdue_amount': 200.0,
            },
        ])
        dash = self._make_dashboard(company_id=self.company.id)
        dash.invalidate_recordset([
            'active_collections_count', 'active_collections_total',
        ])
        self.assertTrue(dash.has_collections_module)
        self.assertEqual(dash.active_collections_count, 2)
        self.assertAlmostEqual(
            dash.active_collections_total, 500.0, places=2,
        )

    # ---- period dates ----

    def test_period_dates_mtd(self):
        d = self._make_dashboard(period_mode='mtd')
        self.assertEqual(d.period_date_from, self.today.replace(day=1))
        self.assertEqual(d.period_date_to, self.today)

    def test_period_dates_ytd(self):
        d = self._make_dashboard(period_mode='ytd')
        self.assertEqual(d.period_date_from, self.today.replace(month=1, day=1))

    def test_period_dates_last_30(self):
        d = self._make_dashboard(period_mode='last_30')
        self.assertEqual(d.period_date_from, self.today - timedelta(days=30))

    def test_snapshot_and_refresh_advance_stale_relative_window(self):
        dashboard = self._make_dashboard(period_mode='mtd')
        stale_from = self.today - timedelta(days=40)
        stale_to = self.today - timedelta(days=1)
        self.env.cr.execute(
            "UPDATE eh_account_dashboard "
            "SET period_date_from = %s, period_date_to = %s WHERE id = %s",
            (stale_from, stale_to, dashboard.id),
        )
        dashboard.invalidate_recordset()

        snapshot = dashboard.get_dashboard_snapshot()
        self.assertEqual(
            snapshot['period']['date_from'],
            self.today.replace(day=1).isoformat(),
        )
        self.assertEqual(snapshot['period']['date_to'], self.today.isoformat())

        self.env.cr.execute(
            "UPDATE eh_account_dashboard SET period_date_to = %s WHERE id = %s",
            (stale_to, dashboard.id),
        )
        dashboard.invalidate_recordset()
        dashboard.action_refresh()
        self.assertEqual(dashboard.period_date_to, self.today)

    def test_snapshot_never_overwrites_custom_window(self):
        date_from = self.today - timedelta(days=12)
        date_to = self.today - timedelta(days=3)
        dashboard = self._make_dashboard(
            period_mode='custom',
            period_date_from=date_from,
            period_date_to=date_to,
        )
        snapshot = dashboard.get_dashboard_snapshot()
        self.assertEqual(snapshot['period']['date_from'], date_from.isoformat())
        self.assertEqual(snapshot['period']['date_to'], date_to.isoformat())

    def test_update_period_validates_and_returns_applied_custom_window(self):
        dashboard = self._make_dashboard(period_mode='mtd')
        with self.assertRaisesRegex(ValidationError, 'both a start date'):
            dashboard.update_period('custom', False, self.today)
        with self.assertRaisesRegex(ValidationError, 'on or before'):
            dashboard.update_period(
                'custom', self.today, self.today - timedelta(days=1),
            )

        date_from = self.today - timedelta(days=12)
        date_to = self.today - timedelta(days=3)
        snapshot = dashboard.update_period(
            'custom', date_from.isoformat(), date_to.isoformat(), False,
        )
        self.assertEqual(dashboard.period_mode, 'custom')
        self.assertEqual(dashboard.period_date_from, date_from)
        self.assertEqual(dashboard.period_date_to, date_to)
        self.assertFalse(dashboard.posted_only)
        self.assertEqual(snapshot['period']['date_from'], date_from.isoformat())
        self.assertEqual(snapshot['period']['date_to'], date_to.isoformat())

    def test_direct_write_rejects_reversed_custom_window(self):
        dashboard = self._make_dashboard(
            period_mode='custom',
            period_date_from=self.today - timedelta(days=10),
            period_date_to=self.today,
        )
        with self.assertRaisesRegex(ValidationError, 'on or before'):
            dashboard.write({
                'period_date_from': self.today,
                'period_date_to': self.today - timedelta(days=1),
            })

    def test_prior_pl_and_sparklines_include_every_tile_account_type(self):
        account_types = dict(
            self.env['account.account']._fields['account_type'].selection,
        )
        required = {
            'income_other', 'expense_other', 'expense_depreciation',
            'expense_direct_cost',
        }
        if not required.issubset(account_types):
            self.skipTest('extended Odoo P&L account types unavailable')

        income_other = self._ensure_account(
            self.env, '4091', 'Dashboard Other Income', 'income_other',
        )
        expense_other = self._ensure_account(
            self.env, '6091', 'Dashboard Other Expense', 'expense_other',
        )
        depreciation = self._ensure_account(
            self.env, '6092', 'Dashboard Depreciation',
            'expense_depreciation',
        )
        direct_cost = self._ensure_account(
            self.env, '5091', 'Dashboard Direct Cost',
            'expense_direct_cost',
        )
        prior_day = self.today - timedelta(days=15)
        current_day = self.today - timedelta(days=2)

        # Prior-period baselines must include secondary P&L types.
        self.post_balanced_move([
            {'account': self.account_cash, 'debit': 100.0},
            {'account': income_other, 'credit': 100.0},
        ], date=prior_day)
        for account, amount in (
            (expense_other, 10.0),
            (depreciation, 20.0),
            (direct_cost, 30.0),
        ):
            self.post_balanced_move([
                {'account': account, 'debit': amount},
                {'account': self.account_cash, 'credit': amount},
            ], date=prior_day)

        # Trailing sparklines must use exactly the same sets as the tiles.
        for account, amount, is_income in (
            (self.account_revenue, 50.0, True),
            (income_other, 100.0, True),
            (self.account_expense, 5.0, False),
            (expense_other, 10.0, False),
            (depreciation, 20.0, False),
            (direct_cost, 30.0, False),
        ):
            self.post_balanced_move([
                {
                    'account': self.account_cash,
                    'debit': amount if is_income else 0.0,
                    'credit': 0.0 if is_income else amount,
                },
                {
                    'account': account,
                    'credit': amount if is_income else 0.0,
                    'debit': 0.0 if is_income else amount,
                },
            ], date=current_day)

        dashboard = self._make_dashboard(
            period_mode='custom',
            period_date_from=self.today - timedelta(days=9),
            period_date_to=self.today,
        )
        deltas = dashboard._eh_compute_prior_period_deltas()
        self.assertAlmostEqual(deltas['revenue']['prior'], 100.0)
        self.assertAlmostEqual(deltas['expense']['prior'], 60.0)
        self.assertAlmostEqual(
            sum(item['value'] for item in dashboard._eh_pl_trend_series(
                'income', days=30,
            )),
            250.0,
        )
        self.assertAlmostEqual(
            sum(item['value'] for item in dashboard._eh_pl_trend_series(
                'expense', days=30,
            )),
            125.0,
        )

    def test_prior_ar_ap_are_historical_balances_not_today_residuals(self):
        prior_day = self.today - timedelta(days=15)
        settlement_day = self.today - timedelta(days=5)

        invoice = self.post_balanced_move([
            {
                'account': self.account_receivable,
                'debit': 400.0,
                'partner': self.partner_a,
            },
            {'account': self.account_revenue, 'credit': 400.0},
        ], date=prior_day)
        receipt = self.post_balanced_move([
            {'account': self.account_cash, 'debit': 400.0},
            {
                'account': self.account_receivable,
                'credit': 400.0,
                'partner': self.partner_a,
            },
        ], date=settlement_day)
        (invoice.line_ids | receipt.line_ids).filtered(
            lambda line: line.account_id == self.account_receivable
        ).reconcile()

        bill = self.post_balanced_move([
            {'account': self.account_expense, 'debit': 500.0},
            {
                'account': self.account_payable,
                'credit': 500.0,
                'partner': self.partner_b,
            },
        ], date=prior_day)
        payment = self.post_balanced_move([
            {
                'account': self.account_payable,
                'debit': 500.0,
                'partner': self.partner_b,
            },
            {'account': self.account_cash, 'credit': 500.0},
        ], date=settlement_day)
        (bill.line_ids | payment.line_ids).filtered(
            lambda line: line.account_id == self.account_payable
        ).reconcile()

        dashboard = self._make_dashboard(
            period_mode='custom',
            period_date_from=self.today - timedelta(days=9),
            period_date_to=self.today,
        )
        dashboard.invalidate_recordset([
            'receivable_total', 'payable_total',
        ])
        self.assertAlmostEqual(dashboard.receivable_total, 0.0)
        self.assertAlmostEqual(dashboard.payable_total, 0.0)
        deltas = dashboard._eh_compute_prior_period_deltas()
        self.assertAlmostEqual(deltas['receivable_total']['prior'], 400.0)
        self.assertAlmostEqual(deltas['payable_total']['prior'], 500.0)

    # ---- receivables ----

    def test_receivable_total_aggregates_open_lines(self):
        self._post_invoice(self.partner_a, 100.0)
        self._post_invoice(self.partner_b, 250.0)
        d = self._make_dashboard()
        self.assertAlmostEqual(d.receivable_total, 350.0)

    def test_receivable_overdue_filters_by_due_date(self):
        # 100 due in 30 days (not overdue), 250 due 10 days ago (overdue).
        self._post_invoice(self.partner_a, 100.0, days_ago_due=-30)
        self._post_invoice(self.partner_b, 250.0, days_ago_due=10)
        d = self._make_dashboard()
        self.assertAlmostEqual(d.receivable_overdue, 250.0)
        self.assertEqual(d.receivable_days_overdue_max, 10)

    def test_receivable_zero_when_no_open_lines(self):
        d = self._make_dashboard()
        self.assertEqual(d.receivable_total, 0.0)
        self.assertEqual(d.receivable_overdue, 0.0)
        self.assertEqual(d.receivable_days_overdue_max, 0)

    def test_cash_position_value_account_count_and_drill_scope(self):
        dashboard = self._make_dashboard(company_id=self.company.id)
        dashboard.invalidate_recordset([
            'cash_position', 'cash_account_count', 'cash_journal_count',
        ])
        before = dashboard.cash_position
        self.post_balanced_move([
            {'account': self.account_cash, 'debit': 127.35},
            {'account': self.account_revenue, 'credit': 127.35},
        ], date=self.today)
        dashboard.invalidate_recordset([
            'cash_position', 'cash_account_count', 'cash_journal_count',
        ])

        self.assertAlmostEqual(dashboard.cash_position - before, 127.35, 2)
        self.assertGreaterEqual(dashboard.cash_account_count, 1)
        snapshot = dashboard.get_dashboard_snapshot()
        self.assertEqual(
            snapshot['liquidity']['cash_account_count'],
            dashboard.cash_account_count,
        )
        action = dashboard.action_drilldown_cash()
        self.assertEqual(action['res_model'], 'account.move.line')
        self.assertIn(
            ('account_id.account_type', '=', 'asset_cash'), action['domain'],
        )
        self.assertIn(('date', '<=', self.today), action['domain'])
        self.assertIn(('parent_state', '=', 'posted'), action['domain'])

    def test_snapshot_cash_isolated_to_dashboard_company(self):
        dashboard_a = self._make_dashboard(company_id=self.company.id)
        dashboard_a.invalidate_recordset(['cash_position'])
        cash_a_before = dashboard_a.cash_position

        company_b = self.env['res.company'].sudo().create({
            'name': 'EH Dashboard Cash Isolation B',
            'currency_id': self.company.currency_id.id,
        })
        self.env.user.sudo().write({'company_ids': [(4, company_b.id)]})
        env_b = self.env['res.users'].with_context(
            allowed_company_ids=[self.company.id, company_b.id],
        ).with_company(company_b).env
        cash_b = self._ensure_account(
            env_b, 'DB1000', 'Dashboard B Cash', 'asset_cash',
        )
        equity_b = self._ensure_account(
            env_b, 'DB3000', 'Dashboard B Equity', 'equity',
        )
        journal_b = self._ensure_journal(
            env_b, company_b, 'general', 'DBMI', 'Dashboard B Misc',
        )
        move_b = env_b['account.move'].create({
            'move_type': 'entry',
            'journal_id': journal_b.id,
            'date': self.today,
            'line_ids': [
                (0, 0, {
                    'name': 'Dashboard B cash',
                    'account_id': cash_b.id,
                    'debit': 222.0,
                }),
                (0, 0, {
                    'name': 'Dashboard B equity',
                    'account_id': equity_b.id,
                    'credit': 222.0,
                }),
            ],
        })
        move_b.action_post()
        dashboard_b = self.Dashboard.with_context(
            allowed_company_ids=[self.company.id, company_b.id],
        ).create({
            'name': 'Dashboard company B',
            'company_id': company_b.id,
            'period_mode': 'mtd',
            'posted_only': True,
        })

        dashboard_a.invalidate_recordset(['cash_position'])
        self.assertAlmostEqual(dashboard_a.cash_position, cash_a_before, 2)
        self.assertAlmostEqual(dashboard_b.cash_position, 222.0, 2)
        self.assertEqual(
            dashboard_b.get_dashboard_snapshot()['company']['id'], company_b.id,
        )

    # ---- period P/L ----

    def test_period_revenue_includes_income_balance(self):
        # The default _post_invoice posts on (today - 30 days), which
        # falls outside the dashboard's default MTD window. Override
        # the period to cover the post date so the test exercises
        # the income aggregation, not the date filter.
        self._post_invoice(self.partner_a, 100.0)
        self._post_invoice(self.partner_b, 250.0)
        d = self._make_dashboard(
            period_mode='custom',
            period_date_from=self.today - timedelta(days=60),
            period_date_to=self.today,
        )
        self.assertGreaterEqual(d.period_revenue, 350.0 - 0.01)

    def test_period_drill_matches_tile_types_and_posted_scope(self):
        d = self._make_dashboard(
            period_mode='custom',
            period_date_from=self.today - timedelta(days=30),
            period_date_to=self.today,
            posted_only=False,
        )
        revenue = d.action_drilldown_period_revenue()['domain']
        expense = d.action_drilldown_period_expense()['domain']
        self.assertIn(
            ('account_id.account_type', 'in', ('income', 'income_other')),
            revenue,
        )
        self.assertIn(
            ('account_id.account_type', 'in', (
                'expense', 'expense_other', 'expense_depreciation',
                'expense_direct_cost',
            )),
            expense,
        )
        self.assertNotIn(('parent_state', '=', 'posted'), revenue)
        d.posted_only = True
        posted = d.action_drilldown_period_revenue()['domain']
        self.assertEqual(posted.count(('parent_state', '=', 'posted')), 1)

    def test_year_end_provenance_moves_are_not_operating_pl(self):
        if 'eh.year.end.run' not in self.env:
            self.skipTest('eh_account_year_end not installed')
        reversal_day = self.today - timedelta(days=4)
        closing_day = reversal_day - timedelta(days=1)
        operating_day = reversal_day + timedelta(days=1)
        self.company.sudo().write({
            'fiscalyear_last_day': closing_day.day,
            'fiscalyear_last_month': str(closing_day.month),
        })
        fiscal_dates = self.company.compute_fiscalyear_dates(closing_day)
        dashboard = self._make_dashboard(
            company_id=self.company.id,
            period_mode='custom',
            period_date_from=reversal_day,
            period_date_to=self.today,
        )
        dashboard.invalidate_recordset(['period_revenue'])
        revenue_before = dashboard.period_revenue
        trend_before = sum(
            point['value']
            for point in dashboard._eh_pl_trend_series('income', days=30)
        )
        ratio_before = dashboard._eh_ratio_type_flows(
            reversal_day, self.today,
        ).get('income', 0.0)

        closing = self.post_balanced_move([
            {'account': self.account_revenue, 'debit': 100.0},
            {'account': self.account_equity, 'credit': 100.0},
        ], date=closing_day)
        reversal = self.post_balanced_move([
            {'account': self.account_equity, 'debit': 100.0},
            {'account': self.account_revenue, 'credit': 100.0},
        ], date=reversal_day)
        self.post_balanced_move([
            {'account': self.account_cash, 'debit': 50.0},
            {'account': self.account_revenue, 'credit': 50.0},
        ], date=operating_day)
        run = self.env['eh.year.end.run'].sudo().create({
            'name': 'Dashboard year-end provenance',
            'state': 'reversed',
            'company_id': self.company.id,
            'fiscal_year_start': fiscal_dates['date_from'],
            'fiscal_year_end': fiscal_dates['date_to'],
            'journal_id': self.journal_misc.id,
            'retained_earnings_account_id': self.account_equity.id,
            'move_id': closing.id,
            'reversal_move_id': reversal.id,
            'closing_move_ids': [(4, closing.id)],
            'reversal_move_ids': [(4, reversal.id)],
        })

        dashboard.invalidate_recordset(['period_revenue'])
        self.assertEqual(
            dashboard._eh_operational_excluded_move_ids(),
            sorted([closing.id, reversal.id]),
        )
        self.assertAlmostEqual(
            dashboard.period_revenue - revenue_before, 50.0, 2,
        )
        trend_after = sum(
            point['value']
            for point in dashboard._eh_pl_trend_series('income', days=30)
        )
        self.assertAlmostEqual(trend_after - trend_before, 50.0, 2)
        ratio_after = dashboard._eh_ratio_type_flows(
            reversal_day, self.today,
        ).get('income', 0.0)
        self.assertAlmostEqual(ratio_after - ratio_before, -50.0, 2)
        drill = dashboard.action_drilldown_period_revenue()['domain']
        self.assertIn(
            ('move_id', 'not in', sorted(run._year_end_entry_move_ids())),
            drill,
        )

    # ---- optional KPIs ----

    def test_optional_modules_probed_via_registry(self):
        d = self._make_dashboard()
        # Whether the optional modules are present depends on the test
        # install set; we just verify the booleans match the registry.
        self.assertEqual(
            d.has_approval_module,
            'eh.approval.policy' in self.env,
        )
        self.assertEqual(
            d.has_collections_module,
            'eh.collections.case' in self.env,
        )
        self.assertEqual(
            d.has_budget_module,
            'eh.budget.budget' in self.env,
        )

    def test_optional_kpis_zero_when_module_missing(self):
        d = self._make_dashboard()
        if not d.has_approval_module:
            self.assertEqual(d.pending_approval_count, 0)
        if not d.has_collections_module:
            self.assertEqual(d.active_collections_count, 0)
            self.assertEqual(d.active_collections_total, 0.0)
        if not d.has_budget_module:
            self.assertEqual(d.active_budget_count, 0)
            self.assertEqual(d.overrun_budget_count, 0)

    def test_credit_breach_count_has_no_500_cap_and_uses_dashboard_company(self):
        if 'eh.credit.override.log' not in self.env:
            self.skipTest('eh_account_credit_limit not installed')
        Partner = self.env['res.partner'].sudo().with_company(self.company)
        partners = Partner.create([
            {
                'name': 'Dashboard capped-credit %03d' % index,
                'company_id': self.company.id,
                'customer_rank': 1,
                'eh_credit_limit': 1.0,
            }
            for index in range(501)
        ])
        lines = [
            {
                'account': self.account_receivable,
                'debit': 2.0,
                'partner': partner,
                'date_maturity': self.today,
            }
            for partner in partners
        ]
        lines.append({'account': self.account_revenue, 'credit': 1002.0})
        self.post_balanced_move(lines, date=self.today)
        dashboard_a = self._make_dashboard(company_id=self.company.id)
        dashboard_a.invalidate_recordset([
            'credit_limit_breach_count', 'control_signal_total',
        ])
        self.assertEqual(dashboard_a.credit_limit_breach_count, 501)

        company_b = self.env['res.company'].sudo().create({
            'name': 'EH Dashboard Credit Isolation B',
            'currency_id': self.company.currency_id.id,
        })
        self.env.user.sudo().write({'company_ids': [(4, company_b.id)]})
        env_b = self.env['res.users'].sudo().with_context(
            allowed_company_ids=[self.company.id, company_b.id],
        ).with_company(company_b).env
        receivable_b = self._ensure_account(
            env_b, 'DC1100', 'Dashboard B Receivable', 'asset_receivable',
        )
        revenue_b = self._ensure_account(
            env_b, 'DC4000', 'Dashboard B Revenue', 'income',
        )
        journal_b = self._ensure_journal(
            env_b, company_b, 'general', 'DCMI', 'Dashboard Credit B Misc',
        )
        partner_b = env_b['res.partner'].create({
            'name': 'Dashboard B breach',
            'company_id': company_b.id,
            'customer_rank': 1,
            'eh_credit_limit': 1.0,
        })
        move_b = env_b['account.move'].create({
            'move_type': 'entry',
            'journal_id': journal_b.id,
            'date': self.today,
            'line_ids': [
                (0, 0, {
                    'name': 'B receivable',
                    'account_id': receivable_b.id,
                    'partner_id': partner_b.id,
                    'date_maturity': self.today,
                    'debit': 2.0,
                }),
                (0, 0, {
                    'name': 'B revenue',
                    'account_id': revenue_b.id,
                    'credit': 2.0,
                }),
            ],
        })
        move_b.action_post()
        dashboard_b = self.Dashboard.with_context(
            allowed_company_ids=[self.company.id, company_b.id],
        ).create({
            'name': 'Dashboard credit company B',
            'company_id': company_b.id,
            'period_mode': 'mtd',
            'posted_only': True,
        })
        dashboard_b.invalidate_recordset([
            'credit_limit_breach_count', 'control_signal_total',
        ])
        self.assertEqual(dashboard_b.credit_limit_breach_count, 1)

    # ---- open_for_current_user ----

    def test_open_for_current_user_creates_singleton(self):
        # The default entry point now returns the Owl client action
        # (tag eh_account_dashboard.board) with the resolved record id
        # passed via context. Calling twice should resolve to the same
        # underlying dashboard record.
        action = self.Dashboard.open_for_current_user()
        self.assertEqual(action['type'], 'ir.actions.client')
        self.assertEqual(action['tag'], 'eh_account_dashboard.board')
        first_id = action['context']['eh_dashboard_id']
        action2 = self.Dashboard.open_for_current_user()
        self.assertEqual(
            action2['context']['eh_dashboard_id'], first_id,
        )

    def test_open_form_for_current_user_returns_form_action(self):
        # The form-view escape hatch still resolves to the act_window
        # form action so power users can edit fields directly.
        action = self.Dashboard.open_form_for_current_user()
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], self._dashboard_model())
        self.assertEqual(action['view_mode'], 'form')

    def test_get_dashboard_snapshot_shape(self):
        # The Owl board contract: snapshot must include the record id,
        # the period block, and one entry per KPI section. The values
        # are computed by the existing field computes; only assert the
        # keys here so the test is robust to ledger contents.
        d = self._make_dashboard()
        snap = d.get_dashboard_snapshot()
        self.assertEqual(snap['record_id'], d.id)
        for section in (
            'period', 'currency', 'company',
            'liquidity', 'pnl', 'modules',
            'operations', 'controls', 'cash_trend',
        ):
            self.assertIn(section, snap)
        self.assertIsInstance(snap['cash_trend'], list)

    def test_payable_prior_delta_flat_ap_reports_zero(self):
        # A single open vendor bill posted before the prior window's end
        # stays open (unpaid) across both periods, so accounts payable is
        # flat. The prior-period delta on payable_total must be ~zero.
        #
        # payable_total is displayed absolute while the prior cumulative
        # sum is signed (credit-side negative); before the fix the delta
        # compared abs(current) against a negative prior, roughly doubling
        # the delta and reporting a spurious ~+200% swing.
        amount = 5000.0
        post_date = self.today - timedelta(days=90)
        self.post_balanced_move(
            [
                {'account': self.account_expense, 'debit': amount},
                {'account': self.account_payable, 'credit': amount},
            ],
            date=post_date,
        )
        dash = self._make_dashboard(
            company_id=self.company.id,
            period_mode='last_30',
        )
        self.assertAlmostEqual(dash.payable_total, amount, places=2)
        deltas = dash._eh_compute_prior_period_deltas()
        payable = deltas['payable_total']
        self.assertAlmostEqual(payable['current'], amount, places=2)
        self.assertAlmostEqual(payable['prior'], amount, places=2)
        self.assertAlmostEqual(payable['delta'], 0.0, places=2)
        self.assertAlmostEqual(payable['pct'] or 0.0, 0.0, places=2)

    def _dashboard_model(self):
        return 'eh.account.dashboard'

    # ---- drilldowns ----

    def test_drilldown_receivables_returns_action(self):
        d = self._make_dashboard()
        action = d.action_drilldown_receivables()
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], 'account.move.line')
        self.assertIn(
            ('account_id.account_type', '=', 'asset_receivable'),
            action['domain'],
        )

    def test_drilldown_pending_approvals_when_no_module(self):
        d = self._make_dashboard()
        if not d.has_approval_module:
            action = d.action_drilldown_pending_approvals()
            self.assertFalse(action)

    def test_listing_and_client_sources_match_dashboard_contract(self):
        module_root = Path(__file__).resolve().parents[1]
        manifest = ast.literal_eval(
            (module_root / '__manifest__.py').read_text(),
        )
        listing = (module_root / 'static/description/index.html').read_text()
        template = (
            module_root / 'static/src/dashboard/dashboard.xml'
        ).read_text()
        client = (
            module_root / 'static/src/dashboard/dashboard.js'
        ).read_text()
        sparkline = (
            module_root / 'static/src/dashboard/sparkline.js'
        ).read_text()
        stylesheet = (
            module_root / 'static/src/dashboard/dashboard.scss'
        ).read_text()
        view = (module_root / 'views/dashboard_views.xml').read_text()
        pot = (
            module_root / 'i18n/eh_account_dashboard.pot'
        ).read_text()

        self.assertEqual(manifest['version'], '19.0.1.5.4')
        self.assertNotIn('AR and AP aging', manifest['summary'])
        self.assertNotIn('20 ms', listing)
        self.assertNotIn('100,000+ journal lines', listing)
        self.assertNotIn('caption="\'Cash position\'"', template)
        self.assertIn('caption="labels.cashPosition"', template)
        self.assertIn('ratioProvenanceItems', template)
        self.assertIn('visibilitychange', client)
        self.assertIn('document.hidden', client)
        self.assertIn('_t("Cash position")', client)
        self.assertNotIn('uid:', sparkline)
        self.assertNotIn('.eh_dash_attn_allclear', stylesheet)
        self.assertEqual(view.count('name="has_approval_module"'), 1)
        for msgid in (
            'Cash position', 'Interest: name heuristic',
            'Inventory: no account detected', 'View cash entries',
        ):
            self.assertIn('msgid "%s"' % msgid, pot)


@tagged('eh_account_dashboard', 'integration', 'post_install', '-at_install')
class TestDashboardDocumentCounts(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Dashboard = cls.env['eh.account.dashboard']
        cls.today = fields.Date.context_today(cls.env['res.users'])

    def _dash(self):
        return self.Dashboard.create({
            'name': 'Doc counts', 'period_mode': 'mtd', 'posted_only': True,
            'company_id': self.company.id,
        })

    def _invoice(self, move_type, account, invoice_date, post=False):
        move = self.env['account.move'].create({
            'move_type': move_type,
            'partner_id': self.partner_a.id,
            'invoice_date': invoice_date,
            'invoice_line_ids': [(0, 0, {
                'name': 'Line', 'account_id': account.id,
                'quantity': 1.0, 'price_unit': 100.0,
                'tax_ids': [(6, 0, [])],
            })],
        })
        if post:
            move.action_post()
        return move

    def test_document_counts(self):
        dash = self._dash()
        counts = ['draft_invoice_count', 'late_invoice_count',
                  'draft_bill_count', 'late_bill_count']
        dash.invalidate_recordset(counts)
        base = {k: dash[k] for k in counts}

        # Draft customer invoice.
        self._invoice('out_invoice', self.account_revenue, self.today)
        # Posted, overdue customer invoice (due in the past, unpaid).
        late_move = self._invoice('out_invoice', self.account_revenue,
                                  self.today - timedelta(days=40), post=True)
        late_move.invoice_date_due = self.today - timedelta(days=10)
        # Draft vendor bill.
        self._invoice('in_invoice', self.account_expense, self.today)

        dash.invalidate_recordset(counts)
        self.assertEqual(dash.draft_invoice_count - base['draft_invoice_count'], 1)
        self.assertEqual(
            dash.late_invoice_count - base['late_invoice_count'], 1,
            msg="due=%s state=%s pay=%s" % (
                late_move.invoice_date_due, late_move.state,
                late_move.payment_state))
        self.assertEqual(dash.draft_bill_count - base['draft_bill_count'], 1)
        self.assertEqual(dash.late_bill_count - base['late_bill_count'], 0)

    def test_document_counts_in_snapshot(self):
        dash = self._dash()
        snap = dash.get_dashboard_snapshot()
        self.assertIn('documents', snap)
        self.assertIn('draft_invoice_count', snap['documents'])
        self.assertIn('integrity', snap)
        self.assertIn('sequence_hole_count', snap['integrity'])

    def test_sequence_hole_detection(self):
        journal = self.env['account.journal'].create({
            'name': 'Seq Journal', 'code': 'SEQJ', 'type': 'sale',
            'company_id': self.company.id,
        })
        dash = self._dash()
        dash.invalidate_recordset(['sequence_hole_count'])
        base = dash.sequence_hole_count

        moves = self.env['account.move']
        for _i in range(3):
            move = self.env['account.move'].create({
                'move_type': 'out_invoice', 'journal_id': journal.id,
                'partner_id': self.partner_a.id, 'invoice_date': self.today,
                'invoice_line_ids': [(0, 0, {
                    'name': 'L', 'account_id': self.account_revenue.id,
                    'quantity': 1.0, 'price_unit': 10.0,
                    'tax_ids': [(6, 0, [])],
                })],
            })
            move.action_post()
            moves += move

        dash.invalidate_recordset(['sequence_hole_count'])
        self.assertEqual(dash.sequence_hole_count - base, 0)  # contiguous

        # Reset the middle posting to draft: leaves a gap among posted.
        moves[1].button_draft()
        dash.invalidate_recordset(['sequence_hole_count'])
        self.assertGreaterEqual(dash.sequence_hole_count - base, 1)

    def test_unhashed_zero_without_hash_journal(self):
        dash = self._dash()
        dash.invalidate_recordset(['unhashed_entry_count'])
        self.assertEqual(dash.unhashed_entry_count, 0)

    def test_to_reconcile_count(self):
        bank = self.env['account.journal'].create({
            'name': 'Dash Bank', 'code': 'BNKD', 'type': 'bank',
            'company_id': self.company.id,
        })
        dash = self._dash()
        dash.invalidate_recordset(['to_reconcile_count'])
        base = dash.to_reconcile_count
        self.env['account.bank.statement.line'].create({
            'journal_id': bank.id, 'date': self.today,
            'amount': 100.0, 'payment_ref': 'unreconciled',
        })
        dash.invalidate_recordset(['to_reconcile_count'])
        self.assertEqual(dash.to_reconcile_count - base, 1)

    def test_bank_ops_in_snapshot(self):
        dash = self._dash()
        snap = dash.get_dashboard_snapshot()
        self.assertIn('bank_ops', snap)
        self.assertIn('to_reconcile_count', snap['bank_ops'])
        self.assertGreaterEqual(snap['bank_ops']['to_check_count'], 0)
