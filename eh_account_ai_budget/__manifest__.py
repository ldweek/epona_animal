# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'AI Budget Commentary',
    'summary': 'Budget variance commentary for period close packs, deterministic template with live LLM augmentation option, headline plus largest movers, zero API keys by default, Odoo 19 Community accounting.',
    'description': 'Wires deterministic budget variance commentary live onto budgets in Odoo 19 Community. Each budget record gains a non-stored computed field that produces plain-English, direction-aware budget-vs-actual analysis without network access. The explicit AI refresh action stores only genuine provider output. A missing, manual, failed, or empty provider raises a clear error and leaves any existing AI narrative unchanged instead of persisting deterministic prose under an AI label.',
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.1.3',
    'depends': ['eh_account_ai_agent', 'eh_account_budget_pro'],
    'data': ['views/budget_views.xml'],
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif'],
}
