# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The statement structures (a statement
# of changes in equity reconciling opening to closing equity by component, and
# a statement of comprehensive income adding OCI to profit for total
# comprehensive income) are IAS 1 as published; no code or comments derive
# from any proprietary or third-party module.
#
##############################################################################
{
    'name': 'IAS 1 Primary Statements',
    'summary': 'Prepare the two IAS 1 primary statements a plain ledger does not give you, a statement of changes in equity and a statement of comprehensive income, each tied back to the posted general ledger. Search: odoo 19 statement of changes in equity, IAS 1 primary statements, statement of comprehensive income, other comprehensive income OCI, total comprehensive income, equity reconciliation, SOCE SOCI, owners and non-controlling interests attribution, OCI recycling reclassification IAS 1.82A, interim financial reporting IAS 34 condensed statements.',
    'description': (
        'This module adds the IAS 1 statement of changes in equity and '
        'statement of comprehensive income, with one authoritative statement '
        'per company and period. SoCI includes the IAS 1.82 profit-or-loss '
        'line items, related tax for every OCI component, associates and joint '
        'ventures OCI, reclassification sections, owners/NCI attribution and '
        'annual or IAS 34 interim comparatives. SoCE reconciles every equity '
        'component through retrospective restatement, item-level OCI, profit, '
        'dividends and share movements, including the IAS 1.107 dividend-per-'
        'share disclosure. Structural OCI tags are sourced only from explicit '
        'suite account settings; translated account names are never used as '
        'configuration. Signed statements freeze, preserve consolidation '
        'provenance, and tie to the posted ledger. The cross-statement control '
        'independently compares P&L profit with balance-sheet-implied current-'
        'year earnings, SoCI and SoCE for the same period.'
    ),
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.3.0',
    'depends': ['eh_account_base', 'account', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'data/oci_tags.xml',
        'views/statement_views.xml',
        'views/tieout_views.xml',
        'data/menus.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif', 'static/description/statements_01_soci.png'],
}
