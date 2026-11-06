# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The dashboard composes KPIs
# computed via the suite's existing SQL builder against standard
# Odoo accounting tables. No layout, naming, or template derives
# from any proprietary or third-party Odoo module.
#
##############################################################################
{
    'name': 'Financial Dashboard',
    'summary': 'Company-scoped Odoo 19 Community financial dashboard for cash-account balance, open receivable and payable totals with overdue status, period profit and loss, 30-day trends, financial ratios and installed-suite control signals. Includes posted/custom period controls, matching drill-down scopes, visible-tab refresh and per-user company isolation. Search: Odoo 19 Community financial dashboard, accounting KPI dashboard, cash position dashboard, open receivables payables, period P and L, CFO dashboard, cash flow sparkline, multi-company KPI dashboard.',
    'description': """One screen summarises a company's financial state and opens the records behind its actionable figures. Six core tiles show cash-account balance, current open receivables (total, overdue and oldest overdue age), current open payables (total and overdue), and selected-period revenue, expense and net. Optional sections appear only for installed suite modules and cover approvals, collections, active budgets, credit-limit signals, SEPA mandate dormancy, year-end, period-close and FX revaluation runs.

Ledger money aggregates use the suite's parameterised MoveLineQuery with an explicit dashboard-company scope; record counters and open-item residuals use company-scoped ORM aggregates. Cash position is the cumulative balance of asset_cash accounts through today. Receivable and payable tiles show current residuals, not configurable aging buckets. Operating P&L, trends, deltas and ratio flows exclude only closing and reversal moves proven by Year-End Closing run links, so a window crossing fiscal close does not turn those engine entries into operating activity.

A "needs attention" rail sits beside the trend charts and surfaces day-to-day accounting hygiene from standard Odoo tables: overdue customer invoices and vendor bills, bank statement lines still to reconcile, posted entries flagged for review, the draft invoice and bill backlog, sequence gaps in posted journals, and entries posted without an inalterable hash. Each row that maps to records opens them in one click.

Pick month, quarter or year to date, trailing 30 or 90 days, or an ordered custom range, plus a posted-only toggle. Cash, open-item and P&L drills use the same account types, company and posted/date scope as their tile; the active-budget and 30-day credit-override actions use their matching counter scope. Thirty-day cash, revenue and expense sparklines sit beside equal-length prior-period deltas. Auto-refresh runs every 60 seconds only while the browser tab is visible, resumes once on return, and coalesces overlapping requests.

Ratio tiles disclose whether inventory, interest and income-tax inputs came from an account tag, a documented name heuristic or no detected account. The form and Owl board share the same model constraints, including rejection of reversed custom ranges from RPC, imports or direct writes.

The layout is an original dark command-centre design built as an Owl client action: a near-black ground, a single mint accent, monospaced numerals, and a strict spacing grid. It does not derive its layout, naming, or markup from any stock Odoo dashboard.

This module is read-only. It posts no journal entries and changes no accounting state. It reads standard Odoo accounting tables and your installed suite modules, and shows you what is there.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.5.5',
    'depends': ['eh_account_base', 'eh_account_dynamic_reports', 'account'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'views/dashboard_views.xml',
        'views/res_company_views.xml',
        'data/menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'eh_account_dashboard/static/src/dashboard/dashboard.scss',
            'eh_account_dashboard/static/src/dashboard/sparkline.js',
            'eh_account_dashboard/static/src/dashboard/kpi_tile.js',
            'eh_account_dashboard/static/src/dashboard/dashboard.js',
            'eh_account_dashboard/static/src/dashboard/dashboard.xml',
        ],
        'web.assets_unit_tests': [
            'eh_account_dashboard/static/tests/**/*.test.js',
        ],
        'web.assets_tests': [
            'eh_account_dashboard/static/tests/tours/dashboard_test_tour.js',
        ],
    },
    'images': [
        'static/description/banner.gif',
        'static/description/dashboard_01_overview.png',
        'static/description/dashboard_02_overdue_card.png',
        'static/description/dashboard_03_control_signals.png',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
