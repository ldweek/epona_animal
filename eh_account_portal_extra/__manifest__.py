# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The customer portal extension
# uses Odoo's public portal infrastructure (portal mixin, /my routes,
# QWeb templates) to surface the suite's existing customer statement
# generator. No code derives from any proprietary or third-party Odoo
# module beyond the public framework conventions.
#
##############################################################################
{
    'name': 'Customer Portal Extra',
    'summary': 'Customer-selected statement PDFs and a company-scoped overdue-invoice portal card.',
    'description': """Customer Portal Extra turns the standard Odoo 19 Community customer portal into a self-service accounts receivable corner. It adds two things and does them carefully.

First, an inline statement form. The logged-in customer chooses start/end dates and either an open-item or activity statement, then receives an ephemeral PDF rendered by the same audited handler as the back office. The controller pins the partner and active company server-side, applies the configured range limit, and never persists the reusable report payload. Because the handler rewinds partial reconciliations created after the chosen end date, a back-dated statement reflects what was actually outstanding then, not today's residual.

Second, a live AR balance card on the portal home. It shows open customer-invoice count, the positive overdue invoice residual, the oldest overdue invoice due date, and a days-overdue counter in active-company currency. Credits, unapplied payments, refunds, other partners and sister-company ledgers cannot reduce or contaminate the amount.

Access is strictly partner scoped. The authenticated statement route resolves the logged-in user's commercial partner and active company and accepts no override. Invalid dates, an excessive configured range, concurrent generation, missing report registration and render failures return private branded pages; render exceptions are logged with their traceback.

The installed portal pages add no analytics tag, tracking pixel, external CDN or custom JavaScript. A documented Accounting setting controls the maximum inclusive statement period (1 to 3660 days). No new report definition is introduced; the module reuses the customer statement generator from eh_account_dynamic_reports.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.0.8',
    'depends': [
        'eh_account_base',
        'eh_account_dynamic_reports',
        'portal',
        'account',
    ],
    'data': [
        'views/res_config_settings_views.xml',
        'views/portal_templates.xml',
    ],
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
