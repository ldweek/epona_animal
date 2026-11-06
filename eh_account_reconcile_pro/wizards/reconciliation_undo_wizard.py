# -*- encoding: utf-8 -*-
"""Explicit, dated and reasoned reconciliation undo."""

from odoo import fields, models


class EhReconciliationUndoWizard(models.TransientModel):
    _name = 'eh.reconciliation.undo.wizard'
    _description = "Controlled reconciliation undo"

    audit_id = fields.Many2one(
        'eh.reconciliation.audit', required=True, readonly=True,
        ondelete='cascade',
        domain="[('decision', 'in', ('match', 'write_off'))]",
    )
    reversal_date = fields.Date(
        required=True, default=fields.Date.context_today,
    )
    reason = fields.Text(
        required=True,
        help="At least 20 characters describing the evidence and correction.",
    )

    def action_confirm(self):
        self.ensure_one()
        self._eh_check_access('write')
        self.audit_id._eh_undo(self.reversal_date, self.reason)
        return {'type': 'ir.actions.act_window_close'}
