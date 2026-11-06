# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The expected-credit-loss mechanics
# (a provision matrix of ageing buckets and loss rates under the simplified
# approach, a loss allowance carried as a contra-asset, the movement to
# impairment loss) are IFRS 9 as published; no code or comments derive from
# any proprietary or third-party module.
#
##############################################################################
{
    'name': 'IFRS 9 Expected Loss',
    'summary': 'IFRS 9 expected credit loss for Odoo 19 Community, built as a provision matrix of ageing buckets and loss rates that posts only the movement in the loss allowance to impairment loss. Search: odoo 19 expected credit loss, IFRS 9 ECL, provision matrix, simplified approach, loss allowance, impairment of receivables, bad debt allowance, ageing buckets loss rate, general 3-stage model, EAD LGD PD, roll-forward loss allowance.',
    'description': "Expected Credit Loss measures the loss allowance on trade receivables under IFRS 9. Each run is scoped to one legal entity and carries a provision matrix of contiguous, non-overlapping ageing buckets; population reconstructs point-in-time receivable residuals at the reporting date and fails closed when a matrix band or installed exposure provider is incomplete. It computes the closing allowance, and posting recognises only the movement from the opening allowance in one balanced entry: an increase debits impairment loss and credits a dedicated non-receivable loss-allowance contra account, while a decrease reverses it (IFRS 9.5.5.8). A general 3-stage model (EAD, LGD, 12-month and lifetime PD), an explicit keyed-versus-defaulted Stage 3 PD policy, probability-weighted scenarios and optional present-value discounting are supported. Foreign-currency write-offs preserve source-currency evidence. Posting and reversal are manager-gated; reversal uses an explicit open accounting date, and a computed or posted run is frozen until its controlled revision or reversal workflow.",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.2.8',
    'depends': ['eh_account_base', 'account', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'views/ecl_run_views.xml',
        'views/ecl_engine_views.xml',
        'data/menus.xml',
    ],
    'assets': {
        'web.assets_tests': [
            'eh_account_ecl/static/tests/tours/ecl_test_tour.js',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif', 'static/description/ecl_01_run.png'],
}
