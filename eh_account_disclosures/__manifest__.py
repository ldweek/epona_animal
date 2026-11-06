# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The disclosure structures (related
# party relationships and transactions, operating segments reconciled to
# entity totals, financial-instrument risk exposures, and interests in other
# entities) are IAS 24, IFRS 8, IFRS 7 and IFRS 12 as published; no code or
# comments derive from any proprietary or third-party module.
#
##############################################################################
{
    'name': 'IFRS Disclosure Notes',
    'summary': 'Build the notes to your financial statements as governed registers generated from the ledger, not spreadsheets. Search: odoo 19 related party disclosure, IAS 24 related party transactions, KMP compensation, IFRS 8 operating segments reconciliation, IFRS 8 major customer, IFRS 7 financial risk disclosure, IFRS 7.39 maturity analysis, IFRS 7.40 sensitivity analysis, expected credit loss staging table, provision matrix, IFRS 12 interests in other entities, non-controlling interest, disclosure register, notes to financial statements.',
    'description': "Financial Statement Disclosures maintains seven disclosure registers that a plain ledger does not produce, and generates their numbers instead of asking for them. A related-party register (IAS 24) records relationships, transactions and outstanding balances, ties the outstanding balance back to the linked contact's posted receivable and payable lines, and carries the IAS 24.17 key-management compensation categories with the share-based figure prefilled from the IFRS 2 engine when installed. An operating-segment report (IFRS 8) captures segment revenue, result, assets and liabilities, reconciles the segment totals to entity totals, flags each segment against the IFRS 8.13 ten-percent thresholds, derives segment revenue and result from analytic-tagged postings, and computes the IFRS 8.34 major-customer test from posted revenue by customer. A financial-instrument risk register (IFRS 7) classifies exposures by credit, liquidity and market risk and ties carrying amounts to named ledger accounts. A credit-risk note (IFRS 7.35A-N) auto-feeds its staging table, loss-allowance reconciliation and provision-matrix summary from posted expected-credit-loss runs when the ECL engine is installed, flagging any manual override that disagrees. A contractual-maturity run (IFRS 7.39) buckets undiscounted cash flows from selected liability accounts, per-instrument coupon schedules, open receivables and payables, and IFRS 16 lease schedules, under configurable band schemes. A sensitivity analysis (IFRS 7.40) computes the currency shock on net open monetary positions per currency and the interest-rate shock on floating-rate instruments, with the assumptions disclosed alongside. An interests-in-other-entities register (IFRS 12) records subsidiaries, associates, joint ventures and structured entities with ownership, non-controlling interest, significant restrictions and the significant-judgements narrative. Every register can be finalised and locked by an accounting manager, and posts no journal entries.",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.2.3',
    'depends': ['eh_account_base', 'account', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'views/disclosure_views.xml',
        'data/menus.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif', 'static/description/disclosures_01_fin_risk.png'],
}
