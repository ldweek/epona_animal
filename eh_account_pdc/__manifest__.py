# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Post Dated Cheques (PDC)',
    'summary': 'First-class post dated cheque management for Odoo 19 Community. One cheque register handles issued and received cheques through a guarded lifecycle, with cheque books, concurrency-safe serial allocation, direction-specific cheque-transit ledgers, Odoo outstanding-account settlement, bounce and replacement, and daily auto-presentation. Search terms: post dated cheque management odoo, PDC odoo 19 community, cheque register odoo, issued and received cheque tracking, cheque bounce workflow, cheque in transit account odoo, cheque lifecycle state machine odoo.',
    'description': """Post dated cheques are part of normal trade in many markets, and Odoo Community has no native handling. This module makes the cheque a real record, not a memo note on a payment. Issued cheques (vendor pay) and received cheques (customer collect) live on one model with a direction flag and a guarded lifecycle: draft, registered, presented, cleared, bounced, replaced, cancelled. Each transition stamps the user and timestamp and is blocked unless the state machine allows it.

Cheque books are tracked per bank journal with start and end serials, a next-serial pointer and a remaining count. Registration of an issued cheque takes a row lock on the book so two concurrent registrations queue instead of racing the pointer, rejects any serial that is not the next one in line, and flips the book to exhausted automatically when the last serial is consumed. A unique constraint on book and cheque number is the safety net.

Present, clear and bounce each post real balanced journal entries. Presentation uses dedicated reconcilable incoming-asset and outgoing-liability cheque-transit accounts, never bank-statement suspense. Clearance transfers transit to the bank journal payment-method line's outstanding receipts or payments account, never straight to bank GL; bank-statement reconciliation recognises cash movement. Foreign-currency clearance fully reconciles both transit legs so Odoo posts clear-date exchange gain or loss and no residual stays stranded. Account resolution hard-fails when any transit, outstanding, receivable or payable route is missing or aliases bank GL or statement suspense.

The bounce wizard records a configurable reason (insufficient funds, signature, stop payment, account closed, post dated, technical, amount mismatch, other), the bank dishonour date and a bank-charge amount. The bounce reversal is dated at the dishonour date, not the day the operator recorded it, validated against presentation and accounting lock dates; a locked dishonour period requires explicit current-date posting while original dishonour date stays disclosed. Nonzero charges debit bounce-charge expense and credit outstanding payments at that date; bank-statement reconciliation later clears outstanding payments to bank GL. The reversal fires only for its own proved posted presentation entry, preventing double or foreign reversal. Replacement chains new cheque evidence. Daily auto-presentation isolates every cheque in its own savepoint.

IFRS 9 recognition mapping. Registered: no entry; underlying receivable or payable stays recognised. Presented: no cash recognition; exposure moves into direction-specific cheque transit because bank statement has not confirmed cash. Cleared: transit transfers to outstanding receipts/payments pending bank reconciliation. Bounced: presentation reverses at dishonour date and charges are expensed against outstanding payments.

Foreign-currency cheques are monetary items: present and clear entries carry signed foreign amounts, and clearance fully reconciles transit so Odoo recognises exact clear-date exchange difference. Open incoming-cheque transit remains available to period-end FX revaluation and to eh_ecl_exposure_lines(), whose loss-allowance population restores presented exposures after source receivables were reconciled.

Pairs with eh_account_collections for follow up on bounced customer cheques.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.2.7',
    'depends': ['eh_account_base', 'account', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/bounce_reasons.xml',
        'data/cron.xml',
        'views/cheque_book_views.xml',
        'views/cheque_views.xml',
        'views/bounce_reason_views.xml',
        'views/res_partner_views.xml',
        'views/account_move_views.xml',
        'views/account_journal_views.xml',
        'views/res_config_settings_views.xml',
        'wizards/replace_cheque_wizard_views.xml',
        'wizards/bounce_cheque_wizard_views.xml',
        'report/cheque_register_report.xml',
        'report/cheque_print_report.xml',
        'data/menus.xml',
    ],
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
