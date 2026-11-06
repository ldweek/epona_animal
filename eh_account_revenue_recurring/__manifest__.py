# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Revenue Recurring Bridge',
    'summary': 'Link a recurring invoice template to an IFRS 15 revenue contract: generated invoices credit the contract liability (deferred revenue) instead of direct income, and the contract recognition run releases it as performance obligations are satisfied. Auto installs when both Revenue Recognition (IFRS 15) and Recurring Invoices Pro are present. Search: odoo recurring invoice deferred revenue, subscription revenue recognition IFRS 15, contract liability billing, recurring billing revenue contract bridge.',
    'description': """A thin bridge between Recurring Invoices Pro and Revenue Recognition (IFRS 15).

When both modules are installed this auto installs and adds an optional revenue contract link on the recurring invoice template. A linked template routes every generated invoice line to the contract's contract liability account instead of the line's income account, so recurring billing lands as deferred revenue on the balance sheet rather than immediate income.

On posting, the generated invoice registers itself as billing on the contract: the billed amount joins the contract's billed-versus-recognised position, and if the contract carries a contract asset (revenue recognised ahead of billing) a balanced reclassification entry clears the asset first, exactly mirroring the contract's own Record Billing convention. The recognition run then releases the contract liability to revenue as performance obligations are satisfied, per the five-step model.

Unlinked templates are untouched and keep posting to income exactly as before.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.0.5',
    'depends': ['eh_account_revenue', 'eh_account_recurring_invoices'],
    'data': [
        'views/template_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': True,
    'images': ['static/description/banner.gif'],
}
