# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Dynamic Account Reports',
    'summary': 'Next generation dynamic financial reports for Odoo 19 Community, built on a SQL-direct aggregation engine that stays fast on large ledgers. Profit and Loss, Balance Sheet, Trial Balance, General Ledger, Partner Ledger, Aged Receivable, Aged Payable, Cash Flow (direct and indirect), plus Customer and Vendor Statements and Analytic Balance. Compare custom or successive periods in either order, pivot Profit and Loss, Balance Sheet, and Trial Balance by analytic account or plan with independent totals, and drill into the exact scoped cell. Hierarchical chart nesting, grouped screen/XLSX/PDF headers, currency-aware exports, and an append-only audit log complete the suite. Keywords: Odoo 19 dynamic financial reports, profit and loss report, balance sheet, trial balance, analytic columns, period comparison, drill down journal entries, accounting report XLSX PDF export, fast reports on large database.',
    'description': """The financial reporting layer of the ERP Heritage accounting suite for Odoo 19 Community. Fifteen accountant reports are built on a single AbstractModel handler hierarchy and rendered through one OWL viewer, so the look and feel is uniform across every report. The hot path is plain Python SQL, not the ORM: a parameter-bound, posted-only, company-scoped query against account.move.line that stays responsive on large databases.

Reports included: Executive Summary, Deferred Revenue, Deferred Expense, Bank Reconciliation Proof, Profit and Loss, Balance Sheet, Trial Balance, General Ledger, Partner Ledger, Aged Receivable, Aged Payable, Cash Flow (both direct and indirect methods), Customer Statement, Vendor Statement, and Analytic Balance.

Engineering wedges that competitors gloss over:

A report-input-version cache. Each company carries a server-owned atomic SQL counter covering posted-ledger changes plus report-visible master data, configuration, exchange rates, and supported suite sub-ledgers. Cached results are keyed by normalized effective options, that version, and strict company-scope equality, so stale or overlapping-scope payloads are rejected.

An append-only audit log. Every render writes one row, including cache hits, and that row points back at the source compute it was served from. The model rejects edits and blocks deletion, so the trail cannot be altered even by an administrator.

Drill down. Data lines (accounts, partners, journal items) click through to the underlying journal items or the journal entry form. Section headers, subtotals and computed rows are aggregates and are not drillable.

Comparatives. Prior period, prior year, and explicit custom date windows are native. Profit and Loss, Balance Sheet, Executive Summary, and Trial Balance can show several successive periods in ascending or descending order; Trial Balance repeats its complete opening, movement, and closing block for every period. Single-comparison variance percentages remain divide-by-zero safe and year shifting remains leap-day safe.

Analytic columns. Profit and Loss, Balance Sheet, and Trial Balance pivot horizontally by selected analytic accounts or analytic plans and can combine those groups with every comparison period. Each period carries an independently queried Total, so overlapping distributions and unallocated amounts never make a summed column look authoritative. Grouped headers remain aligned on screen and in XLSX and PDF.

Cell-accurate drilldown. Every new period and analytic value column carries its own validated date, company, analytic-account, and analytic-plan scope. Clicking a drillable cell replays that exact scope, including weighted analytic allocation, instead of opening a broader ledger domain; synthetic retained-earnings cells fail closed when no exact journal-line projection exists.

Currency-aware rendering. Every monetary cell renders with the company currency symbol, and the XLSX writer emits a matching Excel number-format string so the spreadsheet matches the screen cell for cell. Sectioned reports nest by account.group with per-user fold persistence.

Presentation currency. A currency selector restates any report into a chosen currency. Balance-sheet and ledger balances use the period-end closing rate, while Profit and Loss flows use an audit-visible, day-weighted period-average rate in line with IAS 21. Labels, dates and percentages remain untouched. For companies that post in more than one currency, the General Ledger and Partner Ledger add an "Amount in Currency" column that shows each line's original transaction amount and currency code next to the company-currency figures.

Multi-select filtering. Beside each single-pick filter, a "Pick more" button opens a searchable, checkbox records picker so you can scope a report to several partners, accounts or analytic accounts at once.

This is the free anchor for the suite. For a custom report builder and scheduled email delivery, see the Dynamic Reports Pro module that builds on top of this one.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.8.5',
    'depends': ['eh_account_base'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/menus.xml',
        'data/reports.xml',
        'data/account_tags.xml',
        'data/paperformat.xml',
        'data/report_pdf.xml',
        'report/report_dynamic_pdf_template.xml',
        'views/res_partner_views.xml',
        'views/noncash_transaction_views.xml',
    ],
    'post_init_hook': 'post_init_hook',
    'assets': {
        'web.assets_backend': ['eh_account_dynamic_reports/static/src/components/**/*'],
        # WS5 hoot suite: pure table-craft logic (windowing / search / variance
        # semantics) tested in isolation. Loaded only into the unit-test bundle.
        'web.assets_unit_tests': [
            'eh_account_dynamic_reports/static/tests/**/*',
        ],
    },
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': True,
    'auto_install': False,
}
