# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
res.company extension: company level fallback for the PDC bounce charge
expense account. The journal level account (account.journal
.eh_pdc_bounce_charge_account_id) wins when set; this fallback serves
deployments that expense dishonour fees to one account for every bank.
"""

from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    eh_pdc_bounce_charge_account_id = fields.Many2one(
        'account.account',
        string="PDC Bounce Charges Account",
        check_company=True,
        domain="[('account_type', 'in', ('expense', 'expense_direct_cost'))]",
        help="Fallback expense account debited when a post dated cheque "
             "bounces with bank charges and the bank journal has no "
             "bounce charges account of its own.",
    )
