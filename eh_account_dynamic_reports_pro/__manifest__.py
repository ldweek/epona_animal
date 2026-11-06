# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Dynamic Reports Pro',
    'summary': 'Premium upgrade for Dynamic Accounting Reports: schedule reports to land in inboxes and chat channels on an hourly cron, compose new reports in a form, save per-user filter sets, and project bounded aggregate reports forward by month. Odoo 19 Community custom financial report builder, scheduled accounting report email, automated PDF and XLSX delivery cron, Slack and Microsoft Teams report webhook, saved report filter views, multi-period P and L and cash flow forecast, multi-company report scheduling.',
    'description': """Dynamic Reports Pro adds four capabilities on top of the free Dynamic Accounting Reports engine: scheduled multi-channel delivery, a form-driven custom report builder, per-user saved views, and multi-period forward projection.

Scheduled delivery binds any published report to an options set, a recipient list, and a cadence (daily, weekly, or monthly), then an hourly cron runs every schedule whose next run is due. Each delivery attaches a PDF, an XLSX, or both, and can fan out to email plus a Slack, Microsoft Teams, or generic HTTP webhook in the same run. Channels succeed independently: an email bounce does not block the webhook, and the schedule is marked successful if at least one channel lands.

The custom report builder lets you compose new reports without Python or XML. Define section headers, account roll-up lines (by code prefix or account type, with sign), and arithmetic formulas in a form, with drag-to-reorder line ordering. Each builder publishes into the same OWL viewer, PDF, and XLSX pipeline as the standard reports and renders a single Amount column.

Saved views capture a complete filter set under a name, can be pinned for quick access or shared company-wide, and carry usage telemetry so dead views are easy to spot.

Forecasting projects bounded aggregate reports (including P and L, Balance Sheet, Cash Flow, Executive Summary, and custom builds) forward N months using flat or compound monthly growth. Only current monetary cells grow; historical comparison values and non-monetary measures remain unchanged.

Requires eh_account_dynamic_reports (free, install first).""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.1.13',
    'depends': ['eh_account_dynamic_reports'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/cron.xml',
        'views/saved_view_views.xml',
        'views/schedule_views.xml',
        'views/forecast_views.xml',
        'views/builder_views.xml',
        'data/menus.xml',
    ],
    'demo': ['demo/pro_demo.xml'],
    'images': ['static/description/banner.gif'],
    'uninstall_hook': 'uninstall_hook',
    'installable': True,
    'application': False,
    'auto_install': False,
}
