# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.investment.property: property held to earn rentals or for capital
appreciation under IAS 40.

Under the fair value model the property is remeasured to fair value and the
change is recognised in profit or loss (IAS 40.35); the carrying amount then
rolls forward to the new fair value.
"""

import math

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, MissingError, UserError, ValidationError
from odoo.tools import float_compare, float_is_zero


_IP_MOVE_LINK_CAPABILITY = object()
_IP_TRANSFER_LOG_CAPABILITY = object()
_IP_ASSET_TRANSITION_CAPABILITY = object()
_IP_LEDGER_ROLL_CONTEXT_KEY = 'eh_ip_ledger_roll_capability'
_IP_LEDGER_ROLL_CAPABILITY = object()
_IP_DEPRECIATION_EVENT_CONTEXT_KEY = 'eh_ip_depreciation_event_capability'
_IP_DEPRECIATION_EVENT_CAPABILITY = object()


class EhInvestmentProperty(models.Model):
    _name = 'eh.investment.property'
    _description = "Investment property (IAS 40)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'acquisition_date desc, id desc'
    _rec_name = 'name'

    # eh.workflow.guard: the state machine is enforced by the action methods
    # below (each carries the eh_workflow_action flag). A plain user cannot
    # RPC-write state to skip an action and its journal entry.
    _eh_guarded_fields = (
        'state', 'carrying_amount', 'accumulated_depreciation',
        'revaluation_surplus',
        'remeasurement', 'transfer_in_asset_display', 'currency_id',
        'opening_move_id', 'depreciation_history_quarantined',
        'depreciation_history_quarantine_note',
        'opening_evidence_quarantined',
        'opening_evidence_quarantine_note',
        'source_provenance_quarantined',
        'source_provenance_quarantine_note',
        'reversed_by_id', 'reversed_at',
    )

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    description = fields.Char(help="Address or description of the property.")
    state = fields.Selection(
        [('draft', "Draft"), ('held', "Held"),
         ('disposed', "Disposed"), ('transferred', "Transferred"),
         ('reversed', "Reversed"), ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    model_basis = fields.Selection(
        [('fair_value', "Fair value model"), ('cost', "Cost model")],
        default='fair_value', required=True, tracking=True)
    acquisition_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)
    remeasurement_date = fields.Date(
        string="Remeasurement Date", copy=False,
        help="Accounting date for the next fair-value remeasurement.",
    )
    depreciation_date = fields.Date(
        string="Depreciation Date", copy=False,
        help="Accounting date identifying the fiscal depreciation period.",
    )
    initial_cost = fields.Monetary(
        currency_field='currency_id', tracking=True)
    carrying_amount = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id', tracking=True)
    fair_value = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Fair value at the current reporting date. For a cost-model "
             "property this is a disclosure-only measurement required by "
             "IAS 40.79(e); it never changes the posted carrying amount.")
    remeasurement = fields.Monetary(
        compute='_compute_remeasurement', store=True,
        currency_field='currency_id')
    zero_fair_value_confirmed = fields.Boolean(
        string="Confirm Zero Fair Value", copy=False,
        help="Manager attestation that a zero fair value is deliberate, not "
             "a missing measurement. Consumed by the next zero-value posting.",
    )
    zero_fair_value_reason = fields.Text(
        string="Zero Fair Value Evidence", copy=False,
        help="Required evidence supporting a deliberate zero fair value.",
    )

    # --- Cost model (IAS 40.56): held at cost less accumulated depreciation ---
    useful_life_years = fields.Integer(
        string="Remaining Useful Life (years)", tracking=True,
        help="Straight-line life from direct recognition, or the remaining "
             "useful life from the transfer date for a cost-model transfer-in.")
    residual_value = fields.Monetary(
        string="Residual Value", currency_field='currency_id', tracking=True,
        help="Estimated residual value excluded from the depreciable base "
             "under IAS 16.53 as applied by IAS 40.56.")
    accumulated_depreciation = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id', tracking=True)
    depreciation_expense_account_id = fields.Many2one(
        'account.account', string="Depreciation Expense Account",
        tracking=True,
        domain="[('account_type', 'in', ['expense', 'expense_depreciation'])]")
    accumulated_depreciation_account_id = fields.Many2one(
        'account.account', string="Accumulated Depreciation Account",
        tracking=True,
        domain="[('account_type', 'in', "
               "['asset_non_current', 'asset_fixed', 'asset_current'])]")
    depreciation_event_ids = fields.One2many(
        'eh.investment.property.depreciation', 'property_id',
        string="Depreciation Evidence", readonly=True, copy=False,
    )
    depreciation_history_quarantined = fields.Boolean(
        readonly=True, copy=False, index=True,
    )
    depreciation_history_quarantine_note = fields.Text(
        readonly=True, copy=False,
    )
    opening_evidence_quarantined = fields.Boolean(
        readonly=True, copy=False, index=True,
    )
    opening_evidence_quarantine_note = fields.Text(
        readonly=True, copy=False,
    )
    source_provenance_quarantined = fields.Boolean(
        readonly=True, copy=False, index=True,
    )
    source_provenance_quarantine_note = fields.Text(
        readonly=True, copy=False,
    )

    # --- IAS 40.57-62 transfers in and out of investment property ---
    transfer_target_account_id = fields.Many2one(
        'account.account', string="Transfer Target Account", tracking=True,
        help="PP&E or inventory account the property is reclassified into.")
    transfer_reason = fields.Char(string="Transfer Reason")
    transfer_date = fields.Date(
        string="Transfer Date", tracking=True, copy=False,
        help="Date of the change in use driving the transfer (IAS 40.57). "
             "Remeasurement and derecognition entries post on this date; "
             "left empty it defaults to today when the transfer runs.")
    transfer_fair_value = fields.Monetary(
        string="Fair Value at Transfer", currency_field='currency_id',
        tracking=True, copy=False,
        help="Fair value at the date of change in use. Under the fair "
             "value model the property is remeasured to this amount (gap "
             "to profit or loss, IAS 40.60-61) before derecognition and "
             "it becomes the deemed cost of the destination asset. Under "
             "the cost model a transfer never changes the carrying amount "
             "(IAS 40.59); an amount entered here is stored on the "
             "transfer audit trail for disclosure only. Leave at zero "
             "under the fair value model to transfer at the current "
             "carrying amount (already fair value, IAS 40.33).")
    revaluation_surplus_account_id = fields.Many2one(
        'account.account', string="Revaluation Surplus Account",
        tracking=True, domain="[('account_type', '=', 'equity')]",
        help="Equity (OCI) revaluation surplus account taking the uplift "
             "when an owner-occupied asset is transferred into investment "
             "property at a fair value above its carrying amount "
             "(IAS 40.61). It must match the reserve account bound to the "
             "source asset so one attributable balance cannot be routed "
             "through different equity accounts.")
    revaluation_surplus = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id', tracking=True,
        help="Revaluation reserve attributable to this property after an "
             "inbound owner-occupied transfer. It is moved off the terminal "
             "source asset and recycled directly to retained earnings on "
             "disposal, never through profit or loss.")
    retained_earnings_account_id = fields.Many2one(
        'account.account', string="Retained Earnings Account", tracking=True,
        domain="[('account_type', '=', 'equity')]",
        help="Equity account credited when an attributable revaluation "
             "surplus is recycled on disposal (IAS 16.41).")
    transfer_in_asset_id = fields.Many2one(
        'eh.asset', string="Source Fixed Asset", copy=False,
        ondelete='restrict', check_company=True,
        domain="[('company_id', '=', company_id), "
               "('deferred_type', '=', 'asset'), "
               "('asset_class', '=', 'tangible'), "
               "('state', 'in', ['running', 'paused'])]",
        help="Owner-occupied fixed asset being transferred. The workflow "
             "checks caller access, locks this exact asset, and freezes the "
             "source after the inbound transfer posts.")
    transfer_in_asset_display = fields.Char(
        string="Source Fixed Asset (Snapshot)", readonly=True, copy=False,
        help="Display name of the fixed asset this property was "
             "transferred in from (audit trail).")
    transfer_log_ids = fields.One2many(
        'eh.investment.property.transfer', 'property_id',
        string="Transfer Log", readonly=True, copy=False)

    # --- IAS 40.66-69 derecognition on disposal ---
    disposal_date = fields.Date(string="Disposal Date", tracking=True)
    disposal_proceeds = fields.Monetary(
        string="Disposal Proceeds", currency_field='currency_id',
        tracking=True,
        help="Net consideration received or receivable on disposal "
             "(IAS 40.69).")
    disposal_cash_account_id = fields.Many2one(
        'account.account', string="Proceeds / Cash Account", tracking=True,
        help="Cash or receivable account debited with the disposal "
             "proceeds.")
    disposal_gain_loss_account_id = fields.Many2one(
        'account.account', string="Disposal Gain / Loss Account",
        tracking=True,
        domain="[('account_type', 'in', ['income_other', 'expense'])]",
        help="Account taking the gain or loss on derecognition "
             "(IAS 40.69).")

    property_account_id = fields.Many2one(
        'account.account', string="Property Account", tracking=True)
    opening_counterpart_account_id = fields.Many2one(
        'account.account', string="Opening Cost Counterpart", tracking=True,
        help=(
            "Account credited when a directly acquired property is first "
            "recognised at cost (payable, cash or acquisition clearing)."
        ),
    )
    opening_move_id = fields.Many2one(
        'account.move', string="Opening Entry", readonly=True, copy=False,
        ondelete='restrict', check_company=True,
    )
    fv_gain_loss_account_id = fields.Many2one(
        'account.account', string="Fair Value Gain / Loss Account",
        tracking=True,
        domain="[('account_type', 'in', ['income_other', 'expense'])]")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        domain="[('type', '=', 'general'), ('company_id', '=', company_id)]")

    move_ids = fields.One2many('account.move', 'eh_investment_property_id')
    move_count = fields.Integer(compute='_compute_move_count')

    reversal_date = fields.Date(
        string="Correction Date", copy=False,
        help="Explicit open accounting date for controlled full reversal.",
    )
    reversed_by_id = fields.Many2one(
        'res.users', string="Reversed By", readonly=True, copy=False,
    )
    reversed_at = fields.Datetime(
        string="Reversed At", readonly=True, copy=False,
    )

    notes = fields.Text()

    # A fair value model property is remeasured, never depreciated
    # (IAS 40.33-35), so it can never carry accumulated depreciation. The
    # write() hook below cancels any pending balance when the basis switches
    # to the fair value model; this constraint makes the invariant a hard
    # database rule rather than an honour-system convention.
    _check_fv_basis_no_accum_dep = models.Constraint(
        "CHECK (model_basis != 'fair_value' "
        "OR COALESCE(accumulated_depreciation, 0) = 0)",
        'A fair value model investment property cannot carry accumulated '
        'depreciation; it is remeasured, never depreciated (IAS 40.33-35).',
    )

    @api.depends('fair_value', 'carrying_amount')
    def _compute_remeasurement(self):
        for p in self:
            p.remeasurement = p.fair_value - p.carrying_amount

    def _compute_move_count(self):
        for p in self:
            p.move_count = len(p.move_ids)

    def _has_posted_move(self):
        """Return True once any journal entry has been posted for this record.

        Carrying amount and accumulated depreciation are ledger-controlled
        totals; once a move exists they must not be edited by hand, only
        rolled forward by a further posting (IAS 40).
        """
        self.ensure_one()
        return bool(self.move_ids.filtered(
            lambda m: m.state == 'posted'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._eh_assert_company_allowed(
                vals.get('company_id') or self.env.company.id)
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.investment.property') or '/'
        properties = super().create(vals_list)
        properties._eh_validate_references()
        return properties

    # Ledger-controlled totals: only the posting actions below may roll them
    # forward. Once a move exists they are frozen against any other write so
    # the book value can never drift away from the posted ledger (IAS 40).
    # initial_cost, useful_life_years and model_basis are the measurement
    # inputs that fix the depreciable base and the valuation path; once a move
    # exists editing them would silently re-base every subsequent depreciation
    # charge (charge = initial_cost / useful_life_years) or switch the property
    # onto a valuation basis inconsistent with what has already been posted.
    _FROZEN_FIELDS = (
        'carrying_amount', 'accumulated_depreciation',
        'initial_cost', 'residual_value', 'useful_life_years',
        'model_basis', 'company_id',
        'acquisition_date', 'journal_id', 'property_account_id',
        'opening_counterpart_account_id', 'opening_move_id',
    )
    _LATE_CONFIG_FIELDS = (
        'fv_gain_loss_account_id', 'depreciation_expense_account_id',
        'accumulated_depreciation_account_id', 'transfer_target_account_id',
        'revaluation_surplus_account_id', 'disposal_cash_account_id',
        'disposal_gain_loss_account_id', 'retained_earnings_account_id',
    )
    _TERMINAL_EVENT_FIELDS = (
        'transfer_in_asset_id', 'transfer_date', 'transfer_fair_value',
        'transfer_reason', 'disposal_date', 'disposal_proceeds',
    )

    def write(self, vals):
        vals = dict(vals or {})
        provenance_fields = {
            'opening_move_id', 'opening_evidence_quarantined',
            'opening_evidence_quarantine_note',
            'depreciation_history_quarantined',
            'depreciation_history_quarantine_note',
            'source_provenance_quarantined',
            'source_provenance_quarantine_note',
        }
        if provenance_fields.intersection(vals) and self.env.context.get(
                _IP_LEDGER_ROLL_CONTEXT_KEY) is not _IP_LEDGER_ROLL_CAPABILITY:
            raise AccessError(_(
                "Investment-property opening and quarantine provenance is "
                "engine-owned and cannot be changed directly."
            ))
        if vals and not self.env.su:
            self._eh_check_access('write')
        self._eh_lock_properties()
        self._eh_validate_references(vals)
        if not (
            self.env.su
            and self.env.context.get(_IP_LEDGER_ROLL_CONTEXT_KEY)
            is _IP_LEDGER_ROLL_CAPABILITY
        ):
            if 'transfer_in_asset_id' in vals:
                inbound = self.filtered(
                    lambda prop: prop.transfer_log_ids.filtered(
                        lambda log: log.direction == 'in'))
                if inbound:
                    raise UserError(_(
                        "The fixed-asset source is immutable after transfer "
                        "evidence has been posted. Create an explicit "
                        "correction instead of repointing it."))
            terminal_touched = [
                field_name for field_name in self._TERMINAL_EVENT_FIELDS
                if field_name in vals
            ]
            terminal = self.filtered(
                lambda prop: prop.state in ('disposed', 'transferred'))
            if terminal and terminal_touched:
                raise UserError(_(
                    "Completed investment-property event inputs (%s) are "
                    "immutable. Record an explicit correction instead.",
                    ', '.join(terminal_touched)))
        # Depreciation halt on model switch (IAS 40.33-35): a property moved
        # onto the fair value model is remeasured, never depreciated, so any
        # pending accumulated depreciation captured while the record was on
        # the cost model is cancelled in the same write. The switch itself
        # is only reachable before a posted move exists (model_basis is
        # frozen after the first posting), at which point no depreciation
        # charge sits in the ledger and the reset is a pure state clean-up;
        # it also keeps the fair-value/no-accumulated-depreciation database
        # constraint satisfied by construction.
        engine_basis_reset = (
            vals.get('model_basis') == 'fair_value'
            and 'accumulated_depreciation' not in vals
        )
        if engine_basis_reset:
            vals = dict(vals, accumulated_depreciation=0.0)
        late_touched = [
            name for name in self._LATE_CONFIG_FIELDS if name in vals
        ]
        if late_touched and not (
            self.env.su
            and self.env.context.get(_IP_LEDGER_ROLL_CONTEXT_KEY)
            is _IP_LEDGER_ROLL_CAPABILITY
        ):
            for prop in self:
                for field_name in late_touched:
                    new_id = vals.get(field_name) or False
                    old_id = prop[field_name].id or False
                    if old_id == new_id:
                        continue
                    if prop.state == 'draft':
                        continue
                    if (
                        prop.state == 'held'
                        and not old_id
                        and new_id
                        and self.env.user.has_group(
                            'eh_account_base.group_eh_manager'
                        )
                    ):
                        continue
                    raise UserError(_(
                        "Later-event account %(field)s can be filled once by "
                        "an EH Accounting Manager while %(property)s is held; "
                        "an existing value cannot be changed or cleared.",
                        field=self._fields[field_name].string,
                        property=prop.display_name,
                    ))
        touched = [f for f in self._FROZEN_FIELDS if f in vals]
        if touched and not (
                self.env.su
                and self.env.context.get(_IP_LEDGER_ROLL_CONTEXT_KEY)
                is _IP_LEDGER_ROLL_CAPABILITY):
            for p in self:
                if p.state != 'draft' or p._has_posted_move():
                    raise UserError(_(
                        "Carrying amount and accumulated depreciation are "
                        "controlled by the posted ledger for %s and cannot "
                        "be edited directly once entries exist.",
                        p.display_name))
        # The state of a property that carries a posted GL move is itself a
        # control point: resetting it back to draft (or otherwise out of the
        # held / disposed / transferred posted states) would silently lift the
        # ledger-controlled freeze above and orphan its entries. A raw ORM
        # state write without the sanctioned-transition context flag is
        # manager-gated so a plain user cannot un-freeze a GL-backed property.
        # The action methods that legitimately move state carry the flag (they
        # only move INTO a posted state, or cancel a record with no moves).
        if 'state' in vals and not (
                self.env.su
                and self.env.context.get(_IP_LEDGER_ROLL_CONTEXT_KEY)
                is _IP_LEDGER_ROLL_CAPABILITY):
            for p in self:
                if p.state == vals['state']:
                    continue
                if p._has_posted_move() \
                        and vals['state'] not in self._POSTED_STATES:
                    if not self.env.user.has_group(
                            'eh_account_base.group_eh_manager'):
                        raise UserError(_(
                            "Only an EH Accounting Manager may change the "
                            "state of investment property %s once entries "
                            "have been posted for it.", p.display_name))
        if engine_basis_reset and not self.env.su:
            # Caller supplied only a legitimate draft basis change. Clear the
            # engine-owned total first, then write every caller-supplied value
            # as the caller. Savepoint keeps the pair atomic if caller write
            # later fails; no user value rides through sudo.
            caller_vals = dict(vals)
            caller_vals.pop('accumulated_depreciation', None)
            with self.env.cr.savepoint():
                engine_self = self.sudo().with_context(**{
                    _IP_LEDGER_ROLL_CONTEXT_KEY: _IP_LEDGER_ROLL_CAPABILITY,
                })
                super(EhInvestmentProperty, engine_self).write({
                    'accumulated_depreciation': 0.0,
                })
                result = super().write(caller_vals)
        else:
            result = super().write(vals)
        self._eh_check_finite_measurements()
        return result

    @api.constrains(
        'initial_cost', 'carrying_amount', 'fair_value',
        'transfer_fair_value', 'disposal_proceeds', 'residual_value',
        'accumulated_depreciation', 'revaluation_surplus',
    )
    def _eh_check_finite_measurements(self):
        labels = {
            'initial_cost': _("initial cost"),
            'carrying_amount': _("carrying amount"),
            'fair_value': _("fair value"),
            'transfer_fair_value': _("transfer fair value"),
            'disposal_proceeds': _("disposal proceeds"),
            'residual_value': _("residual value"),
            'accumulated_depreciation': _("accumulated depreciation"),
            'revaluation_surplus': _("revaluation surplus"),
        }
        for prop in self:
            for field_name, label in labels.items():
                value = prop[field_name] or 0.0
                if not math.isfinite(value) or value < 0:
                    raise ValidationError(_(
                        "Investment-property %(label)s must be a finite, "
                        "non-negative amount.", label=label,
                    ))
            if (
                prop.model_basis == 'cost'
                and prop.residual_value > prop.initial_cost
            ):
                raise ValidationError(_(
                    "Residual value cannot exceed the property's historical "
                    "cost."
                ))
            if (
                prop.model_basis == 'cost'
                and prop.accumulated_depreciation > (
                    prop.initial_cost - prop.residual_value
                )
            ):
                raise ValidationError(_(
                    "Accumulated depreciation cannot exceed the property's "
                    "depreciable amount after residual value."
                ))

    @api.constrains(
        'revaluation_surplus_account_id', 'retained_earnings_account_id',
    )
    def _eh_check_equity_accounts(self):
        for prop in self:
            for account in (
                prop.revaluation_surplus_account_id,
                prop.retained_earnings_account_id,
            ):
                if account and account.account_type != 'equity':
                    raise ValidationError(_(
                        "Revaluation surplus and retained earnings accounts "
                        "must be equity accounts."
                    ))

    # States in which the property carries a posted GL position: it has been
    # recognised (held) or derecognised (disposed / transferred). A move OUT of
    # these back to draft would strand the posted entries.
    _POSTED_STATES = ('held', 'disposed', 'transferred', 'reversed')

    def unlink(self):
        for p in self:
            if p._has_posted_move():
                raise UserError(_(
                    "Investment property %s cannot be deleted; it carries "
                    "posted journal entries that would be orphaned. Reverse "
                    "or cancel the entries first.", p.display_name))
        return super().unlink()

    def action_activate(self):
        for original in self.sorted('id'):
            p, _asset = original._eh_prepare_action()
            if p.state != 'draft':
                raise UserError(_("Only a draft property can be recognised."))
            p._eh_assert_accounting_ready()
            if not p.opening_counterpart_account_id:
                raise UserError(_(
                    "Configure the opening cost counterpart account before "
                    "recognising a directly acquired property."
                ))
            currency = p.currency_id
            cost = currency.round(p.initial_cost)
            if not math.isfinite(cost) or currency.compare_amounts(cost, 0) <= 0:
                raise UserError(_(
                    "A directly acquired property must be recognised at a "
                    "finite, positive cost."
                ))
            p._eh_validate_property_event_date(
                p.acquisition_date, _("initial recognition"),
            )
            move = p._post_move([
                (
                    p.property_account_id, cost, 0.0,
                    _("Initial recognition %s", p.name),
                ),
                (
                    p.opening_counterpart_account_id, 0.0, cost,
                    _("Initial recognition counterpart %s", p.name),
                ),
            ], date=p.acquisition_date)
            p._eh_ledger_write({
                'state': 'held',
                'carrying_amount': cost,
                'opening_move_id': move.id,
            })
        return True

    def action_remeasure(self):
        self, _asset = self._eh_prepare_action()
        if self.state != 'held':
            raise UserError(_("Only a held property can be remeasured."))
        if self.model_basis != 'fair_value':
            raise UserError(_(
                "Remeasurement to fair value applies only under the fair "
                "value model."))
        self._eh_assert_accounting_ready()
        if not self.fv_gain_loss_account_id:
            raise UserError(_(
                "Configure the journal, property account and fair value "
                "gain/loss account first."))
        currency = self.currency_id
        if not math.isfinite(self.fair_value) or self.fair_value < 0:
            raise UserError(_("Fair value must be finite and non-negative."))
        target = currency.round(self.fair_value)
        zero_attestation = currency.is_zero(target)
        if zero_attestation and (
            not self.zero_fair_value_confirmed
            or not (self.zero_fair_value_reason or '').strip()
        ):
            raise UserError(_(
                "A zero fair value would fully write off the property. Tick "
                "Confirm Zero Fair Value and record reviewer evidence first."
            ))
        change = currency.round(target - self.carrying_amount)
        if currency.is_zero(change):
            raise UserError(_(
                "Fair value equals the carrying amount; nothing to post."))
        if change > 0:
            lines = [
                (self.property_account_id, change, 0.0,
                 _("Fair value increase %s", self.name)),
                (self.fv_gain_loss_account_id, 0.0, change,
                 _("Fair value gain %s", self.name)),
            ]
        else:
            amount = -change
            lines = [
                (self.fv_gain_loss_account_id, amount, 0.0,
                 _("Fair value loss %s", self.name)),
                (self.property_account_id, 0.0, amount,
                 _("Fair value decrease %s", self.name)),
            ]
        event_date = self.remeasurement_date or fields.Date.context_today(self)
        self._eh_validate_property_event_date(event_date, _("remeasurement"))
        zero_reason = (self.zero_fair_value_reason or '').strip()
        self._post_move(lines, date=event_date)
        self._eh_ledger_write({
            'carrying_amount': target,
            'remeasurement_date': event_date,
            'zero_fair_value_confirmed': False,
            'zero_fair_value_reason': False,
        })
        if zero_attestation:
            self.message_post(body=_(
                "Zero fair value posted on %(date)s from explicit manager "
                "evidence: %(reason)s",
                date=event_date,
                reason=zero_reason,
            ))
        return True

    def action_depreciate(self):
        """Post the fiscal-period straight-line cost-model charge."""
        self, _asset = self._eh_prepare_action()
        event_date = self.depreciation_date or fields.Date.context_today(self)
        self._eh_post_cost_depreciation(event_date)
        return True

    def _eh_post_cost_depreciation(self, event_date, terminal=False):
        """Post cost depreciation, including a terminal-date catch-up.

        Ordinary posting recognises one fiscal-period charge, prorated from
        the acquisition date in the first fiscal year. A disposal/transfer
        with no charge for its fiscal year recognises only the days through
        the terminal date. Existing annual evidence means that fiscal period
        is already covered and is never duplicated.
        """
        self.ensure_one()
        if self.model_basis != 'cost':
            raise UserError(_(
                "Depreciation applies only under the cost model. Fair value "
                "model properties are remeasured, not depreciated."))
        if self.state != 'held':
            raise UserError(_("Only a held property can be depreciated."))
        self._eh_assert_accounting_ready()
        if self.depreciation_history_quarantined:
            raise UserError(_(
                "This legacy cost-model property has journal entries but no "
                "reconstructable period-level depreciation history. Resolve "
                "the quarantine before posting another charge."
            ))
        if self.useful_life_years <= 0:
            raise UserError(_(
                "Set a positive useful life in years before depreciating."))
        if not self.journal_id or not self.depreciation_expense_account_id \
                or not self.accumulated_depreciation_account_id:
            raise UserError(_(
                "Configure the journal, depreciation expense account and "
                "accumulated depreciation account first."))
        currency = self.currency_id
        event_date = fields.Date.to_date(event_date)
        fiscal_year = self.company_id.compute_fiscalyear_dates(event_date)
        period_start = fiscal_year['date_from']
        period_end = fiscal_year['date_to']
        existing = self.depreciation_event_ids.filtered(
            lambda event: (
                event.period_start == period_start
                and event.period_end == period_end
            )
        )
        if existing:
            if terminal:
                return False
            raise UserError(_(
                "Depreciation for fiscal period %(start)s to %(end)s has "
                "already been posted for %(property)s.",
                start=period_start, end=period_end,
                property=self.display_name,
            ))
        self._eh_validate_property_event_date(event_date, _("depreciation"))
        depreciable_base = currency.round(
            self.initial_cost - self.residual_value,
        )
        coverage_start = max(period_start, self.acquisition_date)
        coverage_end = event_date if terminal else period_end
        if coverage_end < coverage_start:
            return False
        fiscal_days = (period_end - period_start).days + 1
        service_days = (coverage_end - coverage_start).days + 1
        annual_charge = depreciable_base / self.useful_life_years
        charge = currency.round(annual_charge * service_days / fiscal_days)
        if currency.is_zero(charge):
            if terminal:
                return False
            raise UserError(_(
                "The period depreciation charge rounds to zero; nothing to "
                "post."))
        remaining = currency.round(
            depreciable_base - self.accumulated_depreciation)
        if float_compare(remaining, 0.0, precision_rounding=currency.rounding) \
                <= 0:
            if terminal:
                return False
            raise UserError(_(
                "The property is already fully depreciated; the depreciable "
                "base is exhausted."))
        if float_compare(charge, remaining,
                         precision_rounding=currency.rounding) > 0:
            charge = remaining
        lines = [
            (self.depreciation_expense_account_id, charge, 0.0,
             _("Depreciation %s", self.name)),
            (self.accumulated_depreciation_account_id, 0.0, charge,
             _("Accumulated depreciation %s", self.name)),
        ]
        opening_carrying = currency.round(self.carrying_amount)
        opening_accumulated = currency.round(self.accumulated_depreciation)
        closing_accumulated = currency.round(opening_accumulated + charge)
        closing_carrying = currency.round(opening_carrying - charge)
        if float_compare(
            closing_carrying, self.residual_value,
            precision_rounding=currency.rounding,
        ) < 0:
            raise UserError(_(
                "Depreciation would reduce the property below its residual "
                "value."
            ))
        move = self._post_move(lines, date=event_date)
        self._eh_create_depreciation_event({
            'property_id': self.id,
            'company_id': self.company_id.id,
            'currency_id': self.currency_id.id,
            'period_start': period_start,
            'period_end': period_end,
            'coverage_start': coverage_start,
            'coverage_end': coverage_end,
            'depreciation_date': event_date,
            'charge': charge,
            'opening_carrying': opening_carrying,
            'closing_carrying': closing_carrying,
            'opening_accumulated': opening_accumulated,
            'closing_accumulated': closing_accumulated,
            'initial_cost_snapshot': self.initial_cost,
            'residual_value_snapshot': self.residual_value,
            'useful_life_years_snapshot': self.useful_life_years,
            'journal_id': self.journal_id.id,
            'expense_account_id': self.depreciation_expense_account_id.id,
            'accumulated_account_id': (
                self.accumulated_depreciation_account_id.id
            ),
            'move_id': move.id,
            'posted_at': fields.Datetime.now(),
            'posted_by_id': self.env.user.id,
        })
        self._eh_ledger_write({
            'accumulated_depreciation': closing_accumulated,
            'carrying_amount': closing_carrying,
            'depreciation_date': event_date,
        })
        return move

    def action_transfer_out(self):
        """Reclassify the property out of investment property (IAS 40.57-62).

        The measurement on the way out depends on the valuation basis:

        * Fair value model (IAS 40.60-61): the property is first remeasured
          to transfer_fair_value, the fair value at the date of change in
          use, with the gap recognised in profit or loss exactly like any
          other fair value change of the period (IAS 40.35); the property
          then leaves at that fair value, which becomes the deemed cost of
          the destination asset (IAS 40.60):

              remeasure (only when the gap is non-zero):
                Dr property account / Cr FV gain      (uplift), or
                Dr FV loss / Cr property account      (deficit)
              derecognise:
                Dr transfer target       fair value at transfer date
                  Cr property account    fair value at transfer date

          When transfer_fair_value is left at zero the current carrying
          amount (already fair value per IAS 40.33) is used and no
          remeasurement arises, preserving the previous behaviour.

        * Cost model (IAS 40.59): transfers between investment property
          carried under the cost model and owner-occupied property do not
          change the carrying amount of the property, so no remeasurement
          is posted. The net book value moves at gross cost with the
          accumulated depreciation reversed out rather than stranded:

              Dr transfer target             carrying amount
              Dr accumulated depreciation    accumulated_depreciation
                Cr property account          initial_cost

          A transfer_fair_value entered under the cost model is stored on
          the transfer audit trail for disclosure only and never posted.

        Every transfer writes an immutable eh.investment.property.transfer
        row (date, direction, basis, carrying amount before, fair value,
        delta posted with its routing, move links) so the transfer is
        reconstructable end to end.
        """
        self, _asset = self._eh_prepare_action()
        if self.state != 'held':
            raise UserError(_("Only a held property can be transferred out."))
        if not self.journal_id or not self.property_account_id \
                or not self.transfer_target_account_id:
            raise UserError(_(
                "Configure the journal, property account and transfer target "
                "account first."))
        currency = self.currency_id
        transfer_date = self.transfer_date or fields.Date.context_today(self)
        if not math.isfinite(self.transfer_fair_value) \
                or self.transfer_fair_value < 0:
            raise UserError(_(
                "Transfer fair value must be finite and non-negative."
            ))
        # Validate before a fair-value remeasurement can post. Otherwise a
        # late chronology failure after that first entry would leave a partial
        # transfer in the caller's transaction/savepoint.
        self._eh_validate_property_event_date(transfer_date, _("transfer"))
        if self.model_basis == 'cost':
            self._eh_post_cost_depreciation(transfer_date, terminal=True)
        carrying_before = currency.round(self.carrying_amount)
        remeasure_move = False
        delta = 0.0
        routing = 'none'
        if self.model_basis == 'fair_value':
            # IAS 40.60-61: leave at the fair value at the date of change in
            # use; the gap to the last carrying amount is a fair value
            # change of the period and hits profit or loss before
            # derecognition. Zero means "not supplied": the carrying amount
            # is already fair value (IAS 40.33) and moves unchanged.
            fv = currency.round(self.transfer_fair_value)
            if float_compare(fv, 0.0,
                             precision_rounding=currency.rounding) < 0:
                raise UserError(_(
                    "The fair value at the transfer date cannot be "
                    "negative."))
            if currency.is_zero(fv):
                fv = carrying_before
            delta = currency.round(fv - carrying_before)
            if not currency.is_zero(delta):
                if not self.fv_gain_loss_account_id:
                    raise UserError(_(
                        "Configure the fair value gain/loss account: the "
                        "transfer fair value differs from the carrying "
                        "amount, so the gap must be recognised in profit "
                        "or loss before derecognition (IAS 40.60-61)."))
                if delta > 0:
                    lines = [
                        (self.property_account_id, delta, 0.0,
                         _("Fair value at transfer date %s", self.name)),
                        (self.fv_gain_loss_account_id, 0.0, delta,
                         _("Fair value gain on transfer %s", self.name)),
                    ]
                else:
                    lines = [
                        (self.fv_gain_loss_account_id, -delta, 0.0,
                         _("Fair value loss on transfer %s", self.name)),
                        (self.property_account_id, 0.0, -delta,
                         _("Fair value at transfer date %s", self.name)),
                    ]
                remeasure_move = self._post_move(lines, date=transfer_date)
                self._eh_ledger_write(
                    {'carrying_amount': fv, 'fair_value': fv})
                routing = 'pl'
            carrying = fv
            accumulated = 0.0
            gross = fv
            logged_fv = fv
        else:
            # IAS 40.59: no remeasurement; the fair value, when supplied, is
            # disclosed on the audit trail only.
            carrying = carrying_before
            accumulated = currency.round(self.accumulated_depreciation)
            gross = currency.round(self.initial_cost)
            logged_fv = currency.round(self.transfer_fair_value)
        if currency.is_zero(carrying):
            raise UserError(_(
                "The carrying amount is zero; nothing to reclassify."))
        lines = [(
            self.transfer_target_account_id, carrying, 0.0,
            _("Transfer out of investment property %s", self.name))]
        if not float_is_zero(accumulated,
                             precision_rounding=currency.rounding):
            lines.append((
                self.accumulated_depreciation_account_id, accumulated, 0.0,
                _("Derecognise accumulated depreciation %s", self.name)))
        lines.append((
            self.property_account_id, 0.0, gross,
            _("Derecognise investment property %s", self.name)))
        move = self._post_move(lines, date=transfer_date)
        self._eh_ledger_write({
            'transfer_date': transfer_date,
            'state': 'transferred',
            'carrying_amount': 0.0,
            'accumulated_depreciation': 0.0,
        })
        self._log_transfer(
            direction='out', date=transfer_date, basis=self.model_basis,
            carrying_before=carrying_before, fair_value=logged_fv,
            delta=delta, routing=routing, move=move,
            remeasure_move=remeasure_move,
            note=self.transfer_reason or False)
        return True

    def action_transfer_in(self):
        """Bring an owner-occupied fixed asset into investment property at
        fair value on the date of change in use (IAS 40.57(d)/.61-62).

        Applies to a DRAFT property on the fair value model, linked to an
        eh.asset from the ERP Heritage assets module through
        transfer_in_asset_id. The asset is revalued to transfer_fair_value
        and derecognised into this property in one balanced entry; the
        property opens at that fair value as its deemed cost:

            Dr investment property        fair value at transfer date
            Dr accumulated depreciation   gross - carrying (when any)
            Dr revaluation surplus        min(deficit, asset surplus)
            Dr fair value loss (P&L)      remaining deficit
              Cr asset account            gross cost (incl. revaluations)
              Cr revaluation surplus      uplift (OCI, IAS 40.61)

        IAS 40.61-62 treat the transfer-date revaluation exactly like an
        IAS 16 revaluation: an uplift is credited to the equity revaluation
        surplus (OCI); a deficit first consumes any revaluation surplus
        carried by that asset and only the excess is charged to profit or
        loss. An uplift on an asset carrying an unreversed impairment is
        refused: recovering an impairment must be routed through profit or
        loss as an impairment reversal in the assets module first, capped
        per IAS 36.117 (IAS 40.62(b)); this mirrors the assets module's own
        revaluation wizard gate.

        The source asset becomes terminal after its ledger position is
        derecognised and keeps its full history; the immutable source id plus
        the transfer log row make the transfer reconstructable.

        Under the cost model IAS 40.59 carries both gross cost and accumulated
        depreciation/impairment into the investment-property accounts with no
        remeasurement. Under the fair-value model the source is remeasured at
        the transfer date as described above.

        The assets module is a declared dependency. The selected source asset
        is resolved under caller access, locked, and copied to the immutable
        transfer log at action time.
        """
        self, asset = self._eh_prepare_action(resolve_asset=True)
        if self.state != 'draft':
            raise UserError(_(
                "Transfer in opens the property's ledger position, so it "
                "applies to a draft property only."))
        if asset.state not in ('running', 'paused'):
            raise UserError(_(
                "Only a running or paused asset can be transferred into "
                "investment property; %(asset)s is %(state)s.",
                asset=asset.display_name, state=asset.state))
        asset._eh_assert_available_for_ip_transfer()
        if not self.journal_id or not self.property_account_id:
            raise UserError(_(
                "Configure the journal and property account first."))
        if not asset.asset_account_id:
            raise UserError(_(
                "Fixed asset %s is missing its asset account.",
                asset.display_name))
        self._eh_assert_accounting_ready()
        currency = self.currency_id
        source_state_before = asset.state
        source_surplus_before = currency.round(
            asset.revaluation_surplus or 0.0,
        )
        source_pl_decrease_before = currency.round(
            asset.revaluation_pl_decrease or 0.0,
        )
        source_reserve_account = asset.revaluation_reserve_account_id
        if (
            source_reserve_account
            and self.revaluation_surplus_account_id
            and source_reserve_account != self.revaluation_surplus_account_id
        ):
            raise UserError(_(
                "The source asset's revaluation reserve account must match "
                "the investment property's reserve account. A transfer "
                "cannot debit one equity reserve and attribute the balance "
                "to another."
            ))
        reserve_account = (
            source_reserve_account or self.revaluation_surplus_account_id
        )
        if source_surplus_before > 0 and not reserve_account:
            raise UserError(_(
                "Configure the revaluation surplus account before moving "
                "the source asset's attributable equity reserve."
            ))
        if not math.isfinite(self.transfer_fair_value) \
                or self.transfer_fair_value < 0:
            raise UserError(_(
                "Transfer fair value must be finite and non-negative."
            ))
        fv = currency.round(self.transfer_fair_value)
        if self.model_basis == 'fair_value' and float_compare(
            fv, 0.0, precision_rounding=currency.rounding,
        ) <= 0:
            raise UserError(_(
                "Set the fair value at the transfer date: the property "
                "opens at fair value as its deemed cost (IAS 40.61)."))
        # Ledger decomposition of the asset (from the assets module design):
        # the asset account carries acquisition cost plus posted revaluation
        # adjustments; depreciation and impairments sit on the accumulated
        # depreciation contra. carrying = gross - contra by construction.
        asset.invalidate_recordset([
            'net_book_value', 'total_depreciated',
            'accumulated_impairment', 'revaluation_adjustment',
        ])
        carrying = currency.round(asset.net_book_value)
        gross = currency.round(
            asset.acquisition_cost + asset.revaluation_adjustment)
        contra_balances = self._eh_source_contra_balances(asset)
        contra = currency.round(sum(contra_balances.values()))
        expected_contra = currency.round(gross - carrying)
        if not currency.is_zero(contra - expected_contra):
            raise UserError(_(
                "The source asset subledger does not reconcile to its "
                "journal-account decomposition (expected contra "
                "%(expected).2f, reconstructed %(actual).2f). Correct the "
                "source before transferring it.",
                expected=expected_contra, actual=contra,
            ))
        if float_compare(contra, 0.0,
                         precision_rounding=currency.rounding) < 0:
            raise UserError(_(
                "Fixed asset %s carries a negative depreciation/impairment "
                "contra balance; correct the asset before transferring.",
                asset.display_name))
        if (
            self.model_basis == 'cost'
            and self.useful_life_years <= 0
        ):
            raise UserError(_(
                "Set the remaining useful life in years from the transfer "
                "date before a cost-model transfer-in."
            ))
        if (
            self.model_basis == 'cost'
            and not float_is_zero(
                contra, precision_rounding=currency.rounding,
            )
            and not self.accumulated_depreciation_account_id
        ):
            raise UserError(_(
                "Configure the investment property's accumulated "
                "depreciation account before a cost-model transfer."
            ))
        delta = currency.round(fv - carrying) \
            if self.model_basis == 'fair_value' else 0.0
        dr_to_surplus = 0.0
        dr_to_pl = 0.0
        cr_to_pl = 0.0
        cr_to_surplus = 0.0
        routing = 'none'
        if self.model_basis == 'fair_value' and float_compare(delta, 0.0,
                         precision_rounding=currency.rounding) > 0:
            if asset.accumulated_impairment > 0:
                raise UserError(_(
                    "%(asset)s carries an unreversed impairment of "
                    "%(imp).2f. Recovering it must be recognised as an "
                    "impairment reversal through profit or loss in the "
                    "assets module (capped per IAS 36.117) before any "
                    "remaining uplift can be credited to the revaluation "
                    "surplus on transfer (IAS 40.62(b)).",
                    asset=asset.display_name,
                    imp=asset.accumulated_impairment))
            cr_to_pl = currency.round(min(
                delta, asset.revaluation_pl_decrease or 0.0,
            ))
            cr_to_surplus = currency.round(delta - cr_to_pl)
            if cr_to_pl > 0 and not self.fv_gain_loss_account_id:
                raise UserError(_(
                    "Configure the fair value gain/loss account: the uplift "
                    "must first reverse the source asset's prior revaluation "
                    "decrease in profit or loss (IAS 16.39)."
                ))
            if cr_to_surplus > 0 and not reserve_account:
                raise UserError(_(
                    "Configure the revaluation surplus account: the fair "
                    "value uplift on transfer is credited to equity (OCI) "
                    "per IAS 40.61."))
            routing = (
                'mixed' if cr_to_pl > 0 and cr_to_surplus > 0
                else 'pl' if cr_to_pl > 0 else 'oci'
            )
        elif self.model_basis == 'fair_value' and float_compare(delta, 0.0,
                           precision_rounding=currency.rounding) < 0:
            # IAS 40.62(a): the deficit first reverses the revaluation
            # surplus carried by that asset; only the excess hits P&L.
            dr_to_surplus = currency.round(
                min(-delta, asset.revaluation_surplus or 0.0))
            dr_to_pl = currency.round(-delta - dr_to_surplus)
            if dr_to_surplus > 0 and not reserve_account:
                raise UserError(_(
                    "Configure the revaluation surplus account: the fair "
                    "value deficit first reverses the asset's revaluation "
                    "surplus (IAS 40.62(a))."))
            if dr_to_pl > 0 and not self.fv_gain_loss_account_id:
                raise UserError(_(
                    "Configure the fair value gain/loss account: the fair "
                    "value deficit in excess of the asset's revaluation "
                    "surplus is charged to profit or loss (IAS 40.62(a))."))
            routing = (
                'mixed' if dr_to_pl > 0 and dr_to_surplus > 0
                else 'pl' if dr_to_pl > 0 else 'oci'
            )
        transfer_date = self.transfer_date or fields.Date.context_today(self)
        self._eh_validate_source_transfer_date(asset, transfer_date)
        self._eh_validate_property_event_date(
            transfer_date, _("transfer"), recognition_date=transfer_date,
        )
        if self.model_basis == 'cost':
            lines = [(
                self.property_account_id, gross, 0.0,
                _("Transfer into investment property %s", self.name),
            )]
        else:
            lines = [(
                self.property_account_id, fv, 0.0,
                _("Transfer into investment property %s", self.name),
            )]
        for account, amount in sorted(
            contra_balances.items(), key=lambda item: item[0].id,
        ):
            lines.append((
                account, amount, 0.0,
                _("Derecognise accumulated depreciation and impairment %s",
                  asset.display_name)))
        if dr_to_surplus > 0:
            lines.append((
                reserve_account, dr_to_surplus, 0.0,
                _("Revaluation surplus reversal %s", asset.display_name)))
        if dr_to_pl > 0:
            lines.append((
                self.fv_gain_loss_account_id, dr_to_pl, 0.0,
                _("Fair value loss on transfer %s", self.name)))
        lines.append((
            asset.asset_account_id, 0.0, gross,
            _("Derecognise owner-occupied asset %s", asset.display_name)))
        if self.model_basis == 'cost' and not currency.is_zero(contra):
            lines.append((
                self.accumulated_depreciation_account_id, 0.0, contra,
                _("Carry accumulated depreciation into %s", self.name),
            ))
        if cr_to_pl > 0:
            lines.append((
                self.fv_gain_loss_account_id, 0.0, cr_to_pl,
                _("Prior revaluation decrease reversal %s", self.name),
            ))
        if cr_to_surplus > 0:
            lines.append((
                reserve_account, 0.0, cr_to_surplus,
                _("Revaluation surplus on transfer %s", self.name)))
        move = self._post_move(lines, date=transfer_date)
        # The source ledger position has been fully derecognised. Mark the
        # source asset terminal against this exact sealed move rather than
        # merely pausing it; a paused source could be resumed, depreciated,
        # transferred again, or disposed a second time.
        source_surplus = source_surplus_before
        source_pl_decrease = source_pl_decrease_before
        if self.model_basis == 'fair_value':
            source_surplus = currency.round(
                source_surplus - dr_to_surplus + cr_to_surplus
            )
            source_pl_decrease = currency.round(
                source_pl_decrease - cr_to_pl + dr_to_pl
            )
        asset.with_context(
            eh_ip_asset_transition_capability=_IP_ASSET_TRANSITION_CAPABILITY,
        )._eh_workflow_write({
            'state': 'disposed',
            'disposed_at': fields.Datetime.now(),
            'disposed_by_id': self.env.user.id,
            'disposal_date': transfer_date,
            'disposal_proceeds': 0.0,
            'disposal_partner_id': False,
            'disposal_move_id': move.id,
            'revaluation_surplus': 0.0,
            'revaluation_pl_decrease': source_pl_decrease,
        })
        transferred_amount = fv if self.model_basis == 'fair_value' else carrying
        asset.message_post(body=_(
            "Transferred into investment property %(prop)s at %(basis)s "
            "amount %(amount).2f on %(date)s; the source asset is "
            "terminal and its ledger position was derecognised by entry "
            "%(move)s.",
            prop=self.display_name,
            basis=(
                _("fair-value") if self.model_basis == 'fair_value'
                else _("cost-model carrying")
            ),
            amount=transferred_amount, date=transfer_date,
            move=move.display_name))
        property_cost = fv if self.model_basis == 'fair_value' else gross
        property_carrying = fv \
            if self.model_basis == 'fair_value' else carrying
        property_residual = (
            0.0 if self.model_basis == 'fair_value'
            else currency.round(min(
                asset.salvage_value or 0.0, property_cost,
            ))
        )
        self._eh_ledger_write({
            'state': 'held',
            'initial_cost': property_cost,
            'residual_value': property_residual,
            'carrying_amount': property_carrying,
            'accumulated_depreciation': (
                0.0 if self.model_basis == 'fair_value' else contra
            ),
            'fair_value': fv,
            'acquisition_date': transfer_date,
            'transfer_date': transfer_date,
            'transfer_in_asset_display': asset.display_name,
            'revaluation_surplus': source_surplus,
            'revaluation_surplus_account_id': (
                reserve_account.id if reserve_account else False
            ),
            'opening_move_id': move.id,
        })
        self._log_transfer(
            direction='in', date=transfer_date, basis=self.model_basis,
            carrying_before=carrying, fair_value=fv, delta=delta,
            routing=routing, move=move,
            source_document=asset.display_name, source_asset=asset,
            source_state_before=source_state_before,
            source_revaluation_surplus_before=source_surplus_before,
            source_revaluation_pl_decrease_before=source_pl_decrease_before)
        return True

    def _log_transfer(self, direction, date, basis, carrying_before,
                      fair_value, delta, routing, move,
                      remeasure_move=False, source_document=False,
                      note=False, source_asset=False,
                      source_state_before=False,
                      source_revaluation_surplus_before=0.0,
                      source_revaluation_pl_decrease_before=0.0):
        """Write one immutable audit-trail row per transfer (IAS 40.57-62).

        The row stores everything needed to reconstruct the transfer: the
        measurement basis at the transfer date, the carrying amount
        immediately before, the fair value at the transfer date (posted
        under the fair value model, disclosed-only under the cost model),
        the remeasurement delta actually posted with its P&L/OCI routing,
        and the journal entries.
        """
        self.ensure_one()
        return self.env['eh.investment.property.transfer'].with_context(
            eh_ip_transfer_log_capability=_IP_TRANSFER_LOG_CAPABILITY,
        ).create({
            'property_id': self.id,
            'company_id': self.company_id.id,
            'currency_id': self.currency_id.id,
            'date': date,
            'direction': direction,
            'basis': basis,
            'carrying_before': carrying_before,
            'fair_value': fair_value,
            'delta_posted': delta,
            'delta_routing': routing,
            'move_id': move.id if move else False,
            'remeasure_move_id':
                remeasure_move.id if remeasure_move else False,
            'source_document': source_document or False,
            'source_asset_id': source_asset.id if source_asset else False,
            'source_state_before': source_state_before or False,
            'source_revaluation_surplus_before': (
                source_revaluation_surplus_before
            ),
            'source_revaluation_pl_decrease_before': (
                source_revaluation_pl_decrease_before
            ),
            'note': note or False,
        })

    def action_dispose(self):
        """Derecognise the property on disposal (IAS 40.66-69).

        Post a balanced entry that removes the carrying amount from the
        balance sheet, recognises the proceeds and books the balancing gain
        or loss to profit or loss:

          Dr cash / receivable            disposal_proceeds
          Dr accumulated depreciation     accumulated_depreciation (cost model)
          Dr disposal loss  OR  Cr disposal gain   (balancing figure)
            Cr property account           initial_cost (cost model)
                                    or     carrying_amount (fair value model)

        The gain or loss is proceeds less carrying amount (IAS 40.69). The
        entry balances by construction: the gain/loss line is the residual
        that makes total debits equal total credits.
        """
        self, _asset = self._eh_prepare_action()
        if self.state != 'held':
            raise UserError(_("Only a held property can be disposed."))
        if not self.journal_id or not self.property_account_id \
                or not self.disposal_cash_account_id \
                or not self.disposal_gain_loss_account_id:
            raise UserError(_(
                "Configure the journal, property account, proceeds/cash "
                "account and disposal gain/loss account first."))
        currency = self.currency_id
        surplus = currency.round(self.revaluation_surplus)
        if surplus and (
            not self.revaluation_surplus_account_id
            or not self.retained_earnings_account_id
        ):
            raise UserError(_(
                "Configure the revaluation surplus and retained earnings "
                "accounts: the attributable reserve must be recycled "
                "directly within equity on disposal."
            ))
        if surplus and (
            self.revaluation_surplus_account_id
            == self.retained_earnings_account_id
        ):
            raise UserError(_(
                "Revaluation surplus and retained earnings must be distinct "
                "equity accounts for a disposal recycle."
            ))
        disposal_date = (
            self.disposal_date or fields.Date.context_today(self)
        )
        self._eh_validate_property_event_date(disposal_date, _("disposal"))
        if self.model_basis == 'cost':
            self._eh_post_cost_depreciation(disposal_date, terminal=True)
        proceeds = currency.round(self.disposal_proceeds)
        carrying = currency.round(self.carrying_amount)
        accumulated = currency.round(self.accumulated_depreciation)
        if self.model_basis == 'cost':
            gross = currency.round(self.initial_cost)
        else:
            gross = carrying
            accumulated = 0.0
        # Gain (positive) or loss (negative) = proceeds - carrying amount.
        gain = currency.round(proceeds - carrying)

        lines = []
        if not float_is_zero(proceeds, precision_rounding=currency.rounding):
            lines.append((
                self.disposal_cash_account_id, proceeds, 0.0,
                _("Disposal proceeds %s", self.name)))
        if not float_is_zero(accumulated,
                             precision_rounding=currency.rounding):
            lines.append((
                self.accumulated_depreciation_account_id, accumulated, 0.0,
                _("Derecognise accumulated depreciation %s", self.name)))
        lines.append((
            self.property_account_id, 0.0, gross,
            _("Derecognise investment property %s", self.name)))
        if float_compare(gain, 0.0,
                         precision_rounding=currency.rounding) > 0:
            lines.append((
                self.disposal_gain_loss_account_id, 0.0, gain,
                _("Gain on disposal %s", self.name)))
        elif float_compare(gain, 0.0,
                           precision_rounding=currency.rounding) < 0:
            lines.append((
                self.disposal_gain_loss_account_id, -gain, 0.0,
                _("Loss on disposal %s", self.name)))
        if surplus:
            lines.extend([
                (
                    self.revaluation_surplus_account_id, surplus, 0.0,
                    _("Recycle revaluation surplus %s", self.name),
                ),
                (
                    self.retained_earnings_account_id, 0.0, surplus,
                    _("Revaluation surplus to retained earnings %s", self.name),
                ),
            ])
        self._post_move(lines, date=disposal_date)
        self._eh_ledger_write({
            'state': 'disposed',
            'disposal_date': disposal_date,
            'carrying_amount': 0.0,
            'accumulated_depreciation': 0.0,
            'revaluation_surplus': 0.0,
        })
        return True

    def action_cancel(self):
        for original in self.sorted('id'):
            p, _asset = original._eh_prepare_action()
            if p.move_ids:
                raise UserError(_(
                    "Reverse the posted entries before cancelling %s.",
                    p.display_name))
            p.state = 'cancelled'

    def action_reverse(self):
        """Neutralise posted property history with sealed counter-entries.

        Source entries and event logs stay immutable. Inbound fixed-asset
        transfers restore source subledger only when exact pre-transfer state
        snapshots exist; legacy unsnapshotted transfers fail closed.
        """
        self.ensure_one()
        inbound_logs = self.transfer_log_ids.filtered(
            lambda log: log.direction == 'in'
        )
        p, asset = self._eh_prepare_action(resolve_asset=bool(inbound_logs))
        with self.env.cr.savepoint():
            if p.state not in ('held', 'disposed', 'transferred'):
                raise UserError(_(
                    "Only a posted investment property can use controlled "
                    "reversal."
                ))
            if not p.reversal_date:
                raise UserError(_(
                    "Enter an explicit correction accounting date first."
                ))
            if p.reversal_date > fields.Date.context_today(p):
                raise UserError(_(
                    "Investment-property reversal cannot use a future date."
                ))
            originals = p.move_ids.filtered(
                lambda move: not move.reversed_entry_id
            ).sorted(lambda move: (move.date, move.id), reverse=True)
            if not originals:
                raise UserError(_(
                    "No investment-property entries are available to reverse."
                ))
            if p.reversal_date < max(originals.mapped('date')):
                raise UserError(_(
                    "Correction date cannot precede latest property entry."
                ))
            invalid = originals.filtered(lambda move: (
                move.state != 'posted'
                or not move.eh_sealed
                or move.eh_legacy_unverified_seal
                or move.eh_investment_property_id != p
                or move.company_id != p.company_id
            ))
            if invalid:
                raise UserError(_(
                    "Every property entry must retain exact verified sealed "
                    "provenance before controlled reversal."
                ))
            existing = self.env['account.move'].sudo().search([
                ('reversed_entry_id', 'in', originals.ids),
            ], limit=1)
            if existing:
                raise UserError(_(
                    "A property entry already has a counter-entry; duplicate "
                    "reversal is blocked."
                ))
            inbound_log = inbound_logs[:1]
            if len(inbound_logs) > 1:
                raise UserError(_(
                    "Multiple inbound transfer owners are inconsistent; "
                    "controlled reversal is blocked."
                ))
            if inbound_log:
                if (
                    not asset
                    or inbound_log.source_asset_id != asset.id
                    or inbound_log.source_state_before not in (
                        'running', 'paused'
                    )
                    or asset.state != 'disposed'
                    or asset.disposal_move_id != inbound_log.move_id
                ):
                    raise UserError(_(
                        "Inbound transfer lacks exact source-state evidence. "
                        "Preserve legacy history and record a specialist "
                        "correction instead of guessing source state."
                    ))
            p._eh_require_open_accounting_date(p.reversal_date)
            for original in originals:
                expected_reverse = sorted(
                    (account_id, credit, debit)
                    for account_id, debit, credit
                    in p._eh_move_signature(original)
                )
                reversal = original._eh_reverse_with_verified_capability([{
                    'date': p.reversal_date,
                    'journal_id': original.journal_id.id,
                    'ref': _(
                        "Correction of investment property %s", p.name,
                    ),
                }], cancel=False)
                reversal.with_context(
                    eh_ip_move_link_capability=_IP_MOVE_LINK_CAPABILITY,
                )._eh_write_sealed_metadata(
                    {'eh_investment_property_id': p.id},
                    {'eh_investment_property_id'},
                )
                reversal._eh_post_verified_reversal()
                reversal.invalidate_recordset([
                    'state', 'date', 'eh_sealed',
                    'eh_investment_property_id', 'reversed_entry_id',
                ])
                if (
                    reversal.state != 'posted'
                    or reversal.date != p.reversal_date
                    or not reversal.eh_sealed
                    or reversal.eh_investment_property_id != p
                    or reversal.reversed_entry_id != original
                    or p._eh_move_signature(reversal) != expected_reverse
                ):
                    raise UserError(_(
                        "Property reversal failed exact date, seal, source, "
                        "or accounting-signature validation."
                    ))
            if inbound_log:
                asset.with_context(
                    eh_ip_asset_transition_capability=(
                        _IP_ASSET_TRANSITION_CAPABILITY
                    ),
                )._eh_workflow_write({
                    'state': inbound_log.source_state_before,
                    'disposed_at': False,
                    'disposed_by_id': False,
                    'disposal_date': False,
                    'disposal_proceeds': 0.0,
                    'disposal_partner_id': False,
                    'disposal_move_id': False,
                    'revaluation_surplus': (
                        inbound_log.source_revaluation_surplus_before
                    ),
                    'revaluation_pl_decrease': (
                        inbound_log.source_revaluation_pl_decrease_before
                    ),
                })
                asset.message_post(body=_(
                    "Investment-property transfer to %(property)s was "
                    "corrected on %(date)s; source state %(state)s restored.",
                    property=p.display_name,
                    date=p.reversal_date,
                    state=inbound_log.source_state_before,
                ))
            p._eh_ledger_write({
                'state': 'reversed',
                'carrying_amount': 0.0,
                'accumulated_depreciation': 0.0,
                'revaluation_surplus': 0.0,
                'reversed_by_id': self.env.user.id,
                'reversed_at': fields.Datetime.now(),
            })
            p.message_post(body=_(
                "All posted property entries were neutralised by verified "
                "sealed counter-entries on %s. Original evidence remains "
                "immutable.", p.reversal_date,
            ))
        return True

    def action_view_moves(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('eh_investment_property_id', '=', self.id)],
        }

    def _eh_check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can change or post "
                "investment property."
            ))

    @api.model
    def _eh_assert_company_allowed(self, company_id):
        if (
            not self.env.su
            and company_id
            and int(company_id) not in self.env.companies.ids
        ):
            raise AccessError(_(
                "Investment property may only use a company enabled in the "
                "caller's current company context."
            ))

    @api.model
    def _eh_record_matches_company(self, record, company):
        if not record or not company:
            return True
        domain_builder = getattr(record, '_check_company_domain', None)
        if domain_builder:
            try:
                return bool(record.filtered_domain(domain_builder(company)))
            except (AttributeError, TypeError, ValueError):
                pass
        if 'company_ids' in record._fields:
            candidate = company
            while candidate:
                if candidate in record.company_ids:
                    return True
                candidate = (
                    candidate.parent_id
                    if 'parent_id' in candidate._fields
                    else candidate.browse()
                )
            return False
        if 'company_id' in record._fields:
            return record.company_id == company
        return True

    def _eh_resolve_source_asset(
        self, company, asset_id, required=False, access_mode='read',
    ):
        if not asset_id:
            if required:
                raise UserError(_(
                    "Select the source fixed asset to transfer in."
                ))
            return self.env['eh.asset'].browse()
        asset = (
            asset_id
            if getattr(asset_id, '_name', None) == 'eh.asset'
            else self.env['eh.asset'].browse(int(asset_id))
        )
        try:
            if not self.env.su:
                asset._eh_check_access(access_mode)
            asset = asset.exists()
        except (AccessError, MissingError):
            raise AccessError(_(
                "The source fixed asset is not available to the caller."
            )) from None
        if not asset:
            raise AccessError(_(
                "The source fixed asset is not available to the caller."
            ))
        if asset.company_id != company:
            raise ValidationError(_(
                "The source fixed asset must belong to the investment "
                "property company."
            ))
        for field_name in (
            'asset_account_id', 'accumulated_depreciation_account_id',
            'revaluation_reserve_account_id',
        ):
            account = asset[field_name]
            if not account:
                continue
            if not self.env.su:
                account._eh_check_access('read')
            if not self._eh_record_matches_company(account, company):
                raise ValidationError(_(
                    "Source fixed-asset accounts must be available to the "
                    "investment property company."
                ))
        return asset

    def _eh_validate_source_transfer_date(self, asset, transfer_date):
        """Do not derecognise a source underneath later sealed evidence."""
        self.ensure_one()
        latest = asset._eh_latest_accounting_evidence_date()
        if latest and transfer_date < latest:
            raise UserError(_(
                "Transfer date %(date)s precedes existing source-asset "
                "accounting evidence dated %(latest)s on %(asset)s. Post a "
                "current-period correction instead.",
                date=transfer_date, latest=latest,
                asset=asset.display_name))
        return transfer_date

    def _eh_validate_property_event_date(
        self, event_date, label, recognition_date=False,
    ):
        self.ensure_one()
        if not event_date:
            raise UserError(_("Set the accounting date for %s.", label))
        prior_dates = [recognition_date or self.acquisition_date,
                       self.transfer_date]
        prior_dates += self.move_ids.filtered(
            lambda move: move.state == 'posted').mapped('date')
        prior_dates += self.transfer_log_ids.mapped('date')
        prior_dates += self.depreciation_event_ids.mapped('depreciation_date')
        latest = max((date for date in prior_dates if date), default=False)
        if latest and event_date < latest:
            raise UserError(_(
                "The %(label)s date %(date)s precedes posted "
                "investment-property evidence dated %(latest)s on "
                "%(property)s. Post a current-period correction instead.",
                label=label, date=event_date, latest=latest,
                property=self.display_name))
        return event_date

    def _eh_require_open_accounting_date(self, posting_date):
        """Reject lock-date redating before source provenance is written."""
        self.ensure_one()
        posting_date = fields.Date.to_date(posting_date)
        checker = getattr(self.company_id, '_get_violated_lock_dates', None)
        if checker:
            try:
                violated = checker(posting_date, False, self.journal_id)
            except TypeError as journal_signature_error:
                try:
                    violated = checker(posting_date, False)
                except TypeError:
                    raise journal_signature_error
        else:
            fiscal_checker = self.company_id._get_user_fiscal_lock_date
            try:
                fiscal_lock = fiscal_checker(self.journal_id)
            except TypeError as journal_signature_error:
                try:
                    fiscal_lock = fiscal_checker()
                except TypeError:
                    raise journal_signature_error
            violated = (
                [(fiscal_lock, 'fiscalyear_lock_date')]
                if fiscal_lock and posting_date <= fiscal_lock
                else []
            )
        if violated:
            raise UserError(_(
                "Investment-property accounting date %(date)s is locked. "
                "Choose an explicit open date; silent redating would break "
                "subledger provenance.",
                date=posting_date,
            ))
        return posting_date

    def _eh_move_signature(self, move):
        self.ensure_one()
        return sorted(
            (
                line.account_id.id,
                self.currency_id.round(line.debit),
                self.currency_id.round(line.credit),
            )
            for line in move.line_ids
            if not self.currency_id.is_zero(line.debit - line.credit)
        )

    def _eh_validate_references(self, vals=None):
        vals = vals or {}
        account_fields = (
            'property_account_id', 'fv_gain_loss_account_id',
            'opening_counterpart_account_id',
            'depreciation_expense_account_id',
            'accumulated_depreciation_account_id',
            'transfer_target_account_id', 'revaluation_surplus_account_id',
            'retained_earnings_account_id',
            'disposal_cash_account_id', 'disposal_gain_loss_account_id',
        )
        for prop in self:
            company_id = vals.get('company_id', prop.company_id.id)
            self._eh_assert_company_allowed(company_id)
            company = self.env['res.company'].browse(company_id)
            if not self.env.su:
                company._eh_check_access('read')
            journal_id = vals.get('journal_id', prop.journal_id.id)
            journal = self.env['account.journal'].browse(journal_id)
            if journal:
                if not self.env.su:
                    journal._eh_check_access('read')
                if journal.company_id != company:
                    raise ValidationError(_(
                        "Investment-property journal must belong to its "
                        "company."
                    ))
            for field_name in account_fields:
                account_id = vals.get(field_name, prop[field_name].id)
                account = self.env['account.account'].browse(account_id)
                if not account:
                    continue
                if not self.env.su:
                    account._eh_check_access('read')
                if not self._eh_record_matches_company(account, company):
                    raise ValidationError(_(
                        "Investment-property posting accounts must be "
                        "available to its company."
                    ))
            asset_id = vals.get('transfer_in_asset_id') \
                if 'transfer_in_asset_id' in vals \
                else prop.transfer_in_asset_id
            self._eh_resolve_source_asset(
                company, asset_id, access_mode='read',
            )
            opening_move_id = vals.get('opening_move_id', prop.opening_move_id.id)
            if opening_move_id:
                opening_move = self.env['account.move'].browse(opening_move_id)
                if not self.env.su:
                    opening_move._eh_check_access('read')
                if (
                    not opening_move.exists()
                    or opening_move.company_id != company
                    or opening_move.state != 'posted'
                    or not opening_move.eh_sealed
                    or opening_move.eh_investment_property_id != prop
                ):
                    raise ValidationError(_(
                        "Opening evidence must be the property's own posted, "
                        "sealed, same-company journal entry."
                    ))
        return self

    def _eh_assert_accounting_ready(self):
        for prop in self:
            quarantines = []
            if prop.opening_evidence_quarantined:
                quarantines.append(_("opening recognition"))
            if prop.source_provenance_quarantined:
                quarantines.append(_("fixed-asset transfer provenance"))
            if prop.depreciation_history_quarantined:
                quarantines.append(_("period depreciation history"))
            if quarantines:
                raise UserError(_(
                    "Investment property %(property)s is quarantined for "
                    "%(areas)s. Historical evidence was preserved; resolve "
                    "the exception before further accounting.",
                    property=prop.display_name, areas=', '.join(quarantines),
                ))
            prop._eh_check_finite_measurements()
            if not prop.journal_id or not prop.property_account_id:
                raise UserError(_(
                    "Configure the journal and property account first."
                ))
        return True

    def _eh_ledger_write(self, vals):
        return self.with_context(**{
            _IP_LEDGER_ROLL_CONTEXT_KEY: _IP_LEDGER_ROLL_CAPABILITY,
        }).write(vals)

    def _eh_create_depreciation_event(self, vals):
        self.ensure_one()
        return self.env['eh.investment.property.depreciation'].with_context(
            **{
                _IP_DEPRECIATION_EVENT_CONTEXT_KEY:
                    _IP_DEPRECIATION_EVENT_CAPABILITY,
            }
        ).create(vals)

    def _eh_validate_source_evidence_access(self, asset):
        """Prove access to every source record before returning a sudo asset."""
        self.ensure_one()
        if self.env.su:
            return True
        # Recordsets from different models cannot be unioned. Check each
        # independently while the original caller and company rules are live.
        for recordset in (
            asset.depreciation_line_ids,
            asset.impairment_ids,
            asset.book_ids,
            asset.book_ids.mapped('line_ids'),
            asset.revaluation_move_ids,
        ):
            recordset._eh_check_access('read')
        accounts = asset.asset_account_id | asset.accumulated_depreciation_account_id
        accounts |= asset.impairment_ids.mapped('accumulated_account_id')
        accounts._eh_check_access('read')
        return True

    def _eh_source_contra_balances(self, asset):
        """Reconstruct the exact posted contra balance by destination account."""
        self.ensure_one()
        currency = self.currency_id
        balances = {}

        def add(account, amount):
            amount = currency.round(amount)
            if currency.is_zero(amount):
                return
            if not account or not self._eh_record_matches_company(
                    account, self.company_id):
                raise UserError(_(
                    "Every source depreciation/impairment contra account "
                    "must be available to the property company."
                ))
            balances[account] = currency.round(
                balances.get(account, 0.0) + amount,
            )

        posted_primary = asset.depreciation_line_ids.filtered('is_posted')
        for line in posted_primary:
            if not line.move_id or line.move_id.state != 'posted' \
                    or not line.move_id.eh_sealed:
                raise UserError(_(
                    "Posted source depreciation line %s has incomplete "
                    "sealed journal provenance.", line.display_name,
                ))
            add(asset.accumulated_depreciation_account_id, line.amount)
        gl_book_lines = asset.book_ids.filtered('posts_to_gl').mapped(
            'line_ids'
        ).filtered(lambda line: line.is_posted or line.move_id)
        if gl_book_lines:
            raise UserError(_(
                "Source asset %s has parallel-book GL postings. Its displayed "
                "net book value excludes those additional entries, so it "
                "cannot be transferred until that book evidence is resolved.",
                asset.display_name,
            ))
        for event in asset.impairment_ids.filtered(
            lambda item: item.state == 'posted'
        ):
            if not event.move_id or event.move_id.state != 'posted' \
                    or not event.move_id.eh_sealed:
                raise UserError(_(
                    "Posted source impairment %s has incomplete sealed "
                    "journal provenance.", event.display_name,
                ))
            account = (
                event.accumulated_account_id
                or asset.accumulated_depreciation_account_id
            )
            add(account, -event.amount if event.is_reversal else event.amount)
        return {
            account: amount for account, amount in balances.items()
            if not currency.is_zero(amount)
        }

    def _eh_lock_properties(self):
        ids = tuple(sorted(set(self.ids)))
        if not ids:
            return
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_investment_property WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            (ids,),
        )
        self.invalidate_recordset()

    def _eh_lock_source_asset(self, asset):
        if not asset:
            return
        asset.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_asset WHERE id = %s FOR UPDATE",
            (asset.id,),
        )
        asset.invalidate_recordset()

    def _eh_prepare_action(self, resolve_asset=False):
        self.ensure_one()
        if not self.env.su:
            self._eh_check_access('write')
        self._eh_check_manager()
        self._eh_validate_references()
        self._eh_lock_properties()
        self._eh_validate_references()
        asset = self.env['eh.asset'].browse()
        if resolve_asset:
            asset = self._eh_resolve_source_asset(
                self.company_id,
                self.transfer_in_asset_id,
                required=True,
                access_mode='write',
            )
        if asset:
            self._eh_validate_source_evidence_access(asset)
            self._eh_lock_source_asset(asset)
            asset = self._eh_resolve_source_asset(
                self.company_id,
                self.transfer_in_asset_id,
                required=True,
                access_mode='write',
            )
            self._eh_validate_source_evidence_access(asset)
        return self._eh_workflow_action(), asset.sudo() if asset else asset

    def _post_move(self, legs, date=None):
        self.ensure_one()
        self._eh_assert_accounting_ready()
        requested_date = self._eh_require_open_accounting_date(
            date or fields.Date.context_today(self),
        )
        for account, debit, credit, _label in legs:
            if not account or not self._eh_record_matches_company(
                    account, self.company_id):
                raise ValidationError(_(
                    "Every investment-property posting account must be "
                    "available to the property company."
                ))
            for amount in (debit, credit):
                if not math.isfinite(amount) or amount < 0:
                    raise ValidationError(_(
                        "Journal-entry legs must be finite and non-negative."
                    ))
        lines = [(0, 0, {
            'name': label, 'account_id': account.id,
            'debit': debit, 'credit': credit,
        }) for account, debit, credit, label in legs]
        with self.env.cr.savepoint():
            move = self.env['account.move'].with_context(
                eh_ip_move_link_capability=_IP_MOVE_LINK_CAPABILITY,
            )._eh_create_sealed({
                'move_type': 'entry',
                'company_id': self.company_id.id,
                'date': requested_date,
                'journal_id': self.journal_id.id,
                'ref': self.name,
                'eh_investment_property_id': self.id,
                'eh_sealed': True,
                'line_ids': lines,
            })
            move.action_post()
            move.invalidate_recordset([
                'state', 'date', 'eh_sealed',
                'eh_investment_property_id',
            ])
            if (
                move.state != 'posted'
                or move.date != requested_date
                or not move.eh_sealed
                or move.eh_investment_property_id != self
            ):
                raise UserError(_(
                    "Investment-property entry failed exact date, seal, or "
                    "source-provenance validation."
                ))
        return move


class EhInvestmentPropertyDepreciation(models.Model):
    """Append-only period evidence for cost-model depreciation."""

    _name = 'eh.investment.property.depreciation'
    _description = "Investment Property Depreciation Evidence (IAS 40.56)"
    _order = 'period_end desc, id desc'

    property_id = fields.Many2one(
        'eh.investment.property', required=True, readonly=True,
        ondelete='restrict', index=True, check_company=True,
    )
    company_id = fields.Many2one(
        'res.company', required=True, readonly=True, index=True,
    )
    currency_id = fields.Many2one(
        'res.currency', required=True, readonly=True,
    )
    period_start = fields.Date(required=True, readonly=True, index=True)
    period_end = fields.Date(required=True, readonly=True, index=True)
    coverage_start = fields.Date(
        readonly=True,
        help="First day included in this charge after acquisition proration.",
    )
    coverage_end = fields.Date(
        readonly=True,
        help="Last day included; a terminal catch-up ends on the disposal or "
             "transfer date.",
    )
    depreciation_date = fields.Date(required=True, readonly=True)
    charge = fields.Monetary(
        required=True, readonly=True, currency_field='currency_id',
    )
    opening_carrying = fields.Monetary(
        required=True, readonly=True, currency_field='currency_id',
    )
    closing_carrying = fields.Monetary(
        required=True, readonly=True, currency_field='currency_id',
    )
    opening_accumulated = fields.Monetary(
        required=True, readonly=True, currency_field='currency_id',
    )
    closing_accumulated = fields.Monetary(
        required=True, readonly=True, currency_field='currency_id',
    )
    initial_cost_snapshot = fields.Monetary(
        required=True, readonly=True, currency_field='currency_id',
    )
    residual_value_snapshot = fields.Monetary(
        readonly=True, currency_field='currency_id',
    )
    useful_life_years_snapshot = fields.Integer(required=True, readonly=True)
    journal_id = fields.Many2one(
        'account.journal', required=True, readonly=True,
        ondelete='restrict', check_company=True,
    )
    expense_account_id = fields.Many2one(
        'account.account', required=True, readonly=True,
        ondelete='restrict', check_company=True,
    )
    accumulated_account_id = fields.Many2one(
        'account.account', required=True, readonly=True,
        ondelete='restrict', check_company=True,
    )
    move_id = fields.Many2one(
        'account.move', required=True, readonly=True,
        ondelete='restrict', check_company=True,
    )
    posted_at = fields.Datetime(required=True, readonly=True)
    posted_by_id = fields.Many2one(
        'res.users', required=True, readonly=True, ondelete='restrict',
    )

    _one_charge_per_fiscal_period = models.Constraint(
        'unique(property_id, period_start, period_end)',
        'Only one depreciation charge may be posted per fiscal period.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get(
                _IP_DEPRECIATION_EVENT_CONTEXT_KEY) \
                is not _IP_DEPRECIATION_EVENT_CAPABILITY:
            raise AccessError(_(
                "Investment-property depreciation evidence can only be "
                "created by the depreciation workflow."
            ))
        Property = self.env['eh.investment.property']
        for vals in vals_list:
            prop = Property.browse(vals.get('property_id')).exists()
            move = self.env['account.move'].browse(vals.get('move_id')).exists()
            if not prop or not move:
                raise ValidationError(_(
                    "Depreciation evidence requires an existing property and "
                    "journal entry."
                ))
            if (
                vals.get('company_id') != prop.company_id.id
                or vals.get('currency_id') != prop.currency_id.id
                or move.company_id != prop.company_id
                or move.eh_investment_property_id != prop
                or move.state != 'posted'
                or not move.eh_sealed
                or move.date != fields.Date.to_date(vals.get('depreciation_date'))
                or vals.get('journal_id') != move.journal_id.id
                or vals.get('posted_by_id') != self.env.user.id
            ):
                raise ValidationError(_(
                    "Depreciation evidence must match the property's exact "
                    "posted, sealed journal entry and actor."
                ))
            start = fields.Date.to_date(vals.get('period_start'))
            end = fields.Date.to_date(vals.get('period_end'))
            coverage_start = fields.Date.to_date(
                vals.get('coverage_start') or start,
            )
            coverage_end = fields.Date.to_date(
                vals.get('coverage_end') or end,
            )
            event_date = fields.Date.to_date(vals.get('depreciation_date'))
            if not start or not end or not event_date \
                    or not start <= event_date <= end:
                raise ValidationError(_(
                    "Depreciation date must fall inside its fiscal period."
                ))
            if not start <= coverage_start <= coverage_end <= end:
                raise ValidationError(_(
                    "Depreciation coverage must fall inside its fiscal period."
                ))
            numeric_fields = (
                'charge', 'opening_carrying', 'closing_carrying',
                'opening_accumulated', 'closing_accumulated',
                'initial_cost_snapshot',
                'residual_value_snapshot',
            )
            if any(
                not math.isfinite(vals.get(name, 0.0))
                or vals.get(name, 0.0) < 0
                for name in numeric_fields
            ) or vals.get('useful_life_years_snapshot', 0) <= 0:
                raise ValidationError(_(
                    "Depreciation evidence amounts and policy snapshots must "
                    "be finite and non-negative."
                ))
            if vals.get('residual_value_snapshot', 0.0) \
                    > vals.get('initial_cost_snapshot', 0.0):
                raise ValidationError(_(
                    "Depreciation evidence residual value cannot exceed its "
                    "cost snapshot."
                ))
            currency = prop.currency_id
            if (
                not currency.is_zero(
                    vals['opening_carrying'] - vals['charge']
                    - vals['closing_carrying']
                )
                or not currency.is_zero(
                    vals['opening_accumulated'] + vals['charge']
                    - vals['closing_accumulated']
                )
            ):
                raise ValidationError(_(
                    "Depreciation evidence roll-forward does not reconcile."
                ))
        return super().create(vals_list)

    def write(self, vals):
        raise AccessError(_(
            "Investment-property depreciation evidence is immutable."
        ))

    def unlink(self):
        raise AccessError(_(
            "Investment-property depreciation evidence is immutable."
        ))


class EhInvestmentPropertyTransfer(models.Model):
    """Immutable audit trail of IAS 40.57-62 transfers.

    One row per transfer into or out of investment property, written by the
    transfer actions and never edited: it captures the measurement basis at
    the transfer date, the carrying amount immediately before, the fair
    value at the transfer date, the remeasurement delta actually posted with
    its P&L/OCI routing, and the journal entries, so any transfer can be
    reconstructed end to end from the log alone.
    """

    _name = 'eh.investment.property.transfer'
    _description = "Investment Property Transfer Log (IAS 40.57-62)"
    _order = 'date desc, id desc'

    property_id = fields.Many2one(
        'eh.investment.property', string="Investment Property",
        required=True, ondelete='restrict', index=True, readonly=True)
    company_id = fields.Many2one(
        'res.company', required=True, index=True, readonly=True)
    currency_id = fields.Many2one('res.currency', required=True,
                                  readonly=True)
    date = fields.Date(
        required=True, readonly=True,
        help="Date of the change in use (IAS 40.57).")
    direction = fields.Selection(
        [('in', "Into investment property"),
         ('out', "Out of investment property")],
        required=True, readonly=True)
    basis = fields.Selection(
        [('fair_value', "Fair value model"), ('cost', "Cost model")],
        string="Basis at Transfer", required=True, readonly=True)
    carrying_before = fields.Monetary(
        currency_field='currency_id', readonly=True,
        help="Carrying amount immediately before the transfer.")
    fair_value = fields.Monetary(
        currency_field='currency_id', readonly=True,
        help="Fair value at the transfer date. Posted under the fair value "
             "model; stored for disclosure only under the cost model, where "
             "IAS 40.59 keeps the carrying amount unchanged.")
    delta_posted = fields.Monetary(
        currency_field='currency_id', readonly=True,
        help="Signed remeasurement actually posted on transfer (fair value "
             "less carrying amount before). Zero when no remeasurement "
             "arises.")
    delta_routing = fields.Selection(
        [('none', "No remeasurement"), ('pl', "Profit or loss"),
         ('oci', "OCI revaluation surplus"),
         ('mixed', "Profit or loss and OCI")],
        required=True, default='none', readonly=True,
        help="Where the remeasurement delta was recognised: profit or loss "
             "(IAS 40.35/.60-62) or the equity revaluation surplus "
             "(IAS 40.61).")
    remeasure_move_id = fields.Many2one(
        'account.move', string="Remeasurement Entry", readonly=True,
        ondelete='restrict', copy=False)
    move_id = fields.Many2one(
        'account.move', string="Transfer Entry", readonly=True,
        ondelete='restrict', copy=False)
    source_document = fields.Char(
        readonly=True,
        help="Source of an inbound transfer, e.g. the fixed asset "
             "derecognised into the property.")
    source_asset_id = fields.Integer(
        string="Source Fixed Asset ID", readonly=True, copy=False,
        help="Immutable fixed-asset identifier captured by the transfer "
             "engine. Stored on the append-only log so later edits to the "
             "property master cannot repoint source ownership.")
    source_state_before = fields.Char(
        readonly=True, copy=False,
        help="Source fixed-asset state immediately before transfer.",
    )
    source_revaluation_surplus_before = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Source revaluation surplus immediately before transfer.",
    )
    source_revaluation_pl_decrease_before = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Source prior P&L revaluation decrease immediately before transfer.",
    )
    note = fields.Char(readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get('eh_ip_transfer_log_capability')
            is not _IP_TRANSFER_LOG_CAPABILITY
        ):
            raise AccessError(_(
                "Investment-property transfer evidence can only be created "
                "by the transfer workflow."
            ))
        Property = self.env['eh.investment.property']
        for vals in vals_list:
            prop = Property.browse(vals.get('property_id')).exists()
            move = self.env['account.move'].browse(vals.get('move_id')).exists()
            remeasure = self.env['account.move'].browse(
                vals.get('remeasure_move_id'),
            ).exists()
            if not prop or not move:
                raise ValidationError(_(
                    "Transfer evidence requires an existing property and "
                    "transfer journal entry."
                ))
            if (
                vals.get('company_id') != prop.company_id.id
                or vals.get('currency_id') != prop.currency_id.id
                or vals.get('basis') != prop.model_basis
                or move.company_id != prop.company_id
                or move.eh_investment_property_id != prop
                or move.state != 'posted'
                or not move.eh_sealed
                or move.date != fields.Date.to_date(vals.get('date'))
            ):
                raise ValidationError(_(
                    "Transfer evidence must match the property's exact "
                    "posted, sealed, same-company transfer entry."
                ))
            if remeasure and (
                remeasure.company_id != prop.company_id
                or remeasure.eh_investment_property_id != prop
                or remeasure.state != 'posted'
                or not remeasure.eh_sealed
                or remeasure.date != fields.Date.to_date(vals.get('date'))
            ):
                raise ValidationError(_(
                    "Transfer remeasurement evidence is inconsistent."
                ))
            if vals.get('direction') == 'in':
                asset = self.env['eh.asset'].browse(
                    vals.get('source_asset_id'),
                ).exists()
                if not asset or asset.company_id != prop.company_id:
                    raise ValidationError(_(
                        "Inbound transfer evidence requires the exact "
                        "same-company source fixed asset."
                    ))
                if vals.get('source_state_before') not in ('running', 'paused'):
                    raise ValidationError(_(
                        "Inbound transfer evidence must snapshot the source "
                        "asset's running or paused pre-transfer state."
                    ))
            for field_name in ('carrying_before', 'fair_value', 'delta_posted'):
                value = vals.get(field_name, 0.0)
                if not math.isfinite(value):
                    raise ValidationError(_(
                        "Transfer evidence amounts must be finite."
                    ))
            if vals.get('carrying_before', 0.0) < 0 \
                    or vals.get('fair_value', 0.0) < 0:
                raise ValidationError(_(
                    "Transfer carrying amount and fair value cannot be "
                    "negative."
                ))
            for field_name in (
                'source_revaluation_surplus_before',
                'source_revaluation_pl_decrease_before',
            ):
                value = vals.get(field_name, 0.0)
                if not math.isfinite(value) or value < 0:
                    raise ValidationError(_(
                        "Source revaluation snapshots must be finite and "
                        "non-negative."
                    ))
        return super().create(vals_list)

    def write(self, vals):
        raise UserError(_(
            "Investment property transfer log rows are an immutable audit "
            "trail; they are written once by the transfer actions and never "
            "edited."))

    def unlink(self):
        raise UserError(_(
            "Investment property transfer log rows are an immutable audit "
            "trail and cannot be deleted."))


class EhAsset(models.Model):
    """Protect fixed assets derecognised into investment property."""

    _inherit = 'eh.asset'

    def _eh_has_ip_transfer_owner(self):
        self.ensure_one()
        Log = self.env['eh.investment.property.transfer'].sudo()
        if Log.search_count([
            ('direction', '=', 'in'), ('source_asset_id', '=', self.id),
            ('property_id.state', '!=', 'reversed'),
        ], limit=1):
            return True
        # Upgrade-safe fallback for pre-1.1.4 logs until migration captures
        # the immutable source id.
        return bool(self.env['eh.investment.property'].sudo().search_count([
            ('transfer_in_asset_id', '=', self.id),
            ('transfer_log_ids.direction', '=', 'in'),
            ('state', '!=', 'reversed'),
        ], limit=1))

    def _eh_assert_not_ip_transferred(self):
        if (
            self.env.context.get('eh_ip_asset_transition_capability')
            is _IP_ASSET_TRANSITION_CAPABILITY
        ):
            return True
        for asset in self:
            if asset._eh_has_ip_transfer_owner():
                raise UserError(_(
                    "Asset %s was derecognised into investment property. "
                    "Its source lifecycle is terminal; use the investment-"
                    "property workflow for subsequent accounting.",
                    asset.display_name))
        return True

    def _eh_assert_available_for_ip_transfer(self):
        for asset in self:
            if asset.deferred_type != 'asset' or asset.asset_class != 'tangible':
                raise UserError(_(
                    "Only a tangible fixed asset can transfer into investment "
                    "property; deferred and intangible records use different "
                    "measurement models."
                ))
            if asset.is_under_construction:
                raise UserError(_(
                    "Asset %s is still under construction. Complete and "
                    "reconcile that lifecycle before an IAS 40 "
                    "transfer.", asset.display_name,
                ))
            if asset.parent_asset_id or asset.component_ids:
                raise UserError(_(
                    "Componentised asset %s cannot be transferred alone. "
                    "Reclassify the complete component set through an explicit "
                    "group workflow.", asset.display_name,
                ))
            if asset.origin_source_quarantined \
                    or asset.currency_mismatch_quarantined:
                raise UserError(_(
                    "Asset %s is quarantined and cannot become the basis of "
                    "investment-property accounting.", asset.display_name,
                ))
            if asset.lvp_pool_id:
                raise UserError(_(
                    "Asset %(asset)s belongs to low-value pool %(pool)s. Its "
                    "carrying amount is controlled at pool level and cannot "
                    "be transferred individually into investment property.",
                    asset=asset.display_name,
                    pool=asset.lvp_pool_id.display_name))
            if asset.disposal_move_id or asset._eh_has_ip_transfer_owner():
                raise UserError(_(
                    "Asset %s already carries terminal disposal or "
                    "investment-property transfer evidence and cannot be "
                    "transferred again.", asset.display_name))
            hfs_check = getattr(asset, '_eh_assert_not_hfs_owned', None)
            if hfs_check:
                hfs_check()
        return True

    def _eh_assert_external_master_write_allowed(self, field_names):
        result = super()._eh_assert_external_master_write_allowed(field_names)
        if self.env.context.get(
                'eh_ip_asset_transition_capability') \
                is not _IP_ASSET_TRANSITION_CAPABILITY:
            self._eh_assert_not_ip_transferred()
        return result

    def action_resume(self):
        if not self.env.su:
            self._eh_check_access('write')
        self._eh_lock_for_transition()
        self._eh_assert_not_ip_transferred()
        return super().action_resume()

    def action_set_to_draft(self):
        if not self.env.su:
            self._eh_check_access('write')
        self._eh_lock_for_transition()
        self._eh_assert_not_ip_transferred()
        return super().action_set_to_draft()


class EhAssetDepreciationLine(models.Model):
    _inherit = 'eh.asset.depreciation.line'

    def action_post(self):
        assets = self.mapped('asset_id')
        if not self.env.su:
            self._eh_check_access('write')
            assets._eh_check_access('write')
        assets._eh_lock_for_transition()
        assets._eh_assert_not_ip_transferred()
        return super().action_post()


class EhAssetImpairment(models.Model):
    _inherit = 'eh.asset.impairment'

    def action_post(self):
        assets = self.mapped('asset_id')
        if not self.env.su:
            self._eh_check_access('write')
            assets._eh_check_access('write')
        assets._eh_lock_for_transition()
        assets._eh_assert_not_ip_transferred()
        return super().action_post()

    def action_cancel(self):
        assets = self.mapped('asset_id')
        if not self.env.su:
            self._eh_check_access('write')
            assets._eh_check_access('write')
        assets._eh_lock_for_transition()
        assets._eh_assert_not_ip_transferred()
        return super().action_cancel()


class EhAssetRevalueWizard(models.TransientModel):
    _inherit = 'eh.asset.revalue.wizard'

    def action_revalue(self):
        self.ensure_one()
        self.asset_id._eh_check_access('write')
        self.asset_id._eh_lock_for_transition()
        self.asset_id._eh_assert_not_ip_transferred()
        return super().action_revalue()


class EhAssetDisposeWizard(models.TransientModel):
    _inherit = 'eh.asset.dispose.wizard'

    def action_dispose(self):
        self.ensure_one()
        self.asset_id._eh_check_access('write')
        self.asset_id._eh_lock_for_transition()
        self.asset_id._eh_assert_not_ip_transferred()
        return super().action_dispose()


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_investment_property_id = fields.Many2one(
        'eh.investment.property', string="Investment Property", readonly=True,
        index=True, ondelete='restrict', copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        if any(
            vals.get('eh_investment_property_id') for vals in vals_list
        ) and not (
            self.env.context.get('eh_ip_move_link_capability')
            is _IP_MOVE_LINK_CAPABILITY
        ):
            raise AccessError(_(
                "Investment-property journal provenance is engine-owned."
            ))
        return super().create(vals_list)

    def write(self, vals):
        if 'eh_investment_property_id' in vals and not (
            self.env.context.get('eh_ip_move_link_capability')
            is _IP_MOVE_LINK_CAPABILITY
        ):
            raise AccessError(_(
                "Investment-property journal provenance is immutable."
            ))
        return super().write(vals)
