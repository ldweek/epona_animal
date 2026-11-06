# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'AI Collections Advisor',
    'summary': 'Evidence-aware deterministic collections next actions plus explicitly generated provider narratives for Odoo 19 Community. Promise, payment-plan, dispute, hold, demand-letter age, and write-off controls remain authoritative; deterministic fallback is never stored under an AI label.',
    'description': "A bridge module that wires the deterministic collections next-action engine live onto eh.collections.case records. Every case gains stored, filterable action and priority keys plus a live translated rationale. Declared dependencies keep the stored keys current, while reading the rationale computes only viewer-local text and never writes those keys. The deterministic dunning ladder respects promises, plans, disputes, holds, documented demand-letter age, and write-off review evidence. It runs with zero network access by default. The explicit AI action stores only genuine provider output; a missing, manual, failed, malformed, or control-suppressed provider leaves any existing AI narrative unchanged instead of relabelling deterministic prose as AI.",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.2.2',
    'depends': ['eh_account_ai_agent', 'eh_account_collections'],
    'data': [
        'data/demand_letter_defaults.xml',
        'views/collections_case_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif'],
}
