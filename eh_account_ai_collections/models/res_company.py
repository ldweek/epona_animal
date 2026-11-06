# -*- encoding: utf-8 -*-
"""Company materiality policy for collections write-off guidance."""

from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    eh_ai_collections_writeoff_materiality = fields.Monetary(
        string="Collections write-off review threshold",
        currency_field='currency_id',
        default=1000.0,
        help=(
            "Balances at or above this company-currency amount receive high "
            "priority when the deterministic engine recommends write-off "
            "review. Lower balances remain reviewable at medium priority."
        ),
    )
