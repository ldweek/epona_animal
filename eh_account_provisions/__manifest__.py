# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The provision mechanics (recognise a
# present obligation at the best estimate, discount to present value where
# material, unwind the discount as a finance cost, and utilise against the
# liability on settlement) are IAS 37 as published; no code or comments
# derive from any proprietary or third-party module.
#
##############################################################################
{
    'name': 'IAS 37 Provisions',
    'summary': 'IAS 37 provisions for Odoo 19 Community that recognise a present obligation at the best estimate, discount it to present value when the time value of money is material, unwind the discount each period as a finance cost, remeasure to a revised estimate, and utilise against settlement, while contingent items stay disclosure only. Search: odoo 19 provisions, IAS 37 provisions, best estimate provision, present value provision, discount unwinding finance cost, onerous contract provision, restructuring provision, warranty provision, contingent liability disclosure, provision remeasurement.',
    'description': 'A dedicated IAS 37 provision register for Odoo 19 Community. Each provision is recognised at the present value of the undiscounted best estimate, posting a debit to the expense account and a credit to the provision liability. Where the time value of money is material, a pre-tax discount rate and a settlement horizon discount the estimate, and the Unwind Discount action accretes the carrying amount towards the undiscounted figure by posting the period finance cost. Remeasure books a change in estimate through profit or loss, Utilise settles expenditure against the liability, and Reverse writes back a provision no longer required. Contingent liabilities and contingent assets are held for disclosure only and any attempt to post them is refused. Posting is gated to the EH Accounting Manager group and every generated journal entry is sealed against edit.',
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
        'views/provision_views.xml',
        'data/menus.xml',
    ],
    'assets': {
        'web.assets_tests': [
            'eh_account_provisions/static/tests/tours/provision_test_tour.js',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif', 'static/description/provisions_01_provision.png'],
}
