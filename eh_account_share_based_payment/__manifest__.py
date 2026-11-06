# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The share-based payment mechanics
# (grant-date fair value for equity-settled awards, expected-forfeiture
# true-up for service and non-market conditions, market conditions embedded
# in the grant-date fair value with no true-up, graded-vesting tranche
# expensing, liability remeasurement for cash-settled awards, incremental
# fair value on modification and acceleration on cancellation) are IFRS 2
# as published; no code or comments derive from any proprietary or
# third-party module.
#
##############################################################################
{
    'name': 'IFRS 2 Share-based Pay',
    'summary': 'IFRS 2 share-based payment engine for Odoo 19 Community that expenses equity-settled awards at grant-date fair value over the vesting period with expected-forfeiture true-up, expenses each graded-vesting tranche over its own vesting period, keeps market-condition expense unreversed, remeasures cash-settled awards to current fair value each period, spreads modification incremental fair value, accelerates on cancellation, and prices options with built-in Black-Scholes and binomial helpers. Search: odoo 19 share based payment, IFRS 2 accounting, employee share options, equity settled awards, cash settled SARs, graded vesting, vesting conditions, option valuation Black Scholes, binomial option pricing, share option expense, IFRS 2 modification, IFRS 2 disclosure.',
    'description': 'A dedicated IFRS 2 share-based payment engine for Odoo 19 Community. Plans define the settlement mode, vesting terms and condition kind; grants under a plan carry the instruments, the grant-date fair value and an updatable forfeiture estimate. Period runs compute the cumulative expense per IFRS 2 and post the period delta as a sealed journal entry: equity-settled awards accrue grant-date fair value times the instruments expected to vest times the vested fraction (each graded tranche over its own vesting period), estimates true up through the current period, non-market failures reverse in full while market-condition failures never reverse once service is rendered, and cash-settled awards remeasure the liability to current fair value each period until settlement. Beneficial modifications spread their incremental fair value over the remaining vesting period, cancellation accelerates the unrecognised balance immediately, and a built-in valuation helper prices options with Black-Scholes or a Cox-Ross-Rubinstein binomial tree. The plan carries an IFRS 2.45 rollforward of granted, forfeited, exercised and expired instruments with the weighted average exercise price. Posting is gated to the EH Accounting Manager group and every generated journal entry is sealed against edit.',
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.0.12',
    'depends': ['eh_account_base', 'account', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'views/sbp_views.xml',
        'data/menus.xml',
    ],
    'assets': {
        'web.assets_tests': [
            'eh_account_share_based_payment/static/tests/tours/sbp_test_tour.js',
        ],
    },
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
