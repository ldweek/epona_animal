# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The fair-value hierarchy (Level 1
# quoted prices, Level 2 observable inputs, Level 3 unobservable inputs) and
# the remeasurement to fair value through profit or loss or OCI are IFRS 13
# and the relevant measurement standards as published; no code or comments
# derive from any proprietary or third-party module.
#
##############################################################################
{
    'name': 'IFRS 13 Fair Value',
    'summary': 'IFRS 13 hierarchy, IFRS 9 classification, multicurrency remeasurement, controlled correction, derecognition and Level 3 disclosures.',
    'description': 'This module records and posts fair value under IFRS 13 with IFRS 9 classification. Each item (eh.fair.value.item) is classified in the three-level hierarchy by input observability, its valuation technique is captured as market, income or cost approach, and for Level 3 the significant unobservable inputs are documented. An IFRS 9.4.1 engine derives the classification from a structured SPPI questionnaire (fixed dates, solely principal and interest, no leverage, no contingent returns) and the business model (hold to collect, hold to collect and sell, other): amortised cost, FVOCI debt, FVOCI equity election or FVTPL, with the P&L/OCI routing derived from the classification rather than chosen by hand, and nonsense combinations blocked (a liability cannot take the equity election, a derivative is always FVTPL, the election is irrevocable once posted). Remeasuring posts the change from the current carrying amount to fair value in one balanced entry and rolls the carrying amount forward. Derecognising posts the disposal entry and, atomically, settles the accumulated OCI reserve: FVOCI debt recycles it to profit or loss, an FVOCI equity election transfers it within equity to retained earnings and never through profit or loss. The Level 3 roll-forward reconciliation (IFRS 13.93(e)) can be ledger-fed from the posted entries and cannot be closed unless its closing balance ties to the fair value, and a per-item sensitivity table (input shock times elasticity) produces the IFRS 13.93(h) quantitative disclosure.',
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.1.8',
    'depends': ['eh_account_base', 'account', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'views/fair_value_item_views.xml',
        'data/menus.xml',
    ],
    'assets': {
        'web.assets_tests': [
            'eh_account_fair_value/static/tests/tours/fair_value_test_tour.js',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif', 'static/description/fair_value_01.png'],
}
