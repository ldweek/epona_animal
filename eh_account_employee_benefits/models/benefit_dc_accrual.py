# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.benefit.dc.accrual: defined contribution accrual (IAS 19.51).

Deliberately trivial: when the employee has rendered service, the
contribution payable to a defined contribution plan is an expense and a
liability, no actuarial assumptions, no remeasurement. One record per
period, one sealed entry: Dr expense / Cr contributions payable. This is
deliberately limited to defined-contribution accounting under IAS 19.51;
other IAS 19 benefit classes are outside this module's scope.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .account_move import (
    BENEFIT_MOVE_CAPABILITY,
    BENEFIT_MOVE_CONTEXT,
    BENEFIT_PROVENANCE_VERSION,
)
from .benefit_support import (
    assert_accounting_date_open,
    next_company_reference,
)


class EhBenefitDcAccrual(models.Model):
    _name = 'eh.benefit.dc.accrual'
    _description = "Defined contribution accrual (IAS 19.51)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'period_date desc, id desc'
    _check_company_auto = True
    # Block a direct RPC write of state that would skip action_post and its
    # journal entry; only the flagged actions may move state.
    _eh_guarded_fields = (
        'state', 'move_id', 'currency_id', 'provenance_quarantined',
        'provenance_quarantine_note', 'provenance_remediated_by_id',
        'provenance_remediated_at',
    )

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('posted', "Posted"), ('reversed', "Reversed")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    period_date = fields.Date(
        required=True, tracking=True, string="Period",
        help="Service period the contribution relates to (the entry is "
             "dated here).")
    amount = fields.Monetary(
        currency_field='currency_id', required=True, tracking=True,
        help="Contribution payable to the defined contribution plan for "
             "service rendered in the period (IAS 19.51(a)).")

    expense_account_id = fields.Many2one(
        'account.account', string="Expense Account", tracking=True,
        check_company=True,
        domain="[('account_type', 'in', ['expense', 'expense_direct_cost'])]")
    payable_account_id = fields.Many2one(
        'account.account', string="Contributions Payable Account",
        tracking=True, check_company=True,
        domain="[('account_type', 'in', "
               "['liability_current', 'liability_payable'])]")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        check_company=True,
        domain="[('type', '=', 'general'), ('company_id', '=', company_id)]")

    move_id = fields.Many2one(
        'account.move', string="Journal Entry", readonly=True, copy=False,
        check_company=True)
    move_ids = fields.One2many('account.move', 'eh_benefit_dc_accrual_id')
    move_count = fields.Integer(compute='_compute_move_count')
    provenance_quarantined = fields.Boolean(readonly=True, copy=False)
    provenance_quarantine_note = fields.Text(readonly=True, copy=False)
    provenance_remediation_reference = fields.Char(
        copy=False, tracking=True,
        help="Required manager/auditor reference for controlled remediation "
             "of quarantined legacy ledger evidence.")
    provenance_remediated_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False)
    provenance_remediated_at = fields.Datetime(readonly=True, copy=False)
    reversal_date = fields.Date(
        copy=False, tracking=True, string="Reversal Accounting Date",
        help="Exact accounting date for the sealed reversal. If blank, the "
             "original entry date is used; Odoo may not silently shift it.")

    notes = fields.Text()

    _check_amount = models.Constraint(
        'CHECK (amount >= 0)', 'A contribution accrual cannot be negative.')

    _FROZEN_FIELDS = ('period_date', 'amount', 'expense_account_id',
                      'payable_account_id', 'journal_id', 'company_id')
    _FROZEN_STATES = ('posted', 'reversed')

    @api.constrains(
        'journal_id', 'expense_account_id', 'payable_account_id',
    )
    def _check_accounting_configuration(self):
        for accrual in self:
            if accrual.journal_id and accrual.journal_id.type != 'general':
                raise ValidationError(_(
                    "Defined-contribution accruals must use a general journal."
                ))
            if accrual.expense_account_id and (
                accrual.expense_account_id.account_type
                not in ('expense', 'expense_direct_cost')
            ):
                raise ValidationError(_(
                    "The defined-contribution expense account must be an "
                    "expense account."
                ))
            if accrual.payable_account_id and (
                accrual.payable_account_id.account_type
                not in ('liability_current', 'liability_payable')
            ):
                raise ValidationError(_(
                    "The contributions-payable account must be a current or "
                    "payable liability account."
                ))

    def _eh_assert_provenance_ready(self):
        self.ensure_one()
        if self.provenance_quarantined:
            raise UserError(_(
                "Legacy defined-contribution provenance on %(name)s could "
                "not be proved. Accounting actions are blocked pending "
                "controlled remediation. %(note)s",
                name=self.display_name,
                note=self.provenance_quarantine_note or '',
            ))

    def _compute_move_count(self):
        for rec in self:
            rec.move_count = len(rec.move_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                company = self.env['res.company'].browse(
                    vals.get('company_id') or self.env.company.id,
                ).exists()
                sequence_date = fields.Date.to_date(vals.get('period_date'))
                if company and sequence_date:
                    vals['name'] = next_company_reference(
                        self.env, 'eh.benefit.dc.accrual', company,
                        sequence_date,
                    ) or '/'
        return super().create(vals_list)

    def write(self, vals):
        # Posted-figure INPUTS are frozen for everyone (restate via reversal):
        # data integrity, not su-gated. STATE is enforced by the inherited
        # eh.workflow.guard (blocks a non-superuser direct write).
        self._eh_lock_transition()
        frozen = [f for f in self._FROZEN_FIELDS if f in vals]
        if frozen:
            locked = self.filtered(
                lambda r: r.state in self._FROZEN_STATES)
            if locked:
                raise UserError(_(
                    "Accrual figures (%(fields)s) are frozen once "
                    "posted (%(names)s). Reverse the accrual to "
                    "restate it.",
                    fields=', '.join(frozen),
                    names=', '.join(locked.mapped('display_name'))))
        if ('provenance_remediation_reference' in vals
                and self.filtered(lambda r: not r.provenance_quarantined)):
            raise UserError(_(
                "A provenance remediation reference can only be entered "
                "while legacy evidence is quarantined."))
        return super().write(vals)

    def unlink(self):
        self._eh_lock_transition()
        posted = self.filtered(lambda r: r.state in self._FROZEN_STATES)
        if posted:
            raise UserError(_(
                "A posted accrual cannot be deleted (%s); reverse it "
                "instead.", ', '.join(posted.mapped('display_name'))))
        return super().unlink()

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can post benefit accruals."))

    def _eh_assert_period_unposted(self):
        """Serialize and refuse a duplicate live economic-period booking."""
        self.ensure_one()
        key = (
            "eh.benefit.dc.accrual:post-once:%d:%s:%d:%d:%d" % (
                self.company_id.id,
                self.period_date,
                self.journal_id.id,
                self.expense_account_id.id,
                self.payable_account_id.id,
            )
        )
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [key],
        )
        duplicate = self.sudo().with_context(active_test=False).search([
            ('id', '!=', self.id),
            ('state', '=', 'posted'),
            ('company_id', '=', self.company_id.id),
            ('period_date', '=', self.period_date),
            ('journal_id', '=', self.journal_id.id),
            ('expense_account_id', '=', self.expense_account_id.id),
            ('payable_account_id', '=', self.payable_account_id.id),
        ], limit=1)
        if duplicate:
            raise UserError(_(
                "Defined-contribution period %(period)s was already posted "
                "as %(existing)s for this company and ledger route. Reverse "
                "that accrual before posting a replacement.",
                period=self.period_date,
                existing=duplicate.display_name,
            ))

    def _eh_lock_transition(self):
        if not self.env.su:
            self._eh_check_access('write')
        ids = sorted(self.ids)
        if ids:
            self.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM eh_benefit_dc_accrual WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(ids),),
            )
            self.invalidate_recordset()
            self._check_company()
        return self

    def action_post(self):
        self.ensure_one()
        self._check_manager()
        self._eh_lock_transition()
        self._eh_assert_provenance_ready()
        self._check_accounting_configuration()
        if self.state != 'draft':
            raise UserError(_("Only a draft accrual can be posted."))
        missing = []
        for fname, label in (
                ('journal_id', _("journal")),
                ('expense_account_id', _("expense account")),
                ('payable_account_id', _("contributions payable account"))):
            if not self[fname]:
                missing.append(label)
        if missing:
            raise UserError(_(
                "Configure the %(missing)s on %(name)s first.",
                missing=', '.join(missing), name=self.display_name))
        amount = self.currency_id.round(self.amount)
        if self.currency_id.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_("Enter a positive contribution amount."))
        self._eh_assert_period_unposted()
        assert_accounting_date_open(
            self.env, self.company_id, self.journal_id, self.period_date,
            _("defined-contribution posting"),
        )
        move = self.env['account.move'].with_context(**{
            BENEFIT_MOVE_CONTEXT: BENEFIT_MOVE_CAPABILITY,
        })._eh_create_sealed({
            'move_type': 'entry',
            'company_id': self.company_id.id,
            'date': self.period_date,
            'journal_id': self.journal_id.id,
            'ref': self.name,
            'eh_benefit_dc_accrual_id': self.id,
            'eh_benefit_provenance_version': BENEFIT_PROVENANCE_VERSION,
            'eh_sealed': True,
            'line_ids': [
                (0, 0, {'name': _("DC contribution expense %s", self.name),
                        'account_id': self.expense_account_id.id,
                        'debit': amount, 'credit': 0.0}),
                (0, 0, {'name': _("DC contributions payable %s", self.name),
                        'account_id': self.payable_account_id.id,
                        'debit': 0.0, 'credit': amount}),
            ],
        })
        move.action_post()
        self.sudo().write({
            'state': 'posted', 'move_id': move.id})
        return True

    def action_reverse(self):
        self.ensure_one()
        self._check_manager()
        self._eh_lock_transition()
        self._eh_assert_provenance_ready()
        self._check_accounting_configuration()
        if self.state != 'posted':
            raise UserError(_("Only a posted accrual can be reversed."))
        self._eh_validate_posting_move()
        reversal_date = self._eh_reversal_date()
        assert_accounting_date_open(
            self.env, self.company_id, self.journal_id, reversal_date,
            _("defined-contribution reversal"),
        )
        reversal = self.env['account.move'].with_context(**{
            BENEFIT_MOVE_CONTEXT: BENEFIT_MOVE_CAPABILITY,
        })._eh_create_sealed({
            'move_type': 'entry',
            'company_id': self.company_id.id,
            'date': reversal_date,
            'journal_id': self.journal_id.id,
            'ref': _("Reversal of %s", self.name),
            'eh_benefit_dc_accrual_id': self.id,
            'eh_benefit_provenance_version': BENEFIT_PROVENANCE_VERSION,
            'reversed_entry_id': self.move_id.id,
            'eh_sealed': True,
            'line_ids': [(0, 0, {
                'name': _("Reversal: %s", line.name or self.name),
                'account_id': line.account_id.id,
                'debit': line.credit, 'credit': line.debit,
            }) for line in self.move_id.line_ids],
        })
        reversal.action_post()
        if reversal.date != reversal_date:
            raise UserError(_(
                "Odoo shifted the accrual reversal date. Nothing was "
                "reversed; choose an open exact accounting date."))
        self._eh_validate_reversal(reversal)
        self.sudo().write({
            'state': 'reversed', 'reversal_date': reversal_date,
        })
        return True

    def _eh_reversal_date(self):
        self.ensure_one()
        requested = self.reversal_date or self.move_id.date
        if not requested:
            raise UserError(_("Choose a reversal accounting date."))
        if requested < self.move_id.date:
            raise UserError(_(
                "Reversal date %(reversal)s cannot precede original entry "
                "date %(original)s.",
                reversal=requested, original=self.move_id.date))
        return requested

    def _eh_validate_legacy_primary_for_remediation(self):
        self.ensure_one()
        move = self.move_id
        amount = self.currency_id.round(self.amount)
        expected = sorted([
            (self.expense_account_id.id, amount, 0.0),
            (self.payable_account_id.id, 0.0, amount),
        ])
        if (
            not move
            or move.state != 'posted'
            or move.eh_benefit_dc_accrual_id != self
            or move.eh_benefit_valuation_id
            or move.reversed_entry_id
            or move.company_id != self.company_id
            or move.journal_id != self.journal_id
            or move.date != self.period_date
            or self._eh_move_signature(move) != expected
        ):
            raise UserError(_(
                "Legacy accrual evidence does not match the frozen source "
                "and cannot use controlled remediation."))
        return move

    def action_remediate_provenance(self):
        """Neutralise exact quarantined ledger evidence under manager review."""
        self.ensure_one()
        self._check_manager()
        self._eh_lock_transition()
        if not self.provenance_quarantined:
            raise UserError(_("This accrual is not quarantined."))
        reference = (self.provenance_remediation_reference or '').strip()
        if len(reference) < 8:
            raise UserError(_(
                "Enter a manager/auditor remediation reference of at least "
                "eight characters."))
        primary = self._eh_validate_legacy_primary_for_remediation()
        evidence = self.move_ids
        if self.state == 'posted':
            reversal_date = self._eh_reversal_date()
            assert_accounting_date_open(
                self.env, self.company_id, self.journal_id, reversal_date,
                _("defined-contribution remediation reversal"),
            )
            values = {
                'move_type': 'entry',
                'company_id': self.company_id.id,
                'date': reversal_date,
                'journal_id': self.journal_id.id,
                'ref': _("Audited remediation of %s", self.name),
                'eh_benefit_dc_accrual_id': self.id,
                'eh_benefit_provenance_version': BENEFIT_PROVENANCE_VERSION,
                'reversed_entry_id': primary.id,
                'eh_sealed': True,
                'line_ids': [(0, 0, {
                    'name': _("Audited remediation: %s", line.name or self.name),
                    'account_id': line.account_id.id,
                    'debit': line.credit, 'credit': line.debit,
                }) for line in primary.line_ids],
            }
            move_model = self.env['account.move'].with_context(**{
                BENEFIT_MOVE_CONTEXT: BENEFIT_MOVE_CAPABILITY,
            })
            if primary.eh_legacy_unverified_seal and not primary.eh_sealed:
                reversal = primary.with_context(**{
                    BENEFIT_MOVE_CONTEXT: BENEFIT_MOVE_CAPABILITY,
                })._eh_reverse_reviewed_legacy_quarantine([values])
            else:
                reversal = move_model._eh_create_sealed(values)
                reversal.action_post()
            if reversal.date != reversal_date:
                raise UserError(_(
                    "Odoo shifted the remediation reversal date. Nothing "
                    "was remediated; choose an open exact date."))
            self._eh_validate_reversal(reversal)
        elif self.state == 'reversed':
            reversals = evidence - primary
            if len(reversals) != 1:
                raise UserError(_(
                    "Quarantined reversed evidence must contain one exact "
                    "counter-entry."))
            reversal = reversals
            reverse_signature = sorted(
                (account_id, credit, debit)
                for account_id, debit, credit
                in self._eh_move_signature(primary)
            )
            if (
                reversal.state != 'posted'
                or reversal.reversed_entry_id != primary
                or reversal.company_id != self.company_id
                or reversal.journal_id != self.journal_id
                or reversal.date < primary.date
                or self._eh_move_signature(reversal) != reverse_signature
            ):
                raise UserError(_(
                    "Legacy reversal does not exactly neutralise the primary "
                    "entry and cannot be released."))
            reversal_date = reversal.date
        else:
            raise UserError(_(
                "Only posted or reversed quarantined evidence can be "
                "remediated."))
        self.sudo().write({
            'state': 'reversed',
            'reversal_date': reversal_date,
            'provenance_quarantined': False,
            'provenance_quarantine_note': _(
                "Legacy evidence neutralised under review reference %s; "
                "original evidence remains immutable.", reference),
            'provenance_remediated_by_id': self.env.user.id,
            'provenance_remediated_at': fields.Datetime.now(),
        })
        return True

    def _eh_move_signature(self, move):
        self.ensure_one()
        currency = self.currency_id
        return sorted(
            (
                line.account_id.id,
                currency.round(line.debit),
                currency.round(line.credit),
            )
            for line in move.line_ids
            if not currency.is_zero(line.debit - line.credit)
        )

    def _eh_validate_posting_move(self):
        self.ensure_one()
        self._eh_assert_provenance_ready()
        self._check_accounting_configuration()
        move = self.move_id
        if not self.env.su and move:
            move._eh_check_access('read')
        amount = self.currency_id.round(self.amount)
        expected = sorted([
            (self.expense_account_id.id, amount, 0.0),
            (self.payable_account_id.id, 0.0, amount),
        ])
        if (
            not move
            or move.state != 'posted'
            or not move.eh_sealed
            or move.eh_benefit_dc_accrual_id != self
            or move.eh_benefit_valuation_id
            or move.eh_benefit_provenance_version
               != BENEFIT_PROVENANCE_VERSION
            or move.reversed_entry_id
            or move.company_id != self.company_id
            or move.journal_id != self.journal_id
            or move.date != self.period_date
            or self._eh_move_signature(move) != expected
        ):
            raise UserError(_(
                "The linked defined-contribution entry failed sealed "
                "provenance validation and will not be reversed."
            ))

    def _eh_validate_reversal(self, reversal):
        self.ensure_one()
        reverse_signature = sorted(
            (account_id, credit, debit)
            for account_id, debit, credit
            in self._eh_move_signature(self.move_id)
        )
        if (
            reversal.state != 'posted'
            or not reversal.eh_sealed
            or reversal.eh_benefit_dc_accrual_id != self
            or reversal.eh_benefit_valuation_id
            or reversal.eh_benefit_provenance_version
               != BENEFIT_PROVENANCE_VERSION
            or reversal.reversed_entry_id != self.move_id
            or reversal.company_id != self.company_id
            or reversal.journal_id != self.journal_id
            or reversal.date < self.move_id.date
            or (self.reversal_date and reversal.date != self.reversal_date)
            or self._eh_move_signature(reversal) != reverse_signature
        ):
            raise UserError(_(
                "The defined-contribution reversal failed sealed provenance "
                "validation."
            ))

    def action_view_moves(self):
        self.ensure_one()
        if not self.env.su:
            self._eh_check_access('read')
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('eh_benefit_dc_accrual_id', '=', self.id)],
        }
