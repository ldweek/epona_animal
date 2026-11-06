# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'FX Period End Revaluation',
    'summary': 'IAS 21 period end revaluation of open monetary foreign currency balances for Odoo 19 Community, posting the unrealised gain or loss to one balanced, audited journal entry with optional auto reversal on a finance-controlled open date. Includes automatic currency exchange rate updates from many sources with a daily cron and automatic cross derivation into the company currency, per (account, partner, currency) adjustment lines, signed asset and liability gain or loss classification, manager only post and reverse, and an IFRS 9 hedge accounting engine. Rate sources cover central bank feeds (European Central Bank, Bank of Canada, National Bank of Poland, Bank of Russia, Central Bank of Turkey, Reserve Bank of Australia, Central Bank of Brazil, Central Bank of Kuwait, Central Bank of Bahrain, HMRC), broad coverage aggregators, optional keyed services, and an offline Gulf decree peg table. Search terms: Odoo 19 FX revaluation, IAS 21 foreign currency revaluation, automatic currency exchange rate update, live exchange rate feed Odoo accounting, unrealised gain loss period end, multi currency month end revaluation, foreign currency receivable payable retranslation, central bank rate feed, auto reverse FX journal entry, IFRS 9 hedge accounting, hedge effectiveness dollar offset regression, CTA reserve registry, IAS 21.48 disposal reclassification, net investment hedge CTA linkage, realized versus unrealized FX split.',
    'description': """Translate open monetary foreign currency balances at the period end closing rate and post the unrealised gain or loss to P and L, per IAS 21 for the common case where the company keeps its functional currency in the ledger and transacts partly in foreign currencies.

The compute step scans every open foreign currency journal item on flagged accounts, aggregates by account, partner and currency, fetches the closing rate once per currency, and produces a revaluation line per group. The post step derives the gain and loss legs from those same per line adjustments, so the move balances by construction, with the balancing leg booked to the configured FX gain or FX loss account. Odoo's standard journal entry validation rejects any imbalance on post, and a run with no adjustments raises explicitly instead of posting an empty entry.

Each line stores the closing rate snapshot, the old and new balance, and the adjustment. Every run stamps user and timestamp on each transition (draft, computed, posted, reversed, cancelled) with full chatter tracking. Audit fields are readonly in the UI. Post and reverse are guarded to managers, and a posted run cannot be re posted.

Automatic currency exchange rate updates run on a daily cron, or on demand with the Update now button, through a pluggable provider registry. Central bank feeds need no API key and publish against their own base currency, which the module cross derives into the company currency automatically: European Central Bank with daily and 90 day historical XML, Bank of Canada, National Bank of Poland, Czech National Bank, Bank of Russia, National Bank of Romania, Bulgarian National Bank, Sveriges Riksbank, National Bank of Kazakhstan, Central Bank of Uzbekistan, Central Bank of Turkey, Reserve Bank of Australia, Central Bank of Brazil, Central Bank of Kuwait, Central Bank of Bahrain, Bank Negara Malaysia, Bank of the Republic of Colombia, Central Reserve Bank of Peru, Central Bank of Uruguay, and HMRC monthly. Broad coverage aggregators reach roughly 160 currencies, optional keyed services (including Bank of Mexico) are supported through a per company API key field, and an offline Gulf decree peg table serves the hard pegged dollar rates without any network call. The configuration auto defaults the provider from the company country.

A per company fallback provider fills any currency the primary source does not return, or covers the whole run when the primary is briefly unreachable, so a national feed that omits an exotic currency still resolves. Per currency manual overrides let finance pin a rate that has no clean feed while every other currency keeps auto updating, finer grained than an all or nothing manual mode. Each provider is selected per company; a single provider outage is caught per company so it never blocks the others. A missing closing rate raises explicitly, naming the currency, rather than falling back silently.

The module also ships a functional IFRS 9 hedge accounting engine (cash flow, fair value, net investment) with dollar offset and regression effectiveness testing, OCI versus P and L split posting, and OCI to P and L reclassification. The hedge workspace is available from FX Revaluation > Hedge Accounting; fair value changes are supplied manually or pushed from a treasury system into movement records (there is no automatic instrument valuation).

A CTA reserve registry tracks the cumulative translation reserve of each foreign operation on the parent's books. Net investment hedges park their effective portion in the position's equity account with the journal entry tagged to the position, so the reserve balance is fed straight from the ledger. On disposal of the foreign operation the full accumulated balance, including hedge amounts parked there, is reclassified from equity to profit or loss per IAS 21.48, with profit-or-loss recycling for associates/JVs and equity reattribution to NCI for subsidiary partial disposals that retain control under IAS 21.48A to 48C. Each revaluation run also splits its revaluation-date adjustment by subsequently settled versus still-open source items, shown on the run form, list and PDF report.

Pairs with the period close workflow for close validation and with the dynamic reports for revaluation history.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.7.0',
    'depends': ['eh_account_base', 'account', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'data/cron.xml',
        'views/account_account_views.xml',
        'views/fx_rate_config_views.xml',
        'views/fx_revaluation_run_views.xml',
        'views/fx_revaluation_line_views.xml',
        'views/fx_cta_position_views.xml',
        'views/fx_hedge_views.xml',
        'report/fx_revaluation_report.xml',
        'data/menus.xml',
    ],
    'assets': {
        'web.assets_tests': [
            'eh_account_fx_revaluation/static/tests/tours/fx_revaluation_test_tour.js',
        ],
    },
    'images': ['static/description/banner.gif'],
    'installable': True,
    'post_init_hook': 'post_init_hook',
    'application': False,
    'auto_install': False,
}
