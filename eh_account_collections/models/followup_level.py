# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.collections.followup.level: ordered escalation levels.

A level captures one rung of the dunning ladder: how overdue must a case
be to trigger this level, what action the system takes (send a templated
email, schedule a call activity, escalate to legal), and how long to
wait before the next level kicks in.

The cron _cron_send_followups on eh.collections.case picks the highest
applicable level for each case (based on days_overdue_max) and applies
its action. The case stores current_followup_level_id so re-runs do not
duplicate sends and the next level always advances forward.

Levels are per-company so different subsidiaries can run different
dunning policies without contention.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class EhCollectionsFollowupLevel(models.Model):
    _name = 'eh.collections.followup.level'
    _description = "Collections follow-up level"
    _order = 'sequence, days_overdue, id'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(
        default=10,
        help="Drag to reorder. Lower numbers fire first.",
    )
    active = fields.Boolean(default=True)

    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
        index=True,
    )

    days_overdue = fields.Integer(
        required=True,
        help=(
            "Trigger this level when the case's days_overdue_max is at "
            "least this many days. Levels fire in ascending order; a "
            "case at 45 days overdue with levels at 7 / 30 / 60 sits on "
            "the 30 level until it crosses 60."
        ),
    )

    action_type = fields.Selection(
        [
            ('email', "Send templated email"),
            ('email_and_activity', "Send email and schedule a call activity"),
            ('activity', "Schedule a call activity (no email)"),
            ('escalate', "Move case to Escalated stage (no email)"),
        ],
        required=True, default='email',
    )

    mail_template_id = fields.Many2one(
        'mail.template',
        domain="[('model', '=', 'eh.collections.case')]",
        help=(
            "Mail template used when action_type involves email. Templates "
            "must be bound to eh.collections.case so {{ object.* }} "
            "expressions resolve to the case."
        ),
    )

    also_send_sms = fields.Boolean(
        string="Also send SMS",
        help=(
            "When set, an SMS rendered from the SMS template is sent to "
            "the partner's mobile alongside whatever the action type "
            "does, giving multi-channel dunning."
        ),
    )
    sms_template_id = fields.Many2one(
        'sms.template',
        domain="[('model', '=', 'eh.collections.case')]",
        help=(
            "SMS template used when 'Also send SMS' is on. Bound to "
            "eh.collections.case so {{ object.* }} resolves to the case."
        ),
    )
    responsible_type = fields.Selection(
        [
            ('collector', "Case collector"),
            ('salesperson', "Customer salesperson"),
            ('account_manager', "Accounting manager"),
        ],
        default='collector', required=True,
        string="Activity responsible",
        help=(
            "Who the scheduled follow-up activity is assigned to: the "
            "case collector, the customer's salesperson, or an "
            "accounting manager."
        ),
    )

    activity_type_id = fields.Many2one(
        'mail.activity.type',
        help=(
            "Activity type to schedule when action_type involves an "
            "activity. Defaults to 'Call' when not set."
        ),
    )
    activity_summary = fields.Char(
        translate=True,
        help="Default summary for the scheduled activity.",
    )
    activity_offset_days = fields.Integer(
        default=2,
        help=(
            "Days from today to set the activity due date. 0 means today; "
            "2 means the day after tomorrow."
        ),
    )

    delay_days = fields.Integer(
        default=lambda self: (
            self.env.company.eh_collections_grace_days or 14
        ),
        help=(
            "Days to wait after this level fires before advancing to the "
            "next level. Used to compute the case's next_followup_date."
        ),
    )
    is_terminal = fields.Boolean(
        default=False,
        help=(
            "Mark the last level in the chain. Cases on a terminal level "
            "are not auto-advanced; the cron stops sending follow-ups for "
            "them and a collector must intervene."
        ),
    )

    escalate_to_stage_id = fields.Many2one(
        'eh.collections.stage',
        help=(
            "When action_type is 'escalate', the case moves to this stage. "
            "A target is mandatory so escalation can never silently route "
            "a case into a disputed workflow."
        ),
    )

    notes = fields.Text()

    _check_days_non_negative = models.Constraint(
        'CHECK (days_overdue >= 0)',
        'days_overdue cannot be negative.',
    )
    _check_delay_non_negative = models.Constraint(
        'CHECK (delay_days >= 0)',
        'delay_days cannot be negative.',
    )

    @api.constrains('action_type', 'mail_template_id')
    def _check_email_template(self):
        for rec in self:
            if rec.action_type in ('email', 'email_and_activity') and not rec.mail_template_id:
                raise ValidationError(_(
                    "Level %(name)s sends email but has no mail_template_id "
                    "configured.",
                    name=rec.name,
                ))

    @api.constrains('action_type', 'escalate_to_stage_id')
    def _check_escalation_stage(self):
        for rec in self:
            if rec.action_type == 'escalate' and not rec.escalate_to_stage_id:
                raise ValidationError(_(
                    "Escalation level %(name)s needs an explicit target "
                    "stage.",
                    name=rec.name,
                ))

    @api.constrains('also_send_sms', 'sms_template_id')
    def _check_sms_template(self):
        for rec in self:
            if rec.also_send_sms and not rec.sms_template_id:
                raise ValidationError(_(
                    "Level %(name)s enables SMS but has no SMS template.",
                    name=rec.name,
                ))
            if (
                rec.sms_template_id
                and rec.sms_template_id.model != 'eh.collections.case'
            ):
                raise ValidationError(_(
                    "Level %(name)s SMS template must target "
                    "eh.collections.case.",
                    name=rec.name,
                ))

    @api.constrains('mail_template_id')
    def _check_template_model(self):
        for rec in self:
            if rec.mail_template_id and rec.mail_template_id.model != 'eh.collections.case':
                raise ValidationError(_(
                    "Level %(name)s template must target "
                    "eh.collections.case (got %(model)r).",
                    name=rec.name,
                    model=rec.mail_template_id.model,
                ))
