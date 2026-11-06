# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Group Consolidation',
    'summary': 'Multi-entity group consolidation for Odoo 19 Community that rolls the trial balance of a parent plus its subsidiaries into one consolidated reporting set. IAS 21 currency translation with a genuine time-weighted average rate versus closing rate, automatic non-controlling-interest carve-out below 100% ownership, balanced intercompany elimination journals, and a source-tagged consolidated run you can trace line by line. Search terms: Odoo 19 consolidation, Odoo Community group consolidation, multi-entity consolidation, IAS 21 currency translation, intercompany elimination, non-controlling interest NCI, consolidated trial balance, CTA translation reserve, multi-company financial consolidation, full equity and proportional method.',
    'description': """Define a consolidation entity once: presentation currency, root company, and the list of member companies with ownership percentage and method (full, equity, or proportional). Each period, run a consolidation. The engine pulls every member's posted balances, translates them to the presentation currency, eliminates intercompany positions, computes non-controlling interest where ownership is below 100 percent, and writes the IAS 21 currency-translation-adjustment difference so the consolidated set balances.

The translation is the real thing. Balance-sheet accounts convert at the closing rate at period end, while income and expense accounts convert at a time-weighted average rate that weights each spot rate by the number of days it was in effect across the period. The gap between the two rates is exactly what the CTA captures. A flat rate produces zero CTA, as it should, and a genuine FX movement produces a genuine adjustment.

Every consolidated number is traceable. Each run line is tagged by source (parent, subsidiary, elimination, CTA, or NCI), and the account-level consolidated trial balance drills back to those lines. The CTA difference is recorded against the configured equity translation-reserve account. An entity can remain a review-only reporting set, or post the reviewed snapshot to a dedicated consolidation-ledger company and preserve its reversal/delta history. Closing non-controlling-interest equity remains a stock KPI; a separate manager-confirmed post-consolidation schedule splits exact-period NCI profit and OCI, and Review snapshots those flows as server-owned statement-source output.

Intercompany elimination journals are balanced on post: debit must equal credit, or the post is refused. Only posted member moves and only posted eliminations feed a run, so draft and unposted entries never leak into the consolidated set.

The run has a draft to computed to reviewed to closed lifecycle. Recompute is idempotent: re-running Compute deletes reproducible projections and rebuilds them while durable disposal evidence survives and is replayed. Settled inputs and generated lines are locked at model level, every state change is captured in the chatter audit trail, and reset/reopen corrections require Accounting Manager privileges.

Method depth. Proportional members roll up at ownership share, applied after translation so the IAS 21 arithmetic is undistorted, and NCI configuration is blocked for them by constraint. Member account maps standardise subsidiary charts during extraction. Equity-method members are mandatory-config: current profit, cumulative carrying adjustments, dividends, OCI and documented foreign-associate FX are rolled forward, while the IAS 28.1A fair value option remains available. The IFRS 3 investment elimination runs automatically from reconciled account-by-account acquisition components, with acquisition NCI on a proportionate or fair-value basis, positive goodwill on an asset and any IFRS 3.34 bargain-purchase gain on explicit income. The CTA plug is split between parent and NCI per member. On disposal, immutable events preserve and replay the IAS 21.48/48C treatment. IFRS 10.B87/B92-93 guards block a compute when a member reports more than three months off the group date or is not policy-aligned, with an audited manager override.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.3.0',
    'depends': [
        'eh_account_base',
        'eh_account_fx_revaluation',
        'account',
        'mail',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/consol_security.xml',
        'data/sequences.xml',
        'views/consol_entity_views.xml',
        'views/consol_run_views.xml',
        'views/consol_elimination_views.xml',
        'data/menus.xml',
    ],
    'assets': {
        'web.assets_tests': [
            'eh_account_consolidation/static/tests/tours/consolidation_test_tour.js',
        ],
    },
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
