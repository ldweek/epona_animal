# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'AU BAS Reporting',
    'summary': 'Quarterly Business Activity Statement for Odoo 19 Community: complete 33-row ATO worksheet, accrual and cash-basis G1/1A/1B calculation, Simpler BAS output, computed W5, immutable lodged evidence, GST-control reconciliation, and synchronized mod-89 ABN/VAT identity. Search terms: Odoo 19 Australian BAS, Business Activity Statement Odoo Community, Odoo GST BAS report Australia, ABN validator Odoo, GST control reconciliation, quarterly BAS run, Simpler BAS, cash basis BAS, PAYG withholding W1 W2 W5, Australian tax localization Odoo Community.',
    'description': """The Business Activity Statement is the quarterly tax report Australian businesses lodge with the ATO. This module produces the figures the operator transfers onto the form and keeps a frozen, auditable record of each lodgement, on Odoo 19 Community.

It ships the canonical 33-row ATO worksheet (G, W, T, F, FTC, WET and LCT labels) as seeded records: G1 total sales, G2 export sales, G3 other GST-free sales, G10 and G11 acquisitions, 1A GST on sales, 1B GST on acquisitions, W1 and W2 PAYG withholding, and the income-tax instalment labels. One BAS run exists per company and quarter.

G1, 1A and 1B use Odoo's authoritative Australian tax-grid tags. G1 additionally includes posted customer revenue with no tax relation; no GST is invented for those sales. Accrual runs recognise invoice dates, while cash runs use bounded set-based reconciliation SQL and settlement-date proportions. Custom tag modes split base from tax journal items and sum signed balances, so refunds reduce figures. W5 is computed from reviewed W2, W3 and W4 manual inputs.

When a run is marked lodged it becomes read-only. Recompute and reset-to-draft are refused; actor, timestamp, tax lock, label definition, amount, lodgeability and company-currency snapshots remain immutable. Compute rebuilds inside a savepoint. A database failure restores the previous worksheet and requests independent durable failure evidence through the shared report-execution audit trail.

GST control reconciliation classifies sale, purchase and unclassified control-account movement before lodgement. A dedicated dynamic-report handler renders the latest exact company/quarter snapshot. Simpler BAS keeps all rows for internal review but exports only G1, 1A, 1B and PAYG-W. Computed rows remain read-only; a separate action exposes only reviewed manual inputs.

Partner ABN is a synchronized alias of Odoo's standard VAT field. A valid ABN written to either field is normalized to the canonical 11-digit value and copied to both; conflicting identities fail closed. The GST-registered flag is classification only and does not pretend to calculate W4 withholding.

Out of scope: SBR lodgement transport (the module produces the figures, not the wire transmission to the ATO), and activity-statement variants beyond the quarterly BAS such as PAYG monthly forms and the IAS for non-GST registrants.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Localizations',
    'version': '19.0.1.2.1',
    'depends': [
        'eh_account_base', 'eh_account_dynamic_reports', 'account', 'l10n_au',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/bas_labels.xml',
        'data/dynamic_report.xml',
        'views/bas_run_views.xml',
        'views/gst_recon_views.xml',
        'views/res_partner_views.xml',
        'data/menus.xml',
    ],
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
