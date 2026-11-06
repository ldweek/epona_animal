# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.credit.override.log: append-only audit row per credit override.

When a manager bypasses the credit gate to post an over-limit
customer invoice, a log row records the partner, the move, the user,
the timestamp, the exposure at override time, the limit at override
time, and the reason. Append-only behaviour (write blocked to
everything but comment, unlink refused) comes from eh.audit.mixin.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError


class EhCreditOverrideLog(models.Model):
    _name = 'eh.credit.override.log'
    _description = "Credit override audit log"
    _inherit = ['eh.audit.mixin']

    _eh_audit_unlink_message = (
        "Credit override log rows cannot be deleted. They are the "
        "audit trail for credit-policy enforcement."
    )

    move_id = fields.Many2one(
        'account.move', required=True,
        ondelete='restrict', index=True,
    )
    partner_id = fields.Many2one(
        'res.partner', required=True,
        ondelete='restrict', index=True,
    )
    company_id = fields.Many2one(
        'res.company', required=True, index=True,
    )

    exposure_at_override = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Partner exposure (open AR plus drafts when policy says "
            "so) at the moment of override."
        ),
    )
    limit_at_override = fields.Monetary(
        currency_field='currency_id',
        help="Effective credit limit applied to the partner at override time.",
    )
    move_amount = fields.Monetary(
        currency_field='currency_id',
        help="Amount of the move that triggered the override.",
    )
    excess = fields.Monetary(
        compute='_compute_excess',
        currency_field='currency_id',
        store=True,
        help=(
            "(exposure + move_amount) - limit. The amount by which "
            "the override exceeds the limit."
        ),
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='company_id.currency_id', store=True, readonly=True,
    )

    reason = fields.Text(required=True)
    reason_author_id = fields.Many2one(
        'res.users', readonly=True,
        help="Authorised user who recorded the reason on the invoice.",
    )
    reason_authored_at = fields.Datetime(
        readonly=True,
        help="Time the authorised user recorded the reason.",
    )

    @api.depends('exposure_at_override', 'limit_at_override', 'move_amount')
    def _compute_excess(self):
        for rec in self:
            rec.excess = (
                (rec.exposure_at_override or 0.0)
                + (rec.move_amount or 0.0)
                - (rec.limit_at_override or 0.0)
            )

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError(_(
                "Credit override logs are created only by the posting gate."
            ))
        return super().create(vals_list)
