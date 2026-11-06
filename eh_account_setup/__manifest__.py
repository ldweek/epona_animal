# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Accounting Setup Guide',
    'summary': "A guided accounting onboarding checklist for Odoo 19 Community that turns the suite's strict configuration errors into 26 tracked setup tasks across 7 categories, each with a deep link into the exact configuration screen. Per-company progress, module-aware visibility, fail-loud deep links, completion ratio that excludes skipped tasks, and a full audit trail. Odoo 19 accounting setup checklist, accounting onboarding guide, multi company configuration tracker, first install accounting setup, guided accounting configuration, per company setup progress, consultant rollout tracker, deep link to configuration screens.",
    'description': """The accounting suite refuses to silently default. Missing rates, missing accounts, missing approval groups all surface explicit errors. That is excellent for correctness and harsh on first install. This module reframes the same configuration work as a guided checklist: one row per setup task, per company, with a deep link into the correct configuration screen and a clear state of to do, done, or skipped.

The Setup Guide menu sits under Accounting > Heritage Operations, where every EH Accounting User can reach it, and lists 26 seeded tasks across 7 categories (foundations, currencies, receivables, payables, approvals, reporting, period close). It opens grouped by category, filtered to To do, and to relevant tasks only. Each task carries an action external id that is resolved through Odoo's safe action serializer at click time; Open jumps straight into the exact configuration screen. The checklist itself is scoped to the active company by a global isolation rule, so records outside the user's allowed companies are never returned.

Tasks tied to optional modules render only when those modules are installed, via a fully searchable is_relevant flag, so filters and group-by act on installed-module state and the list never shows clutter for features the customer did not buy. Each line records who completed the task and when, alongside free notes for the reviewer, and skip and reset are reversible. Seeding is idempotent on both install and new-company creation, creating only missing lines and preserving existing progress.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.0.6',
    'depends': ['eh_account_base', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/setup_tasks.xml',
        'views/setup_task_views.xml',
        'views/setup_task_line_views.xml',
        'data/menus.xml',
    ],
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
    'post_init_hook': 'post_init_hook',
}
