# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.recurring.invoice.template: recurring customer invoice template.

A template binds a customer, a journal, a recurrence cadence, and a list
of lines. The cron _cron_generate_due dispatches all active templates
whose next_run is past, generating one draft customer invoice per
template per pass. Generated invoices stay in draft unless auto_post is
enabled.

Lifecycle: draft -> active -> paused -> active -> finished. Active is
the only state the cron picks up. Finished is terminal: a template
finishes when end_date is past or when count_total has been reached.
"""

import calendar
import logging
import re
from datetime import timedelta

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)


_CODE_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]*$')
RECURRING_MOVE_CONTEXT = 'eh_recurring_move_capability'
RECURRING_MOVE_CAPABILITY = object()


class EhRecurringInvoiceTemplate(models.Model):
    _name = 'eh.recurring.invoice.template'
    _description = "Recurring invoice template"
    _order = 'state, next_run_date asc, name'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _check_company_auto = True
    _CRON_BATCH_SIZE = 50

    # The lifecycle state may only change through the record's own actions
    # (which run under sudo), never a direct RPC/ORM write. Without this a
    # plain user could write({'state': 'active'}) to bypass action_activate's
    # line-present check, or write({'state': 'finished'}) to relabel the
    # record. _eh_guarded_fields is just ('state',) so the default create-side
    # stripping applies; no _eh_create_guarded_fields override is needed.
    _eh_guarded_fields = (
        'state', 'count_generated', 'last_run_at',
        'last_generated_move_id', 'last_error',
        'eh_legacy_integrity_unverified',
        'eh_legacy_integrity_reason',
        'eh_legacy_reviewed_at', 'eh_legacy_reviewed_by_id',
    )

    name = fields.Char(required=True, tracking=True)
    code = fields.Char(required=True, copy=False)

    active = fields.Boolean(default=True)
    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('active', "Active"),
            ('paused', "Paused"),
            ('finished', "Finished"),
        ],
        required=True,
        default='draft',
        tracking=True,
        index=True,
    )

    company_id = fields.Many2one(
        'res.company',
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string="Transaction Currency",
        required=True,
        default=lambda self: self.env.company.currency_id,
        tracking=True,
        help=(
            "Currency used on generated customer invoices. It is frozen "
            "after the template owns accounting history."
        ),
    )
    company_currency_id = fields.Many2one(
        related='company_id.currency_id',
        string="Company Currency",
        store=True,
        readonly=True,
    )

    partner_id = fields.Many2one(
        'res.partner',
        required=True,
        index=True,
        tracking=True,
        check_company=True,
    )
    journal_id = fields.Many2one(
        'account.journal',
        required=True,
        domain="[('type', '=', 'sale')]",
        tracking=True,
        check_company=True,
    )
    payment_term_id = fields.Many2one(
        'account.payment.term', check_company=True)
    fiscal_position_id = fields.Many2one(
        'account.fiscal.position', check_company=True)

    line_ids = fields.One2many(
        'eh.recurring.invoice.line',
        'template_id',
        copy=True,
    )

    interval = fields.Integer(
        required=True,
        default=1,
        help="Number of interval_unit periods between generations.",
    )
    interval_unit = fields.Selection(
        [
            ('day', "Day(s)"),
            ('week', "Week(s)"),
            ('month', "Month(s)"),
            ('quarter', "Quarter(s)"),
            ('year', "Year(s)"),
        ],
        default='month',
        required=True,
    )

    start_date = fields.Date(required=True, default=fields.Date.context_today)
    next_run_date = fields.Date(required=True, default=fields.Date.context_today)
    billing_day = fields.Integer(
        required=True,
        default=lambda self: fields.Date.context_today(self).day,
        help=(
            "Day of month used by monthly, quarterly and yearly schedules. "
            "It is initialised from Next Run Date, and is restored after a "
            "short month clamps a run to its final day."
        ),
    )
    end_date = fields.Date(
        help=(
            "Optional. When set, the template finishes after generating "
            "the last invoice on or before this date."
        ),
    )
    count_total = fields.Integer(
        default=0,
        help=(
            "Optional cap on total invoices. 0 means no cap. "
            "When set, the template finishes after generating "
            "count_total invoices."
        ),
    )
    count_generated = fields.Integer(default=0, readonly=True)
    count_remaining = fields.Integer(compute='_compute_count_remaining')

    auto_post = fields.Boolean(
        default=False,
        help=(
            "When enabled, generated invoices are posted automatically. "
            "Default off so the accountant reviews each draft."
        ),
    )

    ref = fields.Char(help="Default reference applied to generated invoices.")
    narration = fields.Html(help="Default note applied to generated invoices.")

    last_run_at = fields.Datetime(readonly=True)
    last_generated_move_id = fields.Many2one(
        'account.move', readonly=True, ondelete='restrict',
    )
    last_error = fields.Text(readonly=True)
    eh_legacy_integrity_unverified = fields.Boolean(
        string="Legacy Integrity Review Required",
        default=False,
        readonly=True,
        copy=False,
        index=True,
        help=(
            "Set by an upgrade when recurring workflow, counter, service-"
            "period, or journal-link evidence predates durable provenance. "
            "The record is preserved but all mutations and billing actions "
            "remain blocked."
        ),
    )
    eh_legacy_integrity_reason = fields.Text(
        string="Legacy Integrity Review Reason",
        readonly=True,
        copy=False,
    )
    eh_legacy_review_note = fields.Text(
        string="Legacy Review Attestation",
        copy=False,
        help=(
            "Manager explanation of source records checked and corrective "
            "setup required before future billing resumes. Historical "
            "quarantined moves are never promoted to verified evidence."
        ),
    )
    eh_legacy_reviewed_at = fields.Datetime(readonly=True, copy=False)
    eh_legacy_reviewed_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
    )

    generated_move_ids = fields.One2many(
        'account.move', 'eh_recurring_template_id', readonly=True,
    )
    generated_count = fields.Integer(compute='_compute_generated_count')

    recurring_subtotal = fields.Monetary(
        compute='_compute_mrr', currency_field='currency_id',
        help="Untaxed recurring amount per generation (sum of line "
             "subtotals).",
    )
    mrr = fields.Monetary(
        compute='_compute_mrr', currency_field='currency_id',
        help="Monthly recurring revenue: the recurring amount normalised "
             "to a calendar month. Zero unless the template is active.",
    )
    arr = fields.Monetary(
        compute='_compute_mrr', currency_field='currency_id',
        help="Annualised recurring revenue (12 x MRR).",
    )
    recurring_subtotal_company = fields.Monetary(
        compute='_compute_mrr', currency_field='company_currency_id',
        string="Recurring Subtotal (Company Currency)",
        help=(
            "Untaxed recurring amount converted to company currency at the "
            "next billing date for comparable reporting."
        ),
    )
    mrr_company = fields.Monetary(
        compute='_compute_mrr', currency_field='company_currency_id',
        string="MRR (Company Currency)",
        help="MRR converted to company currency for aggregation.",
    )
    arr_company = fields.Monetary(
        compute='_compute_mrr', currency_field='company_currency_id',
        string="ARR (Company Currency)",
        help="ARR converted to company currency for aggregation.",
    )

    # Calendar months represented by one unit of each cadence. Day and
    # week use the mean month length (365.25 / 12) so a weekly plan
    # annualises correctly.
    _MONTHS_PER_UNIT = {
        'day': 1.0 / 30.4375,
        'week': 7.0 / 30.4375,
        'month': 1.0,
        'quarter': 3.0,
        'year': 12.0,
    }

    @api.depends(
        'line_ids.subtotal', 'interval', 'interval_unit', 'state',
        'currency_id', 'company_id.currency_id', 'next_run_date',
    )
    def _compute_mrr(self):
        for rec in self:
            subtotal = sum(rec.line_ids.mapped('subtotal'))
            rec.recurring_subtotal = subtotal
            months = (rec.interval or 0) * rec._MONTHS_PER_UNIT.get(
                rec.interval_unit, 1.0)
            monthly = (
                subtotal / months
                if (rec.state == 'active' and months) else 0.0
            )
            rec.mrr = monthly
            rec.arr = monthly * 12.0
            company_currency = rec.company_currency_id
            transaction_currency = rec.currency_id
            conversion_date = (
                rec.next_run_date or fields.Date.context_today(rec)
            )
            if not transaction_currency or not company_currency:
                company_subtotal = company_monthly = company_arr = 0.0
            elif transaction_currency == company_currency:
                company_subtotal = subtotal
                company_monthly = monthly
                company_arr = monthly * 12.0
            else:
                company_subtotal = transaction_currency._convert(
                    subtotal, company_currency, rec.company_id,
                    conversion_date,
                )
                company_monthly = transaction_currency._convert(
                    monthly, company_currency, rec.company_id,
                    conversion_date,
                )
                company_arr = transaction_currency._convert(
                    monthly * 12.0, company_currency, rec.company_id,
                    conversion_date,
                )
            rec.recurring_subtotal_company = company_subtotal
            rec.mrr_company = company_monthly
            rec.arr_company = company_arr

    @api.model
    def eh_total_mrr(self, company=None):
        """Total monthly recurring revenue across active templates."""
        company = company or self.env.company
        templates = self.search([
            ('state', '=', 'active'),
            ('company_id', '=', company.id),
        ])
        return sum(templates.mapped('mrr_company'))

    _unique_code_per_company = models.Constraint(
        'unique(code, company_id)',
        'Template code must be unique per company.',
    )
    _positive_interval = models.Constraint(
        'check(interval > 0)',
        'Interval must be a positive integer.',
    )
    _count_total_non_negative = models.Constraint(
        'check(count_total >= 0)',
        'count_total must be zero or positive.',
    )
    _billing_day_range = models.Constraint(
        'check(billing_day >= 1 AND billing_day <= 31)',
        'Billing day must be between 1 and 31.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        today = fields.Date.context_today(self)
        for incoming in vals_list:
            vals = dict(incoming or {})
            if not vals.get('currency_id'):
                company = self.env['res.company'].browse(
                    vals.get('company_id')
                ).exists() or self.env.company
                vals['currency_id'] = company.currency_id.id
            if not vals.get('billing_day'):
                run_date = fields.Date.to_date(
                    vals.get('next_run_date') or today,
                )
                vals['billing_day'] = run_date.day
            prepared.append(vals)
        return super().create(prepared)

    def _eh_next_copy_code(self):
        """Return a valid, company-scoped code for a duplicated template."""
        self.ensure_one()
        base = "%s_copy" % self.code
        candidate = base
        suffix = 2
        Template = self.with_context(active_test=False)
        while Template.search_count([
            ('company_id', '=', self.company_id.id),
            ('code', '=', candidate),
        ], limit=1):
            candidate = "%s_%s" % (base, suffix)
            suffix += 1
        return candidate

    def copy(self, default=None):
        """Duplicate into a new draft setup with fresh server evidence."""
        self.ensure_one()
        values = dict(default or {})
        values.setdefault('code', self._eh_next_copy_code())
        values.update({
            'state': 'draft',
            'count_generated': 0,
            'last_run_at': False,
            'last_generated_move_id': False,
            'last_error': False,
            'eh_legacy_integrity_unverified': False,
            'eh_legacy_integrity_reason': False,
            'eh_legacy_review_note': False,
            'eh_legacy_reviewed_at': False,
            'eh_legacy_reviewed_by_id': False,
        })
        return super().copy(values)

    @api.constrains('code')
    def _check_code_format(self):
        for rec in self:
            if not _CODE_RE.match(rec.code or ''):
                raise ValidationError(_(
                    "Template code must match [a-zA-Z][a-zA-Z0-9_]* (got %r).",
                ) % rec.code)

    # ---- onchange (live form feedback) ----

    @api.onchange('partner_id')
    def _onchange_partner_id_terms(self):
        """Pre-fill payment_term and fiscal_position from the
        partner's commercial entity when picked.

        Without this the user has to copy the partner's terms onto
        every template by hand. With it, picking the partner is
        usually enough.
        """
        for rec in self:
            if not rec.partner_id:
                continue
            commercial = rec.partner_id.commercial_partner_id or rec.partner_id
            if not rec.payment_term_id and commercial.property_payment_term_id:
                rec.payment_term_id = commercial.property_payment_term_id
            if not rec.fiscal_position_id and commercial.property_account_position_id:
                rec.fiscal_position_id = commercial.property_account_position_id

    @api.onchange('next_run_date')
    def _onchange_next_run_date_billing_day(self):
        for rec in self:
            if rec.next_run_date:
                rec.billing_day = rec.next_run_date.day

    @api.onchange('interval', 'interval_unit')
    def _onchange_interval_warn_long(self):
        """Soft-warn the user when they configure a recurrence
        longer than 12 months. A 5-year-monthly schedule is
        typically a typo (interval_unit set to year instead of
        month).
        """
        for rec in self:
            if not (rec.interval and rec.interval_unit):
                continue
            months_equiv = rec.interval * (
                12 if rec.interval_unit == 'year'
                else 3 if rec.interval_unit == 'quarter'
                else 1 if rec.interval_unit == 'month'
                else 0
            )
            if months_equiv > 12:
                return {
                    'warning': {
                        'title': "Long recurrence",
                        'message': (
                            "Recurrence is %s %s - this generates a "
                            "schedule longer than a year between "
                            "invoices. Verify this is intentional." % (
                                rec.interval, rec.interval_unit,
                            )
                        ),
                    }
                }

    @api.depends('count_total', 'count_generated')
    def _compute_count_remaining(self):
        for rec in self:
            if rec.count_total:
                rec.count_remaining = max(
                    rec.count_total - rec.count_generated, 0,
                )
            else:
                rec.count_remaining = 0

    @api.depends('generated_move_ids')
    def _compute_generated_count(self):
        for rec in self:
            rec.generated_count = len(rec.generated_move_ids)

    def _eh_lock_for_integrity(self):
        """Serialize configuration/lifecycle edits with invoice generation."""
        if not self.env.su:
            self._eh_check_access('write')
        ids = sorted(self.ids)
        if ids:
            self.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM eh_recurring_invoice_template "
                "WHERE id IN %s ORDER BY id FOR UPDATE",
                (tuple(ids),),
            )
            self.invalidate_recordset()
            self._check_company()
        return self

    def _eh_history_frozen_fields(self):
        """Configuration anchors that cannot move after accounting history."""
        return {'company_id', 'currency_id'}

    def _eh_has_accounting_history(self):
        """Return whether this template owns any counter or move evidence."""
        self.ensure_one()
        if self.count_generated or self.last_generated_move_id:
            return True
        return bool(self.env['account.move'].sudo().search_count([
            '|',
            ('eh_recurring_template_id', '=', self.id),
            ('eh_recurring_proration_template_id', '=', self.id),
        ], limit=1))

    def _eh_assert_integrity_trusted(self):
        quarantined = self.filtered('eh_legacy_integrity_unverified')
        if quarantined:
            raise UserError(_(
                "Recurring template %(templates)s is quarantined because its "
                "legacy workflow or journal evidence has no durable server "
                "provenance. Preserve it and recreate a reviewed template; "
                "billing actions and edits are blocked.",
                templates=', '.join(quarantined.mapped('display_name')),
            ))
        return True

    def write(self, vals):
        self._eh_lock_for_integrity()
        if set(vals) == {'eh_legacy_review_note'} and self.filtered(
            'eh_legacy_integrity_unverified'
        ):
            if not self.env.user.has_group(
                'eh_account_base.group_eh_manager'
            ):
                raise AccessError(_(
                    "Only an EH Accounting Manager can attest a legacy "
                    "recurring-template review."
                ))
            return super().write(vals)
        if 'eh_legacy_review_note' in vals:
            raise AccessError(_(
                "Legacy review attestation is frozen after review."
            ))
        self._eh_assert_integrity_trusted()
        frozen = self._eh_history_frozen_fields().intersection(vals)
        if frozen:
            for template in self:
                changed = any(
                    template[field_name].id != vals[field_name]
                    for field_name in frozen
                )
                if changed and template._eh_has_accounting_history():
                    raise UserError(_(
                        "The company and transaction currency of recurring "
                        "template %s are frozen after its first accounting "
                        "history. Recreate the template for different "
                        "ownership or billing currency.",
                        template.display_name,
                    ))
        return super().write(vals)

    def action_review_legacy_and_resume_setup(self):
        """Release template configuration without blessing old moves.

        Upgrade-quarantined journal rows retain both legacy flags forever.
        Manager attestation only reopens template configuration; active
        schedules become paused so next run cannot fire until dates, lines,
        taxes and fiscal position are reviewed deliberately.
        """
        self.ensure_one()
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can complete legacy review."
            ))
        self._eh_lock_for_integrity()
        if not self.eh_legacy_integrity_unverified:
            raise UserError(_(
                "Template %s does not require legacy review.",
                self.display_name,
            ))
        note = ' '.join((self.eh_legacy_review_note or '').split())
        if len(note) < 20:
            raise UserError(_(
                "Record at least 20 characters describing evidence reviewed "
                "and corrective setup required."
            ))
        historic_moves = self.env['account.move'].sudo().search([
            '|', '|',
            ('eh_recurring_template_id', '=', self.id),
            ('eh_recurring_proration_template_id', '=', self.id),
            ('eh_recurring_proration_source_move_id.eh_recurring_template_id',
             '=', self.id),
        ])
        if historic_moves:
            historic_moves.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM account_move WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                [tuple(sorted(historic_moves.ids))],
            )
            historic_moves.invalidate_recordset([
                'eh_recurring_legacy_unverified',
                'eh_legacy_unverified_seal', 'eh_sealed',
            ])
            if historic_moves.filtered(
                lambda move: (
                    not move.eh_recurring_legacy_unverified
                    or not move.eh_legacy_unverified_seal
                    or move.eh_sealed
                )
            ):
                raise UserError(_(
                    "Legacy linked moves changed during review; review must "
                    "restart from a consistent quarantine snapshot."
                ))
        reviewed_at = fields.Datetime.now()
        self.env.cr.execute(
            "UPDATE eh_recurring_invoice_template "
            "SET eh_legacy_integrity_unverified = FALSE, "
            "    eh_legacy_reviewed_at = %s, "
            "    eh_legacy_reviewed_by_id = %s, "
            "    state = CASE WHEN state = 'active' THEN 'paused' "
            "                 ELSE state END "
            "WHERE id = %s AND eh_legacy_integrity_unverified = TRUE",
            [reviewed_at, self.env.user.id, self.id],
        )
        if self.env.cr.rowcount != 1:
            raise UserError(_(
                "Legacy review state changed concurrently; reload and retry."
            ))
        self.invalidate_recordset([
            'eh_legacy_integrity_unverified', 'eh_legacy_reviewed_at',
            'eh_legacy_reviewed_by_id', 'state',
        ])
        self.message_post(body=_(
            "Legacy template review completed by %(user)s. %(count)s "
            "historical move(s) remain quarantined and excluded from "
            "verified evidence. Review future schedule and resume manually. "
            "Attestation: %(note)s",
            user=self.env.user.display_name,
            count=len(historic_moves),
            note=note,
        ))
        return True

    def unlink(self):
        self._eh_lock_for_integrity()
        self._eh_assert_integrity_trusted()
        historic = self.filtered(
            lambda template: template._eh_has_accounting_history()
        )
        if historic:
            raise UserError(_(
                "Recurring template %(templates)s has generated, proration, "
                "or bridge accounting history and cannot be deleted. Archive "
                "or finish it so its source provenance remains intact.",
                templates=', '.join(historic.mapped('display_name')),
            ))
        return super().unlink()

    # ---- lifecycle ----

    def action_activate(self):
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state == 'finished':
                raise UserError(_(
                    "Cannot reactivate a finished template. Duplicate it.",
                ))
            if not rec.line_ids:
                raise UserError(_(
                    "Cannot activate template '%s' without lines.",
                ) % rec.name)
            rec.state = 'active'

    def action_pause(self):
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state == 'active':
                rec.state = 'paused'

    def action_resume(self):
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state == 'paused':
                rec.state = 'active'

    def action_finish(self):
        self = self._eh_workflow_action()
        for rec in self:
            rec.state = 'finished'

    # ---- generation ----

    def action_generate_now(self):
        """Manually generate one invoice and advance next_run.

        Takes a row lock first so two concurrent requests (a double click
        or a browser retry) cannot both read the same next_run_date and
        generate two invoices for the SAME period. Serialised by the lock,
        the second request observes the advanced next_run and generates the
        following period, exactly as two deliberate clicks would.
        """
        self._eh_check_access('write')
        for rec in self:
            rec._eh_lock_for_generate()
            if rec.state not in ('active', 'draft'):
                raise UserError(_(
                    "Can only generate from active or draft templates.",
                ))
            if rec._eh_finish_if_exhausted():
                continue
            rec._generate_one()
            rec._advance_next_run()
            rec._maybe_finish()
        return True

    def _eh_lock_for_generate(self):
        """Acquire a row lock on this template and drop cached field values
        so a serialised concurrent call reads the latest next_run_date
        rather than a stale pre-advance snapshot.
        """
        self.ensure_one()
        self._eh_lock_for_integrity()

    @api.model
    def _cron_generate_due(self, limit=None):
        """Generate one deterministic, resumable batch of due templates.

        Actual cron executions commit after each isolated template through
        core's progress API (or a pre-19 fallback). Direct/manual calls do
        not commit their surrounding transaction, which keeps tests and
        callers atomic. A bounded batch leaves the remaining rows due for the
        next scheduler acquisition instead of holding an unbounded database
        transaction across every company.
        """
        today = fields.Date.context_today(self)
        domain = [
            ('state', '=', 'active'),
            ('active', '=', True),
            ('eh_legacy_integrity_unverified', '=', False),
            ('next_run_date', '<=', today),
        ]
        batch_limit = int(limit or self._CRON_BATCH_SIZE)
        batch_limit = max(1, min(batch_limit, 1000))
        total_due = self.search_count(domain)
        due = self.search(
            domain,
            order='company_id, next_run_date, id',
            limit=batch_limit,
        )
        processed = 0
        for tpl in due:
            try:
                with self.env.cr.savepoint():
                    # Serialise against the manual path (action_generate_now):
                    # take the SAME row lock before reading next_run_date and
                    # generating, so a manual generate racing this cron cannot
                    # both observe the same period and double-generate.
                    tpl._eh_lock_for_generate()
                    if (
                        tpl.state != 'active'
                        or not tpl.active
                        or tpl.next_run_date > today
                    ):
                        continue
                    if tpl._eh_finish_if_exhausted():
                        continue
                    tpl._generate_one()
                    # Generation and pointer movement are one accounting
                    # unit. If either fails, the invoice and counter update
                    # roll back together so retrying the retained period can
                    # never duplicate a successfully-created invoice.
                    tpl._advance_next_run()
                    tpl._maybe_finish()
            except Exception as exc:
                _logger.warning(
                    "eh.recurring.invoice.template %s generation failed: %s",
                    tpl.id, exc,
                )
                tpl._record_failure(exc)
            processed += 1
            remaining = max(total_due - processed, 0)
            if not self._eh_commit_cron_progress(1, remaining):
                break
        return True

    @api.model
    def _eh_commit_cron_progress(self, processed, remaining):
        """Commit one cron unit and return whether its time budget remains."""
        # Odoo 18/19 identify scheduled execution with cron_id; 16/17 expose
        # only lastcall. Direct model/test callers normally carry neither and
        # must never commit their surrounding transaction.
        if (
            not self.env.context.get('cron_id')
            and 'lastcall' not in self.env.context
        ):
            return True
        progress = getattr(self.env['ir.cron'], '_commit_progress', None)
        if progress and self.env.context.get('cron_id'):
            return bool(progress(processed, remaining=remaining))
        # Odoo 16/17 have no progress record API and only expose lastcall;
        # Odoo 18 has cron_id/progress tracking but no public commit helper.
        # Retain the same per-template durability on all three series.
        self.env.cr.commit()
        return True

    def _generate_one(self):
        """Create one draft account.move from this template."""
        self.ensure_one()
        self._eh_assert_integrity_trusted()
        self._eh_validate_generation_targets()
        if not self.line_ids:
            raise UserError(_(
                "Template '%s' has no lines; nothing to generate.",
            ) % self.name)
        Move = self.env['account.move'].with_context(
            **self._eh_move_create_context())
        invoice_vals = self._build_invoice_vals()
        generation_key = invoice_vals['eh_recurring_generation_key']
        existing = self.env['account.move'].sudo().search([
            ('eh_recurring_generation_key', '=', generation_key),
            ('state', '!=', 'cancel'),
        ], limit=1)
        if existing:
            raise UserError(_(
                "Billing period %(period)s for template %(template)s "
                "already has active invoice %(invoice)s. Reverse/correct "
                "that invoice, or advance Next Run Date; rewinding the "
                "schedule cannot bill the same period twice.",
                period=self.next_run_date,
                template=self.display_name,
                invoice=existing.display_name,
            ))
        # Ordinary draft invoices remain reviewable. The AccountMove._post
        # hook below stamps the verified seal only after posting succeeds.
        # Proration is different: its two entries are an atomic accounting
        # correction and are sealed from creation in the wizard.
        move = Move.create(invoice_vals)
        if self.auto_post:
            move.action_post()
        if self.last_error:
            self.message_post(body=_(
                "Recurring generation recovered for billing period %(date)s. "
                "The prior failure remains in chatter; current error status "
                "has been cleared.",
                date=self.next_run_date,
            ))
        self.sudo().write({
            'last_run_at': fields.Datetime.now(),
            'last_generated_move_id': move.id,
            'last_error': False,
            'count_generated': self.count_generated + 1,
        })
        return move

    def _eh_move_create_context(self):
        """Unforgeable in-process capability for generated-move linkage."""
        return {RECURRING_MOVE_CONTEXT: RECURRING_MOVE_CAPABILITY}

    def _eh_validate_generation_targets(self):
        """Validate every copied relation before account.move creation."""
        self.ensure_one()
        if self.company_id not in self.env.companies and not self.env.su:
            raise AccessError(_(
                "The recurring template company is outside your allowed "
                "company scope."))
        self._check_company()
        self.partner_id._eh_check_access('read')
        self.journal_id._eh_check_access('read')
        for target in (self.payment_term_id, self.fiscal_position_id):
            if target:
                target._eh_check_access('read')
        self.line_ids._eh_check_access('read')
        for targets in (
                self.line_ids.mapped('product_id'),
                self.line_ids.mapped('account_id'),
                self.line_ids.mapped('tax_ids')):
            if targets:
                targets._eh_check_access('read')
        return True

    def _build_invoice_vals(self):
        self.ensure_one()
        invoice_date = self.next_run_date or fields.Date.context_today(self)
        period_start, period_end = self._eh_period_bounds(invoice_date)
        line_vals = []
        for line in self.line_ids.sorted(lambda l: l.sequence):
            line_dict = {
                'name': line.name,
                'quantity': line.quantity or 1.0,
                'price_unit': line.price_unit or 0.0,
                'sequence': line.sequence,
            }
            if line.product_id:
                line_dict['product_id'] = line.product_id.id
            if line.account_id:
                account = (
                    self.fiscal_position_id.map_account(line.account_id)
                    if self.fiscal_position_id else line.account_id
                )
                line_dict['account_id'] = account.id
            if line.tax_ids:
                taxes = (
                    self.fiscal_position_id.map_tax(line.tax_ids)
                    if self.fiscal_position_id else line.tax_ids
                )
                line_dict['tax_ids'] = [(6, 0, taxes.ids)]
            line_vals.append((0, 0, line_dict))

        vals = {
            'move_type': 'out_invoice',
            'partner_id': self.partner_id.id,
            'journal_id': self.journal_id.id,
            'company_id': self.company_id.id,
            'currency_id': self.currency_id.id,
            'invoice_date': invoice_date,
            'invoice_line_ids': line_vals,
            'eh_recurring_template_id': self.id,
            'eh_recurring_period_start': period_start,
            'eh_recurring_period_end': period_end,
            'eh_recurring_generation_key': "%s:%s" % (
                self.id, period_start.isoformat(),
            ),
        }
        if self.payment_term_id:
            vals['invoice_payment_term_id'] = self.payment_term_id.id
        if self.fiscal_position_id:
            vals['fiscal_position_id'] = self.fiscal_position_id.id
        if self.ref:
            vals['ref'] = self.ref
        if self.narration:
            vals['narration'] = self.narration
        return vals

    def _eh_period_bounds(self, period_start=None):
        """Return immutable service-period bounds for one generation.

        Boundary calculation mirrors ``_advance_next_run`` exactly, including
        end-of-month anchoring. This keeps consecutive invoice periods
        contiguous and gives later proration a posted source period rather
        than guessing from the already-advanced ``next_run_date``.
        """
        self.ensure_one()
        period_start = (
            fields.Date.to_date(period_start)
            or self.next_run_date
            or fields.Date.context_today(self)
        )
        next_start = self._anchor_day_of_month(
            period_start + self._compute_delta()
        )
        if next_start <= period_start:
            raise UserError(_(
                "Recurring period boundaries must advance beyond %s.",
                period_start,
            ))
        return period_start, next_start - timedelta(days=1)

    def _eh_prepare_proration_move_vals(self, vals):
        """Extension hook for bridges that own proration accounting."""
        self.ensure_one()
        return vals

    def _advance_next_run(self):
        for rec in self:
            base = (
                (rec.next_run_date or fields.Date.context_today(rec))
                + rec._compute_delta()
            )
            rec.next_run_date = rec._anchor_day_of_month(base)

    def _anchor_day_of_month(self, candidate):
        """Pin the day-of-month back to the explicit billing day for calendar
        cadences, clamped to the candidate month's length.

        relativedelta clamps an out-of-range day down (Jan 31 + 1 month
        becomes Feb 28). Advancing off that clamped value would carry the
        shortened day forward forever (Feb 28 -> Mar 28 -> ...), so the
        intended day-of-month silently decays. Re-anchoring to the
        configured billing day on every advance keeps a 31st-of-month
        schedule landing on the 31st whenever the month allows, while
        still honouring the relative advance. Failed periods do not enter
        this method: cron pauses them at their original billing date.

        Day and week cadences have no day-of-month semantics; they pass
        through unchanged.
        """
        self.ensure_one()
        if self.interval_unit not in ('month', 'quarter', 'year'):
            return candidate
        last_day = calendar.monthrange(candidate.year, candidate.month)[1]
        target_day = min(self.billing_day, last_day)
        return candidate.replace(day=target_day)

    def _compute_delta(self):
        self.ensure_one()
        if self.interval_unit == 'day':
            return relativedelta(days=self.interval)
        if self.interval_unit == 'week':
            return relativedelta(weeks=self.interval)
        if self.interval_unit == 'month':
            return relativedelta(months=self.interval)
        if self.interval_unit == 'quarter':
            return relativedelta(months=3 * self.interval)
        if self.interval_unit == 'year':
            return relativedelta(years=self.interval)
        return relativedelta(months=self.interval)

    def _maybe_finish(self):
        """Finish the template when end_date or count_total cap is reached.

        Called from action_generate_now and the cron; the state write is
        routed through the guard's sudo helper so it passes the workflow guard
        regardless of the acting user (the cron runs as its own user, not
        necessarily the superuser).
        """
        for rec in self:
            if (
                rec.end_date
                and rec.next_run_date
                and rec.next_run_date > rec.end_date
            ):
                rec._eh_workflow_write({'state': 'finished'})
                continue
            if rec.count_total and rec.count_generated >= rec.count_total:
                rec._eh_workflow_write({'state': 'finished'})

    def _eh_finish_if_exhausted(self):
        """Fail closed before generation when schedule cap already expired."""
        self.ensure_one()
        exhausted = bool(
            (self.count_total and self.count_generated >= self.count_total)
            or (
                self.end_date
                and self.next_run_date
                and self.next_run_date > self.end_date
            )
        )
        if exhausted and self.state != 'finished':
            self._eh_workflow_write({'state': 'finished'})
            self.invalidate_recordset(['state'])
        return exhausted

    def _record_failure(self, exc):
        for rec in self:
            message = str(exc)[:8000]
            failed_period = rec.next_run_date
            rec._eh_workflow_write({'state': 'paused'})
            rec.sudo().write({
                'last_run_at': fields.Datetime.now(),
                'last_error': message,
            })
            rec.message_post(body=_(
                "Recurring generation failed for billing period %(date)s. "
                "The period was not advanced and the template was paused. "
                "Repair the setup, then Resume to retry the same period. "
                "Error: %(error)s",
                date=failed_period,
                error=message,
            ))

    def action_view_generated(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Generated Invoices"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('eh_recurring_template_id', '=', self.id)],
            'context': {'default_eh_recurring_template_id': self.id},
        }


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_recurring_template_id = fields.Many2one(
        'eh.recurring.invoice.template',
        readonly=True,
        ondelete='restrict',
        copy=False,
        index=True,
        check_company=True,
        help=(
            "When set, this invoice was generated by the named recurring "
            "template. Used for traceability and counters."
        ),
    )
    eh_recurring_period_start = fields.Date(
        readonly=True, copy=False, index=True,
        help="First service date billed by this generated invoice.",
    )
    eh_recurring_period_end = fields.Date(
        readonly=True, copy=False, index=True,
        help="Last service date billed by this generated invoice.",
    )
    eh_recurring_generation_key = fields.Char(
        readonly=True,
        copy=False,
        index=True,
        help=(
            "Immutable template/service-period key. It is released only "
            "when a draft generation is cancelled or a posted invoice is "
            "neutralised through the controlled correction workflow."
        ),
    )
    eh_recurring_correction_move_id = fields.Many2one(
        'account.move',
        string="Recurring Correction Entry",
        readonly=True,
        copy=False,
        ondelete='restrict',
        check_company=True,
        help="Verified sealed counter-entry created by controlled correction.",
    )
    eh_recurring_corrected_at = fields.Datetime(
        readonly=True, copy=False,
    )
    eh_recurring_corrected_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
    )
    eh_recurring_legacy_unverified = fields.Boolean(
        string="Legacy Recurring Provenance Unverified",
        default=False,
        readonly=True,
        copy=False,
        index=True,
        help=(
            "Set by an upgrade when recurring invoice/proration linkage "
            "predates durable engine provenance. The link is retained for "
            "audit, while the move is quarantined and cannot be accepted as "
            "current source evidence."
        ),
    )

    @staticmethod
    def _eh_recurring_provenance_fields():
        return {
            'eh_recurring_template_id',
            'eh_recurring_period_start',
            'eh_recurring_period_end',
            'eh_recurring_generation_key',
            'eh_recurring_correction_move_id',
            'eh_recurring_corrected_at',
            'eh_recurring_corrected_by_id',
            'eh_recurring_legacy_unverified',
        }

    _unique_recurring_generation_key = models.Constraint(
        'UNIQUE (eh_recurring_generation_key)',
        'A recurring template service period can have only one active '
        'generated invoice.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        if any(
            any(
                vals.get(field_name)
                for field_name in self._eh_recurring_provenance_fields()
            )
            for vals in vals_list
        ) and (
                self.env.context.get(RECURRING_MOVE_CONTEXT)
                is not RECURRING_MOVE_CAPABILITY):
            raise AccessError(_(
                "Recurring-invoice journal provenance is server-owned."))
        return super().create(vals_list)

    def write(self, vals):
        if self._eh_recurring_provenance_fields().intersection(vals) and (
                self.env.context.get(RECURRING_MOVE_CONTEXT)
                is not RECURRING_MOVE_CAPABILITY):
            raise AccessError(_(
                "Recurring-invoice journal provenance is server-owned."))
        return super().write(vals)

    def _post(self, soft=True):
        posted = super()._post(soft=soft)
        generated = posted.filtered(lambda move: (
            move.eh_recurring_template_id
            and not move.eh_recurring_legacy_unverified
            and not move.eh_legacy_unverified_seal
        ))
        if generated:
            generated._eh_stamp_verified_seal()
        return posted

    def button_cancel(self):
        """Release the period key only after core cancels an editable draft."""
        generated = self.filtered(lambda move: (
            move.state == 'draft'
            and move.eh_recurring_template_id
            and move.eh_recurring_generation_key
            and not move.eh_sealed
        ))
        result = super().button_cancel()
        cancelled = generated.filtered(lambda move: move.state == 'cancel')
        if cancelled:
            cancelled.with_context(**{
                RECURRING_MOVE_CONTEXT: RECURRING_MOVE_CAPABILITY,
            }).write({'eh_recurring_generation_key': False})
        return result

    def action_eh_correct_recurring_invoice(self):
        """Open the manager-only correction wizard for one generated move."""
        self.ensure_one()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can correct a recurring "
                "invoice."
            ))
        self._eh_assert_recurring_correction_source()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Correct Recurring Invoice"),
            'res_model': 'eh.recurring.invoice.correction.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_move_id': self.id,
                'default_date': fields.Date.context_today(self),
            },
        }

    def _eh_assert_recurring_correction_source(self):
        self.ensure_one()
        self._eh_check_access('write')
        if (
            self.move_type != 'out_invoice'
            or self.state != 'posted'
            or not self.eh_recurring_template_id
            or not self.eh_recurring_generation_key
            or self.eh_recurring_proration_template_id
            or not self.eh_sealed
            or self.eh_legacy_unverified_seal
            or self.eh_recurring_legacy_unverified
            or self.eh_recurring_correction_move_id
        ):
            raise UserError(_(
                "Only an uncorrected, verified sealed, posted invoice from "
                "the recurring generation engine can use this correction "
                "workflow."
            ))
        if self.env['account.move'].sudo().search_count([
            ('reversed_entry_id', '=', self.id),
        ], limit=1):
            raise UserError(_(
                "This recurring invoice already has a counter-entry."
            ))
        return True

    def _eh_correct_recurring_invoice(self, correction_date, reason):
        """Post an exact linked reversal and release the period for reissue."""
        self.ensure_one()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can correct a recurring "
                "invoice."
            ))
        correction_date = fields.Date.to_date(correction_date)
        reason = ' '.join((reason or '').split())
        if len(reason) < 10:
            raise UserError(_(
                "Record at least 10 characters explaining the correction."
            ))
        today = fields.Date.context_today(self)
        if not correction_date or correction_date < self.date:
            raise UserError(_(
                "Correction date cannot precede the original invoice date."
            ))
        if correction_date > today:
            raise UserError(_("Correction date cannot be in the future."))
        with self.env.cr.savepoint():
            self.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM account_move WHERE id = %s FOR UPDATE",
                (self.id,),
            )
            self.invalidate_recordset()
            self._eh_assert_recurring_correction_source()
            violated_locks = self._get_violated_lock_dates(
                correction_date, False,
            )
            if violated_locks:
                raise UserError(_(
                    "The recurring correction date %(date)s is locked. "
                    "Choose an open accounting date.",
                    date=correction_date,
                ))
            reversal = self._eh_reverse_with_verified_capability([{
                'date': correction_date,
                'journal_id': self.journal_id.id,
                'ref': _(
                    "Recurring correction %(invoice)s: %(reason)s",
                    invoice=self.display_name,
                    reason=reason,
                ),
            }], cancel=False)
            reversal._eh_post_verified_reversal()
            reversal.invalidate_recordset([
                'state', 'date', 'eh_sealed', 'reversed_entry_id',
            ])
            if (
                reversal.state != 'posted'
                or reversal.date != correction_date
                or not reversal.eh_sealed
                or reversal.reversed_entry_id != self
            ):
                raise UserError(_(
                    "The recurring correction failed its sealed reversal "
                    "integrity check; no period key was released."
                ))
            guarded = self.with_context(**{
                RECURRING_MOVE_CONTEXT: RECURRING_MOVE_CAPABILITY,
            })
            guarded._eh_write_sealed_metadata({
                'eh_recurring_generation_key': False,
                'eh_recurring_correction_move_id': reversal.id,
                'eh_recurring_corrected_at': fields.Datetime.now(),
                'eh_recurring_corrected_by_id': self.env.user.id,
            }, {
                'eh_recurring_generation_key',
                'eh_recurring_correction_move_id',
                'eh_recurring_corrected_at',
                'eh_recurring_corrected_by_id',
            })
            self.eh_recurring_template_id.message_post(body=_(
                "Recurring invoice %(invoice)s was neutralised by linked "
                "sealed correction %(correction)s on %(date)s. Reason: "
                "%(reason)s. The same service period may now be deliberately "
                "reissued from the reviewed template.",
                invoice=self.display_name,
                correction=reversal.display_name,
                date=correction_date,
                reason=reason,
            ))
        return reversal
