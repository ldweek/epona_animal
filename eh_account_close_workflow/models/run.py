# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.close.run: one execution of a checklist for a period.

Lifecycle:

  open -> in_progress -> pending_approval -> closed
  closed -> reopened -> in_progress (manager only path)

A run cannot move to pending_approval while any required task is still
pending or in_progress. A run cannot close while any task is blocked.
Reopening a closed run does not erase the prepared / reviewed / approved
audit trail; it sets reopened_at and reopened_by alongside.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from dateutil.relativedelta import relativedelta

# Segregation-of-duties anchors. The approve gate reads these to stop a
# preparer or reviewer from approving their own close, so they must be
# tamper-proof. Every identity and timestamp is server-owned: public writes
# cannot assign, reassign, clear, or backdate any anchor.
_SOD_ID_ANCHORS = (
    'prepared_by_id', 'reviewed_by_id', 'approved_by_id', 'reopened_by_id',
)
_SOD_AT_ANCHORS = {
    'prepared_at': 'prepared_by_id',
    'reviewed_at': 'reviewed_by_id',
    'approved_at': 'approved_by_id',
    'reopened_at': 'reopened_by_id',
}

_RUN_SCOPE_FIELDS = {
    'company_id', 'checklist_id', 'period_from', 'period_to',
    'ledger_lock_policy',
}
_RUN_SIGNED_CONTENT_FIELDS = {'name', 'notes'}
_RUN_ENGINE_FIELDS = {'ledger_lock_applied_date'}
_CLOSE_RUN_LOCK_NAMESPACE = 1162036325


class EhCloseRun(models.Model):
    _name = 'eh.close.run'
    _description = "Period close run"
    _order = 'state, period_to desc, id desc'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _rec_name = 'name'
    _check_company_auto = True

    # State is a control-bearing lifecycle: each transition is gated by an
    # action_* method (SoD checks, blocking-check rescan, manager auth). The
    # guard blocks a plain user RPC-writing state to skip those gates; the
    # actions below carry the flag via self._eh_workflow_action().
    _eh_guarded_fields = ('state',)

    name = fields.Char(
        required=True, tracking=True,
        help=(
            "Human-readable label for this close run. Convention: "
            "'<Company> <Period> Close', e.g. 'Acme March 2026 Close'."
        ),
    )

    checklist_id = fields.Many2one(
        'eh.close.checklist',
        required=True,
        check_company=True,
        ondelete='restrict',
        index=True,
        tracking=True,
        help=(
            "Reusable checklist template that seeds the run's task "
            "list. Tasks copied from the template are independent "
            "instances; editing a task on the run does not change "
            "the template."
        ),
    )

    company_id = fields.Many2one(
        'res.company',
        required=True,
        default=lambda self: self.env.company,
        index=True,
        help=(
            "Legal entity this close covers. Each entity has its own "
            "independent close run; multi-entity groups should run one "
            "checklist per company."
        ),
    )

    period_from = fields.Date(
        required=True,
        default=lambda self: fields.Date.context_today(self).replace(day=1),
        help=(
            "Inclusive start date of the period being closed. Defaults "
            "to the first day of the month derived from period_to when "
            "left blank."
        ),
    )
    period_to = fields.Date(
        required=True,
        default=lambda self: fields.Date.context_today(self) + relativedelta(
            day=31,
        ),
        help="Inclusive end date of the period being closed.",
    )
    ledger_lock_policy = fields.Selection(
        [
            ('fiscalyear', "Advance Global Lock Date"),
            ('signoff_only', "Sign-off Only (No Ledger Lock)"),
        ],
        required=True, default='fiscalyear', readonly=True,
        help="Snapshot of checklist ledger-control policy. Approval either "
             "advances company's Global Lock Date through period end or "
             "records sign-off only.",
    )
    ledger_lock_applied_date = fields.Date(
        readonly=True, copy=False, tracking=True,
        help="Effective company Global Lock Date verified atomically when this "
             "run was approved. Empty for Sign-off Only runs.",
    )

    state = fields.Selection(
        [
            ('open', "Open"),
            ('in_progress', "In Progress"),
            ('pending_approval', "Pending Approval"),
            ('closed', "Closed"),
            ('reopened', "Reopened"),
        ],
        default='open',
        required=True,
        tracking=True,
        index=True,
        help=(
            "Lifecycle state. open: just instantiated, no tasks worked. "
            "in_progress: preparer is working through tasks. "
            "pending_approval: a separate reviewer requested manager sign-off; no "
            "more task edits without reopening. closed: approved and "
            "locked. reopened: manager unlocked for further edits; "
            "audit trail of original prepare/review/approve preserved."
        ),
    )

    prepared_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
        help=(
            "First user who transitioned the run to in_progress. "
            "Captured for the segregation-of-duties check at approval "
            "time: a preparer cannot also approve."
        ),
    )
    prepared_at = fields.Datetime(
        readonly=True, copy=False,
        help="Timestamp at which prepared_by_id moved the run to in_progress.",
    )
    reviewed_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
        help=(
            "First user who requested approval. Every later review after a "
            "reopen is preserved in the append-only sign-off events."
        ),
    )
    reviewed_at = fields.Datetime(
        readonly=True, copy=False,
        help="Timestamp at which reviewed_by_id requested approval.",
    )
    approved_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
        help=(
            "First user who approved the close. Every later approval after "
            "a reopen is preserved in the append-only sign-off events."
        ),
    )
    approved_at = fields.Datetime(
        readonly=True, copy=False,
        help="Timestamp at which approved_by_id closed the run.",
    )
    reopened_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
        help=(
            "First user who reopened the run after close. Every later reopen "
            "is preserved in the append-only sign-off events."
        ),
    )
    reopened_at = fields.Datetime(
        readonly=True, copy=False,
        help="Timestamp at which reopened_by_id reopened the closed run.",
    )
    signoff_event_ids = fields.One2many(
        'eh.close.signoff', 'run_id', readonly=True, copy=False,
        help=(
            "Append-only history of every prepare, review, approval, and "
            "reopen event across all close cycles."
        ),
    )

    task_ids = fields.One2many(
        'eh.close.task', 'run_id', copy=False,
        help="Tasks instantiated from the checklist template for this run.",
    )

    task_count = fields.Integer(
        compute='_compute_task_progress',
        help="Total number of tasks instantiated on this run.",
    )
    task_done_count = fields.Integer(
        compute='_compute_task_progress',
        help=(
            "Tasks in done or not_applicable state. Both count as "
            "completed for progress reporting."
        ),
    )
    task_blocked_count = fields.Integer(
        compute='_compute_task_progress',
        help=(
            "Tasks marked blocked. Approval is gated until every "
            "blocked task is resolved or marked not_applicable."
        ),
    )
    task_pending_count = fields.Integer(
        compute='_compute_task_progress',
        help=(
            "Tasks still in pending or in_progress. Required pending "
            "tasks block the request-approval transition."
        ),
    )
    progress_pct = fields.Float(
        compute='_compute_task_progress',
        digits=(5, 2),
        help=(
            "Percentage of tasks completed (done + not_applicable / "
            "total). Used by the kanban progress bar."
        ),
    )

    notes = fields.Html(
        help=(
            "Free-form notes from preparer, reviewer, or approver. "
            "Goes into the audit pack alongside the chatter trail."
        ),
    )
    return_reason = fields.Text(
        copy=False,
        help=(
            "Material reviewer explanation required before a submitted run "
            "can be returned to work. The action copies it into chatter and "
            "then clears this input field."
        ),
    )

    check_ids = fields.One2many(
        'eh.close.check', 'run_id', copy=False,
        help="Results of the most recent automated pre-close scan.",
    )
    has_failed_blocking_checks = fields.Boolean(
        compute='_compute_check_state',
        help="True when a blocking automated check is in 'fail' status.",
    )

    @api.constrains('company_id', 'period_from', 'period_to', 'state')
    def _check_no_overlapping_run(self):
        """Reject a second live close run over the same company + period.

        A period may only have one governing close run in flight. Two
        overlapping runs on one company/period would let two preparers
        sign off the same ledger independently, defeating the close
        governance trail. Overlap is date-range intersection: runs A and
        B collide when A.from <= B.to and B.from <= A.to. Both bounds are
        mandatory, so an undated scratch run can never bypass this control or
        trigger a whole-ledger automated scan.
        """
        for rec in self:
            domain = [
                ('id', '!=', rec.id),
                ('company_id', '=', rec.company_id.id),
                ('period_from', '<=', rec.period_to),
                ('period_to', '>=', rec.period_from),
            ]
            # Only genuine, still-governing runs collide. Every state in
            # this model is live (there is no cancelled/abandoned state),
            # so all of them are considered, including 'closed' (an
            # approved run still owns its period) and 'reopened'.
            # The governing run may be hidden by a narrower record rule.
            # It must still participate in the uniqueness control.
            clash = self.sudo().search(domain, limit=1)
            if clash:
                raise ValidationError(_(
                    "A close run already governs this company and period: "
                    "'%(other)s'. Only one close run may cover a given "
                    "company and period at a time; reopen or supersede the "
                    "existing run instead of creating an overlapping one.",
                    other=clash.display_name,
                ))

    @api.constrains('period_from', 'period_to')
    def _check_period_order(self):
        for rec in self:
            if (
                rec.period_from
                and rec.period_to
                and rec.period_from > rec.period_to
            ):
                raise ValidationError(_(
                    "Close period start date cannot be after end date."))

    @api.depends('check_ids.status', 'check_ids.is_blocking')
    def _compute_check_state(self):
        for rec in self:
            rec.has_failed_blocking_checks = bool(rec.check_ids.filtered(
                lambda c: c.is_blocking and c.status == 'fail',
            ))

    @api.depends('task_ids.state')
    def _compute_task_progress(self):
        for rec in self:
            tasks = rec.task_ids
            total = len(tasks) or 1
            done = len(tasks.filtered(
                lambda t: t.state in ('done', 'not_applicable'),
            ))
            blocked = len(tasks.filtered(
                lambda t: t.state == 'blocked',
            ))
            pending = len(tasks.filtered(
                lambda t: t.state in ('pending', 'in_progress'),
            ))
            rec.task_count = len(tasks)
            rec.task_done_count = done
            rec.task_blocked_count = blocked
            rec.task_pending_count = pending
            rec.progress_pct = round(
                (done / total * 100.0) if tasks else 0.0, 2,
            )

    # ---- segregation-of-duties integrity ----

    @api.constrains('company_id', 'checklist_id')
    def _check_checklist_company(self):
        for rec in self:
            if (
                rec.checklist_id.company_id
                and rec.checklist_id.company_id != rec.company_id
            ):
                raise ValidationError(_(
                    "Checklist '%(checklist)s' belongs to %(check_company)s "
                    "and cannot govern a close run for %(run_company)s.",
                    checklist=rec.checklist_id.display_name,
                    check_company=rec.checklist_id.company_id.display_name,
                    run_company=rec.company_id.display_name,
                ))

    @api.model
    def _eh_lock_companies(self, company_ids):
        """Serialize overlap checks for every affected company."""
        for company_id in sorted({cid for cid in company_ids if cid}):
            self.env.cr.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                (_CLOSE_RUN_LOCK_NAMESPACE, company_id),
            )

    def _eh_lock_for_transition(self, operation='write'):
        """Check caller access, then serialize lifecycle transitions.

        Task actions lock the same parent row. Approval and task mutation
        therefore cannot both pass against stale state in concurrent workers.
        """
        if not self.env.su:
            self._eh_check_access(operation)
        ids = sorted(self.ids)
        if ids:
            self.env.cr.execute(
                "SELECT id FROM eh_close_run "
                "WHERE id IN %s ORDER BY id FOR UPDATE",
                (tuple(ids),),
            )
            self.invalidate_recordset([
                'state', 'company_id', 'checklist_id',
                'period_from', 'period_to',
                *_SOD_ID_ANCHORS, *_SOD_AT_ANCHORS,
            ])
        return self

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            self._eh_check_access('create')
            forged = set().union(*(
                set(vals) & (set(_SOD_ID_ANCHORS) | set(_SOD_AT_ANCHORS))
                for vals in vals_list
            ))
            if forged:
                raise AccessError(_(
                    "Close-run sign-off fields are server-owned and cannot "
                    "be supplied at creation: %s",
                    ', '.join(sorted(forged)),
                ))
        seeded_vals_list = []
        Checklist = self.env['eh.close.checklist']
        for incoming in vals_list:
            vals = dict(incoming)
            # API callers historically passed explicit ``None`` values from
            # action_create_run, which suppresses field defaults. Normalise
            # every shape to a bounded month before constraints and checks.
            period_from = fields.Date.to_date(vals.get('period_from'))
            period_to = fields.Date.to_date(vals.get('period_to'))
            if not period_from and not period_to:
                today = fields.Date.context_today(self)
                period_from = today.replace(day=1)
                period_to = today + relativedelta(day=31)
            elif not period_from:
                period_from = period_to.replace(day=1)
            elif not period_to:
                period_to = period_from + relativedelta(day=31)
            vals.update({
                'period_from': period_from,
                'period_to': period_to,
            })
            if vals.get('checklist_id') and (
                not vals.get('task_ids') or not self.env.su
            ):
                checklist = Checklist.browse(
                    vals['checklist_id'],
                ).exists()
                if len(checklist) != 1:
                    raise UserError(_("Select one valid close checklist."))
                if not self.env.su:
                    checklist._eh_check_access('read')
                if not checklist.task_template_ids:
                    raise UserError(_(
                        "Checklist '%s' has no task templates; cannot start "
                        "a run.", checklist.display_name,
                    ))
                if not self.env.su:
                    vals['ledger_lock_policy'] = (
                        checklist.ledger_lock_policy
                    )
                vals['task_ids'] = [
                    (0, 0, {
                        'sequence': template.sequence,
                        'name': template.name,
                        'description': template.description,
                        'responsible_role': template.responsible_role,
                        'is_required': template.is_required,
                    })
                    for template in checklist.task_template_ids.sorted(
                        lambda template: (template.sequence, template.id)
                    )
                ]
            seeded_vals_list.append(vals)
        self._eh_lock_companies(
            vals.get('company_id') or self.env.company.id
            for vals in seeded_vals_list
        )
        return super().create(seeded_vals_list)

    def write(self, vals):
        if not self.env.su:
            self._eh_check_access('write')
            forged = set(vals) & (
                set(_SOD_ID_ANCHORS) | set(_SOD_AT_ANCHORS)
                | _RUN_ENGINE_FIELDS
            )
            if forged:
                raise AccessError(_(
                    "Close-run sign-off fields are server-owned and cannot "
                    "be edited directly: %s",
                    ', '.join(sorted(forged)),
                ))
            if (
                'ledger_lock_policy' in vals
                and not self.env.user.has_group(
                    'eh_account_base.group_eh_manager')
            ):
                raise AccessError(_(
                    "Only an Accounting Manager can change a close run's "
                    "ledger-control policy before work starts."))
            scope_changes = set(vals) & _RUN_SCOPE_FIELDS
            if scope_changes and self.filtered(lambda rec: rec.state != 'open'):
                raise UserError(_(
                    "Company, checklist, and period become immutable once a "
                    "close run starts. Recreate an unfinished run to correct "
                    "its scope."))
            signed_changes = set(vals) & _RUN_SIGNED_CONTENT_FIELDS
            if signed_changes and self.filtered(
                lambda rec: rec.state in ('pending_approval', 'closed')
            ):
                raise UserError(_(
                    "Submitted or closed run content is immutable. Reopen the "
                    "run before adding working notes."))
        affected_companies = set(self.mapped('company_id').ids)
        if vals.get('company_id'):
            affected_companies.add(vals['company_id'])
        if set(vals) & _RUN_SCOPE_FIELDS:
            self._eh_lock_companies(affected_companies)
        return super().write(vals)

    def unlink(self):
        self._eh_lock_for_transition(operation='unlink')
        if not self.env.su and self.filtered(lambda rec: rec.state != 'open'):
            raise UserError(_(
                "A close run cannot be deleted after work starts; its audit "
                "history must remain available."))
        return super().unlink()

    # ---- onchange (live form feedback) ----

    @api.onchange('period_to')
    def _onchange_period_to_derive_from(self):
        """Default period_from to the first day of the same month
        when the user picks period_to and hasn't set period_from yet.

        Most close runs cover a single month or quarter; this saves
        a click for the common case while leaving custom ranges
        intact (we only fill an empty period_from).
        """
        for rec in self:
            if not rec.period_to:
                continue
            if rec.period_from and rec.period_from <= rec.period_to:
                continue
            rec.period_from = rec.period_to.replace(day=1)

    # ---- lifecycle ----

    def _eh_record_signoff(self, event_type, event_at):
        self.ensure_one()
        return self.env['eh.close.signoff'].sudo().create({
            'run_id': self.id,
            'event_type': event_type,
            'user_id': self.env.user.id,
            'event_at': event_at,
        })

    def _eh_current_cycle_signers(self):
        """Return latest preparer/reviewer after most recent reopen."""
        self.ensure_one()
        events = self.signoff_event_ids.sorted('id')
        reopens = events.filtered(lambda event: event.event_type == 'reopen')
        cutoff = reopens[-1].id if reopens else 0
        cycle = events.filtered(lambda event: event.id > cutoff)
        prepares = cycle.filtered(lambda event: event.event_type == 'prepare')
        reviews = cycle.filtered(lambda event: event.event_type == 'review')
        return (
            prepares[-1].user_id if prepares else self.prepared_by_id,
            reviews[-1].user_id if reviews else self.reviewed_by_id,
        )

    def _eh_apply_ledger_lock(self):
        """Apply configured ledger control before recording closed state."""
        self.ensure_one()
        if self.ledger_lock_policy == 'signoff_only':
            return False
        if self.ledger_lock_policy != 'fiscalyear':
            raise UserError(_(
                "Unsupported close-run ledger-control policy: %s.",
                self.ledger_lock_policy,
            ))
        if not self.period_to:
            raise UserError(_(
                "A Global Lock close requires an explicit period end date."))
        if self.period_to > fields.Date.context_today(self):
            raise UserError(_(
                "Cannot close and lock a future period ending %s.",
                self.period_to,
            ))
        company = self.company_id
        self.env.cr.execute(
            "SELECT id FROM res_company WHERE id = %s FOR UPDATE",
            [company.id],
        )
        company.invalidate_recordset(['fiscalyear_lock_date'])
        target = max(
            company.fiscalyear_lock_date or self.period_to,
            self.period_to,
        )
        if company.fiscalyear_lock_date != target:
            company.write({'fiscalyear_lock_date': target})
        company.invalidate_recordset(['fiscalyear_lock_date'])
        if not company.fiscalyear_lock_date \
                or company.fiscalyear_lock_date < self.period_to:
            raise UserError(_(
                "Company Global Lock Date did not advance through %s; close "
                "approval was rolled back.",
                self.period_to,
            ))
        self.write({
            'ledger_lock_applied_date': company.fiscalyear_lock_date,
        })
        return company.fiscalyear_lock_date

    def action_start(self):
        self._eh_lock_for_transition()
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state not in ('open', 'reopened'):
                continue
            signed_at = fields.Datetime.now()
            rec.write({
                'state': 'in_progress',
                'prepared_by_id': (
                    rec.prepared_by_id.id or self.env.user.id
                ),
                'prepared_at': rec.prepared_at or signed_at,
            })
            rec._eh_record_signoff('prepare', signed_at)
        return True

    def action_request_approval(self):
        self._eh_lock_for_transition()
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state not in ('in_progress',):
                raise UserError(_(
                    "Can only request approval from in progress.",
                ))
            blocking = rec.task_ids.filtered(
                lambda t: t.is_required and t.state in ('pending', 'in_progress'),
            )
            if blocking:
                raise UserError(_(
                    "Cannot request approval: %d required task(s) still pending: %s",
                ) % (
                    len(blocking),
                    ', '.join(blocking.mapped('name')[:5]),
                ))
            current_preparer, _current_reviewer = (
                rec._eh_current_cycle_signers()
            )
            if not current_preparer:
                raise UserError(_(
                    "Start the run before review so its preparer is recorded."
                ))
            if current_preparer.id == self.env.user.id:
                raise UserError(_(
                    "Segregation of duties: %(preparer)s prepared this run "
                    "and cannot also review it. A different accounting user "
                    "must request approval.",
                    preparer=current_preparer.display_name,
                ))
            # Recompute the automated checks against the live ledger at the
            # gate, so a stale or hand-tampered stored check row cannot pass
            # approval while the period still has draft / unbalanced / unhashed
            # entries. The freshly written rows drive has_failed_blocking_checks.
            rec._eh_refresh_checks()
            rec.invalidate_recordset([
                'check_ids', 'has_failed_blocking_checks',
            ])
            if rec.has_failed_blocking_checks:
                failed = rec.check_ids.filtered(
                    lambda c: c.is_blocking and c.status == 'fail')
                raise UserError(_(
                    "Cannot request approval: automated close checks "
                    "failed: %s. Resolve them and re-run the checks.",
                    ', '.join(failed.mapped('name')),
                ))
            signed_at = fields.Datetime.now()
            rec.write({
                'state': 'pending_approval',
                'reviewed_by_id': rec.reviewed_by_id.id or self.env.user.id,
                'reviewed_at': rec.reviewed_at or signed_at,
            })
            rec._eh_record_signoff('review', signed_at)
        return True

    def action_approve(self):
        # Authorization gate: closing a period is a manager-only control.
        # This sits ahead of the different-person SoD checks so a
        # non-manager who was neither preparer nor reviewer still cannot
        # approve and close a period. The suite anchors this control on
        # eh_account_base.group_eh_manager.
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an Accounting Manager can approve and close a run.",
            ))
        self._eh_lock_for_transition()
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state != 'pending_approval':
                raise UserError(_(
                    "Can only approve from pending approval.",
                ))
            current_preparer, current_reviewer = (
                rec._eh_current_cycle_signers()
            )
            # Segregation of duties uses current cycle events, while original
            # first-cycle anchors remain immutable for compatibility.
            if (
                current_reviewer
                and current_reviewer.id == self.env.user.id
            ):
                raise UserError(_(
                    "Segregation of duties: %s requested this approval "
                    "and cannot also approve it. Another manager must "
                    "review this close run.",
                    current_reviewer.display_name,
                ))
            # Same guard against current-cycle preparer self-approving.
            if (
                current_preparer
                and current_preparer.id == self.env.user.id
            ):
                raise UserError(_(
                    "Segregation of duties: %s prepared this run and "
                    "cannot approve their own work.",
                    current_preparer.display_name,
                ))
            blocked = rec.task_ids.filtered(lambda t: t.state == 'blocked')
            if blocked:
                raise UserError(_(
                    "Cannot close: %d task(s) blocked: %s",
                ) % (
                    len(blocked),
                    ', '.join(blocked.mapped('name')[:5]),
                ))
            # Ledger state may have changed after approval was requested.
            # Re-scan while holding the run lock and refuse stale approval.
            rec._eh_refresh_checks()
            rec.invalidate_recordset([
                'check_ids', 'has_failed_blocking_checks',
            ])
            if rec.has_failed_blocking_checks:
                failed = rec.check_ids.filtered(
                    lambda c: c.is_blocking and c.status == 'fail')
                raise UserError(_(
                    "Cannot approve: automated close checks failed: %s. "
                    "Resolve them before closing the period.",
                    ', '.join(failed.mapped('name')),
                ))
            # Requesting review must not lock the ledger. Apply the company
            # lock atomically with the manager's final close approval.
            rec._eh_apply_ledger_lock()
            signed_at = fields.Datetime.now()
            rec.write({
                'state': 'closed',
                'approved_by_id': rec.approved_by_id.id or self.env.user.id,
                'approved_at': rec.approved_at or signed_at,
            })
            rec._eh_record_signoff('approve', signed_at)
        return True

    def action_return_to_work(self):
        """Withdraw submitted evidence so blocked work can be resolved."""
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an Accounting Manager can return a submitted run "
                "to work.",
            ))
        self._eh_lock_for_transition()
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state != 'pending_approval':
                raise UserError(_(
                    "Only a pending-approval run can be returned to work.",
                ))
            reason = (rec.return_reason or '').strip()
            if len(reason) < 10:
                raise UserError(_(
                    "Enter a material return reason of at least 10 "
                    "characters before returning this run to work."
                ))
            returned_at = fields.Datetime.now()
            rec.write({'state': 'in_progress', 'return_reason': False})
            rec._eh_record_signoff('return_to_work', returned_at)
            rec.message_post(body=_(
                "Submitted close run returned to work by %(reviewer)s. "
                "Reason: %(reason)s. Existing review evidence remains in "
                "sign-off history.",
                reviewer=self.env.user.display_name,
                reason=reason,
            ))
        return True

    def action_reopen(self):
        # Authorization gate: reopening a closed period is a manager-only
        # control and must be anchored on the suite manager group, not the
        # core account manager group. A non-EH account manager must not be
        # able to reopen a period the EH controls closed.
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an Accounting Manager can reopen a closed run.",
            ))
        self._eh_lock_for_transition()
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state != 'closed':
                raise UserError(_(
                    "Can only reopen a closed run.",
                ))
            signed_at = fields.Datetime.now()
            rec.write({
                'state': 'reopened',
                'reopened_by_id': rec.reopened_by_id.id or self.env.user.id,
                'reopened_at': rec.reopened_at or signed_at,
            })
            rec._eh_record_signoff('reopen', signed_at)
            if rec.ledger_lock_applied_date:
                rec.message_post(body=_(
                    "Close workflow reopened by %s. Company Global Lock Date "
                    "remains %s; use a controlled Odoo lock exception for "
                    "authorised adjustments instead of silently lowering it.",
                    self.env.user.display_name,
                    rec.company_id.fiscalyear_lock_date,
                ))
        return True

    # ---- automated pre-close checks ----

    def action_run_checks(self):
        """Rescan the ledger for the run's period and refresh check rows.

        Rows are system-written. Caller must hold write access to the run;
        submitted and closed evidence can only be refreshed by the approval
        gate itself.
        """
        self._eh_lock_for_transition()
        for rec in self.sudo():
            if rec.state in ('pending_approval', 'closed'):
                raise UserError(_(
                    "Submitted or closed check evidence cannot be refreshed "
                    "outside the approval workflow."))
            rec._eh_refresh_checks()
        return True

    def _eh_refresh_checks(self):
        """Replace check evidence from live ledger data under server trust."""
        Check = self.env['eh.close.check'].sudo()
        for rec in self.sudo():
            rec.check_ids.sudo().unlink()
            for vals in rec._eh_compute_checks():
                Check.create(dict(vals, run_id=rec.id))

    def _eh_compute_checks(self):
        """Return a list of check-result dicts for this run's period."""
        self.ensure_one()
        return [
            self._eh_check_draft_entries(),
            self._eh_check_unbalanced_entries(),
            self._eh_check_open_suspense(),
        ]

    def _eh_period_move_domain(self, state=None):
        domain = [
            ('company_id', '=', self.company_id.id),
            ('date', '>=', self.period_from),
            ('date', '<=', self.period_to),
        ]
        if state:
            domain.append(('state', '=', state))
        return domain

    def _eh_check_draft_entries(self):
        count = self.env['account.move'].search_count(
            self._eh_period_move_domain(state='draft'))
        return {
            'code': 'draft_entries',
            'name': _("Draft journal entries in period"),
            'status': 'fail' if count else 'pass',
            'count': count,
            'is_blocking': True,
            'detail': (_("%d draft entries must be posted or deleted "
                         "before close.") % count) if count else '',
        }

    def _eh_check_unbalanced_entries(self):
        # Normal Odoo posting refuses an unbalanced move. This check is for
        # exceptional/imported evidence, so aggregate in PostgreSQL and only
        # return the anomaly count; never materialise every move and line in
        # the period in Python. The threshold mirrors currency.is_zero.
        self.env.cr.execute(
            """
            SELECT COUNT(*)
              FROM (
                    SELECT move.id
                      FROM account_move AS move
                      JOIN account_move_line AS line
                        ON line.move_id = move.id
                     WHERE move.company_id = %s
                       AND move.state = 'posted'
                       AND move.date >= %s
                       AND move.date <= %s
                     GROUP BY move.id
                    HAVING ABS(SUM(line.balance)) >= %s
                   ) AS exceptional_unbalanced_moves
            """,
            (
                self.company_id.id,
                self.period_from,
                self.period_to,
                self.company_id.currency_id.rounding / 2,
            ),
        )
        count = self.env.cr.fetchone()[0]
        return {
            'code': 'unbalanced_entries',
            'name': _("Unbalanced posted entries"),
            'status': 'fail' if count else 'pass',
            'count': count,
            'is_blocking': True,
            'detail': (_("%d posted entries do not balance to zero.")
                       % count) if count else '',
        }

    def _eh_check_open_suspense(self):
        journals = self.env['account.journal'].search([
            ('type', 'in', ('bank', 'cash')),
            ('company_id', '=', self.company_id.id),
        ])
        suspense = journals.mapped('suspense_account_id')
        count = 0
        if suspense:
            domain = [
                ('account_id', 'in', suspense.ids),
                ('reconciled', '=', False),
                ('parent_state', '=', 'posted'),
                ('company_id', '=', self.company_id.id),
            ]
            if self.period_to:
                domain.append(('date', '<=', self.period_to))
            count = self.env['account.move.line'].search_count(domain)
        return {
            'code': 'open_suspense',
            'name': _("Open bank/cash suspense lines"),
            'status': 'warn' if count else 'pass',
            'count': count,
            'is_blocking': False,
            'detail': (_("%d statement lines remain in suspense.")
                       % count) if count else '',
        }

    def action_view_tasks(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Tasks"),
            'res_model': 'eh.close.task',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('run_id', '=', self.id)],
            'context': {'default_run_id': self.id},
        }
