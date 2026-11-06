# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The five-step model (identify the
# contract, the performance obligations, the transaction price, allocate the
# price by standalone selling price, recognise as obligations are satisfied)
# is IFRS 15 as published; no code or comments derive from any proprietary
# or third-party module.
#
##############################################################################
{
    'name': 'IFRS 15 Revenue',
    'summary': 'IFRS 15 five-step revenue recognition for Odoo 19 Community that captures a contract with its performance obligations, allocates the transaction price by standalone selling price, and recognises revenue at a point in time or over time by a documented output or input measurement method, posting balanced journals and tracking the contract asset and contract liability. Search: odoo 19 revenue recognition, IFRS 15, performance obligation, transaction price allocation, standalone selling price, contract asset contract liability, deferred revenue, over time point in time, percentage of completion, cost to cost input method, output method milestones units delivered, variable consideration constraint reassessment review, significant financing component, contract modification, contract closure validation.',
    'description': 'This module implements the IFRS 15 five-step model on real journal entries. A revenue contract holds one or more performance obligations, each with a standalone selling price, and the transaction price is allocated across them in proportion to those prices (IFRS 15.74). Each obligation is satisfied at a point in time or over time under a documented measurement method (IFRS 15.35-45, B14-B19): output milestones or units delivered, or input costs incurred or time elapsed, with the cost and unit drivers computing the percentage complete automatically and a mandatory basis note recording why the method depicts the transfer of control. Recognising posts the incremental amount since the last run, crediting revenue and clearing first any contract liability then the contract asset (IFRS 15.105-107). Billing debits the receivable and credits first the contract asset then the contract liability, so the balance sheet always shows the correct net contract position. It also handles variable consideration with the constraint and its period-end reassessment workflow with a frozen audit trail (IFRS 15.50-59, 15.56), specifically allocated discounts (IFRS 15.81-83), a significant financing component with advance and arrears interest that is re-measured on catch-up modifications (IFRS 15.60-65), separate, prospective and cumulative catch-up contract modifications (IFRS 15.18-21), and contract closure validation that blocks closing while obligations remain unsatisfied unless a manager releases the remainder to profit or loss or a refund liability with a recorded reason. Every posting is balanced by construction and gated to the EH Accounting Manager group.',
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.2.12',
    'depends': ['eh_account_base', 'account', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'wizard/revenue_modification_wizard_views.xml',
        'views/revenue_constraint_review_views.xml',
        'views/revenue_contract_views.xml',
        'data/menus.xml',
    ],
    'assets': {
        'web.assets_tests': [
            'eh_account_revenue/static/tests/tours/revenue_test_tour.js',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif', 'static/description/revenue_01_contract.png'],
}
