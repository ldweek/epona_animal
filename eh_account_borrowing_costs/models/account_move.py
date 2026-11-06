# -*- encoding: utf-8 -*-
"""Immutable IAS 23 journal-entry provenance."""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError


BORROWING_MOVE_CONTEXT = 'eh_borrowing_move_capability'
BORROWING_MOVE_CAPABILITY = object()


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_borrowing_cost_id = fields.Many2one(
        'eh.borrowing.cost', string="Borrowing-cost Period", readonly=True,
        index=True, ondelete='restrict', copy=False, check_company=True)

    @api.model_create_multi
    def create(self, vals_list):
        provenance_requested = bool(
            self.env.context.get('default_eh_borrowing_cost_id')
            or any(vals.get('eh_borrowing_cost_id') for vals in vals_list)
        )
        if provenance_requested and not (
            self.env.context.get(BORROWING_MOVE_CONTEXT)
            is BORROWING_MOVE_CAPABILITY
        ):
            raise AccessError(_(
                "Borrowing-cost journal-entry provenance is engine-owned."
            ))
        return super().create(vals_list)

    def write(self, vals):
        if 'eh_borrowing_cost_id' in vals and not (
            self.env.context.get(BORROWING_MOVE_CONTEXT)
            is BORROWING_MOVE_CAPABILITY
        ):
            raise AccessError(_(
                "Borrowing-cost journal-entry provenance is immutable."
            ))
        return super().write(vals)
