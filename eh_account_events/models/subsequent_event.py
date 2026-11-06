# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IAS 10 events after the reporting period."""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .account_move import (
    EVENT_MOVE_CAPABILITY,
    EVENT_MOVE_CONTEXT,
    EVENT_PROVENANCE_VERSION,
)
from .event_sequence import next_company_reference


class EhSubsequentEvent(models.Model):
    _name = 'eh.subsequent.event'
    _description = "Event after the reporting period (IAS 10)"
    _inherit = ['mail.thread', 'eh.gl.reversal', 'eh.workflow.guard']
    _order = 'event_date desc, id desc'
    _rec_name = 'name'
    _check_company_auto = True

    # state is a state machine driven only by the booking/reset actions (which
    # run under sudo); a direct non-superuser RPC write to it is refused by the
    # inherited eh.workflow.guard, closing the "write({'state': 'posted'}) skips
    # action_book_adjusting_entry and its journal entry" bypass.
    _eh_guarded_fields = (
        'state', 'move_id', 'currency_id', 'next_period', 'treatment',
        'legacy_provenance_quarantined',
        'legacy_provenance_quarantine_note',
        'legacy_remediation_move_id', 'legacy_remediated_by_id',
        'legacy_remediated_at',
    )

    reference = fields.Char(
        required=True, copy=False, default='/', readonly=True, index=True,
        help="Company-scoped IAS 10 register reference derived from the "
             "event date.")
    name = fields.Char(required=True, tracking=True)
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    reporting_date = fields.Date(
        required=True, help="End of the reporting period.")
    event_date = fields.Date(
        required=True, default=fields.Date.context_today,
        help="Date the event occurred, after the reporting period.")
    authorized_for_issue_date = fields.Date(
        string="Authorised for issue",
        default=lambda self: self._default_authorized_for_issue_date(),
        tracking=True,
        help="Date the financial statements were authorised for issue "
             "(IAS 10.5-6). IAS 10 covers only events between the reporting "
             "date and this date. An event dated after it must be registered "
             "against the following reporting period and is refused here "
             "(IAS 10.3). Defaults from the "
             "date the latest year-end close was posted when that module is "
             "installed; leave empty for an unbounded register.")
    authorized_for_issue_by = fields.Char(
        string="Authorised for issue by", tracking=True,
        help="Person or governing body that authorised the financial "
             "statements for issue (IAS 10.17).")
    next_period = fields.Boolean(
        string="Next period", compute='_compute_next_period', store=True,
        help="Legacy audit indicator for a pre-control row dated after the "
             "authorised-for-issue cutoff. New or edited rows outside the IAS "
             "10 window are refused and must be registered against the next "
             "reporting period.")
    is_adjusting = fields.Boolean(
        string="Adjusting event",
        help="True when the event provides evidence of conditions that "
             "existed at the reporting date (adjust the statements); false "
             "when it is indicative of conditions that arose after (disclose "
             "only) - IAS 10.8-10.")
    treatment = fields.Selection(
        [('adjust', "Adjust the financial statements"),
         ('disclose', "Disclose only")],
        compute='_compute_treatment', store=True)
    category = fields.Selection(
        [('litigation', "Litigation settlement"),
         ('asset_value', "Asset valuation evidence"),
         ('receivable', "Customer insolvency"),
         ('dividend', "Dividend declared"),
         ('acquisition', "Acquisition / disposal"),
         ('other', "Other")],
        default='other', required=True)
    estimated_effect = fields.Monetary(
        currency_field='currency_id',
        help="Estimated financial effect when estimation status is "
             "Estimated (IAS 10.21).")
    estimation_status = fields.Selection(
        [
            ('estimated', "Estimated"),
            ('cannot_estimate', "Cannot be estimated"),
        ],
        required=True, default='estimated', tracking=True,
        help="Distinguishes an estimated amount (including a genuine zero) "
             "from the IAS 10.21(b) disclosure that no reliable estimate "
             "can be made.")
    description = fields.Text()

    # --- Optional GL booking of an adjusting event (IAS 10.8) -------------
    # Opt-in. An adjusting event provides evidence of conditions that existed
    # at the reporting date, so the financial statements are adjusted before
    # authorisation. Populating the two accounts below and booking posts that
    # adjusting entry to the reporting period; a register that leaves them
    # blank behaves exactly as before. A non-adjusting (disclose-only) event
    # cannot be booked (IAS 10.10). Booking is manager-gated and frozen.
    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('posted', "Posted"),
            ('reversed', "Neutralised"),
        ],
        default='draft', required=True, tracking=True, copy=False,
        help="Posted once the adjusting entry has been written to the "
             "general ledger.")
    journal_id = fields.Many2one(
        'account.journal', string="Journal",
        check_company=True,
        domain="[('type', '=', 'general')]",
        help="Journal for the adjusting entry.")
    debit_account_id = fields.Many2one(
        'account.account', string="Debit account",
        check_company=True,
        help="Account debited by the adjusting entry.")
    credit_account_id = fields.Many2one(
        'account.account', string="Credit account",
        check_company=True,
        help="Account credited by the adjusting entry.")
    move_id = fields.Many2one(
        'account.move', string="Adjusting entry", readonly=True, copy=False,
        check_company=True,
        help="Journal entry booking the adjusting event to the reporting "
             "period.")
    move_count = fields.Integer(compute='_compute_move_count')
    legacy_provenance_quarantined = fields.Boolean(
        readonly=True, copy=False,
        help="Set by a fail-closed upgrade audit when legacy stored scope, "
             "computed output, or journal evidence cannot be trusted.")
    legacy_provenance_quarantine_note = fields.Text(readonly=True, copy=False)
    legacy_remediation_reference = fields.Char(
        string="Remediation reference", copy=False,
        help="Manager/auditor reference authorising neutralisation of exact "
             "quarantined legacy evidence.")
    legacy_remediation_date = fields.Date(
        string="Remediation accounting date", copy=False,
        help="Open accounting date for exact neutralisation. It cannot "
             "precede the legacy primary entry.")
    legacy_remediation_move_id = fields.Many2one(
        'account.move', string="Legacy neutralisation entry", readonly=True,
        copy=False, check_company=True)
    legacy_remediated_by_id = fields.Many2one(
        'res.users', string="Remediated by", readonly=True, copy=False)
    legacy_remediated_at = fields.Datetime(
        string="Remediated at", readonly=True, copy=False)

    # Fields feeding the adjusting entry, frozen once it is posted so a booked
    # adjustment cannot be retro-edited out from under its journal entry.
    _FROZEN_AFTER_POSTED = (
        'reference', 'name', 'reporting_date', 'event_date', 'category',
        'is_adjusting', 'estimated_effect', 'estimation_status',
        'journal_id', 'debit_account_id', 'credit_account_id', 'company_id',
        'authorized_for_issue_date', 'authorized_for_issue_by',
    )
    _TRANSITION_INPUT_FIELDS = (
        'state', 'reference', 'name', 'company_id', 'currency_id',
        'reporting_date', 'event_date', 'authorized_for_issue_date',
        'authorized_for_issue_by', 'next_period', 'is_adjusting',
        'treatment', 'category', 'estimated_effect', 'estimation_status',
        'journal_id', 'debit_account_id', 'credit_account_id', 'move_id',
        'legacy_provenance_quarantined',
        'legacy_provenance_quarantine_note',
        'legacy_remediation_reference', 'legacy_remediation_date',
        'legacy_remediation_move_id',
    )
    _LOCKED_WRITE_FIELDS = frozenset(
        _eh_guarded_fields + _FROZEN_AFTER_POSTED + (
            'legacy_remediation_reference', 'legacy_remediation_date',
        )
    )

    @api.model
    def _default_authorized_for_issue_date(self):
        """Soft lookup: the date the latest year-end close was posted.

        When eh_account_year_end is installed, the day the company's most
        recent year-end closing run was posted is the best available proxy
        for the date the statements were authorised for issue; without the
        module (or with no posted run) the date stays manual. Registry
        lookup only - no hard dependency on the year-end module.
        """
        if 'eh.year.end.run' not in self.env:
            return False
        run = self.env['eh.year.end.run'].search(
            [('company_id', '=', self.env.company.id),
             ('state', '=', 'posted')],
            order='fiscal_year_end desc, id desc', limit=1)
        if run and run.posted_at:
            return fields.Date.to_date(run.posted_at)
        return False

    @api.depends('move_id', 'legacy_remediation_move_id')
    def _compute_move_count(self):
        for e in self:
            e.move_count = len(e.move_id | e.legacy_remediation_move_id)

    def _eh_assert_provenance_ready(self):
        self.ensure_one()
        if self.legacy_provenance_quarantined:
            raise UserError(_(
                "Legacy subsequent-event evidence on %(name)s is "
                "quarantined and cannot drive a ledger transition until it "
                "has been reviewed and corrected. %(note)s",
                name=self.display_name,
                note=self.legacy_provenance_quarantine_note or '',
            ))

    @api.depends('event_date', 'authorized_for_issue_date')
    def _compute_next_period(self):
        for e in self:
            e.next_period = bool(
                e.authorized_for_issue_date and e.event_date
                and e.event_date > e.authorized_for_issue_date)

    @api.onchange('reporting_date')
    def _onchange_reporting_date_authorized(self):
        """The default authorisation date comes from the latest posted
        year-end close, which belongs to an earlier period. When the user
        picks a reporting date after that default, clear it rather than
        leave a value the constraint below would refuse."""
        for e in self:
            if (e.authorized_for_issue_date and e.reporting_date
                    and e.authorized_for_issue_date < e.reporting_date):
                e.authorized_for_issue_date = False

    @api.model_create_multi
    def create(self, vals_list):
        """Mirror the onchange guard for programmatic creates.

        The default authorised-for-issue date is the posting date of the
        latest posted year-end close, which belongs to an earlier period.
        The form onchange clears that stale default when the user picks a
        later reporting date, but onchanges never run for imports, XML-RPC
        or create() in code: without this guard any programmatic create
        whose reporting date falls after the last close would trip
        _check_authorized_after_reporting. A caller that explicitly passes
        authorized_for_issue_date (in the vals or as a context default) is
        left untouched - a genuinely inconsistent explicit date is still a
        data error the constraint refuses.
        """
        if not self.env.su:
            company_ids = {
                vals.get('company_id', self.env.company.id)
                for vals in vals_list
            }
            companies = self.env['res.company'].browse(company_ids)
            companies._eh_check_access('read')
            if companies - self.env.companies:
                raise AccessError(_(
                    "A subsequent event must belong to one of the actor's "
                    "allowed companies."
                ))
        if 'default_authorized_for_issue_date' not in self.env.context:
            for vals in vals_list:
                if 'authorized_for_issue_date' in vals:
                    continue
                reporting = fields.Date.to_date(
                    vals.get('reporting_date')
                    or self.env.context.get('default_reporting_date'))
                if not reporting:
                    continue
                company = (
                    self.env['res.company'].browse(vals['company_id'])
                    if vals.get('company_id') else self.env.company)
                default = self.with_company(
                    company)._default_authorized_for_issue_date()
                if default and default < reporting:
                    vals['authorized_for_issue_date'] = False
        for vals in vals_list:
            if vals.get('reference', '/') == '/':
                company = (
                    self.env['res.company'].browse(vals['company_id'])
                    if vals.get('company_id') else self.env.company
                )
                sequence_date = fields.Date.to_date(
                    vals.get('event_date')
                    or self.env.context.get('default_event_date')
                    or fields.Date.context_today(self)
                )
                vals['reference'] = next_company_reference(
                    self.env, 'eh.subsequent.event', company, sequence_date,
                ) or '/'
        return super().create(vals_list)

    @api.constrains(
        'authorized_for_issue_date', 'reporting_date', 'event_date',
    )
    def _check_event_window(self):
        """The statements of a period are authorised for issue after that
        period ends (IAS 10.4-6): an authorisation date before the reporting
        date is a data error, not a policy choice."""
        for e in self:
            if (e.event_date and e.reporting_date
                    and e.event_date <= e.reporting_date):
                raise ValidationError(_(
                    "The event date (%(event)s) must be strictly after the "
                    "end of the reporting period (%(reporting)s) for the "
                    "IAS 10 register.",
                    event=e.event_date, reporting=e.reporting_date,
                ))
            if (e.authorized_for_issue_date and e.reporting_date
                    and e.authorized_for_issue_date < e.reporting_date):
                raise ValidationError(_(
                    "The authorised-for-issue date (%(auth)s) cannot be "
                    "before the end of the reporting period (%(rep)s): "
                    "financial statements are authorised for issue after "
                    "the period they report on ends (IAS 10.4-6).",
                    auth=e.authorized_for_issue_date, rep=e.reporting_date))
            if (
                e.authorized_for_issue_date
                and e.event_date
                and e.event_date > e.authorized_for_issue_date
            ):
                raise ValidationError(_(
                    "The event date (%(event)s) cannot be after the "
                    "authorised-for-issue cutoff (%(authorized)s). Register "
                    "the event against the following reporting period (IAS "
                    "10.3).",
                    event=e.event_date,
                    authorized=e.authorized_for_issue_date,
                ))

    @api.constrains('estimation_status', 'estimated_effect')
    def _check_estimation_status(self):
        for event in self:
            currency = event.currency_id or event.company_id.currency_id
            if (
                event.estimation_status == 'cannot_estimate'
                and not currency.is_zero(event.estimated_effect)
            ):
                raise ValidationError(_(
                    "Clear the estimated financial effect when the effect "
                    "cannot be estimated (IAS 10.21(b))."
                ))

    @api.depends('is_adjusting')
    def _compute_treatment(self):
        for e in self:
            e.treatment = 'adjust' if e.is_adjusting else 'disclose'

    @api.constrains('journal_id', 'debit_account_id', 'credit_account_id')
    def _check_accounting_configuration(self):
        """The browser domains are advisory; enforce posting shape in ORM."""
        for event in self:
            if event.journal_id and event.journal_id.type != 'general':
                raise ValidationError(_(
                    "Subsequent-event adjustments must use a general journal."
                ))
            if (
                event.debit_account_id
                and event.credit_account_id
                and event.debit_account_id == event.credit_account_id
            ):
                raise ValidationError(_(
                    "The adjusting-entry debit and credit accounts must be "
                    "different accounts."
                ))

    @api.constrains('category', 'is_adjusting')
    def _check_dividend_not_adjusting(self):
        """IAS 10.12-13: a dividend declared after the reporting period is a
        non-adjusting event. It is not recognised as a liability at the
        reporting date, only disclosed. Marking it adjusting (which would let
        action_book_adjusting_entry recognise it into the reporting period) is
        a black-letter violation, so refuse it."""
        for e in self:
            if e.category == 'dividend' and e.is_adjusting:
                raise ValidationError(_(
                    "A dividend declared after the reporting period is a "
                    "non-adjusting event (IAS 10.12-13): it is not recognised "
                    "as a liability at the reporting date, only disclosed. "
                    "Leave %s as disclose-only.", e.name))

    def write(self, vals):
        if self._LOCKED_WRITE_FIELDS.intersection(vals):
            self._eh_lock_transitions()
        if vals.get('company_id'):
            for event in self.filtered(
                    lambda record: record.company_id.id
                    != vals['company_id']):
                linked = self.env['account.move'].sudo().search_count([
                    ('eh_subsequent_event_id', '=', event.id),
                ])
                if linked:
                    raise UserError(_(
                        "The company is immutable once a subsequent event "
                        "has stamped journal evidence."
                    ))
        if vals.get('company_id') and not self.env.su:
            company = self.env['res.company'].browse(vals['company_id'])
            company._eh_check_access('read')
            if company not in self.env.companies:
                raise AccessError(_(
                    "A subsequent event cannot be moved to a company "
                    "outside the actor's allowed companies."
                ))
        # Posted-figure INPUTS are frozen for everyone (restate via reversal
        # through action_reset_to_draft): a data-integrity guard, not su-gated.
        # STATE transitions are enforced by the inherited eh.workflow.guard,
        # which blocks a non-superuser direct write; the sanctioned booking/
        # reset actions run under sudo. Provenance is env.su, not a context key.
        frozen = [f for f in self._FROZEN_AFTER_POSTED if f in vals]
        if frozen:
            for e in self:
                if e.state in ('posted', 'reversed'):
                    raise UserError(_(
                        "%(name)s carries a posted adjusting entry; "
                        "%(fields)s cannot be changed. Reset it to draft "
                        "(which reverses the entry) before editing.",
                        name=e.name, fields=', '.join(frozen)))
        remediation_inputs = {
            'legacy_remediation_reference', 'legacy_remediation_date',
        }.intersection(vals)
        if remediation_inputs and any(e.state == 'reversed' for e in self):
            raise UserError(_(
                "Legacy remediation evidence is immutable after the exact "
                "counter-entry posts."
            ))
        return super().write(vals)

    def unlink(self):
        self._eh_lock_transitions()
        for e in self:
            if e.state == 'posted' or e.move_id:
                raise UserError(_(
                    "%s has a posted adjusting entry and cannot be deleted. "
                    "This preserves the IAS 10 audit trail.", e.name))
        return super().unlink()

    def action_book_adjusting_entry(self):
        """Book the adjusting event to the reporting period (manager only).

        An adjusting event is booked to ``reporting_date`` (the end of the
        reporting period), debiting and crediting the two chosen accounts by
        ``estimated_effect``, balanced by construction. A non-adjusting event
        is disclose-only and cannot be booked (IAS 10.10).
        """
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can book an adjusting entry."))
        self._eh_lock_transitions()
        self._compute_next_period()
        self._compute_treatment()
        self.flush_recordset(['next_period', 'treatment'])
        # Run as su so the guarded state write below passes the inherited
        # eh.workflow.guard; env.user is preserved for the audit trail.
        self = self._eh_workflow_action()
        for e in self:
            e._eh_assert_provenance_ready()
            # Fail closed for any legacy cross-company configuration that
            # predates the field-level ``check_company`` guards.
            e._check_company()
            e._check_accounting_configuration()
            if e.state != 'draft':
                raise UserError(_("%s is already posted.", e.name))
            if e.treatment != 'adjust':
                raise UserError(_(
                    "Only an adjusting event books an entry. A non-adjusting "
                    "event is disclosed only and is not adjusted into the "
                    "financial statements (IAS 10.10)."))
            if e.estimation_status == 'cannot_estimate':
                raise UserError(_(
                    "An event whose financial effect cannot be estimated is "
                    "disclosure evidence only and cannot produce an "
                    "adjusting journal entry."
                ))
            if (e.authorized_for_issue_date and e.event_date
                    and e.event_date > e.authorized_for_issue_date):
                raise UserError(_(
                    "%(name)s is dated %(event)s, after the financial "
                    "statements were authorised for issue on %(auth)s. "
                    "IAS 10 covers only events up to the authorisation date "
                    "(IAS 10.3): this event belongs to the next reporting "
                    "period and cannot be booked as an adjusting entry of "
                    "the period ended %(rep)s.",
                    name=e.name, event=e.event_date,
                    auth=e.authorized_for_issue_date, rep=e.reporting_date))
            if not (e.journal_id and e.debit_account_id
                    and e.credit_account_id):
                raise UserError(_(
                    "Set the journal, the debit account and the credit "
                    "account before booking the adjusting entry."))
            currency = e.currency_id or e.company_id.currency_id
            amount = currency.round(e.estimated_effect)
            if currency.compare_amounts(amount, 0.0) <= 0:
                raise UserError(_(
                    "An adjusting entry needs a positive estimated financial "
                    "effect. Use the debit/credit account fields to express "
                    "direction; record a zero effect as disclosure only "
                    "(IAS 10.21)."))
            move = self.env['account.move'].with_context(**{
                EVENT_MOVE_CONTEXT: EVENT_MOVE_CAPABILITY,
            })._eh_create_sealed({
                'move_type': 'entry',
                'journal_id': e.journal_id.id,
                'date': e.reporting_date,
                'ref': _("Adjusting event: %s", e.name),
                'company_id': e.company_id.id,
                'eh_subsequent_event_id': e.id,
                'eh_event_provenance_version': EVENT_PROVENANCE_VERSION,
                'eh_sealed': True,
                'line_ids': [
                    (0, 0, {
                        'name': e.name,
                        'account_id': e.debit_account_id.id,
                        'debit': amount,
                        'credit': 0.0,
                    }),
                    (0, 0, {
                        'name': e.name,
                        'account_id': e.credit_account_id.id,
                        'debit': 0.0,
                        'credit': amount,
                    }),
                ],
            })
            move.action_post()
            e.move_id = move.id
            e.state = 'posted'
        return True

    def _eh_lock_transitions(self):
        """Authorize and serialize posting/reset on each source record."""
        self._eh_check_access('write')
        ids = sorted(self.ids)
        if not ids:
            return self
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_subsequent_event WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            (tuple(ids),),
        )
        self.invalidate_recordset(self._TRANSITION_INPUT_FIELDS)
        self._check_company()
        if not self.env.su and any(
            event.company_id not in self.env.companies for event in self
        ):
            raise AccessError(_(
                "The subsequent-event company is outside the actor's "
                "allowed companies."
            ))
        return self

    def action_reset_to_draft(self):
        """Reverse the posted adjusting entry and reopen (manager only)."""
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can reset a posted adjusting "
                "entry to draft."))
        self._eh_lock_transitions()
        for e in self:
            e._eh_assert_provenance_ready()
            if e.state != 'posted':
                raise UserError(_(
                    "%s is not posted; there is nothing to reset.", e.name))
            move = e.move_id
            if move:
                e._eh_validate_adjusting_move(move)
                reversal = move.with_context(**{
                    EVENT_MOVE_CONTEXT: EVENT_MOVE_CAPABILITY,
                })._eh_reverse_with_verified_capability([{
                    'date': e.reporting_date,
                    'journal_id': move.journal_id.id,
                    'ref': _("Reversal of adjusting event: %s", e.name),
                }], cancel=False)
                reversal.with_context(**{
                    EVENT_MOVE_CONTEXT: EVENT_MOVE_CAPABILITY,
                })._eh_write_sealed_metadata(
                    {
                        'eh_subsequent_event_id': e.id,
                        'eh_event_provenance_version':
                            EVENT_PROVENANCE_VERSION,
                    },
                    {
                        'eh_subsequent_event_id',
                        'eh_event_provenance_version',
                    },
                )
                reversal._eh_post_verified_reversal()
                e._eh_seal_reversal(reversal)
                e._eh_validate_reversal(move, reversal)
            # state moves to draft through sudo so the inherited
            # eh.workflow.guard recognises this as a sanctioned action write
            # (env.su), not a forgeable direct RPC re-key.
            e.sudo().write({'state': 'draft', 'move_id': False})
        return True

    def _eh_validate_legacy_primary_for_remediation(self):
        """Prove exact frozen shape without claiming legacy provenance."""
        self.ensure_one()
        move = self.move_id
        if (
            not move
            or move.state != 'posted'
            or move.reversed_entry_id
            or move.company_id != self.company_id
            or move.journal_id != self.journal_id
            or move.date != self.reporting_date
            or move.eh_event_provenance_version != 0
            or move.eh_accounting_change_id
            or (
                move.eh_subsequent_event_id
                and move.eh_subsequent_event_id != self
            )
            or self._eh_move_signature(move) != self._eh_expected_signature()
        ):
            raise UserError(_(
                "Legacy adjusting-entry evidence does not exactly match the "
                "frozen source and cannot use controlled remediation."
            ))
        if self.env['account.move'].sudo().search_count([
            ('reversed_entry_id', '=', move.id),
        ]):
            raise UserError(_(
                "Legacy adjusting-entry evidence already has a counter-entry."
            ))
        return move

    def _eh_validate_legacy_remediation(self, primary, reversal):
        self.ensure_one()
        reverse_signature = sorted(
            (account_id, credit, debit)
            for account_id, debit, credit
            in self._eh_move_signature(primary)
        )
        if (
            not reversal
            or reversal.state != 'posted'
            or not reversal.eh_sealed
            or reversal.company_id != self.company_id
            or reversal.journal_id != self.journal_id
            or reversal.reversed_entry_id != primary
            or reversal.eh_subsequent_event_id != self
            or reversal.eh_accounting_change_id
            or reversal.eh_event_provenance_version
               != EVENT_PROVENANCE_VERSION
            or self._eh_move_signature(reversal) != reverse_signature
        ):
            raise UserError(_(
                "Legacy adjusting-entry neutralisation failed exact sealed "
                "provenance validation."
            ))

    def action_remediate_legacy_provenance(self):
        """Neutralise exact quarantined evidence; never bless or delete it."""
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can remediate quarantined "
                "subsequent-event evidence."
            ))
        self._eh_lock_transitions()
        for event in self:
            if not event.legacy_provenance_quarantined:
                raise UserError(_("This subsequent event is not quarantined."))
            if event.state != 'posted':
                raise UserError(_(
                    "Only posted quarantined evidence can be neutralised."
                ))
            reference = (event.legacy_remediation_reference or '').strip()
            if len(reference) < 8:
                raise UserError(_(
                    "Enter a manager/auditor remediation reference of at "
                    "least eight characters."
                ))
            remediation_date = event.legacy_remediation_date
            primary = event._eh_validate_legacy_primary_for_remediation()
            if not remediation_date:
                raise UserError(_("Choose a remediation accounting date."))
            if remediation_date < primary.date:
                raise UserError(_(
                    "Remediation date %(date)s cannot precede legacy entry "
                    "date %(primary)s.",
                    date=remediation_date, primary=primary.date,
                ))
            if remediation_date > fields.Date.context_today(event):
                raise UserError(_(
                    "A legacy remediation counter-entry cannot be future-"
                    "dated."
                ))
            remediation_line_label = _("Audited remediation")
            values = {
                'move_type': 'entry',
                'company_id': event.company_id.id,
                'date': remediation_date,
                'journal_id': event.journal_id.id,
                'ref': _("Audited remediation of %s", event.name),
                'eh_subsequent_event_id': event.id,
                'eh_event_provenance_version': EVENT_PROVENANCE_VERSION,
                'reversed_entry_id': primary.id,
                'eh_sealed': True,
                'line_ids': [(0, 0, {
                    'name': "%s: %s" % (
                        remediation_line_label, line.name or event.name,
                    ),
                    'account_id': line.account_id.id,
                    'debit': line.credit,
                    'credit': line.debit,
                }) for line in primary.line_ids],
            }
            with self.env.cr.savepoint():
                guarded_primary = primary.with_context(**{
                    EVENT_MOVE_CONTEXT: EVENT_MOVE_CAPABILITY,
                })
                if (
                    primary.eh_legacy_unverified_seal
                    and not primary.eh_sealed
                ):
                    reversal = (
                        guarded_primary
                        ._eh_reverse_reviewed_legacy_quarantine([values])
                    )
                else:
                    reversal = self.env['account.move'].with_context(**{
                        EVENT_MOVE_CONTEXT: EVENT_MOVE_CAPABILITY,
                    })._eh_create_sealed(values)
                    reversal.action_post()
                if reversal.date != remediation_date:
                    raise UserError(_(
                        "Odoo shifted the remediation entry from %(requested)s "
                        "to %(actual)s. Nothing was remediated; choose an open "
                        "exact date.",
                        requested=remediation_date, actual=reversal.date,
                    ))
                event._eh_validate_legacy_remediation(primary, reversal)
                event.sudo().write({
                    'state': 'reversed',
                    'legacy_provenance_quarantined': False,
                    'legacy_provenance_quarantine_note': _(
                        "Legacy evidence neutralised under review reference "
                        "%s; original evidence remains immutable.", reference,
                    ),
                    'legacy_remediation_move_id': reversal.id,
                    'legacy_remediated_by_id': self.env.user.id,
                    'legacy_remediated_at': fields.Datetime.now(),
                })
        return True

    def _eh_move_signature(self, move):
        self.ensure_one()
        currency = self.currency_id or self.company_id.currency_id
        return sorted(
            (
                line.account_id.id,
                currency.round(line.debit),
                currency.round(line.credit),
            )
            for line in move.line_ids
            if not currency.is_zero(line.debit - line.credit)
        )

    def _eh_expected_signature(self):
        self.ensure_one()
        currency = self.currency_id or self.company_id.currency_id
        amount = currency.round(self.estimated_effect)
        return sorted([
            (self.debit_account_id.id, amount, 0.0),
            (self.credit_account_id.id, 0.0, amount),
        ])

    def _eh_validate_adjusting_move(self, move):
        self.ensure_one()
        if not self.env.su:
            move._eh_check_access('read')
        if (
            move.state != 'posted'
            or not move.eh_sealed
            or move.company_id != self.company_id
            or move.eh_subsequent_event_id != self
            or move.eh_accounting_change_id
            or move.eh_event_provenance_version
               != EVENT_PROVENANCE_VERSION
            or move.reversed_entry_id
            or move.journal_id != self.journal_id
            or move.date != self.reporting_date
            or self._eh_move_signature(move) != self._eh_expected_signature()
        ):
            raise UserError(_(
                "The linked adjusting entry no longer proves the sealed "
                "posting produced by this event. It will not be reversed "
                "or unlinked."
            ))

    def _eh_validate_reversal(self, original, reversal):
        self.ensure_one()
        if len(reversal) != 1:
            raise UserError(_(
                "The adjusting-event reversal did not produce exactly one "
                "entry."
            ))
        reverse_signature = sorted(
            (account_id, credit, debit)
            for account_id, debit, credit
            in self._eh_move_signature(original)
        )
        rev = reversal.ensure_one()
        if (
            rev.state != 'posted'
            or not rev.eh_sealed
            or rev.company_id != self.company_id
            or rev.eh_subsequent_event_id != self
            or rev.eh_accounting_change_id
            or rev.eh_event_provenance_version
               != EVENT_PROVENANCE_VERSION
            or rev.reversed_entry_id != original
            or rev.journal_id != self.journal_id
            or self._eh_move_signature(rev) != reverse_signature
        ):
            raise UserError(_(
                "The adjusting-event reversal failed sealed provenance "
                "validation."
            ))

    def action_view_move(self):
        self.ensure_one()
        self._eh_check_access('read')
        if self.move_id:
            self.move_id._eh_check_access('read')
            if self.state == 'reversed' and self.legacy_remediation_move_id:
                self._eh_validate_legacy_remediation(
                    self.move_id, self.legacy_remediation_move_id,
                )
            else:
                self._eh_assert_provenance_ready()
                self._eh_validate_adjusting_move(self.move_id)
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.move_id.id,
        }
