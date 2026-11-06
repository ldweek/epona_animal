# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression: the dashboard P&L aggregations must include the standard
Odoo 19 'expense_other' ('Other Expenses', IAS 1) account type.

Omitting it silently drops real operating cost (FX losses, bank charges,
donations), inflating net income, net margin, ROA, ROE and EBIT and
potentially masking a covenant/margin threshold breach. Two aggregations
are covered:

* the ratio engine's RATIO_EXPENSE_TYPES (dashboard_ratios.py), which
  feeds net_income / net_margin_pct and every derived ratio;
* the period P&L tiles' expense tuple (dashboard.py), which feeds
  period_expense / period_net.

'expense_other' only exists on Odoo 19+, so the whole case is gated on
the selection being present and no-ops on 16/17/18 (where the value
would never match any account and behaviour is unchanged).
"""

from datetime import date

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase

from .test_golden_ratios import (
    _flatten, _make_account, _make_company, _make_journal, _post,
)


def _has_expense_other(env):
    field = env['account.account']._fields['account_type']
    return 'expense_other' in dict(field.selection)


@tagged('eh_account_dashboard', 'post_install', '-at_install')
class TestExpenseOtherIncluded(EhGoldenTestCase):
    """Fresh-company ledger with an 'expense_other' account; the P&L
    aggregations must subtract it from profit.

    Ledger (period 2025-01-01..2025-12-31):
        Revenue (income)              1,000,000
        Operating expenses (expense)    600,000
        Other expenses (expense_other)  150,000
      -> net income = 1,000,000 - 600,000 - 150,000 = 250,000
      -> net margin = 250,000 / 1,000,000 * 100 = 25.00

    If 'expense_other' were dropped, expenses would be 600,000, net
    income 400,000 and net margin 40.00 (the pre-fix defect).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Dashboard = cls.env['eh.account.dashboard']
        env = cls.env
        c = cls.company = _make_company(env, 'EH Expense-Other Co', 'USD')
        journal = _make_journal(env, c, 'REXO')
        cls.a_cash = _make_account(
            env, c, '1000', 'Cash at Bank', 'asset_cash')
        cls.a_equity = _make_account(
            env, c, '3000', 'Share Capital', 'equity')
        cls.a_revenue = _make_account(
            env, c, '4000', 'Sales Revenue', 'income')
        cls.a_opex = _make_account(
            env, c, '6000', 'Operating Expenses', 'expense')
        # Only create the other-expense account when the type exists
        # (Odoo 19+); on earlier series the whole test skips.
        cls.a_other = None
        if _has_expense_other(env):
            cls.a_other = _make_account(
                env, c, '6800', 'Other Expenses', 'expense_other')

        mid = date(2025, 6, 15)
        # Opening cash so the balance sheet is non-empty.
        _post(env, c, journal, date(2024, 12, 30), [
            (cls.a_cash, 500000.0, 0.0), (cls.a_equity, 0.0, 500000.0)])
        _post(env, c, journal, mid, [
            (cls.a_cash, 1000000.0, 0.0), (cls.a_revenue, 0.0, 1000000.0)])
        _post(env, c, journal, mid, [
            (cls.a_opex, 600000.0, 0.0), (cls.a_cash, 0.0, 600000.0)])
        if cls.a_other is not None:
            _post(env, c, journal, mid, [
                (cls.a_other, 150000.0, 0.0), (cls.a_cash, 0.0, 150000.0)])

        cls.dash = cls.Dashboard.create({
            'name': 'Expense-other dashboard',
            'company_id': c.id,
            'period_mode': 'custom',
            'period_date_from': date(2025, 1, 1),
            'period_date_to': date(2025, 12, 31),
            'posted_only': True,
        })

    def setUp(self):
        super().setUp()
        if not _has_expense_other(self.env):
            self.skipTest("'expense_other' account type not on this Odoo")

    def test_ratio_engine_includes_expense_other(self):
        """net_margin_pct must be 25.00 (other expense subtracted), not
        40.00 (other expense dropped)."""
        flat = _flatten(self.dash._eh_ratio_payload())
        entry = flat['net_margin_pct']
        self.assertIsNotNone(entry['value'])
        self.assertAlmostEqual(
            entry['value'], 25.00, places=2,
            msg="net margin must include 'expense_other'; got %s "
                "(40.00 means other-expenses were dropped)" % entry['value'])
        # ROA and ROE are driven by the same net_income; both must reflect
        # the lower profit rather than the inflated 400,000.
        self.assertIsNotNone(flat['roa']['value'])
        self.assertIsNotNone(flat['roe']['value'])

    def test_period_tile_includes_expense_other(self):
        """period_expense/period_net tiles must subtract the other
        expense too."""
        self.assertAlmostEqual(
            self.dash.period_expense, 750000.0, places=2,
            msg="period_expense must include 'expense_other'")
        self.assertAlmostEqual(
            self.dash.period_net, 250000.0, places=2,
            msg="period_net must be revenue minus all expense types")
