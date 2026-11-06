# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Append-only lifecycle signatures for every close-run cycle."""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError


class EhCloseSignoff(models.Model):
    _name = 'eh.close.signoff'
    _description = "Close run append-only sign-off event"
    _order = 'event_at, id'

    run_id = fields.Many2one(
        'eh.close.run', required=True, ondelete='cascade', index=True,
    )
    event_type = fields.Selection(
        [
            ('prepare', "Prepared"),
            ('review', "Reviewed"),
            ('approve', "Approved"),
            ('return_to_work', "Returned to Work"),
            ('reopen', "Reopened"),
        ],
        required=True,
        index=True,
    )
    user_id = fields.Many2one(
        'res.users', required=True, ondelete='restrict', index=True,
    )
    event_at = fields.Datetime(required=True, index=True)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError(_(
                "Close sign-off events are recorded only by lifecycle "
                "actions."))
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su:
            raise AccessError(_(
                "Close sign-off events are append-only and cannot be edited."))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise AccessError(_(
                "Close sign-off events are append-only and cannot be deleted."))
        return super().unlink()
