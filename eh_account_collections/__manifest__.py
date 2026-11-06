# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Collections Workbench',
    'summary': 'Kanban driven collections workbench for Odoo 19 Community that turns aged receivables into an active follow up workflow. Auto creates one case per overdue customer from open AR, sends branded dunning emails on a configurable days overdue ladder, schedules call activities, captures promises to pay, escalates broken promises overnight, and issues prorated late fee invoices. Search terms: odoo 19 collections management, accounts receivable follow up, dunning automation, overdue invoice reminders, promise to pay tracking, aged receivables workflow, collections kanban, automated payment reminders, late fee invoicing, days sales outstanding DSO, credit control odoo community, broken promise escalation.',
    'description': 'Collections Workbench turns the aged receivable report from a passive dashboard into an active workflow. A single GROUP BY SQL pass over open receivable lines applies each company grace period and creates one collections case per overdue commercial customer per company, idempotently, so the nightly cron updates totals rather than duplicating cases. A canonical partial EXCLUDE constraint guarantees only one open case across every contact of the commercial customer, with each row isolated by a savepoint when concurrent workers race. Each case surfaces the total overdue, oldest due date, and days overdue across a seven column kanban: New, Contacted, Promise to Pay, Escalated, Disputed, Resolved, Written Off. A configurable per company follow up ladder fires on the shipped defaults of 7, 30, 60, and 90 days overdue, selecting the highest currently applicable unsent step. Each level sends a branded HTML dunning email or schedules its configured activity, advanced by a savepoint isolated daily cron so one delivery failure does not halt the queue. Promises to pay are captured as an amount plus a date on one immutable action row, and when that promise lapses a bounded broken promise cron escalates it once by recording the exact promise action identity. Every collections action is written to an append only action log that is blocked from create, edit, and delete in the user interface and from write and unlink at the model level. A late fee wizard charges a flat or prorated percent per month fee computed in Decimal to the cent, revalidates the live ledger at issuance, posts a standard customer invoice, and records invoice linked evidence under a database unique issuance fingerprint so the same exposure, policy, and issue date cannot be charged twice. A per case Statement of Account PDF listing open invoices and action history renders through the shared suite brand header and footer for print, download, or attachment.',
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.2.8',
    'depends': ['eh_account_base', 'mail', 'sms'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/stages.xml',
        'data/email_templates.xml',
        'data/followup_default.xml',
        'data/cron.xml',
        'views/stage_views.xml',
        'views/action_views.xml',
        'views/followup_level_views.xml',
        'views/case_views.xml',
        'views/res_partner_views.xml',
        'wizards/late_fee_wizard_views.xml',
        'wizards/reminder_wizard_views.xml',
        'report/case_statement_report.xml',
        'data/menus.xml',
    ],
    'demo': ['demo/collections_demo.xml'],
    'assets': {
        'web.assets_backend': ['eh_account_collections/static/src/js/tours/collections_tour.js'],
    },
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
    # The one-open-case-per-partner SQL EXCLUDE constraint is a GiST
    # exclusion over integer columns, which needs the btree_gist
    # extension's integer operator class. Ensure it exists before the
    # model table (and its constraint) are created.
    'pre_init_hook': 'pre_init_hook',
}
