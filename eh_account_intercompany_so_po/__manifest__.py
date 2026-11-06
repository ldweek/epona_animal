# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Intercompany SO/PO',
    'summary': 'Draft sealed inter-company SO/PO counterparts with target-company products, taxes, dates and references, plus controlled fulfilment and invoicing.',
    'description': """Extends the ERP Heritage inter-company engine from invoice and move mirroring to sales and purchase orders, completing the multi-company order cycle.

When a sales order is confirmed to a customer that represents another company (per inter-company configuration), a draft purchase order is created in that company with the source company as the vendor. Confirming a purchase order to such a supplier creates the matching draft sales order. Dates, references, shipping destination, no-variant attributes and unrounded trade value are carried across; destination products and taxes are resolved under destination-company rules while source tax treatment remains visible as audit evidence. Mirrors land in draft for inspection and acceptance, but the paired commercial lines are sealed against unilateral edits. Corrections require cancelling and replacing the source transaction. Down payments, expense re-invoicing and verified receipt adjustments use narrow server-owned corridors and remain outside the original sealed trade scope.

Mirrors are never mirrored back and are created with the configured inter-company service user. When no user is configured, elevated creation remains available only to actors whose allowed-company scope already includes the target; source-only routes fail closed.

Reuses the inter-company configuration and destination-company resolution from eh_account_intercompany (Represented Company field on partner), so a single setup drives both invoice and order mirroring.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.1.4',
    # Alternate SO delivery addresses become real PO dropship destinations;
    # depend on the core route so purchase_stock cannot silently clear them.
    'depends': [
        'eh_account_intercompany', 'sale', 'purchase',
        'stock_dropshipping',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/intercompany_order_views.xml',
        'wizards/intercompany_order_wizard_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif'],
}
