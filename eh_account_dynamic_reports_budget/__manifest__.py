# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Reports: Budget Columns',
    'summary': 'Auditable P&L budget, actual, variance, and variance-percent columns',
    'description': 'Extends the by-nature, single-company Profit and Loss report with Budget, Actual-minus-Budget Variance, and Variance Percent columns. Company-wide budgets compare to company actuals. Analytic-scoped budgets compare to allocation-weighted actuals from journal-item analytic distributions. Users choose disclosed daily-overlap apportionment or exact-period validation. Budget choices show lifecycle and version identity and stay synchronized with company scope.',
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.0.7',
    'depends': ['eh_account_dynamic_reports', 'eh_account_budget_pro'],
    'data': [],
    'assets': {
        'web.assets_backend': [
            'eh_account_dynamic_reports_budget/static/src/budget_filter.js',
            'eh_account_dynamic_reports_budget/static/src/budget_filter.xml',
        ],
        'web.assets_unit_tests': [
            'eh_account_dynamic_reports_budget/static/tests/**/*',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif'],
}
