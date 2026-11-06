# -*- encoding: utf-8 -*-
"""Demand-letter provenance on append-only collections actions."""

from odoo import api, fields, models

from .control import (
    AI_COLLECTIONS_DEMAND_CAPABILITY,
    AI_COLLECTIONS_DEMAND_CONTEXT,
)


class EhCollectionsAction(models.Model):
    _inherit = 'eh.collections.action'

    eh_ai_is_demand_letter = fields.Boolean(
        string="Demand-letter evidence",
        readonly=True,
        copy=False,
        index=True,
        help=(
            "Server-derived evidence that this action was a manual letter "
            "or a configured automated demand-letter follow-up."
        ),
    )
    @api.model_create_multi
    def create(self, vals_list):
        automated = self.env.context.get(
            AI_COLLECTIONS_DEMAND_CONTEXT,
        ) is AI_COLLECTIONS_DEMAND_CAPABILITY
        clean_vals = []
        for incoming in vals_list:
            vals = dict(incoming)
            vals['eh_ai_is_demand_letter'] = bool(
                vals.get('action_type') == 'letter' or automated
            )
            clean_vals.append(vals)
        return super().create(clean_vals)


class EhCollectionsFollowupLevel(models.Model):
    _inherit = 'eh.collections.followup.level'

    eh_ai_is_demand_letter = fields.Boolean(
        string="Treat as demand letter",
        help=(
            "Mark only a formal final notice whose delivery starts the "
            "approved waiting period before external referral. Future "
            "automated action rows receive immutable demand-letter evidence."
        ),
    )
