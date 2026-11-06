# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.close.task: task instance on a close run.

Each task is one item from the parent checklist's task templates,
copied at run creation. Tasks carry their own state machine
(pending, in_progress, done, not_applicable, blocked) and audit
fields (completed_by, completed_at) so the close history shows who
finished what and when.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


_TASK_AUDIT_FIELDS = {'completed_by_id', 'completed_at'}
_EDITABLE_RUN_STATES = ('open', 'in_progress', 'reopened')


class EhCloseTask(models.Model):
    _name = 'eh.close.task'
    _description = "Close run task"
    _order = 'sequence, id'
    _inherit = ['mail.thread', 'eh.workflow.guard']

    # The task state machine gates the parent run's close (required/blocked
    # tasks stop approval). Its transitions run through action_* methods; the
    # guard stops a plain user RPC-writing state to bypass them. Actions below
    # carry the flag via self._eh_workflow_action().
    _eh_guarded_fields = ('state',)

    run_id = fields.Many2one(
        'eh.close.run',
        required=True,
        ondelete='cascade',
        index=True,
        help="Parent close run this task belongs to.",
    )
    sequence = fields.Integer(
        default=10,
        help=(
            "Display order on the run's task list. Lower numbers "
            "render first; the handle widget on the list view writes "
            "this field directly."
        ),
    )

    name = fields.Char(
        required=True,
        help=(
            "Short label visible on the kanban / list view. "
            "Convention: imperative verb + object, e.g. 'Reconcile "
            "bank accounts', 'Post depreciation'."
        ),
    )
    description = fields.Html(
        help=(
            "Detailed instructions for the preparer. Supports rich "
            "text and links so a checklist template can embed "
            "screenshots and references to other Odoo records."
        ),
    )

    state = fields.Selection(
        [
            ('pending', "Pending"),
            ('in_progress', "In Progress"),
            ('done', "Done"),
            ('not_applicable', "Not Applicable"),
            ('blocked', "Blocked"),
        ],
        default='pending',
        required=True,
        tracking=True,
        index=True,
        help=(
            "Task lifecycle state. pending / in_progress are open. "
            "done and not_applicable both count as complete. blocked "
            "prevents the run from closing until resolved."
        ),
    )

    responsible_role = fields.Selection(
        [
            ('accountant', "Accountant"),
            ('manager', "Manager"),
            ('both', "Both"),
        ],
        default='accountant',
        required=True,
        help=(
            "Role typically responsible for this task. Advisory only: "
            "Odoo group permissions are not enforced from this field. "
            "Used by the kanban for swim-lane grouping."
        ),
    )
    is_required = fields.Boolean(
        default=True, tracking=True,
        help=(
            "Required tasks block the run from moving to "
            "pending_approval until they are done or marked "
            "not_applicable. Optional tasks do not block."
        ),
    )

    assigned_user_id = fields.Many2one(
        'res.users', tracking=True,
        help=(
            "User the preparer assigns this task to. Auto-fills with "
            "the current user when action_start fires and the field "
            "is empty."
        ),
    )

    completed_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
        help=(
            "User who marked this task done or not_applicable. "
            "Captured for the close audit trail."
        ),
    )
    completed_at = fields.Datetime(
        readonly=True, copy=False,
        help="Timestamp at which the task was marked complete.",
    )

    blocked_reason = fields.Char(
        tracking=True,
        help=(
            "Free-text explanation of why the task is blocked. "
            "Surfaces on the form and in the close-run audit pack so "
            "the reviewer understands the dependency."
        ),
    )
    notes = fields.Html(
        help=(
            "Working notes the preparer can leave for the reviewer. "
            "Distinct from chatter, which is the formal audit trail."
        ),
    )

    # ---- integrity / parent lifecycle ----

    @api.constrains('state', 'blocked_reason')
    def _check_blocked_reason(self):
        for rec in self:
            if (
                rec.state == 'blocked'
                and len((rec.blocked_reason or '').strip()) < 10
            ):
                raise ValidationError(_(
                    "A blocked close task requires a material reason of at "
                    "least 10 characters."
                ))

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            self._eh_check_access('create')
            forged = set().union(*(
                set(vals) & _TASK_AUDIT_FIELDS for vals in vals_list
            ))
            if forged:
                raise AccessError(_(
                    "Task completion fields are server-owned and cannot be "
                    "supplied at creation: %s",
                    ', '.join(sorted(forged)),
                ))
            run_ids = {
                vals.get('run_id') for vals in vals_list
                if vals.get('run_id')
            }
            runs = self.env['eh.close.run'].browse(sorted(run_ids)).exists()
            if len(runs) != len(run_ids):
                raise AccessError(_("Close run does not exist or is unavailable."))
            runs._eh_lock_for_transition()
            if runs.filtered(lambda rec: rec.state not in _EDITABLE_RUN_STATES):
                raise UserError(_(
                    "Tasks cannot be added after a close run is submitted or "
                    "closed."))
        return super().create(vals_list)

    def _eh_lock_and_assert_run_editable(self, operation='write'):
        if not self.env.su:
            self._eh_check_access(operation)
        runs = self.mapped('run_id')
        runs._eh_lock_for_transition()
        runs.invalidate_recordset(['state'])
        if runs.filtered(lambda rec: rec.state not in _EDITABLE_RUN_STATES):
            raise UserError(_(
                "Tasks are immutable after a close run is submitted or "
                "closed. Reopen the run before changing its work."))
        return self

    def write(self, vals):
        if not self.env.su:
            if set(vals) & _TASK_AUDIT_FIELDS:
                raise AccessError(_(
                    "Task completion identity and timestamp are server-owned."))
            if 'run_id' in vals:
                raise AccessError(_(
                    "A close task cannot be moved to another run."))
            if (
                'is_required' in vals
                and not self.env.user.has_group(
                    'eh_account_base.group_eh_manager')
            ):
                raise AccessError(_(
                    "Only an Accounting Manager can change whether a live "
                    "close task is required."))
            self._eh_lock_and_assert_run_editable()
        return super().write(vals)

    def unlink(self):
        if (
            not self.env.su
            and not self.env.user.has_group(
                'eh_account_base.group_eh_manager')
        ):
            raise AccessError(_(
                "Only an Accounting Manager can remove a live close task."))
        if not self.env.su:
            self._eh_lock_and_assert_run_editable(operation='unlink')
        return super().unlink()

    def _eh_prepare_action(self):
        self._eh_lock_and_assert_run_editable()
        return self._eh_workflow_action()

    # ---- transitions ----

    def action_start(self):
        self = self._eh_prepare_action()
        for rec in self:
            if rec.state != 'pending':
                continue
            rec.state = 'in_progress'
            if not rec.assigned_user_id:
                rec.assigned_user_id = self.env.user
        return True

    def action_mark_done(self):
        # Skip tasks already in a terminal state. Re-marking a 'done' or
        # 'not_applicable' task as done would overwrite completed_by_id
        # and completed_at, destroying the audit trail. The previous
        # guard checked for a non-existent 'closed' state, so the
        # protection was not actually in effect.
        self = self._eh_prepare_action()
        for rec in self:
            if rec.state in ('done', 'not_applicable'):
                continue
            rec.write({
                'state': 'done',
                'completed_by_id': self.env.user.id,
                'completed_at': fields.Datetime.now(),
                'blocked_reason': False,
            })
        return True

    def action_mark_not_applicable(self):
        self = self._eh_prepare_action()
        for rec in self:
            if rec.state in ('done', 'not_applicable'):
                continue
            if (
                rec.is_required
                and not self.env.user.has_group(
                    'eh_account_base.group_eh_manager')
            ):
                raise UserError(_(
                    "Only an Accounting Manager can mark a required close "
                    "task not applicable."))
            rec.write({
                'state': 'not_applicable',
                'completed_by_id': self.env.user.id,
                'completed_at': fields.Datetime.now(),
                'blocked_reason': False,
            })
        return True

    def action_open_block_wizard(self):
        self.ensure_one()
        self._eh_lock_and_assert_run_editable()
        if self.state in ('done', 'not_applicable'):
            raise UserError(_(
                "Reset a completed task to pending before blocking it."))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Block Close Task"),
            'res_model': 'eh.close.task.block.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_task_id': self.id},
        }

    def action_block(self, reason=None):
        self = self._eh_prepare_action()
        reason = (reason or '').strip()
        if len(reason) < 10:
            raise UserError(_(
                "Explain why the task is blocked using at least 10 "
                "characters."
            ))
        for rec in self:
            if rec.state in ('done', 'not_applicable'):
                raise UserError(_(
                    "Reset a completed task to pending before blocking it."))
            rec.write({
                'state': 'blocked',
                'completed_by_id': False,
                'completed_at': False,
                'blocked_reason': reason,
            })
        return True

    def action_unblock(self):
        self = self._eh_prepare_action()
        for rec in self:
            if rec.state != 'blocked':
                continue
            rec.write({
                'state': 'in_progress',
                'blocked_reason': False,
            })
        return True

    def action_reset_to_pending(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an Accounting Manager can reset a task.",
            ))
        self = self._eh_prepare_action()
        for rec in self:
            rec.write({
                'state': 'pending',
                'completed_by_id': False,
                'completed_at': False,
                'blocked_reason': False,
            })
        return True
