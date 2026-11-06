# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Reason capture for the close-task Block action."""

from odoo import _, fields, models
from odoo.exceptions import UserError


class EhCloseTaskBlockWizard(models.TransientModel):
    _name = 'eh.close.task.block.wizard'
    _description = "Block close task"

    task_id = fields.Many2one(
        'eh.close.task', required=True, readonly=True, ondelete='cascade',
    )
    reason = fields.Text(
        required=True,
        help="Material explanation preserved on the blocked task.",
    )

    def action_confirm(self):
        self.ensure_one()
        reason = (self.reason or '').strip()
        if len(reason) < 10:
            raise UserError(_(
                "Explain why the task is blocked using at least 10 "
                "characters."
            ))
        self.task_id.action_block(reason=reason)
        return {'type': 'ir.actions.act_window_close'}
