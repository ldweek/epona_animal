# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The held-for-sale mechanics (measure
# at the lower of carrying amount and fair value less costs to sell, cease
# depreciation on classification, present discontinued operations separately)
# are IFRS 5 as published; no code or comments derive from any proprietary or
# third-party module.
#
##############################################################################
{
    'name': 'IFRS 5 Held for Sale',
    'summary': 'IFRS 5 non-current assets held for sale, disposal groups and discontinued operations for Odoo 19 Community, with exact-date depreciation accrual, lower-of-carrying-amount-and-fair-value-less-costs-to-sell remeasurement, group write-down and carrying-weight reversal allocation, exact journal-item discontinued-operation scope, twelve-month overdue flag, and sealed disposal gain or loss. Search terms: odoo 19 held for sale, IFRS 5 Odoo, discontinued operations, fair value less costs to sell, disposal group accounting, disposal group write-down allocation, cease depreciation held for sale, non-current asset disposal, impairment write-down reversal cap.',
    'description': "This module implements IFRS 5 for non-current assets, disposal groups and discontinued operations in Odoo 19 Community. When a linked asset is classified, earned depreciation is posted through the exact classification date (including a mid-period stub), its carrying amount is seeded from the resulting ledger net book value, and depreciation is then paused (IFRS 5.25); ceasing classification rebuilds future depreciation prospectively from the resume date. The item is measured at the lower of carrying amount and fair value less costs to sell and any shortfall posts through the asset impairment engine. Disposal groups use member lines as the single group measurement record for assets, goodwill and directly associated liabilities. Write-downs follow the IFRS 5.23 order (goodwill first, then carrying-amount allocation over in-scope members subject to fair-value floors); reversals use current carrying-amount weights and remain capped by each non-goodwill member's cumulative write-down (IAS 36.122, IAS 36.124). Group sale allocates gross consideration, including transferred liabilities, consistently across linked assets and seals every disposal move to the group. Discontinued-operation reporting uses immutable exact journal-item scope plus automatically included remeasurement and disposal P&L evidence; the API exposes pre-tax and explicitly tax-adjusted post-tax results without claiming to build a financial-statement layout or mutating chart-account tags. Posting is gated to the EH Accounting Manager group and every entry is stamped for audit.",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.2.1.2',
    'depends': [
        'eh_account_base',
        'eh_account_assets_pro',
        'account',
        'mail',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'views/held_for_sale_views.xml',
        'views/disposal_group_views.xml',
        'data/menus.xml',
    ],
    'assets': {
        'web.assets_tests': [
            'eh_account_held_for_sale/static/tests/tours/held_for_sale_test_tour.js',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif', 'static/description/held_for_sale_01.png'],
}
