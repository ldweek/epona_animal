# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Multi Version Budget Pro',
    'summary': 'Multi-version budgeting with purchase-order encumbrance for Odoo 19 Community: versioned budget records with a parent chain and auto-incrementing version labels, per-account lines, optional analytic dimension with percentage-weighted actuals, sign-aware variance and availability, off/warn/block overrun policy enforced when a purchase order is confirmed, flexible budgets with cost-behaviour classification and activity drivers, price/efficiency variance decomposition, and rolling reforecast snapshots that never overwrite the baseline. Search: odoo 19 community budget, multi version budget, budget vs actual, encumbrance accounting, purchase order commitment budget, budget overrun block purchase order, analytic budget odoo community, budget variance analysis, flexible budget, flexed variance volume variance, price efficiency variance, rolling reforecast, rolling budget, FP&A budget without enterprise.',
    'description': """Multi-version budgeting for Odoo 19 Community, built for finance teams that need a versioned, committed, audit-traceable budget without an Enterprise subscription.

What it does:

* Budget records with name, code, period window (date_from / date_to), and a draft / confirmed / closed lifecycle. Multi company aware, currency taken from the budget's company.
* Per-line budgets pinned to a specific account and period window with a budgeted monetary amount, blocked negative at the database CHECK level so an ORM-bypass path cannot corrupt the variance maths.
* Actuals computed from posted journal items only, in one SQL pass per budget for plain lines (no N+1 on budgets with hundreds of lines), plus one targeted pass per analytic account that a line actually uses.
* Sign-aware variance and availability that normalise income, liability and equity (credit-natured) accounts, so a 10k revenue budget against 10k actual reads as zero variance rather than a 200 percent overrun. A non-zero actual against a zero budget returns 100 percent to flag a full overrun instead of masking it as 0 percent.
* Optional analytic dimension with percentage-weighted actuals, computed identically in the ORM compute and in the SQL report view and parity-tested, so the form and the report never disagree.
* Multi-version budgets: copy a budget to a new version with a parent link, stable root code and monotonic v1 / v2 / v3 ordinal, so long chains stay auditable without accumulated suffixes.

Purchase-order encumbrance:

* Per-budget overrun policy (off, warn, or block) enforced at purchase-order confirm time. The block policy accumulates the required amount per budget line across multiple PO lines before checking availability, so several lines that each fit but collectively overrun are still caught.
* Commitments are idempotent and keyed by PO line: two PO lines that resolve to the same budget line keep separate commitments instead of one silently overwriting the other, and re-confirming a PO updates rather than duplicates.
* When a PO is partially billed, the commitment is released proportionally, shifting dollars from committed to actual. A billed PO with a zero or negative untaxed total releases the commitment fully instead of dividing by zero.

Productivity:

* Split lines monthly: a productivity action replaces each line with one slice per calendar month it overlaps, dividing the budgeted amount equally across those months.
* Forecast seed: generate a starting projection from account history, recording the algorithm used (Holt-Winters, linear trend, or mean) in each line's notes with an explicit fallback by history length, so an auditor can see the projection basis.
* Rolling roll-forward: a daily cron extends confirmed rolling budgets one calendar month forward when they approach their end date, copying the latest month's lines, under a per-record savepoint so one bad budget rolls back only its own slice and never freezes the queue.

Flexible budgets and variance decomposition:

* Cost behaviour per line (fixed, variable, or semi-variable with a fixed portion) with an activity driver: a manual activity register per period on the budget, or another line on the same budget as a revenue proxy for units sold.
* Flexed budget computed at the actual activity level, with the three-way variance tie enforced by construction: static variance = flexed variance (spending) + volume variance (activity), so the decomposition always reconciles to the classic budget-vs-actual figure.
* Price and efficiency split for variable lines carrying quantity data: price variance at actual quantity, efficiency variance at budgeted price against the flexed quantity allowance, plus a residual column that flags quantity data inconsistent with the posted ledger.
* Rolling reforecast: period-stamped forecast revisions snapshot elapsed months at posted actuals and re-project the remaining months with the same forecasting engines, without ever overwriting the baseline; variance is reported against the baseline and against the latest revision side by side.

Engineering principles: posted-only actuals, batch SQL distributed in Python, ORM and report-view parity tested, database-level guards on negative amounts.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.3.11',
    'depends': ['eh_account_base', 'purchase', 'analytic'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'wizards/budget_split_wizard_views.xml',
        'views/budget_views.xml',
        'views/budget_revision_views.xml',
        'views/budget_commitment_views.xml',
        'views/purchase_order_views.xml',
        'report/budget_report.xml',
        'data/menus.xml',
        'data/cron.xml',
    ],
    'demo': ['demo/budget_demo.xml'],
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
