# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The defined-benefit mechanics (net
# interest on the opening net position, service cost and settlement results
# to profit or loss, remeasurements to other comprehensive income without
# recycling, the asset ceiling test, and the obligation and plan-asset
# rollforward disclosures) are IAS 19 as published; no code or comments
# derive from any proprietary or third-party module.
#
##############################################################################
{
    'name': 'IAS 19 Benefits',
    'summary': 'Defined benefit valuations, disclosures, and DC accrual controls',
    'description': 'An IAS 19 accounting layer for defined benefit valuations and defined contribution accruals. Actuarial results are imported inputs; the module has no actuarial engine. It computes the ledger mechanics for net interest, service cost, settlements, non-recycling OCI remeasurements, the asset ceiling, tied obligation and plan-asset rollforwards, and one sealed entry per valuation. A rendered IAS 19.140-141 disclosure schedule presents the recognised liability or asset net per plan. Short-term benefits, other long-term benefits, termination benefits, and reimbursement rights are explicitly outside this module\'s scope.',
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.0.7',
    'depends': ['eh_account_base', 'account', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'report/benefit_disclosure_report.xml',
        'views/benefit_views.xml',
        'data/menus.xml',
    ],
    'assets': {
        'web.assets_tests': [
            'eh_account_employee_benefits/static/tests/tours/benefit_test_tour.js',
        ],
    },
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
