# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
res.config.settings mirrors for the assets-and-leases compliance knobs,
following the suite pattern: persistent field on res.company, related
readonly=False mirror here so the operator edits it on the standard
Settings > Accounting page (ERP Heritage Assets and Leases block).
"""

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    eh_ias36_annual_test_month = fields.Integer(
        related='company_id.eh_ias36_annual_test_month',
        readonly=False,
        string="IAS 36 Annual Test Month",
        help=(
            "Month (1-12) of the fiscal year in which the mandatory "
            "annual impairment test for goodwill and indefinite-life "
            "intangibles falls due (IAS 36.10)."
        ),
    )
    eh_lease_low_value_threshold = fields.Float(
        related='company_id.eh_lease_low_value_threshold',
        readonly=False,
        string="IFRS 16 Low-Value Threshold",
        help=(
            "Value of the underlying asset when new at or below which "
            "a lease qualifies for the IFRS 16 low-value recognition "
            "exemption."
        ),
    )
