# -*- encoding: utf-8 -*-
"""Protect reconciliation decisions from untracked core unreconciliation."""

from odoo import _, api, models
from odoo.exceptions import UserError


_EH_RECONCILIATION_UNDO = 'eh_reconciliation_undo_capability'
_EH_RECONCILIATION_UNDO_CAPABILITY = object()


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    @api.private
    def _eh_remove_reconcile_for_controlled_undo(self):
        guarded = self.with_context(**{
            _EH_RECONCILIATION_UNDO:
                _EH_RECONCILIATION_UNDO_CAPABILITY,
        })
        return guarded.remove_move_reconcile()

    def remove_move_reconcile(self):
        if (
            self.env.context.get(_EH_RECONCILIATION_UNDO)
            is _EH_RECONCILIATION_UNDO_CAPABILITY
        ):
            return super().remove_move_reconcile()
        if self:
            Audit = self.env['eh.reconciliation.audit'].sudo()
            decisions = Audit.search([
                ('decision', 'in', ('match', 'write_off')),
                '|', '|',
                ('aml_id', 'in', self.ids),
                ('adjusting_move_id', 'in', self.move_id.ids),
                ('statement_line_id.move_id', 'in', self.move_id.ids),
            ])
            groups = set(filter(None, decisions.mapped('decision_group')))
            undone = set(Audit.search([
                ('decision', '=', 'undo'),
                ('decision_group', 'in', tuple(groups)),
            ]).mapped('decision_group')) if groups else set()
            active = decisions.filtered(
                lambda row: (
                    not row.decision_group
                    or row.decision_group not in undone
                )
            )
            if active:
                raise UserError(_(
                    "This reconciliation belongs to an ERP Heritage audit "
                    "decision. Use Controlled Undo from Reconciliation "
                    "Decisions so the adjustment is reversed and the reason "
                    "is preserved."
                ))
        return super().remove_move_reconcile()
