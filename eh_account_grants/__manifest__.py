# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The government-grant mechanics (grants
# recognised over the periods that match the related costs, an asset grant
# presented as deferred income or as a deduction from the asset, a
# non-monetary grant recognised at the fair value of the asset received,
# recognition only on reasonable assurance of compliance with conditions, and
# a repayment adjusted prospectively) are IAS 20 as published; no code or
# comments derive from any proprietary or third-party module.
#
##############################################################################
{
    'name': 'IAS 20 Government Grants',
    'summary': 'IAS 20 grant recognition, conditions, clawbacks and repayment',
    'description': "IAS 20 Government Grants records monetary and non-monetary awards with their grantor, agreement reference and source attachments. Deferred-income grants are released manually on explicit earning-period dates; the module does not generate an amortisation schedule. A deduction-from-asset grant credits the selected asset account in full on receipt, but this module does not create a fixed-asset record or recalculate depreciation. Non-monetary awards use the received asset's fair value, and repayment derecognises that received asset instead of assuming a cash settlement. Condition breaches accrue on an operator-selected event date, first reversing unamortised deferred income and charging any excess to profit or loss. Accrued clawbacks may then be settled in full or in controlled partial amounts on explicit settlement dates. Posting remains manager-only, sealed and linked to the grant and grantor; measurement, agreement and condition evidence freezes once recognition begins.",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.1.6',
    'depends': ['eh_account_base', 'account', 'mail'],
    'data': [
        'security/grant_security.xml',
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'views/grant_views.xml',
        'data/menus.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif', 'static/description/grants_01_grant.png'],
}
