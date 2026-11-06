# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Vendor Bill Automation',
    'summary': 'Vendor bill intake inbox with a genuine three-way match against the purchase order line and goods-receipt quantity, per-partner qty, price, amount and over-receipt tolerance profiles, duplicate detection, and manager-gated posting. Odoo three-way match, vendor bill automation Odoo, AP automation Odoo 19 Community, purchase order goods receipt invoice match, vendor bill tolerance profiles, duplicate invoice detection Odoo, accounts payable invoice intake, email to vendor bill Odoo, PO matching tolerance Odoo, vendor bill exception workflow.',
    'description': """A vendor bill intake pipeline that takes routine bills off the manual key-in queue while keeping every posting under human control.

Bills arrive by email or by upload and land in an intake inbox as real, queryable records, each with its own state machine (received, parsed, matched, posted, exception, rejected) and per-transition audit stamps (who, when) plus standard Odoo chatter. A bill that arrives by email creates an intake row from the message body and resolves the sender to a vendor. If more than one partner matches the sender address, the intake is created with no partner rather than guessing, because a wrong vendor on a bill is worse than a blank one.

A pure-Python regex parser extracts the vendor invoice reference and the total from the raw text. The vendor is set manually or resolved from the sender email; due date and line items are entered manually or supplied by an optional extractor adapter. No remote calls are made by the shipped module.

The core is a genuine three-way match per line. Each intake line is matched to its PO line (by partner and product), the received quantity is read from done stock moves, and quantity and price are checked against the tolerance profile in force. Price comparison is multi-currency aware: the PO unit price is converted into the bill currency at the bill date before the tolerance check, so a bill raised in a different currency than the PO does not flag every line as a price exception. A line is only flagged as a price exception when both the per-unit percent tolerance and the absolute line-amount tolerance are breached, so a penny mismatch never blocks posting. Invoiced quantity over the ordered quantity (beyond the over-receipt percent) is flagged distinctly as over-received, not lumped in with a quantity mismatch.

A default tolerance profile ships as install data and is present in every install; per-partner overrides are configured on the vendor (quantity percent, price percent, absolute amount, optional over-receipt percent).

Duplicate detection runs on (vendor, reference, total, date) against both open intakes and already-posted bills. A block can only be overridden by an accounting manager, requires a non-empty reason, and is written to chatter; posting re-checks for duplicates at post time, not only at match time.

Posting is always a manager action. Intakes that match within tolerance are flagged ready to post, and an accounting manager posts them, individually or as a selected batch, in one action. Posting builds a standard vendor bill (account.move, in_invoice) carrying the PO and supplier taxes. Exceptions stay queued for review with the deltas surfaced. A scheduled cron (every 15 minutes by default) promotes received intakes through parse and match, with per-record try/except isolation so one bad intake is logged and skipped without stopping the batch. Posting still requires a manager and is never automatic.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.4.8',
    'depends': [
        'eh_account_base',
        'account',
        'purchase',
        'stock',
        'mail',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'data/tolerance_profile_data.xml',
        'data/cron.xml',
        'views/audit_log_views.xml',
        'views/account_move_views.xml',
        'views/tolerance_profile_views.xml',
        'views/ap_intake_views.xml',
        'views/ap_intake_line_views.xml',
        'views/res_partner_views.xml',
        'views/res_company_views.xml',
        'data/menus.xml',
    ],
    'demo': ['demo/ap_demo.xml'],
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
    'post_init_hook': 'post_init_hook',
}
