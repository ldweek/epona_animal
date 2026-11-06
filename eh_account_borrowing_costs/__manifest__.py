# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The capitalisation mechanics
# (directly-attributable borrowing costs on a qualifying asset, specific
# borrowings net of temporary investment income plus general borrowings at
# the capitalisation rate, capped at costs actually incurred) are IAS 23 as
# published; no code or comments derive from any proprietary or third-party
# module.
#
##############################################################################
{
    'name': 'Borrowing Costs (IAS 23)',
    'summary': 'IAS 23 borrowing-cost capitalisation for Odoo 19 Community that computes the capitalisable amount and posts the reclassification from interest expense to the qualifying asset. odoo 19 borrowing costs, IAS 23 capitalisation, capitalisation rate, qualifying asset, directly attributable borrowing cost, specific and general borrowings, temporary investment income, weighted average expenditure, interest capitalisation.',
    'description': 'This module capitalises the borrowing costs directly attributable to the acquisition, construction or production of a qualifying asset under IAS 23. For each period it takes the borrowing costs on funds borrowed specifically for the asset, deducts the income earned on the temporary investment of those funds, and adds the borrowing costs on general borrowings by applying the capitalisation rate to the expenditure on the asset. The amount capitalised is capped at the borrowing costs actually incurred in the period, and capitalising posts a single balanced journal entry that reclassifies the amount from the interest expense account to the qualifying asset account. Dated expenditure lines, commencement and cessation dates, and suspension spans are all optional and only refine the base when you use them.',
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.0.9',
    'depends': ['eh_account_base', 'account', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'views/borrowing_cost_views.xml',
        'data/menus.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif', 'static/description/borrowing_costs_01.png'],
}
