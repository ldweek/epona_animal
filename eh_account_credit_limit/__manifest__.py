# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. Credit-limit enforcement is a
# standard pattern from corporate finance practice; the implementation
# is composed against the public Odoo account.move post pipeline. No
# code or comments derive from any proprietary or third-party module.
#
##############################################################################
{
    'name': 'Customer Credit Limit',
    'summary': 'Hard customer credit limit enforcement for Odoo 19 Community that blocks the invoice at post time instead of just showing a number. Per-company credit policy, optional per-partner limit override, warn-or-block enforcement, manager override with reason recorded in an append-only audit log, and a live OK/Warn/Over exposure badge on the partner form. Search terms: odoo customer credit limit, block invoice over credit limit, accounts receivable credit control, hard credit limit enforcement, credit hold, per-partner credit limit, credit policy per company, customer exposure receivable, credit limit manager override audit log.',
    'description': """Odoo's standard credit_limit field is informational only. Nothing stops an over-limit customer invoice from posting. This module adds a separate, company-scoped EH credit limit and turns that explicit policy setting into a real posting gate.

Define one credit policy per company. The policy sets the default limit, whether enforcement warns or blocks, whether draft customer invoices and the not-yet-invoiced portion of confirmed sales orders count toward exposure (both opt-in, both off by default), and which group may override. A commercial customer can carry its own limit and policy override; child contacts share that control. Otherwise the company default applies.

When a customer invoice (out_invoice or out_refund) posts, the gate computes the commercial customer's exposure across the complete actor-authorised accounting-branch scope and compares it against the effective limit using company-currency rounding. Block mode serialises that decision on the commercial-customer row. A breach raises a clear error naming exposure, limit, and excess until a manager records an override reason on that specific invoice. Warn mode logs the breach and proceeds without the blocking lock.

Every override writes one row to an append-only log capturing user, timestamp, exposure, limit, move amount, excess, and reason. The log model refuses write to anything but the comment and refuses unlink, so the record of who waved a customer through, and why, cannot be quietly edited away. The override is per invoice: blessing one move does not bless the next.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.3.0',
    'depends': ['eh_account_base', 'account'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'views/credit_policy_views.xml',
        'views/res_partner_views.xml',
        'views/account_move_views.xml',
        'data/menus.xml',
    ],
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
