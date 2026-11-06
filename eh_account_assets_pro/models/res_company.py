# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
res.company extension: assets-and-leases compliance knobs.

* eh_ias36_annual_test_month: month of the fiscal year in which the
  IAS 36.10 annual impairment test for goodwill and indefinite-life
  intangibles falls due; the annual-test cron starts flagging untested
  assets from that month.
* eh_lease_low_value_threshold: value-when-new ceiling (company
  currency) under which an underlying asset qualifies for the IFRS 16.6
  low-value recognition exemption. IFRS 16.BC100 discussed assets in
  the order of USD 5,000 when new, hence the default.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ResCompany(models.Model):
    _inherit = 'res.company'

    eh_ias36_annual_test_month = fields.Integer(
        string="IAS 36 Annual Test Month",
        default=12,
        help=(
            "Month (1-12) in which the mandatory annual impairment test "
            "for goodwill and indefinite-life intangibles (IAS 36.10) "
            "falls due. From this month, assets in the annual-test "
            "population with no test dated in the current fiscal year "
            "are flagged overdue and receive a to-do activity."
        ),
    )
    eh_lease_low_value_threshold = fields.Float(
        string="IFRS 16 Low-Value Threshold",
        default=5000.0,
        help=(
            "Value of the underlying asset when new (company currency) "
            "at or below which a lease qualifies for the IFRS 16.6 "
            "low-value recognition exemption."
        ),
    )

    @api.constrains('eh_ias36_annual_test_month')
    def _check_eh_ias36_annual_test_month(self):
        for company in self:
            if not (1 <= (company.eh_ias36_annual_test_month or 12) <= 12):
                raise ValidationError(_(
                    "The IAS 36 annual test month must be between 1 "
                    "(January) and 12 (December).",
                ))

    @api.constrains('eh_lease_low_value_threshold')
    def _check_eh_lease_low_value_threshold(self):
        for company in self:
            if company.eh_lease_low_value_threshold < 0:
                raise ValidationError(_(
                    "The IFRS 16 low-value threshold cannot be negative.",
                ))
