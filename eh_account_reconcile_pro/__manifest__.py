# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Bank Reconciliation Pro',
    'summary': 'A modern bank reconciliation workspace for Odoo 19 Community Edition with a five-signal suggestion engine that ranks candidate journal items by confidence, so an operator confirms each match with one click or a drag. Odoo 19 Community bank reconciliation, bank reconciliation widget Odoo Community, reconciliation suggestion engine, auto match bank statement lines, user-defined reconciliation rules, reconciliation audit log, bank statement reconciliation workspace, FX rounding write-off, reconciliation exception report.',
    'description': """Bank Reconciliation Pro gives Odoo 19 Community a reconciliation experience built around a scored suggestion engine and a tamper-evident audit trail.

The two-pane workspace puts unreconciled statement lines on the left and scored candidate journal items on the right. A separate kanban lists past reconciliation sessions. For each statement line, a five-signal engine (amount, date proximity, partner, reference token overlap, and 90-day history) scores each candidate and surfaces its combined confidence score in the workspace, with the per-signal breakdown returned by the suggestion API. The operator confirms each match with one click, or drags a candidate journal entry onto the selected line. Tick several candidates and match them to the selected line in one action (1-to-N).

User-defined rules (payment-reference or narration regex, amount band, direction, partner) boost matching candidates so reliable patterns rank higher. Each rule definition is validated at write time, invalid regex and amount_max below amount_min are rejected before a rule can fire. The rule code that fired is stamped on the audit row.

Every match decision records the user, the time, the confidence, the rules that fired, and whether it was an accepted suggestion or a manual override. These audit rows are append-only, write and unlink are blocked at the model level. Managers can use Controlled Undo to reverse a precisely proven reconciliation graph without weakening the append-only evidence trail.

FX residual write-offs auto-route to the company exchange gain or loss account only when the foreign-currency amount is fully settled and a small company-currency residual remains. A hard server-side cap prevents ordinary bank-charge variance from being relabelled as FX, even if an RPC caller asks for a larger tolerance. Write-offs, including FX residual write-offs, post inside a database savepoint so a failed write-off rolls back cleanly without disturbing the session.

A reconciliation exception PDF reports, per journal over a date range, the reconciled percentage, unmatched count and amount, oldest-unmatched age, and write-off and skip counts.

Coexists cleanly with other Community accounting modules.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.3.4',
    'depends': ['eh_account_base', 'account'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'views/reconciliation_audit_views.xml',
        'views/account_bank_statement_views.xml',
        'views/reconciliation_session_views.xml',
        'views/reconciliation_rule_views.xml',
        'wizards/exception_report_wizard_views.xml',
        'wizards/reconciliation_undo_wizard_views.xml',
        'report/reconciliation_exception_report.xml',
        'data/menus.xml',
    ],
    'demo': ['demo/reconcile_demo.xml'],
    'assets': {
        'web.assets_backend': ['eh_account_reconcile_pro/static/src/components/**/*', 'eh_account_reconcile_pro/static/src/js/tours/reconcile_tour.js'],
        'web.assets_unit_tests': [
            'eh_account_reconcile_pro/static/tests/**/*',
        ],
    },
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
