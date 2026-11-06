# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The costing mechanics (standard cost
# cards, the two-way variance decomposition of material, labour and overhead
# into price/rate/spend and usage/efficiency/volume components, contribution
# margin reporting and cost-volume-profit analysis) are textbook management
# accounting as taught in every CMA and CIMA syllabus; no code or comments
# derive from any proprietary or third-party module.
#
##############################################################################
{
    'name': 'Standard Costing & CVP',
    'summary': 'Standard costing and management accounting for Odoo 19 Community: standard cost cards per product with material, labour and overhead elements, period actual capture, a variance run that decomposes the total cost variance into material price and usage, labour rate and efficiency, variable overhead spend and efficiency and fixed overhead spend and volume components that reconcile exactly to actual cost minus standard cost absorbed, optional posting of the variance set as one sealed journal entry, plus contribution margin reporting with break-even, target profit, margin of safety and operating leverage. Search: odoo 19 standard costing, variance analysis odoo, material price variance, labour efficiency variance, overhead volume variance, contribution margin odoo, CVP analysis, break even analysis odoo, cost volume profit, management accounting odoo.',
    'description': 'A management accounting layer for Odoo 19 Community. Standard cost cards hold the per-unit standard quantity and price of each cost element (material, labour, variable overhead, fixed overhead) for a product or free-form item, with one active card per product enforced. Period actuals capture the units produced and the total input quantity and cost per element. The variance run decomposes the difference between actual cost and standard cost absorbed into the full two-way variance set: material price and usage, labour rate and efficiency, variable overhead spend and efficiency, fixed overhead spend and volume, with a favourable-negative sign convention and a reconciliation identity (the variance lines always sum exactly to total actual cost minus total standard cost absorbed) enforced by constraint. Posting is optional and off by default: in analysis-only mode nothing touches the ledger; with posting enabled the run books the variance set as one balanced, sealed journal entry against per-kind variance accounts and an absorption account. The contribution report computes contribution margin per product from the standard variable cost, picks up revenue manually or from posted invoice lines, and derives the CVP set: break-even units and revenue, target-profit units, margin of safety and degree of operating leverage. Inventory valuation integration with stock moves is documented as a later wave; this release is deliberately ledger-independent on the input side so it fits any inventory setup. Posting is gated to the EH Accounting Manager group and every generated journal entry is sealed against edit.',
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.0.7',
    'depends': ['eh_account_base', 'account', 'mail', 'product'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'views/cost_card_views.xml',
        'views/cost_actual_views.xml',
        'views/variance_run_views.xml',
        'views/contribution_views.xml',
        'data/menus.xml',
    ],
    'assets': {
        'web.assets_tests': [
            'eh_account_costing/static/tests/tours/costing_test_tour.js',
        ],
    },
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
