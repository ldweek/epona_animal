# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The earnings-per-share mechanics
# (basic EPS = earnings attributable to ordinary holders over the weighted
# average number of ordinary shares; diluted EPS adjusts both for dilutive
# potential ordinary shares) are IAS 33 as published; no code or comments
# derive from any proprietary or third-party module.
#
##############################################################################
{
    'name': 'EPS (IAS 33)',
    'summary': 'IAS 33 basic and diluted EPS with dated share balances, weighted potential-share classes, treasury-stock methods, and sealed evidence.',
    'description': 'This module adds an Earnings Per Share run that computes basic and diluted EPS under IAS 33 with no journal postings, so it is a pure disclosure calculation. Basic EPS is profit attributable to ordinary holders (net profit less after-tax preference dividends) divided by the weighted average number of ordinary shares, and that weighted average is built from dated share movements, each weighted by the number of days it was outstanding within the period. Diluted EPS sequences potential ordinary shares most-dilutive first, adding each instrument only while it continues to reduce EPS, so anti-dilutive classes are excluded and diluted EPS is never reported above basic. When earnings are split between continuing and discontinued operations, the run reports basic and diluted EPS from continuing operations with the discontinued amount disclosed as the difference, uses continuing profit as the control number for the dilution test, and can prefill the discontinued result from the ledger where the held-for-sale module is installed. Bonus issues, splits, and consolidations are recorded as dated restatement events applied retrospectively to the weighted average, with their cumulative factor restating the comparative period, and the treasury-stock method can resolve its average market price from dated price observations inside the period instead of a single scalar. Once a run is computed, its earnings and share figures are frozen against silent edits, additions, and deletions across the run and its child lines.',
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.2.0',
    'depends': ['eh_account_base', 'account', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'views/eps_run_views.xml',
        'data/menus.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif', 'static/description/eps_01.png'],
}
