# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Subsequent accounting for a recognised business combination.

* eh.bizcombo.adjustment: a measurement-period adjustment (IFRS 3.45-49).
  New information about facts and circumstances that existed at the
  acquisition date, obtained within 12 months of it, restates the
  provisional amounts retrospectively: the stepped asset or liability moves
  against goodwill (with any deferred-tax and proportionate-NCI knock-on),
  and the combination's recognised amounts are updated to the revised
  figures. Frozen once applied; blocked after 12 months or once the
  measurement period is closed.

* eh.bizcombo.contingent.remeasure: a subsequent fair-value remeasurement of
  liability-classified contingent consideration, recognised in profit or
  loss (IFRS 3.58a). Equity-classified contingent consideration is never
  remeasured (IFRS 3.58b), enforced by constraint.
"""

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


class EhBizcomboAdjustment(models.Model):
    _name = 'eh.bizcombo.adjustment'
    _description = "Measurement-period adjustment (IFRS 3.45-49)"
    _inherit = ['mail.thread', 'eh.workflow.guard', 'eh.gl.reversal']
    _order = 'date, id'
    _rec_name = 'name'

    # State advances only through this record's own actions (run under sudo),
    # never a direct RPC write past action_apply and its posted entry.
    _eh_guarded_fields = ('state',)

    combination_id = fields.Many2one(
        'eh.business.combination', required=True, ondelete='cascade',
        index=True)
    company_id = fields.Many2one(
        related='combination_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='combination_id.currency_id', store=True, readonly=True)
    name = fields.Char(
        required=True, string="New Information",
        help="The new information about facts and circumstances that "
             "existed at the acquisition date which drives this adjustment "
             "(IFRS 3.45); disclosed under IFRS 3.B67(a).")
    date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
        help="Date the new information was obtained. Must fall within the "
             "measurement period: at most 12 months after the acquisition "
             "date (IFRS 3.45).")
    retrospective_effective_date = fields.Date(
        related='combination_id.acquisition_date', store=True, readonly=True,
        string="Retrospective Effective Date",
        help="Acquisition date from which provisional amounts are restated "
             "as if measurement had been complete (IFRS 3.49).")
    accounting_date = fields.Date(
        required=True, tracking=True,
        help="Exact date requested for the adjustment journal entry. This is "
             "separate from both the acquisition-date retrospective effect "
             "and the date new information became known.")
    state = fields.Selection(
        [('draft', "Draft"), ('applied', "Applied"),
         ('reversed', "Reversed"), ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)
    line_ids = fields.One2many(
        'eh.bizcombo.adjustment.line', 'adjustment_id', string="Line Deltas",
        help="Provisional identifiable asset or liability lines restated to "
             "a revised fair value (IFRS 3.46).")
    consideration_delta = fields.Monetary(
        currency_field='currency_id',
        help="Change to the consideration transferred arising from the new "
             "information. Positive raises goodwill.")
    consideration_delta_kind = fields.Selection(
        [
            ('clearing', "Cash / Other Consideration"),
            ('contingent', "Contingent Consideration (Acquisition-date FV)"),
        ],
        required=True, default='clearing', tracking=True,
        help="Select Contingent Consideration when new measurement-period "
             "information changes its acquisition-date fair value. That "
             "choice adjusts the proved contingent liability/equity and its "
             "fair-value base; other consideration uses acquisition clearing.")
    nci_delta = fields.Monetary(
        currency_field='currency_id',
        help="Change to the fair-value-basis non-controlling interest. Only "
             "for combinations measuring NCI at fair value; under the "
             "proportionate basis NCI re-derives from the restated net "
             "assets automatically.")
    goodwill_delta = fields.Monetary(
        currency_field='currency_id', readonly=True, copy=False,
        help="Net movement posted against goodwill by this adjustment; the "
             "retrospective restatement disclosed under IFRS 3.49/B67(a).")
    catchup_depreciation_amortisation = fields.Monetary(
        string="Catch-up Depreciation / Amortisation",
        currency_field='currency_id',
        help="Signed cumulative depreciation or amortisation difference from "
             "the acquisition date through the information date (IFRS "
             "3.49(b)). Positive records additional expense; negative reverses "
             "expense or comparative equity.")
    catchup_treatment = fields.Selection(
        [
            ('current_period', "Current-period catch-up"),
            ('comparative_restatement', "Comparative-period restatement"),
        ],
        required=True, default='current_period', tracking=True,
        help="Current-period catch-up posts the offset to profit or loss on "
             "or after the information date. Comparative restatement posts "
             "the offset to the configured comparative equity account on an "
             "authorised prior-period accounting date.")
    catchup_accumulated_account_id = fields.Many2one(
        'account.account', string="Accumulated Depreciation / Amortisation",
        check_company=True,
        domain="[('account_type', 'in', "
               "['asset_non_current', 'asset_fixed'])]",
        help="Contra-asset account carrying the cumulative catch-up.")
    catchup_expense_account_id = fields.Many2one(
        'account.account', string="Catch-up Expense Account",
        check_company=True,
        domain="[('account_type', 'in', ['expense', 'expense_other', "
               "'expense_depreciation', 'expense_direct_cost'])]",
        help="Profit-or-loss offset used for current-period catch-up.")
    comparative_equity_account_id = fields.Many2one(
        'account.account', string="Comparative Restatement Account",
        check_company=True,
        domain="[('account_type', 'in', ['equity', 'equity_unaffected'])]",
        help="Equity/retained-earnings offset for a comparative-period "
             "restatement authorised under the reporting close process.")
    comparative_restatement_note = fields.Text(
        help="Required for comparative restatement: identify comparative "
             "periods, reopening approval and disclosure evidence.")
    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='set null')
    reversal_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict')
    reversed_at = fields.Datetime(readonly=True, copy=False, tracking=True)
    reversed_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False, string="Reversed By")
    previous_consideration_transferred = fields.Monetary(
        currency_field='currency_id', readonly=True, copy=False,
        help="Consideration immediately before this adjustment was applied; "
             "immutable rollback evidence.")
    previous_nci_amount = fields.Monetary(
        currency_field='currency_id', readonly=True, copy=False,
        help="Non-controlling interest immediately before this adjustment "
             "was applied; immutable rollback evidence.")
    previous_contingent_consideration_initial_fv = fields.Monetary(
        currency_field='currency_id', readonly=True, copy=False,
        help="Acquisition-date contingent consideration fair value immediately "
             "before this adjustment; immutable rollback evidence.")
    reversal_basis_version = fields.Integer(readonly=True, copy=False)
    notes = fields.Text()

    _SERVER_FIELDS = (
        'company_id', 'currency_id', 'retrospective_effective_date',
        'goodwill_delta', 'move_id',
        'reversal_move_id', 'reversed_at', 'reversed_by_id',
        'previous_consideration_transferred', 'previous_nci_amount',
        'previous_contingent_consideration_initial_fv',
        'reversal_basis_version',
    )
    _IMMUTABLE_STATES = frozenset({'applied', 'reversed'})

    @api.model
    def _eh_prepare_combinations(self, combination_ids):
        combinations = self.env['eh.business.combination'].browse(
            sorted(set(combination_ids)))
        combinations._eh_check_caller_scope('read')
        combinations._eh_lock()
        return combinations

    @api.model_create_multi
    def create(self, vals_list):
        if (set().union(*(set(vals) for vals in vals_list or [{}]))
                & set(self._SERVER_FIELDS)
                and not (self.env.su
                         and self.env.context.get('eh_bizcombo_engine'))):
            raise AccessError(_(
                "Adjustment company, currency, output and move evidence are "
                "server-owned."))
        for vals in vals_list:
            vals.setdefault(
                'accounting_date',
                vals.get('date') or fields.Date.context_today(self),
            )
        self._eh_prepare_combinations([
            vals.get('combination_id') for vals in vals_list
            if vals.get('combination_id')])
        return super().create(vals_list)

    @api.constrains('date', 'accounting_date', 'catchup_treatment',
                    'combination_id')
    def _check_measurement_period(self):
        """The measurement period cannot exceed one year (IFRS 3.45)."""
        for adj in self:
            acquisition = adj.combination_id.acquisition_date
            if not adj.date or not acquisition:
                continue
            if adj.date < acquisition:
                raise ValidationError(_(
                    "A measurement-period adjustment cannot be dated before "
                    "the acquisition date."))
            if adj.date > acquisition + relativedelta(months=12):
                raise ValidationError(_(
                    "The measurement period ends at most 12 months after "
                    "the acquisition date (IFRS 3.45); new information "
                    "obtained after %s is accounted for prospectively, not "
                    "as a measurement-period adjustment.",
                    acquisition + relativedelta(months=12)))
            if adj.accounting_date and adj.accounting_date < acquisition:
                raise ValidationError(_(
                    "The adjustment accounting date cannot precede the "
                    "acquisition date."))
            if (adj.catchup_treatment == 'current_period'
                    and adj.accounting_date
                    and adj.accounting_date < adj.date):
                raise ValidationError(_(
                    "A current-period catch-up cannot post before the date "
                    "the new information became known."))

    def write(self, vals):
        if not self.env.su:
            self._eh_check_access('write')
        combination_ids = self.mapped('combination_id').ids
        if vals.get('combination_id'):
            combination_ids.append(vals['combination_id'])
        self._eh_prepare_combinations(combination_ids)
        # The combination row is the shared serialization point. Discard any
        # adjustment state/evidence prefetched before this worker waited on it.
        self.invalidate_recordset()
        if (set(vals) & set(self._SERVER_FIELDS)
                and not (self.env.su
                         and self.env.context.get('eh_bizcombo_engine'))):
            raise AccessError(_(
                "Adjustment company, currency, output and move evidence are "
                "server-owned."))
        if 'move_id' in vals and any(
                adj.move_id and adj.move_id.id != vals.get('move_id')
                for adj in self):
            raise AccessError(_("Adjustment move provenance is immutable."))
        reversal_update = (
            self.env.su
            and self.env.context.get('eh_bizcombo_reversal')
            and set(vals) <= {
                'state', 'reversal_move_id', 'reversed_at', 'reversed_by_id',
            }
        )
        for adj in self:
            if adj.state in self._IMMUTABLE_STATES and not reversal_update:
                raise UserError(_(
                    "Measurement-period adjustment %s is applied and "
                    "posted or reversed; it cannot be changed.",
                    adj.display_name))
        return super().write(vals)

    def unlink(self):
        self._eh_prepare_combinations(self.mapped('combination_id').ids)
        self.invalidate_recordset()
        for adj in self:
            if adj.state in self._IMMUTABLE_STATES:
                raise UserError(_(
                    "Measurement-period adjustment %s carries immutable "
                    "posting evidence and cannot be deleted.",
                    adj.display_name))
        return super().unlink()

    def action_cancel(self):
        if not self.env.su:
            self._eh_check_access('write')
        self._eh_prepare_combinations(self.mapped('combination_id').ids)
        self.invalidate_recordset()
        self = self._eh_workflow_action().with_context(
            eh_bizcombo_engine=True)
        for adj in self:
            if adj.state != 'draft':
                raise UserError(_(
                    "Only a draft adjustment can be cancelled."))
            adj.state = 'cancelled'
        return True

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No entry has been posted yet."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move', 'res_id': self.move_id.id,
            'view_mode': 'form', 'views': [(False, 'form')],
        }

    def action_apply(self):
        """Post the retrospective restatement and update the combination.

        Recomputes each goodwill component (identifiable net assets from the
        revised line fair values, deferred tax on the revised step-up,
        proportionate NCI from the restated net assets, consideration and
        fair-value NCI from the scalar deltas), posts one balanced entry for
        the differences with goodwill absorbing the residual (IFRS 3.48),
        then writes the revised amounts onto the frozen combination under
        the restatement context. Balanced by construction because the
        goodwill delta is derived from the same rounded components as the
        other legs.
        """
        self.ensure_one()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can apply a "
                "measurement-period adjustment."))
        if not self.env.su:
            self._eh_check_access('write')
        combo = self.combination_id
        combo._eh_check_caller_scope('write')
        combo._eh_lock()
        # Re-read the entire evidence graph after the shared combination lock.
        # Otherwise a line/state edit committed while this worker waited could
        # survive in the ORM cache and be posted twice or from stale amounts.
        self.invalidate_recordset()
        self.line_ids.invalidate_recordset()
        combo.asset_line_ids.invalidate_recordset()
        combo.contingent_remeasure_ids.invalidate_recordset()
        extra_accounts = self.line_ids.mapped('asset_line_id.account_id')
        extra_accounts |= self.catchup_accumulated_account_id
        extra_accounts |= self.catchup_expense_account_id
        extra_accounts |= self.comparative_equity_account_id
        combo._eh_validate_posting_scope(extra_accounts)
        # Run the state transition as su so the guarded 'state' write is
        # accepted; env.user (checked above) is preserved for audit stamps.
        self = self._eh_workflow_action().with_context(
            eh_bizcombo_engine=True)
        if self.state != 'draft':
            raise UserError(_("Only a draft adjustment can be applied."))
        combo = self.combination_id
        currency = combo.currency_id
        if combo.state != 'recognised':
            raise UserError(_(
                "Measurement-period adjustments restate a recognised "
                "combination; recognise %s first.", combo.display_name))
        if combo.measurement_period_closed:
            raise UserError(_(
                "The measurement period of %s is closed (IFRS 3.45); "
                "account for the new information prospectively instead.",
                combo.display_name))
        if self.date > combo.measurement_period_end:
            raise UserError(_(
                "The measurement period of %s ended on %s (IFRS 3.45); "
                "this adjustment is dated after it.",
                combo.display_name, combo.measurement_period_end))
        if self.accounting_date < combo.acquisition_date:
            raise UserError(_(
                "The adjustment accounting date cannot precede the "
                "acquisition date."))
        if (self.catchup_treatment == 'current_period'
                and self.accounting_date < self.date):
            raise UserError(_(
                "A current-period catch-up cannot post before the date the "
                "new information became known."))
        if not combo.journal_id:
            raise UserError(_("Configure the journal on the combination."))
        if not combo.goodwill_account_id:
            raise UserError(_(
                "Configure the goodwill account on the combination."))
        if not currency.is_zero(combo.bargain_purchase_gain):
            raise UserError(_(
                "%s was recognised as a bargain purchase; restating it "
                "through goodwill is not supported. Reverse and rebook the "
                "combination instead.", combo.display_name))
        if (not self.line_ids
                and currency.is_zero(self.consideration_delta)
                and currency.is_zero(self.nci_delta)):
            raise UserError(_(
                "Enter at least one revised line or a scalar delta; there "
                "is nothing to apply."))
        if combo.nci_measurement == 'proportionate' \
                and not currency.is_zero(self.nci_delta):
            raise UserError(_(
                "Under the proportionate basis the non-controlling interest "
                "re-derives from the restated net assets (IFRS 3.19); leave "
                "the NCI delta nil."))
        catchup = currency.round(self.catchup_depreciation_amortisation)
        if not currency.is_zero(catchup):
            if not self.catchup_accumulated_account_id:
                raise UserError(_(
                    "Configure the accumulated depreciation/amortisation "
                    "account for the catch-up."))
            if self.catchup_treatment == 'current_period':
                if not self.catchup_expense_account_id:
                    raise UserError(_(
                        "Configure the catch-up expense account for a "
                        "current-period adjustment."))
            else:
                if not self.comparative_equity_account_id:
                    raise UserError(_(
                        "Configure the comparative restatement equity "
                        "account."))
                if not (self.comparative_restatement_note or '').strip():
                    raise UserError(_(
                        "Document comparative periods and reopening approval "
                        "before posting a comparative restatement."))

        # ---- recompute every goodwill component with the revisions ----
        revisions = {
            line.asset_line_id.id: currency.round(line.revised_fair_value)
            for line in self.line_ids}
        if combo.asset_line_ids:
            fina_new = 0.0
            step_up_new = 0.0
            for asset in combo.asset_line_ids:
                fair_value = revisions.get(asset.id, asset.fair_value)
                fina_new += -fair_value if asset.is_liability else fair_value
                diff = fair_value - asset.tax_base
                step_up_new += -diff if asset.is_liability else diff
            fina_new = currency.round(fina_new)
            deferred_tax_new = currency.round(
                currency.round(step_up_new) * (combo.tax_rate / 100.0))
        else:
            # Plug-style combination: only scalar deltas can apply.
            fina_new = combo.fv_identifiable_net_assets
            deferred_tax_new = combo.deferred_tax
        deferred_tax_delta = currency.round(
            deferred_tax_new - combo.deferred_tax)
        if not currency.is_zero(deferred_tax_delta) \
                and not combo.deferred_tax_account_id:
            raise UserError(_(
                "Configure the deferred tax account on the combination; the "
                "revised step-up changes the deferred tax."))
        if combo.nci_measurement == 'proportionate':
            # IFRS 3.19: proportionate NCI is a share of the recognised net
            # assets, i.e. net of the IAS 12.19 deferred tax on the step-up.
            # Mirror the stored _compute_nci_amount so the restated figure and
            # the posted NCI leg agree (and goodwill_new stays balanced).
            nci_new = currency.round(
                (fina_new - deferred_tax_new) * (combo.nci_pct / 100.0))
        else:
            nci_new = currency.round(combo.nci_amount + self.nci_delta)
        nci_delta = currency.round(nci_new - combo.nci_amount)
        if not currency.is_zero(nci_delta) and not combo.nci_account_id:
            raise UserError(_(
                "Configure the non-controlling interest account on the "
                "combination."))
        raw_consideration_delta = currency.round(self.consideration_delta)
        contingent_delta = (
            raw_consideration_delta
            if self.consideration_delta_kind == 'contingent' else 0.0
        )
        consideration_delta = (
            raw_consideration_delta
            if self.consideration_delta_kind == 'clearing' else 0.0
        )
        if not currency.is_zero(contingent_delta):
            applied_remeasurements = combo.contingent_remeasure_ids.filtered(
                lambda remeasurement: remeasurement.state == 'applied')
            if applied_remeasurements:
                raise UserError(_(
                    "Reverse applied contingent-consideration remeasurements "
                    "before changing the acquisition-date fair-value base."))
            if not combo.contingent_account_id:
                raise UserError(_(
                    "Configure the contingent consideration account on the "
                    "combination."))
        contingent_new = currency.round(
            combo.contingent_consideration_initial_fv + contingent_delta)
        if currency.compare_amounts(contingent_new, 0.0) < 0:
            raise UserError(_(
                "A measurement-period adjustment cannot reduce acquisition-"
                "date contingent consideration below zero."))
        consideration_new = currency.round(
            combo.consideration_transferred + consideration_delta)
        consideration_delta = currency.round(
            consideration_new - combo.consideration_transferred)
        if not currency.is_zero(consideration_delta) \
                and not combo.clearing_account_id:
            raise UserError(_(
                "Configure the acquisition clearing account on the "
                "combination."))
        goodwill_new = combo._goodwill_raw(
            consideration_new, nci_new, fina_new, deferred_tax_new,
            contingent_consideration=contingent_new)
        if currency.compare_amounts(goodwill_new, 0.0) < 0:
            raise UserError(_(
                "This adjustment would turn the remaining goodwill "
                "negative (a bargain purchase); restating goodwill below "
                "nil is not supported. Reverse and rebook the combination "
                "instead."))
        goodwill_delta = currency.round(goodwill_new - combo.goodwill)

        # ---- build the balanced restatement entry (IFRS 3.48) ----
        legs = []
        for line in self.line_ids:
            asset = line.asset_line_id
            delta = currency.round(
                revisions[asset.id] - asset.fair_value)
            if currency.is_zero(delta):
                continue
            label = _("Measurement-period restatement %s", asset.name)
            debit_side = (delta > 0) != asset.is_liability
            amount = abs(delta)
            if debit_side:
                legs.append((asset.account_id, amount, 0.0, label))
            else:
                legs.append((asset.account_id, 0.0, amount, label))
        if not currency.is_zero(deferred_tax_delta):
            label = _("Deferred tax on restated step-up %s", combo.name)
            if deferred_tax_delta > 0:
                legs.append((combo.deferred_tax_account_id, 0.0,
                             deferred_tax_delta, label))
            else:
                legs.append((combo.deferred_tax_account_id,
                             -deferred_tax_delta, 0.0, label))
        if not currency.is_zero(nci_delta):
            label = _("Non-controlling interest restated %s", combo.name)
            if nci_delta > 0:
                legs.append((combo.nci_account_id, 0.0, nci_delta, label))
            else:
                legs.append((combo.nci_account_id, -nci_delta, 0.0, label))
        if not currency.is_zero(consideration_delta):
            label = _("Consideration restated %s", combo.name)
            if consideration_delta > 0:
                legs.append((combo.clearing_account_id, 0.0,
                             consideration_delta, label))
            else:
                legs.append((combo.clearing_account_id,
                             -consideration_delta, 0.0, label))
        if not currency.is_zero(contingent_delta):
            label = _(
                "Acquisition-date contingent consideration restated %s",
                combo.name,
            )
            if contingent_delta > 0:
                legs.append((combo.contingent_account_id, 0.0,
                             contingent_delta, label))
            else:
                legs.append((combo.contingent_account_id,
                             -contingent_delta, 0.0, label))
        if not currency.is_zero(goodwill_delta):
            label = _("Goodwill restated %s", combo.name)
            if goodwill_delta > 0:
                legs.append((combo.goodwill_account_id, goodwill_delta, 0.0,
                             label))
            else:
                legs.append((combo.goodwill_account_id, 0.0,
                             -goodwill_delta, label))
        if not currency.is_zero(catchup):
            offset_account = (
                self.catchup_expense_account_id
                if self.catchup_treatment == 'current_period'
                else self.comparative_equity_account_id
            )
            label = _(
                "Acquisition-date depreciation/amortisation catch-up %s",
                combo.name,
            )
            if catchup > 0:
                legs.extend([
                    (offset_account, catchup, 0.0, label),
                    (self.catchup_accumulated_account_id, 0.0, catchup,
                     label),
                ])
            else:
                legs.extend([
                    (self.catchup_accumulated_account_id, -catchup, 0.0,
                     label),
                    (offset_account, 0.0, -catchup, label),
                ])
        if not legs:
            raise UserError(_(
                "Every revised amount equals the recognised amount; there "
                "is nothing to apply."))
        move = self._post_move(legs)

        # ---- restate the recognised amounts on the frozen combination ----
        # Capture each line's pre-restatement fair value for the IFRS 3.B67
        # disclosure before the stored amount moves (the adjustment is still
        # draft here, so its own freeze does not bite yet).
        for line in self.line_ids:
            line.previous_fair_value = line.asset_line_id.fair_value
        for line in self.line_ids:
            line.asset_line_id.with_context(
                eh_mp_restate=True).fair_value = revisions[
                    line.asset_line_id.id]
        combo_vals = {}
        if not currency.is_zero(consideration_delta):
            combo_vals['consideration_transferred'] = consideration_new
        if not currency.is_zero(contingent_delta):
            combo_vals['contingent_consideration_initial_fv'] = contingent_new
        if combo.nci_measurement != 'proportionate' \
                and not currency.is_zero(nci_delta):
            combo_vals['nci_amount'] = nci_new
        if combo_vals:
            combo.with_context(eh_mp_restate=True).write(combo_vals)
        self.write({
            'state': 'applied',
            'move_id': move.id,
            'goodwill_delta': goodwill_delta,
            'previous_consideration_transferred':
                combo.consideration_transferred - consideration_delta,
            'previous_nci_amount': combo.nci_amount - nci_delta,
            'previous_contingent_consideration_initial_fv':
                combo.contingent_consideration_initial_fv - contingent_delta,
            'reversal_basis_version': 2,
        })
        return True

    def action_reverse(self, accounting_date=None):
        """Reverse the latest applied retrospective restatement.

        Restatements are cumulative. Enforcing last-in-first-out reversal is
        what makes restoring the captured pre-adjustment acquisition amounts
        deterministic and prevents an older rollback from invalidating a
        later measurement-period entry.
        """
        self.ensure_one()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can reverse a "
                "measurement-period adjustment."))
        if not self.env.su:
            self._eh_check_access('write')
        combo = self.combination_id
        combo._eh_check_caller_scope('write')
        combo._eh_lock()
        self.invalidate_recordset()
        combo.adjustment_ids.invalidate_recordset(['state', 'move_id'])
        if self.state != 'applied' or not self.move_id:
            raise UserError(_(
                "Only an applied adjustment with its original entry can be "
                "reversed."))
        applied = combo.adjustment_ids.filtered(
            lambda adjustment: adjustment.state == 'applied')
        latest = applied.sorted(
            key=lambda adjustment: adjustment.move_id.id or 0,
        )[-1:]
        if latest != self:
            raise UserError(_(
                "Reverse the most recently applied measurement-period "
                "adjustment first."))
        if (
            self.consideration_delta_kind == 'contingent'
            and combo.contingent_remeasure_ids.filtered(
                lambda remeasurement: remeasurement.state == 'applied')
        ):
            raise UserError(_(
                "Reverse applied contingent-consideration remeasurements "
                "before reversing their acquisition-date fair-value base."))
        move = self.move_id
        if (
            move.eh_bizcombo_adjustment_id != self
            or move.company_id != combo.company_id
            or move.state != 'posted'
            or move.move_type != 'entry'
            or not move.eh_sealed
            or move.eh_legacy_unverified_seal
        ):
            raise UserError(_(
                "The adjustment move failed its verified sealed provenance "
                "check and will not be reversed."))
        requested_date = (
            fields.Date.to_date(accounting_date)
            if accounting_date else fields.Date.context_today(self)
        )
        if accounting_date and requested_date < move.date:
            raise UserError(_(
                "The reversal date cannot precede the original adjustment "
                "date %s.", move.date))
        reversal_date = max(requested_date, move.date)
        reversal = move.sudo(False)._eh_reverse_with_verified_capability([{
            'date': reversal_date,
            'journal_id': move.journal_id.id,
            'ref': _("Measurement-period adjustment reversal %s", combo.name),
        }], cancel=False)
        reversal.sudo().with_context(
            eh_bizcombo_engine=True,
        )._eh_write_sealed_metadata(
            {'eh_bizcombo_adjustment_reversal_id': self.id},
            {'eh_bizcombo_adjustment_reversal_id'},
        )
        reversal._eh_post_verified_reversal()
        self._eh_seal_reversal(reversal)

        # Restore the acquisition-date amounts only after the counter-entry is
        # safely posted. Any later exception rolls both operations back in the
        # surrounding transaction.
        for line in self.line_ids:
            line.asset_line_id.sudo().with_context(
                eh_mp_restate=True,
            ).write({'fair_value': line.previous_fair_value})
        if self.reversal_basis_version:
            consideration_before = self.previous_consideration_transferred
            nci_before = self.previous_nci_amount
        else:
            # Upgrade bridge for adjustment entries posted before explicit
            # rollback-basis fields existed.
            consideration_before = combo.currency_id.round(
                combo.consideration_transferred - self.consideration_delta)
            nci_before = combo.currency_id.round(
                combo.nci_amount - self.nci_delta)
        combo_vals = {}
        if (
            self.consideration_delta_kind == 'clearing'
            and not combo.currency_id.is_zero(self.consideration_delta)
        ):
            combo_vals['consideration_transferred'] = consideration_before
        if (
            self.reversal_basis_version >= 2
            and self.consideration_delta_kind == 'contingent'
            and not combo.currency_id.is_zero(self.consideration_delta)
        ):
            combo_vals['contingent_consideration_initial_fv'] = (
                self.previous_contingent_consideration_initial_fv
            )
        if (
            combo.nci_measurement != 'proportionate'
            and not combo.currency_id.is_zero(self.nci_delta)
        ):
            combo_vals['nci_amount'] = nci_before
        if combo_vals:
            combo.sudo().with_context(eh_mp_restate=True).write(combo_vals)
        self._eh_workflow_action().with_context(
            eh_bizcombo_engine=True,
            eh_bizcombo_reversal=True,
        ).write({
            'state': 'reversed',
            'reversal_move_id': reversal.id,
            'reversed_at': fields.Datetime.now(),
            'reversed_by_id': self.env.user.id,
        })
        return True

    def action_view_reversal(self):
        self.ensure_one()
        if not self.reversal_move_id:
            raise UserError(_("No reversal entry has been posted."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move', 'res_id': self.reversal_move_id.id,
            'view_mode': 'form', 'views': [(False, 'form')],
        }

    def _post_move(self, legs):
        self.ensure_one()
        combo = self.combination_id
        lines = [(0, 0, {
            'name': label, 'account_id': account.id,
            'debit': debit, 'credit': credit,
        }) for account, debit, credit, label in legs]
        move = self.env['account.move']._eh_create_sealed({
            'move_type': 'entry',
            'date': self.accounting_date,
            'journal_id': combo.journal_id.id,
            'ref': _("%s measurement-period adjustment", combo.name),
            'eh_sealed': True,
            'line_ids': lines,
        })
        move.sudo().with_context(
            eh_bizcombo_engine=True,
        )._eh_write_sealed_metadata(
            {'eh_bizcombo_adjustment_id': self.id},
            {'eh_bizcombo_adjustment_id'},
        )
        move.action_post()
        if move.state != 'posted' or move.date != self.accounting_date:
            raise UserError(_(
                "Measurement-period entry %(move)s did not post on requested "
                "accounting date %(requested)s (stored date: %(stored)s). "
                "Choose an open date or obtain an authorised lock-date "
                "exception.",
                move=move.display_name,
                requested=self.accounting_date,
                stored=move.date or _("not posted"),
            ))
        return move


class EhBizcomboAdjustmentLine(models.Model):
    _name = 'eh.bizcombo.adjustment.line'
    _description = "Measurement-period line delta (IFRS 3.46)"
    _order = 'id'

    adjustment_id = fields.Many2one(
        'eh.bizcombo.adjustment', required=True, ondelete='cascade',
        index=True)
    company_id = fields.Many2one(
        related='adjustment_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='adjustment_id.currency_id', store=True, readonly=True)
    asset_line_id = fields.Many2one(
        'eh.business.combination.asset', required=True, ondelete='restrict',
        string="Identifiable Line",
        help="The provisional identifiable asset or liability line being "
             "restated.")
    current_fair_value = fields.Monetary(
        related='asset_line_id.fair_value', string="Recognised Fair Value",
        readonly=True)
    previous_fair_value = fields.Monetary(
        currency_field='currency_id', readonly=True, copy=False,
        help="Fair value recognised before this adjustment was applied "
             "(captured on apply, for the IFRS 3.B67(a) disclosure).")
    revised_fair_value = fields.Monetary(
        currency_field='currency_id', required=True,
        help="Revised acquisition-date fair value per the new information "
             "(IFRS 3.46).")
    delta = fields.Monetary(
        compute='_compute_delta', currency_field='currency_id',
        help="Revised fair value less the fair value it replaces.")

    _SERVER_FIELDS = (
        'company_id', 'currency_id', 'current_fair_value',
        'previous_fair_value', 'delta',
    )

    @api.model
    def _eh_prepare_graph(self, adjustment_ids, asset_ids=None):
        adjustments = self.env['eh.bizcombo.adjustment'].browse(
            sorted(set(adjustment_ids)))
        adjustments._eh_check_access('read')
        combinations = adjustments.mapped('combination_id')
        assets = self.env['eh.business.combination.asset'].browse(
            sorted(set(asset_ids or [])))
        if assets:
            assets._eh_check_access('read')
            combinations |= assets.mapped('combination_id')
        combinations._eh_check_caller_scope('read')
        combinations._eh_lock()
        adjustments.invalidate_recordset()
        assets.invalidate_recordset()
        return adjustments, assets

    @api.depends('revised_fair_value', 'previous_fair_value',
                 'asset_line_id.fair_value', 'adjustment_id.state')
    def _compute_delta(self):
        for line in self:
            base = (line.previous_fair_value
                    if line.adjustment_id.state == 'applied'
                    else line.asset_line_id.fair_value)
            raw = line.revised_fair_value - base
            line.delta = (
                line.currency_id.round(raw) if line.currency_id else raw)

    @api.constrains('asset_line_id', 'adjustment_id')
    def _check_lines(self):
        for line in self:
            if (line.asset_line_id.combination_id
                    != line.adjustment_id.combination_id):
                raise ValidationError(_(
                    "The restated line must belong to the adjustment's "
                    "combination."))
        for adjustment in self.mapped('adjustment_id'):
            targets = adjustment.line_ids.mapped('asset_line_id')
            if len(targets) != len(adjustment.line_ids):
                raise ValidationError(_(
                    "An identifiable line can only be restated once per "
                    "adjustment."))

    def _check_not_applied(self, action):
        for line in self:
            if line.adjustment_id.state in ('applied', 'reversed'):
                raise UserError(_(
                    "Measurement-period adjustment %s is applied or "
                    "reversed; its lines cannot be %s.",
                    line.adjustment_id.display_name, action))

    @api.model_create_multi
    def create(self, vals_list):
        if (set().union(*(set(vals) for vals in vals_list or [{}]))
                & set(self._SERVER_FIELDS)
                and not (self.env.su
                         and self.env.context.get('eh_bizcombo_engine'))):
            raise AccessError(_(
                "Adjustment-line related and historical output fields are "
                "server-owned."))
        self._eh_prepare_graph(
            [vals.get('adjustment_id') for vals in vals_list
             if vals.get('adjustment_id')],
            [vals.get('asset_line_id') for vals in vals_list
             if vals.get('asset_line_id')],
        )
        for vals in vals_list:
            adjustment = self.env['eh.bizcombo.adjustment'].browse(
                vals.get('adjustment_id'))
            if adjustment and adjustment.state in ('applied', 'reversed'):
                raise UserError(_(
                    "Measurement-period adjustment %s is applied or "
                    "reversed; lines cannot be added.",
                    adjustment.display_name))
        return super().create(vals_list)

    def write(self, vals):
        if (set(vals) & set(self._SERVER_FIELDS)
                and not (self.env.su
                         and self.env.context.get('eh_bizcombo_engine'))):
            raise AccessError(_(
                "Adjustment-line related and historical output fields are "
                "server-owned."))
        adjustment_ids = self.mapped('adjustment_id').ids
        asset_ids = self.mapped('asset_line_id').ids
        if vals.get('adjustment_id'):
            adjustment_ids.append(vals['adjustment_id'])
        if vals.get('asset_line_id'):
            asset_ids.append(vals['asset_line_id'])
        self._eh_prepare_graph(adjustment_ids, asset_ids)
        self._check_not_applied(_("changed"))
        return super().write(vals)

    def unlink(self):
        self._eh_prepare_graph(
            self.mapped('adjustment_id').ids,
            self.mapped('asset_line_id').ids,
        )
        self._check_not_applied(_("deleted"))
        return super().unlink()


class EhBizcomboContingentRemeasure(models.Model):
    _name = 'eh.bizcombo.contingent.remeasure'
    _description = "Contingent consideration remeasurement (IFRS 3.58)"
    _inherit = ['mail.thread', 'eh.workflow.guard', 'eh.gl.reversal']
    _order = 'date, id'
    _rec_name = 'date'

    # State advances only through this record's own actions (run under sudo),
    # never a direct RPC write past action_apply and its posted entry.
    _eh_guarded_fields = ('state',)

    combination_id = fields.Many2one(
        'eh.business.combination', required=True, ondelete='cascade',
        index=True)
    company_id = fields.Many2one(
        related='combination_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='combination_id.currency_id', store=True, readonly=True)
    date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('applied', "Applied"),
         ('reversed', "Reversed"), ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)
    new_fair_value = fields.Monetary(
        currency_field='currency_id', required=True,
        help="Fair value of the contingent consideration liability at the "
             "remeasurement date (IFRS 3.58a).")
    previous_fair_value = fields.Monetary(
        currency_field='currency_id', readonly=True, copy=False,
        help="Fair value carried before this remeasurement (captured on "
             "apply).")
    delta = fields.Monetary(
        currency_field='currency_id', readonly=True, copy=False,
        help="New fair value less the fair value carried when applied; "
             "recognised in profit or loss (IFRS 3.58a).")
    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='set null')
    reversal_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict')
    reversed_at = fields.Datetime(readonly=True, copy=False, tracking=True)
    reversed_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False, string="Reversed By")
    notes = fields.Text()

    _SERVER_FIELDS = (
        'company_id', 'currency_id', 'previous_fair_value', 'delta', 'move_id',
        'reversal_move_id', 'reversed_at', 'reversed_by_id',
    )
    _IMMUTABLE_STATES = frozenset({'applied', 'reversed'})

    @api.constrains('date', 'combination_id')
    def _check_remeasurement_date(self):
        for remeasurement in self:
            acquisition = remeasurement.combination_id.acquisition_date
            if (remeasurement.date and acquisition
                    and remeasurement.date < acquisition):
                raise ValidationError(_(
                    "A contingent-consideration remeasurement cannot be "
                    "dated before the acquisition date."))

    @api.model
    def _eh_prepare_combinations(self, combination_ids):
        combinations = self.env['eh.business.combination'].browse(
            sorted(set(combination_ids)))
        combinations._eh_check_caller_scope('read')
        combinations._eh_lock()
        return combinations

    @api.model_create_multi
    def create(self, vals_list):
        if (set().union(*(set(vals) for vals in vals_list or [{}]))
                & set(self._SERVER_FIELDS)
                and not (self.env.su
                         and self.env.context.get('eh_bizcombo_engine'))):
            raise AccessError(_(
                "Remeasurement related, output and move fields are "
                "server-owned."))
        self._eh_prepare_combinations([
            vals.get('combination_id') for vals in vals_list
            if vals.get('combination_id')])
        return super().create(vals_list)

    @api.constrains('combination_id')
    def _check_liability_classified(self):
        """IFRS 3.58b: equity-classified contingent consideration is not
        remeasured; its later settlement is accounted for within equity."""
        for rec in self:
            if rec.combination_id.contingent_classification != 'liability':
                raise ValidationError(_(
                    "Equity-classified contingent consideration is not "
                    "remeasured (IFRS 3.58b); its settlement is accounted "
                    "for within equity."))

    def write(self, vals):
        if not self.env.su:
            self._eh_check_access('write')
        combination_ids = self.mapped('combination_id').ids
        if vals.get('combination_id'):
            combination_ids.append(vals['combination_id'])
        self._eh_prepare_combinations(combination_ids)
        self.invalidate_recordset()
        if (set(vals) & set(self._SERVER_FIELDS)
                and not (self.env.su
                         and self.env.context.get('eh_bizcombo_engine'))):
            raise AccessError(_(
                "Remeasurement related, output and move fields are "
                "server-owned."))
        if 'move_id' in vals and any(
                rec.move_id and rec.move_id.id != vals.get('move_id')
                for rec in self):
            raise AccessError(_("Remeasurement move provenance is immutable."))
        reversal_update = (
            self.env.su
            and self.env.context.get('eh_bizcombo_reversal')
            and set(vals) <= {
                'state', 'reversal_move_id', 'reversed_at', 'reversed_by_id',
            }
        )
        for rec in self:
            if rec.state in self._IMMUTABLE_STATES and not reversal_update:
                raise UserError(_(
                    "Contingent consideration remeasurement of %s is "
                    "applied and posted or reversed; it cannot be changed.",
                    rec.combination_id.display_name))
        return super().write(vals)

    def unlink(self):
        self._eh_prepare_combinations(self.mapped('combination_id').ids)
        self.invalidate_recordset()
        for rec in self:
            if rec.state in self._IMMUTABLE_STATES:
                raise UserError(_(
                    "Contingent consideration remeasurement of %s is "
                    "immutable posting evidence and cannot be deleted.",
                    rec.combination_id.display_name))
        return super().unlink()

    def action_cancel(self):
        if not self.env.su:
            self._eh_check_access('write')
        self._eh_prepare_combinations(self.mapped('combination_id').ids)
        self.invalidate_recordset()
        self = self._eh_workflow_action().with_context(
            eh_bizcombo_engine=True)
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_(
                    "Only a draft remeasurement can be cancelled."))
            rec.state = 'cancelled'
        return True

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No entry has been posted yet."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move', 'res_id': self.move_id.id,
            'view_mode': 'form', 'views': [(False, 'form')],
        }

    def action_apply(self):
        """Remeasure the liability to fair value through profit or loss.

        An increase in the liability is a loss (Dr profit or loss, Cr
        contingent consideration liability); a decrease is a gain the other
        way (IFRS 3.58a). The combination's current fair value rolls
        forward through the stored delta.
        """
        self.ensure_one()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can remeasure contingent "
                "consideration."))
        if not self.env.su:
            self._eh_check_access('write')
        combo = self.combination_id
        combo._eh_check_caller_scope('write')
        combo._eh_lock()
        self.invalidate_recordset()
        combo._eh_validate_posting_scope()
        # Run the state transition as su so the guarded 'state' write is
        # accepted; env.user (checked above) is preserved for audit stamps.
        self = self._eh_workflow_action().with_context(
            eh_bizcombo_engine=True)
        if self.state != 'draft':
            raise UserError(_("Only a draft remeasurement can be applied."))
        combo = self.combination_id
        currency = combo.currency_id
        if combo.state != 'recognised':
            raise UserError(_(
                "Recognise %s before remeasuring its contingent "
                "consideration.", combo.display_name))
        if self.date < combo.acquisition_date:
            raise UserError(_(
                "A contingent-consideration remeasurement cannot be dated "
                "before the acquisition date."))
        prior_applied = combo.contingent_remeasure_ids.filtered(
            lambda remeasurement: (
                remeasurement != self
                and remeasurement.state == 'applied'
            ))
        if prior_applied and self.date < max(prior_applied.mapped('date')):
            raise UserError(_(
                "Apply contingent-consideration remeasurements in date order. "
                "The latest applied date is %s.",
                max(prior_applied.mapped('date'))))
        if combo.contingent_classification != 'liability':
            raise UserError(_(
                "Equity-classified contingent consideration is not "
                "remeasured (IFRS 3.58b)."))
        if not combo.journal_id:
            raise UserError(_("Configure the journal on the combination."))
        if not combo.contingent_account_id:
            raise UserError(_(
                "Configure the contingent consideration account on the "
                "combination."))
        pnl_account = (combo.contingent_pnl_account_id
                       or combo.gain_account_id)
        if not pnl_account:
            raise UserError(_(
                "Configure the contingent remeasurement account (or the "
                "bargain purchase gain account it falls back to) on the "
                "combination."))
        new_fair_value = currency.round(self.new_fair_value)
        if currency.compare_amounts(new_fair_value, 0.0) < 0:
            raise UserError(_(
                "The remeasured fair value cannot be negative."))
        previous = currency.round(
            combo.contingent_consideration_current_fv)
        delta = currency.round(new_fair_value - previous)
        if currency.is_zero(delta):
            raise UserError(_(
                "The fair value is unchanged; nothing to remeasure."))
        if delta > 0:
            legs = [
                (pnl_account, delta, 0.0,
                 _("Contingent consideration remeasurement loss %s",
                   combo.name)),
                (combo.contingent_account_id, 0.0, delta,
                 _("Contingent consideration to fair value %s", combo.name)),
            ]
        else:
            legs = [
                (combo.contingent_account_id, -delta, 0.0,
                 _("Contingent consideration to fair value %s", combo.name)),
                (pnl_account, 0.0, -delta,
                 _("Contingent consideration remeasurement gain %s",
                   combo.name)),
            ]
        move = self._post_move(legs)
        self.write({
            'state': 'applied',
            'move_id': move.id,
            'previous_fair_value': previous,
            'delta': delta,
        })
        return True

    def action_reverse(self, accounting_date=None):
        """Reverse the latest applied contingent-consideration remeasurement."""
        self.ensure_one()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can reverse a contingent-"
                "consideration remeasurement."))
        if not self.env.su:
            self._eh_check_access('write')
        combo = self.combination_id
        combo._eh_check_caller_scope('write')
        combo._eh_lock()
        self.invalidate_recordset()
        combo.contingent_remeasure_ids.invalidate_recordset([
            'state', 'move_id', 'delta',
        ])
        if self.state != 'applied' or not self.move_id:
            raise UserError(_(
                "Only an applied remeasurement with its original entry can "
                "be reversed."))
        applied = combo.contingent_remeasure_ids.filtered(
            lambda remeasurement: remeasurement.state == 'applied')
        latest = applied.sorted(
            key=lambda remeasurement: remeasurement.move_id.id or 0,
        )[-1:]
        if latest != self:
            raise UserError(_(
                "Reverse the most recently applied contingent-consideration "
                "remeasurement first."))
        move = self.move_id
        if (
            move.eh_bizcombo_remeasure_id != self
            or move.company_id != combo.company_id
            or move.state != 'posted'
            or move.move_type != 'entry'
            or not move.eh_sealed
            or move.eh_legacy_unverified_seal
        ):
            raise UserError(_(
                "The remeasurement move failed its verified sealed "
                "provenance check and will not be reversed."))
        requested_date = (
            fields.Date.to_date(accounting_date)
            if accounting_date else fields.Date.context_today(self)
        )
        if accounting_date and requested_date < move.date:
            raise UserError(_(
                "The reversal date cannot precede the original remeasurement "
                "date %s.", move.date))
        reversal_date = max(requested_date, move.date)
        reversal = move.sudo(False)._eh_reverse_with_verified_capability([{
            'date': reversal_date,
            'journal_id': move.journal_id.id,
            'ref': _("Contingent consideration reversal %s", combo.name),
        }], cancel=False)
        reversal.sudo().with_context(
            eh_bizcombo_engine=True,
        )._eh_write_sealed_metadata(
            {'eh_bizcombo_remeasure_reversal_id': self.id},
            {'eh_bizcombo_remeasure_reversal_id'},
        )
        reversal._eh_post_verified_reversal()
        self._eh_seal_reversal(reversal)
        self._eh_workflow_action().with_context(
            eh_bizcombo_engine=True,
            eh_bizcombo_reversal=True,
        ).write({
            'state': 'reversed',
            'reversal_move_id': reversal.id,
            'reversed_at': fields.Datetime.now(),
            'reversed_by_id': self.env.user.id,
        })
        return True

    def action_view_reversal(self):
        self.ensure_one()
        if not self.reversal_move_id:
            raise UserError(_("No reversal entry has been posted."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move', 'res_id': self.reversal_move_id.id,
            'view_mode': 'form', 'views': [(False, 'form')],
        }

    def _post_move(self, legs):
        self.ensure_one()
        combo = self.combination_id
        lines = [(0, 0, {
            'name': label, 'account_id': account.id,
            'debit': debit, 'credit': credit,
        }) for account, debit, credit, label in legs]
        move = self.env['account.move']._eh_create_sealed({
            'move_type': 'entry',
            'date': self.date,
            'journal_id': combo.journal_id.id,
            'ref': _("%s contingent consideration remeasurement",
                     combo.name),
            'eh_sealed': True,
            'line_ids': lines,
        })
        move.sudo().with_context(
            eh_bizcombo_engine=True,
        )._eh_write_sealed_metadata(
            {'eh_bizcombo_remeasure_id': self.id},
            {'eh_bizcombo_remeasure_id'},
        )
        move.action_post()
        return move
