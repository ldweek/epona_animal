# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IAS 8 accounting policy changes, estimate changes and error corrections."""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import float_is_zero

from .account_move import (
    EVENT_MOVE_CAPABILITY,
    EVENT_MOVE_CONTEXT,
    EVENT_PROVENANCE_VERSION,
)
from .event_sequence import next_company_reference


_PROFIT_AND_LOSS_ACCOUNT_TYPES = frozenset({
    'income', 'income_other',
    'expense', 'expense_depreciation', 'expense_direct_cost',
})
_NATURAL_CREDIT_ACCOUNT_TYPES = frozenset({
    'liability_payable', 'liability_credit_card', 'liability_current',
    'liability_non_current', 'equity', 'equity_unaffected',
})


class EhAccountingChange(models.Model):
    _name = 'eh.accounting.change'
    _description = "Accounting change / error (IAS 8)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.gl.reversal',
                'eh.workflow.guard']
    _order = 'change_date desc, id desc'
    _rec_name = 'name'
    _check_company_auto = True

    # state is a state machine driven only by the posting/reset actions (which
    # run under sudo); a direct non-superuser RPC write to it is refused by the
    # inherited eh.workflow.guard, closing the "write({'state': 'posted'}) skips
    # action_post_restatement and its journal entry" bypass.
    _eh_guarded_fields = (
        'state', 'move_id', 'currency_id', 'application',
        'equity_presentation',
        'retained_earnings_impact', 'legacy_provenance_quarantined',
        'legacy_provenance_quarantine_note',
        'legacy_remediation_move_id', 'legacy_remediated_by_id',
        'legacy_remediated_at',
    )

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    change_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)
    restatement_date = fields.Date(
        string="Restatement posting date", required=True, tracking=True,
        default=fields.Date.context_today,
        help="Accounting date for the opening-balance restatement. Use the "
             "opening of the earliest comparative period presented when IAS "
             "8 requires retrospective application or restatement. For "
             "backward compatibility it starts from the change date, but it "
             "is a separate, explicit posting decision.")

    change_type = fields.Selection(
        [('policy_change', "Change in accounting policy"),
         ('estimate_change', "Change in accounting estimate"),
         ('error_correction', "Correction of prior-period error")],
        default='policy_change', required=True, tracking=True)
    application = fields.Selection(
        [('retrospective', "Retrospective (restate comparatives)"),
         ('prospective', "Prospective")],
        compute='_compute_application', store=True,
        help="Policy changes and error corrections are retrospective; "
             "estimate changes are prospective (IAS 8.19, 36, 42).")
    equity_presentation = fields.Selection(
        [
            ('retrospective_application',
             "Retrospective application (opening equity)"),
            ('retrospective_restatement',
             "Prior-period error restatement (opening equity)"),
            ('prospective_change', "Prospective change (no opening entry)"),
        ],
        compute='_compute_application', store=True,
        help="Statement-of-changes-in-equity classification. Policy changes "
             "are tagged as retrospective application, prior-period errors "
             "as retrospective restatement, and estimate changes as "
             "prospective with no opening-equity posting (IAS 1.106(b), IAS "
             "8.22 and 42).")
    description = fields.Text(
        help="Nature of the change or error and the reason for it "
             "(IAS 8.29, 49).")

    line_ids = fields.One2many('eh.accounting.change.line', 'change_id')
    retained_earnings_impact = fields.Monetary(
        compute='_compute_impact', store=True, currency_field='currency_id',
        help="Net adjustment to opening retained earnings from the "
             "restatement.")
    notes = fields.Text()

    # --- Multi-period comparative trail (IAS 8.22, 42, 49) ---------------
    # Opt-in. When off (default) the register keeps the original single
    # lumped opening-retained-earnings plug: one net adjustment against one
    # adjustment_account_id. When on, each restatement line carries its own
    # affected account and prior period, and posting books a per-account
    # opening-retained-earnings restatement across one or more prior periods
    # rather than a single net figure. Existing records leave this off and
    # behave exactly as before.
    comparative_mode = fields.Boolean(
        string="Per-account comparative trail",
        tracking=True,
        help="When set, each restatement line carries its affected account "
             "and prior period; posting books the opening retained-earnings "
             "restatement per affected account across the prior periods "
             "(IAS 8.22, 42), instead of one lumped adjustment. Leave off for "
             "the single net opening-retained-earnings plug.")

    # --- Optional GL posting of the restatement (IAS 8.26, 42) ------------
    # A retrospective change or error correction adjusts opening retained
    # earnings against the restated asset/liability. Populating the three
    # accounts below and posting produces that opening balance-sheet entry;
    # a register that leaves them blank behaves exactly as before.
    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('posted', "Posted"),
            ('reversed', "Neutralised"),
        ],
        default='draft', required=True, tracking=True, copy=False,
        help="Posted once the opening retained-earnings restatement has been "
             "written to the general ledger.")
    retained_earnings_account_id = fields.Many2one(
        'account.account', string="Retained earnings account",
        check_company=True,
        domain="[('account_type', '=', 'equity')]",
        help="Equity account carrying opening retained earnings; receives the "
             "retrospective restatement adjustment (IAS 8.26).")
    adjustment_account_id = fields.Many2one(
        'account.account', string="Adjustment (other side) account",
        check_company=True,
        help="The restated asset or liability account posted against opening "
             "retained earnings.")
    journal_id = fields.Many2one(
        'account.journal', string="Journal",
        check_company=True,
        domain="[('type', '=', 'general')]",
        help="Journal for the opening retained-earnings restatement entry.")
    move_id = fields.Many2one(
        'account.move', string="Restatement entry", readonly=True, copy=False,
        check_company=True,
        help="Journal entry posting the opening retained-earnings "
             "restatement.")
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

    # Fields locked once the restatement is posted, so a posted opening
    # retained-earnings adjustment cannot be retro-edited out from under its
    # journal entry. Correcting a posted restatement runs through
    # action_reset_to_draft, which reverses the move first (IAS 8 audit
    # trail). Editing the restatement lines is blocked by the same guard.
    _FROZEN_AFTER_POSTED = (
        'change_date', 'restatement_date', 'change_type',
        'equity_presentation', 'retained_earnings_account_id',
        'adjustment_account_id', 'journal_id', 'line_ids', 'company_id',
        'name', 'comparative_mode',
    )
    _TRANSITION_INPUT_FIELDS = (
        'state', 'company_id', 'currency_id', 'change_date',
        'restatement_date', 'change_type', 'application',
        'equity_presentation', 'retained_earnings_impact',
        'retained_earnings_account_id', 'adjustment_account_id',
        'journal_id', 'line_ids', 'comparative_mode', 'move_id',
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

    @api.depends('move_id', 'legacy_remediation_move_id')
    def _compute_move_count(self):
        for c in self:
            c.move_count = len(c.move_id | c.legacy_remediation_move_id)

    def _eh_assert_provenance_ready(self):
        self.ensure_one()
        if self.legacy_provenance_quarantined:
            raise UserError(_(
                "Legacy accounting-change evidence on %(name)s is "
                "quarantined and cannot drive a ledger transition until it "
                "has been reviewed and corrected. %(note)s",
                name=self.display_name,
                note=self.legacy_provenance_quarantine_note or '',
            ))

    @api.depends('change_type')
    def _compute_application(self):
        for c in self:
            c.application = (
                'prospective' if c.change_type == 'estimate_change'
                else 'retrospective')
            c.equity_presentation = {
                'policy_change': 'retrospective_application',
                'error_correction': 'retrospective_restatement',
                'estimate_change': 'prospective_change',
            }.get(c.change_type, 'retrospective_application')

    @api.constrains('change_date', 'restatement_date', 'change_type')
    def _check_restatement_date(self):
        for change in self:
            if (
                change.change_type != 'estimate_change'
                and change.change_date
                and change.restatement_date
                and change.restatement_date > change.change_date
            ):
                raise ValidationError(_(
                    "The opening-balance restatement date (%(posting)s) "
                    "cannot be after the accounting-change date (%(change)s).",
                    posting=change.restatement_date,
                    change=change.change_date,
                ))

    @api.depends(
        'comparative_mode', 'line_ids.adjustment', 'line_ids.role',
        'line_ids.account_id', 'line_ids.account_id.account_type',
    )
    def _compute_impact(self):
        for c in self:
            # Retained-earnings captions are presentation of the balancing
            # equity effect, not a second economic adjustment. Summing both
            # affected items and their retained-equity caption doubled the
            # restatement. Derive the plug only from affected-item rows.
            affected = c.line_ids.filtered(
                lambda line: line.role == 'affected_item',
            )
            c.retained_earnings_impact = sum(
                line._eh_comparative_ledger_effect()
                if c.comparative_mode and line.account_id
                else line.adjustment
                for line in affected
            )

    @api.constrains(
        'journal_id', 'retained_earnings_account_id',
        'adjustment_account_id', 'comparative_mode', 'line_ids',
    )
    def _check_accounting_configuration(self):
        """Enforce the GL classifications server-side, not only in domains."""
        for change in self:
            if change.journal_id and change.journal_id.type != 'general':
                raise ValidationError(_(
                    "Accounting-change restatements must use a general "
                    "journal."
                ))
            retained = change.retained_earnings_account_id
            adjustment = change.adjustment_account_id
            if retained and retained.account_type != 'equity':
                raise ValidationError(_(
                    "The retained-earnings account must be classified as "
                    "equity."
                ))
            if retained and adjustment and retained == adjustment:
                raise ValidationError(_(
                    "The retained-earnings and adjustment accounts must be "
                    "different accounts."
                ))
            if (
                adjustment
                and adjustment.account_type in _PROFIT_AND_LOSS_ACCOUNT_TYPES
            ):
                raise ValidationError(_(
                    "A prior-period opening restatement cannot post to a "
                    "current-period income or expense account. Represent "
                    "the prior-period result through opening retained "
                    "earnings and select only a balance-sheet adjustment "
                    "account."
                ))
            if change.comparative_mode:
                change.line_ids._eh_check_comparative_account_classification()

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            company_ids = {
                vals.get('company_id', self.env.company.id)
                for vals in vals_list
            }
            companies = self.env['res.company'].browse(company_ids)
            companies._eh_check_access('read')
            if companies - self.env.companies:
                raise AccessError(_(
                    "An accounting change must belong to one of the "
                    "actor's allowed companies."
                ))
        for vals in vals_list:
            change_date = fields.Date.to_date(
                vals.get('change_date')
                or self.env.context.get('default_change_date')
                or fields.Date.context_today(self)
            )
            vals.setdefault('restatement_date', change_date)
            if vals.get('name', '/') == '/':
                company = (
                    self.env['res.company'].browse(vals['company_id'])
                    if vals.get('company_id') else self.env.company
                )
                vals['name'] = next_company_reference(
                    self.env, 'eh.accounting.change', company, change_date,
                ) or '/'
        return super().create(vals_list)

    def write(self, vals):
        if (
            'change_date' in vals
            and 'restatement_date' not in vals
            and all(
                change.restatement_date == change.change_date
                for change in self
            )
        ):
            # Compatibility for records still using old one-date behaviour:
            # while posting date has never diverged, moving the change date
            # moves both. Once explicitly separated, callers must make the
            # posting-date decision themselves.
            vals = dict(vals, restatement_date=vals['change_date'])
        if self._LOCKED_WRITE_FIELDS.intersection(vals):
            self._eh_lock_transitions()
        if vals.get('company_id'):
            for change in self.filtered(
                    lambda record: record.company_id.id
                    != vals['company_id']):
                linked = self.env['account.move'].sudo().search_count([
                    ('eh_accounting_change_id', '=', change.id),
                ])
                if linked:
                    raise UserError(_(
                        "The company is immutable once an accounting change "
                        "has stamped journal evidence."
                    ))
        if vals.get('company_id') and not self.env.su:
            company = self.env['res.company'].browse(vals['company_id'])
            company._eh_check_access('read')
            if company not in self.env.companies:
                raise AccessError(_(
                    "An accounting change cannot be moved to a company "
                    "outside the actor's allowed companies."
                ))
        # Posted-figure INPUTS are frozen for everyone (restate via reversal
        # through action_reset_to_draft): a data-integrity guard, not su-gated.
        # STATE transitions are enforced by the inherited eh.workflow.guard,
        # which blocks a non-superuser direct write; the sanctioned posting/
        # reset actions run under sudo. Provenance is env.su, not a context key.
        frozen = [f for f in self._FROZEN_AFTER_POSTED if f in vals]
        if frozen:
            for c in self:
                if c.state in ('posted', 'reversed'):
                    raise UserError(_(
                        "%(name)s carries a posted opening retained-earnings "
                        "restatement; %(fields)s cannot be changed. Reset the "
                        "restatement to draft (which reverses the entry) "
                        "before editing.",
                        name=c.name, fields=', '.join(frozen)))
        remediation_inputs = {
            'legacy_remediation_reference', 'legacy_remediation_date',
        }.intersection(vals)
        if remediation_inputs and any(c.state == 'reversed' for c in self):
            raise UserError(_(
                "Legacy remediation evidence is immutable after the exact "
                "counter-entry posts."
            ))
        return super().write(vals)

    def action_post_restatement(self):
        """Post the opening retained-earnings restatement to the GL.

        For a retrospective change or error correction, this books the
        restatement against opening retained earnings, balanced by
        construction. A change in estimate is prospective and cannot be posted
        (IAS 8.36); posting is manager-gated. Records that never post keep the
        register behaviour unchanged.

        Two posting shapes:

        * Single-adjustment (default): the net ``retained_earnings_impact`` is
          booked between the retained-earnings equity account and one restated
          asset/liability (``adjustment_account_id``).
        * Per-account comparative trail (``comparative_mode``): each
          restatement line carrying an affected ``account_id`` posts its own
          adjustment leg against that account, and the sum is booked to the
          retained-earnings account across the prior periods (IAS 8.22, 42).
        """
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can post an opening "
                "retained-earnings restatement."))
        self._eh_lock_transitions()
        self._compute_application()
        self._compute_impact()
        self.flush_recordset(['application', 'retained_earnings_impact'])
        # Run as su so the guarded state write below passes the inherited
        # eh.workflow.guard; env.user is preserved for the audit trail.
        self = self._eh_workflow_action()
        for c in self:
            c._eh_assert_provenance_ready()
            # Recheck legacy rows at posting time: ``check_company`` protects
            # new writes but an upgraded database may already contain a
            # foreign journal/account link.
            c._check_company()
            c.line_ids._check_company()
            c._check_accounting_configuration()
            if c.state != 'draft':
                raise UserError(_(
                    "%s is not an unposted draft.", c.name))
            if c.application != 'retrospective':
                raise UserError(_(
                    "Only retrospective changes and error corrections post an "
                    "opening retained-earnings restatement. A change in "
                    "accounting estimate is applied prospectively (IAS 8.36) "
                    "and cannot be posted."))
            if not c.journal_id:
                raise UserError(_(
                    "Set the journal before posting the restatement."))
            if not c.retained_earnings_account_id:
                raise UserError(_(
                    "Set the retained-earnings account before posting the "
                    "restatement."))
            currency = c.currency_id or c.company_id.currency_id
            rounding = currency.rounding or 0.01
            if c.comparative_mode:
                move_line_vals = c._eh_comparative_move_lines(currency)
            else:
                move_line_vals = c._eh_single_move_lines(currency, rounding)
            move = self.env['account.move'].with_context(**{
                EVENT_MOVE_CONTEXT: EVENT_MOVE_CAPABILITY,
            })._eh_create_sealed({
                'move_type': 'entry',
                'journal_id': c.journal_id.id,
                'date': c.restatement_date,
                'ref': _("Restatement: %s", c.name),
                'company_id': c.company_id.id,
                'eh_accounting_change_id': c.id,
                'eh_event_provenance_version': EVENT_PROVENANCE_VERSION,
                'line_ids': move_line_vals,
                'eh_sealed': True,
            })
            move.action_post()
            c.move_id = move.id
            c.state = 'posted'
        return True

    def _eh_lock_transitions(self):
        """Authorize and serialize posting/reset on each source record."""
        self._eh_check_access('write')
        ids = sorted(self.ids)
        if not ids:
            return self
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_accounting_change WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            (tuple(ids),),
        )
        self.invalidate_recordset(self._TRANSITION_INPUT_FIELDS)
        self._check_company()
        if not self.env.su and any(
            change.company_id not in self.env.companies for change in self
        ):
            raise AccessError(_(
                "The accounting-change company is outside the actor's "
                "allowed companies."
            ))
        return self

    def _eh_single_move_lines(self, currency, rounding):
        """Original lumped opening-RE plug: net impact vs one account."""
        self.ensure_one()
        if not self.adjustment_account_id:
            raise UserError(_(
                "Set the adjustment (other side) account before posting the "
                "restatement."))
        impact = currency.round(self.retained_earnings_impact)
        if float_is_zero(impact, precision_rounding=rounding):
            raise UserError(_(
                "The restatement has no net impact on opening retained "
                "earnings; there is nothing to post."))
        # A positive impact increases opening retained earnings (a credit to
        # the equity account, debit to the adjustment account); a negative
        # impact reverses it. Legs net to zero by construction.
        re_debit = -impact if impact < 0 else 0.0
        re_credit = impact if impact > 0 else 0.0
        adj_debit = impact if impact > 0 else 0.0
        adj_credit = -impact if impact < 0 else 0.0
        return [
            (0, 0, {
                'name': _("Opening retained earnings restatement"),
                'account_id': self.retained_earnings_account_id.id,
                'debit': re_debit,
                'credit': re_credit,
            }),
            (0, 0, {
                'name': _("Restated %s", self.adjustment_account_id.name),
                'account_id': self.adjustment_account_id.id,
                'debit': adj_debit,
                'credit': adj_credit,
            }),
        ]

    def _eh_comparative_move_lines(self, currency):
        """Per-account opening-RE restatement across the prior periods.

        Each line carrying an affected account posts its own adjustment leg to
        that account; the offsetting opening-retained-earnings leg is the sum
        of those adjustments, so the entry balances by construction. The
        retained-earnings account itself is not an affected line: booking its
        adjustment there directly would double-count against the balancing leg.
        """
        self.ensure_one()
        re_account = self.retained_earnings_account_id
        nonzero_lines = self.line_ids.filtered(
            lambda ln: (
                ln.role == 'affected_item'
                and not currency.is_zero(currency.round(ln.adjustment))
            )
        )
        omitted = nonzero_lines.filtered(
            lambda ln: not ln.account_id or ln.account_id == re_account
        )
        if omitted:
            raise UserError(_(
                "Every non-zero comparative restatement line must post to "
                "an affected account different from retained earnings. "
                "The following line(s) would otherwise be omitted from the "
                "general ledger: %s",
                ', '.join(omitted.mapped('display_name')),
            ))
        account_lines = nonzero_lines
        if not account_lines:
            raise UserError(_(
                "The per-account comparative trail needs at least one "
                "restatement line with an affected account (other than the "
                "retained-earnings account) before it can be posted."))
        move_lines = []
        re_total = 0.0
        for line in account_lines:
            ledger_effect = currency.round(
                line._eh_comparative_ledger_effect(),
            )
            if currency.is_zero(ledger_effect):
                continue
            # ``adjustment`` is a financial-statement presentation movement.
            # Assets carry natural debit balances; liabilities/equity carry
            # natural credit balances. Convert presentation movement to one
            # debit-positive ledger effect before building both legs.
            move_lines.append((0, 0, {
                'name': _("Restated %(item)s%(period)s",
                          item=line.name,
                          period=line._eh_period_suffix()),
                'account_id': line.account_id.id,
                'debit': ledger_effect if ledger_effect > 0 else 0.0,
                'credit': -ledger_effect if ledger_effect < 0 else 0.0,
            }))
            re_total += ledger_effect
        re_total = currency.round(re_total)
        if not move_lines:
            raise UserError(_(
                "The per-account comparative trail has no non-zero ledger "
                "adjustment to post."))
        # Balancing opening-retained-earnings leg: mirror of the summed
        # per-account adjustments, so total debits equal total credits. A
        # genuine balance-sheet reclassification can net to zero retained
        # earnings; its affected-account legs already balance and need no
        # fabricated zero equity line.
        if not currency.is_zero(re_total):
            move_lines.append((0, 0, {
                'name': _("Opening retained earnings restatement"),
                'account_id': re_account.id,
                'debit': -re_total if re_total < 0 else 0.0,
                'credit': re_total if re_total > 0 else 0.0,
            }))
        return move_lines

    def action_reset_to_draft(self):
        """Reverse the posted restatement and reopen the record (manager only).

        Editing a posted restatement out of band would silently alter an
        opening balance-sheet adjustment. The only sanctioned correction path
        reverses the posted move (preserving both legs in the ledger for the
        IAS 8 audit trail), clears the link, and returns the record to draft
        so a corrected restatement can be re-posted.
        """
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can reset a posted "
                "restatement to draft."))
        self._eh_lock_transitions()
        for c in self:
            c._eh_assert_provenance_ready()
            if c.state != 'posted':
                raise UserError(_(
                    "%s is not posted; there is nothing to reset.", c.name))
            move = c.move_id
            if move:
                c._eh_validate_restatement_move(move)
                reversal = move.with_context(**{
                    EVENT_MOVE_CONTEXT: EVENT_MOVE_CAPABILITY,
                })._eh_reverse_with_verified_capability([{
                    'date': c.restatement_date,
                    'journal_id': move.journal_id.id,
                    'ref': _("Reversal of restatement: %s", c.name),
                }], cancel=False)
                reversal.with_context(**{
                    EVENT_MOVE_CONTEXT: EVENT_MOVE_CAPABILITY,
                })._eh_write_sealed_metadata(
                    {
                        'eh_accounting_change_id': c.id,
                        'eh_event_provenance_version':
                            EVENT_PROVENANCE_VERSION,
                    },
                    {
                        'eh_accounting_change_id',
                        'eh_event_provenance_version',
                    },
                )
                reversal._eh_post_verified_reversal()
                c._eh_seal_reversal(reversal)
                c._eh_validate_reversal(move, reversal)
            # state moves to draft through sudo so the inherited
            # eh.workflow.guard recognises this as a sanctioned action write
            # (env.su), not a forgeable direct RPC re-key.
            c.sudo().write({'state': 'draft', 'move_id': False})
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
            or move.date != self.restatement_date
            or move.eh_event_provenance_version != 0
            or move.eh_subsequent_event_id
            or (
                move.eh_accounting_change_id
                and move.eh_accounting_change_id != self
            )
            or self._eh_move_signature(move) != self._eh_expected_signature()
        ):
            raise UserError(_(
                "Legacy restatement evidence does not exactly match the "
                "frozen source and cannot use controlled remediation."
            ))
        if self.env['account.move'].sudo().search_count([
            ('reversed_entry_id', '=', move.id),
        ]):
            raise UserError(_(
                "Legacy restatement evidence already has a counter-entry."
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
            or reversal.eh_accounting_change_id != self
            or reversal.eh_subsequent_event_id
            or reversal.eh_event_provenance_version
               != EVENT_PROVENANCE_VERSION
            or self._eh_move_signature(reversal) != reverse_signature
        ):
            raise UserError(_(
                "Legacy restatement neutralisation failed exact sealed "
                "provenance validation."
            ))

    def action_remediate_legacy_provenance(self):
        """Neutralise exact quarantined evidence; never bless or delete it."""
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can remediate quarantined "
                "restatement evidence."
            ))
        self._eh_lock_transitions()
        for change in self:
            if not change.legacy_provenance_quarantined:
                raise UserError(_("This restatement is not quarantined."))
            if change.state != 'posted':
                raise UserError(_(
                    "Only posted quarantined evidence can be neutralised."
                ))
            reference = (change.legacy_remediation_reference or '').strip()
            if len(reference) < 8:
                raise UserError(_(
                    "Enter a manager/auditor remediation reference of at "
                    "least eight characters."
                ))
            remediation_date = change.legacy_remediation_date
            primary = change._eh_validate_legacy_primary_for_remediation()
            if not remediation_date:
                raise UserError(_("Choose a remediation accounting date."))
            if remediation_date < primary.date:
                raise UserError(_(
                    "Remediation date %(date)s cannot precede legacy entry "
                    "date %(primary)s.",
                    date=remediation_date, primary=primary.date,
                ))
            if remediation_date > fields.Date.context_today(change):
                raise UserError(_(
                    "A legacy remediation counter-entry cannot be future-"
                    "dated."
                ))
            remediation_line_label = _("Audited remediation")
            values = {
                'move_type': 'entry',
                'company_id': change.company_id.id,
                'date': remediation_date,
                'journal_id': change.journal_id.id,
                'ref': _("Audited remediation of %s", change.name),
                'eh_accounting_change_id': change.id,
                'eh_event_provenance_version': EVENT_PROVENANCE_VERSION,
                'reversed_entry_id': primary.id,
                'eh_sealed': True,
                'line_ids': [(0, 0, {
                    'name': "%s: %s" % (
                        remediation_line_label, line.name or change.name,
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
                change._eh_validate_legacy_remediation(primary, reversal)
                change.sudo().write({
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

    def _eh_expected_move_lines(self):
        self.ensure_one()
        currency = self.currency_id or self.company_id.currency_id
        if self.comparative_mode:
            commands = self._eh_comparative_move_lines(currency)
        else:
            commands = self._eh_single_move_lines(
                currency, currency.rounding or 0.01)
        return commands

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
        return sorted(
            (
                values['account_id'],
                currency.round(values.get('debit', 0.0)),
                currency.round(values.get('credit', 0.0)),
            )
            for _operation, _virtual_id, values
            in self._eh_expected_move_lines()
        )

    def _eh_validate_restatement_move(self, move):
        self.ensure_one()
        if not self.env.su:
            move._eh_check_access('read')
        if (
            move.state != 'posted'
            or not move.eh_sealed
            or move.company_id != self.company_id
            or move.eh_accounting_change_id != self
            or move.eh_subsequent_event_id
            or move.eh_event_provenance_version
               != EVENT_PROVENANCE_VERSION
            or move.reversed_entry_id
            or move.journal_id != self.journal_id
            or move.date != self.restatement_date
            or self._eh_move_signature(move) != self._eh_expected_signature()
        ):
            raise UserError(_(
                "The linked restatement entry no longer proves the sealed "
                "posting produced by this accounting change. It will not be "
                "reversed or unlinked."
            ))

    def _eh_validate_reversal(self, original, reversal):
        self.ensure_one()
        if len(reversal) != 1:
            raise UserError(_(
                "The restatement reversal did not produce exactly one entry."
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
            or rev.eh_accounting_change_id != self
            or rev.eh_subsequent_event_id
            or rev.eh_event_provenance_version
               != EVENT_PROVENANCE_VERSION
            or rev.reversed_entry_id != original
            or rev.journal_id != self.journal_id
            or self._eh_move_signature(rev) != reverse_signature
        ):
            raise UserError(_(
                "The restatement reversal failed sealed provenance "
                "validation."
            ))

    def unlink(self):
        self._eh_lock_transitions()
        for c in self:
            if c.state in ('posted', 'reversed') or c.move_id:
                raise UserError(_(
                    "%s has a posted opening retained-earnings restatement "
                    "and cannot be deleted. This preserves the IAS 8 audit "
                    "trail.", c.name))
        return super().unlink()

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
                self._eh_validate_restatement_move(self.move_id)
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.move_id.id,
        }


class EhAccountingChangeLine(models.Model):
    _name = 'eh.accounting.change.line'
    _description = "Accounting change restatement line"
    _order = 'change_id, sequence, id'
    _check_company_auto = True

    change_id = fields.Many2one(
        'eh.accounting.change', required=True, ondelete='cascade', index=True,
        check_company=True)
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one(
        related='change_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='change_id.currency_id', store=True, readonly=True)

    name = fields.Char(
        required=True,
        help="Affected financial-statement line item.")
    role = fields.Selection(
        [
            ('affected_item', "Affected item"),
            ('retained_earnings', "Retained-earnings caption"),
            ('disclosure', "Disclosure only"),
        ],
        required=True, default='affected_item', index=True,
        help="Affected-item rows determine the opening retained-earnings "
             "plug. A retained-earnings caption presents that same plug and "
             "is not summed again. Disclosure rows never drive posting.",
    )
    # Optional per-line prior period the figures belong to (IAS 8.22, 42:
    # each prior period presented is restated). Left blank the line behaves
    # exactly as before; populated it lets one register carry the comparative
    # trail across more than one prior year.
    fiscal_year = fields.Integer(
        string="Prior year",
        help="Prior financial year these comparative figures relate to. Used "
             "by the per-account comparative trail to restate more than one "
             "prior period.")
    period_label = fields.Char(
        string="Period",
        help="Free-form label for the prior period (for example a half-year "
             "or quarter) when a bare year is not enough.")
    # Optional affected general-ledger account. Populated only for the
    # per-account comparative trail, where each affected line posts its own
    # adjustment leg to this account against opening retained earnings.
    account_id = fields.Many2one(
        'account.account', string="Affected account",
        check_company=True,
        help="General-ledger account this restatement line affects. Set only "
             "for the per-account comparative trail; the adjustment is posted "
             "to this account against opening retained earnings.")
    as_previously_reported = fields.Monetary(currency_field='currency_id')
    adjustment = fields.Monetary(
        currency_field='currency_id',
        help="Restatement adjustment (signed).")
    as_restated = fields.Monetary(
        compute='_compute_restated', store=True, currency_field='currency_id')

    _FROZEN_LINE_FIELDS = (
        'name', 'as_previously_reported', 'adjustment', 'sequence',
        'account_id', 'fiscal_year', 'period_label', 'role', 'change_id')
    _SERVER_OWNED_FIELDS = frozenset({
        'company_id', 'currency_id', 'as_restated',
    })

    def _eh_period_suffix(self):
        """Readable ' (period)' suffix for a move line, or '' when unset."""
        self.ensure_one()
        label = self.period_label or (
            str(self.fiscal_year) if self.fiscal_year else '')
        return _(" (%s)", label) if label else ''

    def _eh_comparative_ledger_effect(self):
        """Return debit-positive GL effect for presentation adjustment."""
        self.ensure_one()
        account_type = self.account_id.account_type
        if account_type in _PROFIT_AND_LOSS_ACCOUNT_TYPES:
            raise ValidationError(_(
                "Comparative restatement line %(line)s uses income/expense "
                "account %(account)s. Prior-period profit or loss belongs in "
                "opening retained earnings, not current-period P&L.",
                line=self.display_name,
                account=self.account_id.display_name,
            ))
        if account_type in _NATURAL_CREDIT_ACCOUNT_TYPES:
            return -self.adjustment
        return self.adjustment

    def _eh_check_comparative_account_classification(self):
        for line in self.filtered(
            lambda record: (
                record.change_id.comparative_mode
                and record.role == 'affected_item'
                and record.account_id
            ),
        ):
            line._eh_comparative_ledger_effect()

    @api.constrains('account_id', 'role', 'change_id')
    def _check_comparative_account_classification(self):
        self._eh_check_comparative_account_classification()

    @api.depends('as_previously_reported', 'adjustment')
    def _compute_restated(self):
        for line in self:
            line.as_restated = line.as_previously_reported + line.adjustment

    @api.model_create_multi
    def create(self, vals_list):
        # Appending a line to a posted restatement would recompute the stored
        # retained_earnings_impact off the posted move, silently drifting the
        # parent figures away from the entry in the ledger. Freezing a posted
        # restatement therefore requires a create guard alongside write/unlink;
        # a create-append hole is not optional. The sanctioned correction path
        # (action_reset_to_draft) flips the parent back to draft before any
        # line is added, so it passes this guard unchanged.
        if not self.env.su and any(
            self._SERVER_OWNED_FIELDS.intersection(vals)
            for vals in vals_list
        ):
            raise AccessError(_(
                "Restatement line company, currency, and computed output "
                "are server-owned."
            ))
        changes = self._eh_lock_and_authorize_parents(
            [v['change_id'] for v in vals_list if v.get('change_id')])
        for change in changes:
            if change.state != 'draft':
                raise UserError(_(
                    "%s is posted; a restatement line cannot be added. Reset "
                    "the restatement to draft (which reverses the entry) "
                    "before editing.", change.name))
        return super().create(vals_list)

    def _eh_lock_and_authorize_parents(self, parent_ids):
        changes = self.env['eh.accounting.change'].browse(
            sorted(set(parent_ids)))
        if changes and not self.env.su:
            changes._eh_check_access('write')
        if changes:
            changes.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM eh_accounting_change WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(changes.ids),),
            )
            changes.invalidate_recordset()
        return changes

    def _eh_check_parent_not_posted(self, action):
        # A posted restatement is booked to the GL; its lines feed the stored
        # retained_earnings_impact and the disclosure, so a direct line edit
        # or unlink would drift the record off the posted move. Block it unless
        # the restatement has first been reset to draft (which reverses the
        # entry). The parent reset flips the parent to draft before any line
        # edits, so the sanctioned path passes.
        for line in self:
            if line.change_id.state != 'draft':
                raise UserError(_(
                    "This restatement is posted; its lines cannot be %s. "
                    "Reset the restatement to draft (which reverses the "
                    "entry) before editing.", action))

    def write(self, vals):
        if not self.env.su and self._SERVER_OWNED_FIELDS.intersection(vals):
            raise AccessError(_(
                "Restatement line company, currency, and computed output "
                "are server-owned."
            ))
        parents = self.mapped('change_id')
        if vals.get('change_id'):
            parents |= self.env['eh.accounting.change'].browse(
                vals['change_id'])
        parents = self._eh_lock_and_authorize_parents(parents.ids)
        if any(parent.state != 'draft' for parent in parents):
            raise UserError(_(
                "A posted restatement line cannot be changed or reparented."
            ))
        if any(f in vals for f in self._FROZEN_LINE_FIELDS):
            self._eh_check_parent_not_posted(_("changed"))
        return super().write(vals)

    def unlink(self):
        self._eh_lock_and_authorize_parents(self.mapped('change_id').ids)
        self._eh_check_parent_not_posted(_("removed"))
        return super().unlink()
