# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The goodwill formula (consideration
# transferred plus non-controlling interest plus any previously-held interest
# less the fair value of identifiable net assets acquired) is IFRS 3, and the
# equity-method carrying roll-forward (cost plus share of profit less
# dividends and impairment) is IAS 28, both as published; no code or comments
# derive from any proprietary or third-party module.
#
##############################################################################
{
    'name': 'IFRS 3 & IAS 28',
    'summary': 'IFRS 3 goodwill and IAS 28 equity method for Odoo 19 Community, with a full purchase price allocation, deferred tax on the fair-value step-up, step-acquisition remeasurement, measurement-period adjustments, and contingent consideration. Search terms: odoo 19 goodwill, IFRS 3 business combination, purchase price allocation, bargain purchase gain, non-controlling interest at fair value or proportionate share, previously-held interest step acquisition remeasurement, measurement period adjustment, contingent consideration fair value, IAS 28 equity method, investment in associate, joint venture, share of profit pickup, IAS 12 deferred tax on acquisition.',
    'description': "This module adds two IFRS mechanics a plain ledger does not carry. On the IFRS 3 side it computes goodwill as consideration transferred (including the acquisition-date fair value of contingent consideration) plus non-controlling interest plus any previously-held interest less the fair value of identifiable net assets acquired, recognises a bargain purchase gain when that arithmetic is negative, and posts a full purchase price allocation that keeps consideration, non-controlling interest and previously-held interest distinct. The simple goodwill path is restricted to acquisitions without NCI or a previously-held interest. Step acquisitions remeasure the previously-held interest to fair value with the gain or loss posted to profit or loss. Measurement-period adjustments track acquisition-date retrospective effect, information-known date and exact accounting date separately; they can post auditable IFRS 3.49(b) depreciation/amortisation catch-up to current profit or loss or an approved comparative equity account. Liability-classified contingent consideration is remeasured to fair value through profit or loss in chronological order, while equity-classified contingent consideration is never remeasured. On the IAS 28 side an investment can be posted at cost or adopted from an exact posted opening entry without duplicating cash. It rolls forward for profit or loss, dividends and impairment, supports proportional disposal while the equity method continues, and on cessation recognises retained-interest fair value plus signed OCI/CTA recycling. Posting actions are manager-gated, entries are sealed, and disposal memorandum fields preserve carrying amount, proceeds, retained fair value, OCI and closed unrecognised losses.",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.2.0',
    'depends': ['eh_account_base', 'account', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'views/business_combination_views.xml',
        'views/equity_investment_views.xml',
        'data/menus.xml',
    ],
    'assets': {
        'web.assets_tests': [
            'eh_account_business_combination/static/tests/tours/business_combination_test_tour.js',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif', 'static/description/business_combination_01.png'],
}
