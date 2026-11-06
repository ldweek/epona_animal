# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.reconciliation.audit: per line audit of reconciliation decisions.

One row per (statement_line, decision) pair. For a match decision we record
the target AML id, the suggestion engine confidence at decision time, the
heuristics that fired, the user, and whether the user accepted a suggestion
or made the decision manually. For write-off decisions we record the same
metadata with aml_id null.

Why a separate model and not chatter on bank.statement.line:

* The audit log is queryable: how many decisions per partner per month, what
  is the average confidence on confirmed matches, how often does the
  suggestion engine win versus manual matching.
* The history score in the suggestion engine queries this table directly.
* It survives statement line deletion via ondelete='cascade' on the
  statement_line side; we lose the link but keep aggregate stats.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class EhReconciliationAudit(models.Model):
    _name = 'eh.reconciliation.audit'
    _description = "Per line reconciliation decision audit"
    _order = 'decided_at desc'
    _rec_name = 'display_name'

    display_name = fields.Char(compute='_compute_display_name', store=True)

    session_id = fields.Many2one(
        'eh.reconciliation.session',
        ondelete='restrict',
        index=True,
    )
    statement_line_id = fields.Many2one(
        'account.bank.statement.line',
        required=True,
        ondelete='restrict',
        index=True,
    )
    aml_id = fields.Many2one(
        'account.move.line',
        ondelete='set null',
        index=True,
    )
    user_id = fields.Many2one(
        'res.users',
        required=True,
        default=lambda self: self.env.user,
        index=True,
    )
    decided_at = fields.Datetime(
        required=True,
        default=fields.Datetime.now,
        index=True,
    )

    confidence = fields.Float(
        default=0.0,
        digits=(3, 4),
        help=(
            "Suggestion engine total score at decision time (0 to 1). "
            "0 indicates a manual match without engine guidance."
        ),
    )
    rules_fired = fields.Char(
        help=(
            "Comma separated names of heuristics that scored above zero "
            "for this match. For example: amount,date,partner."
        ),
    )

    decision = fields.Selection(
        [
            ('match', "Matched"),
            ('write_off', "Written off"),
            ('skip', "Skipped"),
            ('undo', "Controlled Undo"),
        ],
        required=True,
        index=True,
    )
    source = fields.Selection(
        [
            ('manual', "Manual"),
            ('drag_drop', "Drag and Drop"),
            ('suggestion', "Accepted Suggestion"),
            ('rule', "Reconciliation Rule"),
            ('bulk', "Bulk Operation"),
        ],
        default='manual',
        required=True,
    )
    decision_group = fields.Char(
        index=True,
        copy=False,
        help=(
            "Server-generated identifier joining every audit row and ledger "
            "adjustment produced by one reconciliation decision."
        ),
    )
    adjusting_move_id = fields.Many2one(
        'account.move', ondelete='restrict', index=True, copy=False,
    )
    original_audit_id = fields.Many2one(
        'eh.reconciliation.audit', ondelete='restrict', index=True,
        copy=False,
    )
    reversal_move_id = fields.Many2one(
        'account.move', ondelete='restrict', index=True, copy=False,
    )
    reversal_date = fields.Date(copy=False)
    undo_reason = fields.Text(copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError(_(
                "Reconciliation audit rows are created only by session "
                "decision actions."
            ))
        now = fields.Datetime.now()
        protected = []
        for incoming in vals_list:
            vals = dict(incoming or {})
            if not vals.get('user_id'):
                raise AccessError(_(
                    "Reconciliation audit creation requires an explicit "
                    "decision actor."
                ))
            vals['decided_at'] = now
            protected.append(vals)
        return super().create(protected)

    @api.depends('decision', 'statement_line_id', 'decided_at')
    def _compute_display_name(self):
        for rec in self:
            sl_label = rec.statement_line_id.payment_ref or 'statement line'
            ts = (
                fields.Datetime.to_string(rec.decided_at)
                if rec.decided_at else ''
            )
            rec.display_name = "%s: %s (%s)" % (
                rec.decision or 'pending', sl_label, ts,
            )

    def action_open_undo_wizard(self):
        self.ensure_one()
        self._eh_check_access('read')
        if self.decision not in ('match', 'write_off'):
            raise UserError(_(
                "Only a match or write-off decision can be undone."
            ))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Controlled Reconciliation Undo"),
            'res_model': 'eh.reconciliation.undo.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_audit_id': self.id},
        }

    @api.private
    def _eh_undo(self, reversal_date, reason):
        """Undo one decision graph, reverse its adjustment and append audit."""
        self.ensure_one()
        self._eh_check_access('read')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can undo reconciliation."
            ))
        reason = ' '.join((reason or '').split())
        if len(reason) < 20:
            raise UserError(_(
                "Record at least 20 characters explaining the undo."
            ))
        reversal_date = fields.Date.to_date(reversal_date)
        if not reversal_date:
            raise UserError(_("A reversal date is required."))
        if self.decision not in ('match', 'write_off'):
            raise UserError(_("This audit row is not an undoable decision."))
        if not self.decision_group:
            raise UserError(_(
                "This legacy decision predates deterministic ledger "
                "provenance and cannot be undone automatically. Review it "
                "with a manual correcting entry."
            ))

        Audit = self.env['eh.reconciliation.audit']
        originals = Audit.search([
            ('decision_group', '=', self.decision_group),
            ('decision', 'in', ('match', 'write_off')),
        ], order='id')
        if not originals or any(
            row.statement_line_id != self.statement_line_id
            or row.adjusting_move_id != self.adjusting_move_id
            for row in originals
        ):
            raise UserError(_(
                "The reconciliation decision graph is incomplete or "
                "inconsistent; no ledger mutation was made."
            ))

        originals.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_reconciliation_audit WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            (tuple(originals.ids),),
        )
        if Audit.search_count([
            ('decision_group', '=', self.decision_group),
            ('decision', '=', 'undo'),
        ], limit=1):
            raise UserError(_(
                "This reconciliation decision has already been undone."
            ))

        statement_move = self.statement_line_id.move_id
        adjusting = self.adjusting_move_id
        candidates = originals.mapped('aml_id')
        affected_moves = statement_move | adjusting | candidates.move_id
        affected_moves._eh_check_access('read')
        if affected_moves:
            affected_moves.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM account_move WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(sorted(affected_moves.ids)),),
            )
            affected_moves.invalidate_recordset()

        original_date = adjusting.date if adjusting else statement_move.date
        if original_date and reversal_date < original_date:
            raise UserError(_(
                "Reversal date cannot precede the original accounting date."
            ))

        if adjusting:
            if (
                adjusting.state != 'posted'
                or not adjusting.eh_sealed
                or adjusting.eh_legacy_unverified_seal
            ):
                raise UserError(_(
                    "The adjusting entry is no longer a verified posted "
                    "entry; no automated undo is safe."
                ))
            decision_lines = adjusting.line_ids
            allowed_lines = (
                decision_lines | statement_move.line_ids | candidates
            )
        else:
            # Direct matches have no reclassification move. Select only the
            # statement lines whose reconcile edges actually reach the
            # audited candidate set.
            decision_lines = statement_move.line_ids.filtered(
                lambda line: bool(
                    (
                        line.matched_debit_ids.debit_move_id
                        | line.matched_debit_ids.credit_move_id
                        | line.matched_credit_ids.debit_move_id
                        | line.matched_credit_ids.credit_move_id
                    ) & candidates
                )
            )
            allowed_lines = statement_move.line_ids | candidates
        partials = (
            decision_lines.matched_debit_ids
            | decision_lines.matched_credit_ids
        )
        endpoints = partials.debit_move_id | partials.credit_move_id
        if not decision_lines or not partials or (endpoints - allowed_lines):
            raise UserError(_(
                "The reconciliation graph changed after the recorded "
                "decision. Review it manually; no partial reconcile was "
                "removed."
            ))
        decision_lines._eh_remove_reconcile_for_controlled_undo()

        reversal = self.env['account.move']
        if adjusting:
            reversal = adjusting._eh_reverse_with_verified_capability([{
                'date': reversal_date,
                'ref': _(
                    "Controlled undo of reconciliation %s",
                    self.decision_group,
                ),
            }], cancel=False)
            reversal._eh_post_verified_reversal()
            # Close the original/reversal pair wherever the account permits
            # reconciliation. Non-reconcilable P&L legs already net to zero
            # in the GL and deliberately remain outside residual tracking.
            pair_lines = adjusting.line_ids | reversal.line_ids
            keys = {
                (line.account_id.id, line.partner_id.id or False,
                 line.currency_id.id or False)
                for line in pair_lines if line.account_id.reconcile
            }
            for account_id, partner_id, currency_id in keys:
                group = pair_lines.filtered(lambda line: (
                    line.account_id.id == account_id
                    and (line.partner_id.id or False) == partner_id
                    and (line.currency_id.id or False) == currency_id
                    and not line.reconciled
                ))
                if group:
                    group.reconcile()

        self.statement_line_id.invalidate_recordset(['is_reconciled'])
        if getattr(self.statement_line_id, 'is_reconciled', False):
            raise UserError(_(
                "The controlled undo did not reopen the statement line; all "
                "changes were rolled back."
            ))
        return Audit.sudo().create({
            'session_id': self.session_id.id,
            'statement_line_id': self.statement_line_id.id,
            'aml_id': False,
            'user_id': self.env.user.id,
            'confidence': 0.0,
            'rules_fired': '',
            'decision': 'undo',
            'source': 'manual',
            'decision_group': self.decision_group,
            'original_audit_id': self.id,
            'reversal_move_id': reversal.id if reversal else False,
            'reversal_date': reversal_date,
            'undo_reason': reason,
        })

    # ----------- audit-log immutability -----------
    # These rows are the decision trail the suggestion engine's history score
    # and every reconciliation review read as fact. They are written once at
    # decision time and never legitimately mutated or deleted, so they are
    # strictly append-only: write() and unlink() are refused for everyone. The
    # previous context-flag escape was caller-controllable (any RPC caller
    # could pass eh_internal_audit_write/unlink) and was never set by the app,
    # so it was a bypass, not a control. The ACL sets perm_write=0/perm_unlink=0
    # on top of these guards as defence in depth.
    def write(self, vals):
        raise UserError(_(
            "Reconciliation audit rows are append-only and cannot be "
            "edited once recorded."))

    @api.ondelete(at_uninstall=False)
    def _unlink_immutable_audit(self):
        raise UserError(_(
            "Reconciliation audit rows cannot be deleted; they are an "
            "immutable decision trail."))
