# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The investment-property mechanics
# (a policy choice of the fair value model or the cost model, under the
# fair value model a remeasurement to fair value with the change recognised
# in profit or loss, and transfers into or out of investment property at the
# fair value at the date of change in use with the IAS 40.59/.61-62
# carrying-over and P&L/OCI routing rules) are IAS 40 as published; no code
# or comments derive from any proprietary or third-party module.
#
##############################################################################
{
    'name': 'Investment Property',
    'summary': 'IAS 40 investment property for Odoo 19 Community with the fair value model and the cost model, transfers at fair value on the transfer date with an immutable transfer audit trail, each measurement change posted straight to the ledger. Search: odoo 19 investment property, IAS 40, fair value model, cost model, revaluation to profit or loss, straight line depreciation, transfer of investment property, change in use, deemed cost, revaluation surplus, disposal derecognition, rental property, capital appreciation.',
    'description': 'This module records property held to earn rentals or for capital appreciation under IAS 40. On recognition the property is measured at cost, and the entity chooses either the fair value model or the cost model as its policy. Under the fair value model each remeasurement to fair value posts a balanced entry and recognises the change in profit or loss, then rolls the carrying amount forward to the new fair value. Transfers follow the standard faithfully: a fair value model property leaving investment property is remeasured to the fair value at the date of change in use, with the gap recognised in profit or loss before derecognition and that fair value carried out as the deemed cost of the destination; a cost model property transfers at its unchanged carrying amount per IAS 40.59, with the fair value at the transfer date stored for disclosure; and an owner occupied fixed asset from the ERP Heritage assets module can be transferred in at fair value, with an uplift credited to the equity revaluation surplus and a deficit charged to profit or loss after first consuming any surplus on that asset, per IAS 40.61 and 62. Every transfer writes an immutable audit trail row recording the basis, the carrying amount before, the fair value at the transfer date, the delta posted with its routing, and the journal entries. Under the cost model the module posts straight line depreciation charges, halts depreciation cleanly when the basis switches to fair value, and it also handles full derecognition on disposal, with the gain or loss falling out as the balancing figure. Every posting is gated to the EH Accounting Manager group, stamped for audit, and refuses to run when an account, journal, state, or change is missing rather than failing silently.',
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.1.8',
    'depends': [
        'eh_account_base', 'eh_account_assets_pro', 'account', 'mail',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'views/investment_property_views.xml',
        'data/menus.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif', 'static/description/investment_property_01.png'],
}
