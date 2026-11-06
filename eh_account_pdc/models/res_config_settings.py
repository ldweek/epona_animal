# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
res.config.settings mirror for the PDC bounce charge fallback account,
following the suite pattern: persistent field on res.company, related
readonly=False mirror here so the operator edits it on the standard
Settings > Accounting page (ERP Heritage Operations block).
"""

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    eh_pdc_bounce_charge_account_id = fields.Many2one(
        related='company_id.eh_pdc_bounce_charge_account_id',
        readonly=False,
        string="PDC Bounce Charges Account",
        help="Fallback expense account for cheque dishonour bank charges "
             "when the bank journal has none configured.",
    )
