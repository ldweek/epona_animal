# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Period Close Workflow',
    'summary': 'Bounded period-close checklists with a strict three-person sign-off, automated ledger checks, global-lock approval, reasoned rework and append-only evidence.',
    'description': """Period Close Workflow turns the repeatable month-end close that most teams run from a spreadsheet into a tracked process inside Odoo. Build a reusable checklist once, instantiate it for a period, work the tasks, request approval, and close, with a defensible audit trail at the end.

Reusable checklist templates carry ordered tasks. Each run copies those tasks into independent per-run instances, so editing a live run never mutates the template and never touches a closed prior period. One default Standard Monthly Close checklist (8 tasks) ships and installs on first install. Deeper cadence and per-subsidiary catalogues are deployment-specific.

Each task instance moves through pending, in progress, done, not applicable, or blocked. The run itself moves through open, in progress, pending approval, closed, and reopened. The sign-off chain records who prepared, who reviewed, and who approved, each with a readonly signer and timestamp.

The state machine enforces the controls a spreadsheet cannot. Every run has a bounded period. A run cannot request approval while any required task is still pending or in progress. Optional tasks are intentionally non-blocking, so a close can proceed with optional items open. Blocking a task requires a material reason, and a run cannot close while any task is blocked. Segregation of duties is enforced in code: the preparer cannot request review, and neither the preparer nor reviewer can give final approval. A third, different manager must sign off.

Automated checks rescan draft moves, exceptional imported unbalanced postings, and open bank or cash suspense. Global Lock checklists atomically advance Odoo's company-wide lock through period end at final approval; Sign-off Only checklists intentionally record evidence without moving that lock.

Re-marking an already-completed task is a no-op. The original signer and timestamp are preserved, never overwritten. Where rework is genuinely needed, an Accounting Manager can run a Reset to Pending action that clears the completion and any blocked reason, and that reset is itself recorded in chatter. A closed run can be reopened by a manager for post-close adjustments, and the reopen preserves the original prepared, reviewed, approved trio while stamping who reopened it and when.

Every state transition and sign-off is recorded in the standard Odoo chatter via mail.thread tracking, with user and timestamp. Tasks are real records, not free text, so they are queryable, exportable, and audit ready. Per-company record rules isolate checklists, runs, and tasks across companies.

Pairs naturally with the dynamic reports and reconciliation modules in the suite at close time.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.1.7',
    'depends': ['eh_account_base', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/checklist_default.xml',
        'views/checklist_views.xml',
        'views/run_views.xml',
        'views/task_block_wizard_views.xml',
        'data/menus.xml',
    ],
    'demo': ['demo/close_demo.xml'],
    'assets': {
        'web.assets_backend': ['eh_account_close_workflow/static/src/js/tours/close_tour.js'],
    },
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
