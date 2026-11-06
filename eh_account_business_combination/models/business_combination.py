# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.business.combination: an IFRS 3 acquisition and its goodwill.

Goodwill = consideration transferred (including the acquisition-date fair
value of contingent consideration, IFRS 3.39) + non-controlling interest
+ fair value of any previously-held interest - fair value of identifiable
net assets acquired (IFRS 3.32). A negative result is a bargain purchase
gain recognised in profit or loss (IFRS 3.34).

Step acquisitions remeasure the previously-held interest to acquisition-date
fair value with the gain or loss in profit or loss (IFRS 3.42). Subsequent
mechanics live in business_combination_subsequent.py: measurement-period
adjustments (IFRS 3.45-49) and contingent-consideration remeasurement
(IFRS 3.58).
"""

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


_MIN_PERCENTAGE = 0.0
_MAX_PERCENTAGE = 100.0


class EhBusinessCombination(models.Model):
    _name = 'eh.business.combination'
    _description = "Business combination (IFRS 3)"
    _inherit = [
        'mail.thread', 'mail.activity.mixin', 'eh.workflow.guard',
        'eh.gl.reversal',
    ]
    _order = 'acquisition_date desc, id desc'
    _rec_name = 'name'

    # State and the measurement-period close flag advance only through this
    # record's own actions (which run under sudo), never a direct RPC write.
    _eh_guarded_fields = (
        'state', 'measurement_period_closed', 'reversal_move_id',
        'reversed_at', 'reversed_by_id',
    )
    _eh_create_guarded_fields = _eh_guarded_fields

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    acquiree_name = fields.Char(required=True, tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('recognised', "Recognised"),
         ('reversed', "Reversed"), ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    acquisition_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)

    consideration_transferred = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Fair value of consideration transferred (IFRS 3.37).")
    nci_measurement = fields.Selection(
        [('fair_value', "Fair value"),
         ('proportionate', "Proportionate share of net assets")],
        default='fair_value', required=True,
        help="Measurement basis for non-controlling interest (IFRS 3.19).")
    nci_pct = fields.Float(
        string="NCI Ownership %", digits=(7, 4),
        help="Non-controlling (minority) ownership percentage. Under the "
             "proportionate basis (IFRS 3.19) the non-controlling interest is "
             "this percentage of the fair value of identifiable net assets.")
    nci_amount = fields.Monetary(
        currency_field='currency_id', tracking=True,
        compute='_compute_nci_amount', store=True, readonly=False,
        help="Non-controlling interest in the acquiree at acquisition. Under "
             "the fair-value basis this is entered directly; under the "
             "proportionate basis it is the NCI percentage of the fair value "
             "of identifiable net assets (IFRS 3.19).")
    previously_held_interest_fv = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Fair value of any equity interest held before the acquisition "
             "(step acquisition, IFRS 3.42).")
    previously_held_interest_carrying = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Carrying amount of the previously-held equity interest "
             "immediately before the acquisition date. Enter it to have the "
             "IFRS 3.42 remeasurement gain or loss computed and posted to "
             "profit or loss on recognition; leave nil to treat the fair "
             "value as already recognised (no remeasurement, the prior "
             "behaviour).")
    remeasurement_gain = fields.Monetary(
        compute='_compute_remeasurement_gain', store=True,
        currency_field='currency_id',
        help="Remeasurement of the previously-held interest to its "
             "acquisition-date fair value, recognised in profit or loss on "
             "recognition of the combination (IFRS 3.42): fair value less "
             "carrying amount. Nil while no carrying amount is entered.")
    remeasure_gain_account_id = fields.Many2one(
        'account.account', string="Step Remeasurement Account",
        check_company=True,
        tracking=True,
        domain="[('account_type', 'in', ['income', 'income_other', "
               "'expense', 'expense_other', 'expense_depreciation', "
               "'expense_direct_cost'])]",
        help="Profit or loss account for the IFRS 3.42 remeasurement of the "
             "previously-held interest. Falls back to the bargain purchase "
             "gain account when empty.")
    fx_reclass_note = fields.Text(
        string="FX Reclassification Note",
        help="IAS 21.48 tie-in: if the previously-held interest was a "
             "foreign-currency investment, any cumulative translation "
             "difference recycled on obtaining control is reclassified by "
             "the consolidation / FX revaluation module, not posted here. "
             "Document where that recycling is recognised so it is not "
             "double-posted.")
    contingent_consideration_initial_fv = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Acquisition-date fair value of contingent consideration. Part "
             "of the consideration transferred in the goodwill computation "
             "(IFRS 3.39) and recognised as a liability or equity per its "
             "classification (IFRS 3.40).")
    contingent_classification = fields.Selection(
        [('liability', "Liability"), ('equity', "Equity")],
        default='liability', required=True, tracking=True,
        help="IFRS 3.40 classification of contingent consideration. A "
             "liability is remeasured to fair value each period through "
             "profit or loss; an equity-classified instrument is not "
             "remeasured and its settlement is accounted for within equity "
             "(IFRS 3.58).")
    contingent_account_id = fields.Many2one(
        'account.account', string="Contingent Consideration Account",
        check_company=True,
        tracking=True,
        help="Balance-sheet account credited for the acquisition-date fair "
             "value of contingent consideration: a liability account when "
             "liability-classified, an equity account when "
             "equity-classified.")
    contingent_pnl_account_id = fields.Many2one(
        'account.account', string="Contingent Remeasurement Account",
        check_company=True,
        tracking=True,
        domain="[('account_type', 'in', ['income', 'income_other', "
               "'expense', 'expense_other', 'expense_depreciation', "
               "'expense_direct_cost'])]",
        help="Profit or loss account for subsequent fair-value remeasurement "
             "of liability-classified contingent consideration (IFRS 3.58). "
             "Falls back to the bargain purchase gain account when empty.")
    contingent_consideration_current_fv = fields.Monetary(
        compute='_compute_contingent_current_fv', store=True,
        currency_field='currency_id',
        string="Contingent Consideration (Current FV)",
        help="Acquisition-date fair value rolled forward for every applied "
             "remeasurement (IFRS 3.58).")
    contingent_remeasure_ids = fields.One2many(
        'eh.bizcombo.contingent.remeasure', 'combination_id',
        string="Contingent Remeasurements")
    adjustment_ids = fields.One2many(
        'eh.bizcombo.adjustment', 'combination_id',
        string="Measurement-Period Adjustments")
    measurement_period_closed = fields.Boolean(
        readonly=True, tracking=True, copy=False,
        help="Set when the measurement period is closed (IFRS 3.45): no "
             "further measurement-period adjustment can be applied. Closes "
             "at the latest 12 months after the acquisition date.")
    measurement_period_end = fields.Date(
        compute='_compute_measurement_period_end',
        help="Outer limit of the measurement period: 12 months after the "
             "acquisition date (IFRS 3.45).")
    fv_identifiable_net_assets = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Fair value of the identifiable assets acquired less the "
             "liabilities assumed (IFRS 3.18). Used when no identifiable "
             "asset lines are entered; otherwise the lines drive the total.")
    asset_line_ids = fields.One2many(
        'eh.business.combination.asset', 'combination_id',
        string="Identifiable Assets & Liabilities",
        help="Identifiable assets acquired and liabilities assumed at fair "
             "value (IFRS 3.18).")
    identifiable_net_assets = fields.Monetary(
        compute='_compute_identifiable_net_assets', store=True,
        currency_field='currency_id',
        help="Sum of identifiable assets less liabilities assumed, at fair "
             "value, from the entered lines (IFRS 3.18).")

    tax_rate = fields.Float(
        string="Deferred Tax Rate %", digits=(7, 4),
        help="Rate applied to the aggregate fair-value step-up of the "
             "identifiable asset and liability lines to raise deferred tax at "
             "acquisition (IAS 12.19/24). Fair-value adjustments that are not "
             "in the tax base create temporary differences: a taxable "
             "difference raises a deferred tax liability, a deductible one a "
             "deferred tax asset, and either adjusts goodwill (IAS 12.66). "
             "Leave nil to book no deferred tax.")
    fair_value_step_up = fields.Monetary(
        compute='_compute_deferred_tax', store=True,
        currency_field='currency_id',
        help="Aggregate fair-value step-up of the identifiable asset and "
             "liability lines: the taxable temporary difference between fair "
             "value and tax base (IAS 12.19).")
    deferred_tax = fields.Monetary(
        compute='_compute_deferred_tax', store=True,
        currency_field='currency_id',
        help="Deferred tax on the aggregate fair-value step-up. Positive is a "
             "deferred tax liability (raises goodwill); negative is a deferred "
             "tax asset (lowers goodwill) (IAS 12.24, .66).")
    deferred_tax_account_id = fields.Many2one(
        'account.account', string="Deferred Tax Account", tracking=True,
        check_company=True,
        help="Balance-sheet account for the deferred tax liability or asset "
             "raised on the fair-value step-up (IAS 12).")

    goodwill = fields.Monetary(
        compute='_compute_goodwill', store=True, currency_field='currency_id',
        help="Positive goodwill recognised as an asset.")
    bargain_purchase_gain = fields.Monetary(
        compute='_compute_goodwill', store=True, currency_field='currency_id',
        help="Gain recognised in profit or loss when the acquisition is a "
             "bargain purchase.")

    goodwill_account_id = fields.Many2one(
        'account.account', string="Goodwill Account", tracking=True,
        check_company=True,
        domain="[('account_type', 'in', "
               "['asset_non_current', 'asset_fixed'])]")
    clearing_account_id = fields.Many2one(
        'account.account', string="Acquisition Clearing Account",
        check_company=True,
        tracking=True,
        help="Account that carries the net of consideration and net assets "
             "recognised off this record; the goodwill entry's counterpart.")
    gain_account_id = fields.Many2one(
        'account.account', string="Bargain Purchase Gain Account",
        check_company=True,
        tracking=True,
        domain="[('account_type', '=', 'income_other')]")
    nci_account_id = fields.Many2one(
        'account.account', string="Non-controlling Interest Account",
        check_company=True,
        tracking=True,
        help="Equity account credited for the non-controlling interest in "
             "the full purchase price allocation entry (IFRS 3.19).")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        check_company=True,
        domain="[('type', '=', 'general'), ('company_id', '=', company_id)]")

    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='set null')
    reversal_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict')
    reversed_at = fields.Datetime(readonly=True, copy=False, tracking=True)
    reversed_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False, string="Reversed By")

    notes = fields.Text()

    @api.constrains('nci_pct', 'tax_rate')
    def _check_percentage_ranges(self):
        for combination in self:
            if not _MIN_PERCENTAGE <= combination.nci_pct <= _MAX_PERCENTAGE:
                raise ValidationError(_(
                    "NCI ownership percentage must be between 0 and 100."))
            if not _MIN_PERCENTAGE <= combination.tax_rate <= _MAX_PERCENTAGE:
                raise ValidationError(_(
                    "Deferred tax rate must be between 0 and 100."))

    def _validate_measurement_inputs(self):
        """Fail closed for legacy rows created before input constraints."""
        self.ensure_one()
        if not _MIN_PERCENTAGE <= self.nci_pct <= _MAX_PERCENTAGE:
            raise UserError(_(
                "NCI ownership percentage must be between 0 and 100."))
        if not _MIN_PERCENTAGE <= self.tax_rate <= _MAX_PERCENTAGE:
            raise UserError(_(
                "Deferred tax rate must be between 0 and 100."))
        negative_lines = self.asset_line_ids.filtered(
            lambda line: line.fair_value < 0.0)
        if negative_lines:
            raise UserError(_(
                "Identifiable asset and liability fair values cannot be "
                "negative. Use the Liability flag to control posting side: "
                "%s.", ', '.join(negative_lines.mapped('name'))))

    @api.depends('asset_line_ids.fair_value', 'asset_line_ids.is_liability')
    def _compute_identifiable_net_assets(self):
        for c in self:
            assets = sum(
                line.fair_value for line in c.asset_line_ids
                if not line.is_liability)
            liabilities = sum(
                line.fair_value for line in c.asset_line_ids
                if line.is_liability)
            net = assets - liabilities
            c.identifiable_net_assets = (
                c.currency_id.round(net) if c.currency_id else net)

    @api.depends('tax_rate', 'asset_line_ids.fair_value',
                 'asset_line_ids.tax_base', 'asset_line_ids.is_liability')
    def _compute_deferred_tax(self):
        """Deferred tax on the aggregate fair-value step-up (IAS 12.19/24).

        The step-up is the taxable temporary difference between an item's fair
        value (its carrying amount on acquisition) and its tax base. For an
        asset a fair value above tax base is a taxable difference; for a
        liability the sign reverses. Aggregating both and applying the rate
        gives a net deferred tax liability when positive, a deferred tax asset
        when negative. With no rate set the step-up carries no deferred tax, so
        records that leave the rate nil behave exactly as before.
        """
        for c in self:
            step_up = 0.0
            for line in c.asset_line_ids:
                diff = line.fair_value - line.tax_base
                step_up += -diff if line.is_liability else diff
            step_up = c.currency_id.round(step_up) if c.currency_id else step_up
            c.fair_value_step_up = step_up
            raw = step_up * (c.tax_rate / 100.0)
            c.deferred_tax = c.currency_id.round(raw) if c.currency_id else raw

    def _fv_identifiable_net_assets(self):
        """Fair value of identifiable net assets driving the goodwill sum.

        When identifiable-asset lines are entered they drive the total
        (IFRS 3.18); otherwise the manual fv_identifiable_net_assets field is
        used, so records without lines keep their prior behaviour.
        """
        self.ensure_one()
        if self.asset_line_ids:
            return self.identifiable_net_assets
        return self.fv_identifiable_net_assets

    @api.depends('nci_measurement', 'nci_pct', 'fv_identifiable_net_assets',
                 'identifiable_net_assets', 'asset_line_ids', 'deferred_tax')
    def _compute_nci_amount(self):
        """Proportionate NCI is measured, not entered (IFRS 3.19).

        Under the proportionate basis the non-controlling interest is the NCI
        ownership percentage of the *recognised* identifiable net assets. Those
        are the fair value of identifiable net assets net of the IAS 12.19
        deferred tax raised on the fair-value step-up, so NCI is measured on the
        same post-tax base that _goodwill_raw folds into goodwill (IFRS 3.19,
        IAS 12.66); measuring it on the pre-tax base would overstate both NCI
        and goodwill by the NCI share of the deferred tax. With no tax rate the
        deferred tax is nil and the figure is unchanged. Under the fair-value
        basis the entered amount is kept, so records that measure NCI at fair
        value behave exactly as before.
        """
        for c in self:
            if c.nci_measurement == 'proportionate':
                raw = (c._fv_identifiable_net_assets() - c.deferred_tax) * (
                    c.nci_pct / 100.0)
                c.nci_amount = (
                    c.currency_id.round(raw) if c.currency_id else raw)
            else:
                # Preserve the directly entered fair-value amount; leaving the
                # field untouched keeps an unset new record at its default 0.0.
                c.nci_amount = c.nci_amount

    @api.depends('previously_held_interest_fv',
                 'previously_held_interest_carrying')
    def _compute_remeasurement_gain(self):
        """IFRS 3.42 remeasurement of the previously-held interest.

        Fair value less carrying amount, recognised in profit or loss when
        the combination is recognised. Gated on a non-nil carrying amount so
        records that only enter the fair value (the prior data shape) keep
        their exact prior behaviour: no remeasurement posting.
        """
        for c in self:
            if not c.previously_held_interest_carrying:
                c.remeasurement_gain = 0.0
                continue
            raw = (c.previously_held_interest_fv
                   - c.previously_held_interest_carrying)
            c.remeasurement_gain = (
                c.currency_id.round(raw) if c.currency_id else raw)

    @api.depends('contingent_consideration_initial_fv',
                 'contingent_remeasure_ids.state',
                 'contingent_remeasure_ids.delta')
    def _compute_contingent_current_fv(self):
        """Current fair value: initial FV plus every applied remeasurement.

        Each applied remeasurement stores its delta against the fair value
        current at the moment it was applied, so the sum telescopes to the
        latest fair value regardless of the order records were applied in.
        """
        for c in self:
            applied = c.contingent_remeasure_ids.filtered(
                lambda r: r.state == 'applied')
            raw = (c.contingent_consideration_initial_fv
                   + sum(applied.mapped('delta')))
            c.contingent_consideration_current_fv = (
                c.currency_id.round(raw) if c.currency_id else raw)

    @api.depends('acquisition_date')
    def _compute_measurement_period_end(self):
        for c in self:
            c.measurement_period_end = c.acquisition_date and (
                c.acquisition_date + relativedelta(months=12))

    def _goodwill_raw(self, consideration, nci, fina, deferred_tax,
                      contingent_consideration=None):
        """IFRS 3.32 goodwill arithmetic for a given set of components.

        Shared by the stored compute and the measurement-period adjustment
        engine (which previews the restated goodwill before posting) so both
        always agree, including the rounding step.
        """
        self.ensure_one()
        contingent = (
            self.contingent_consideration_initial_fv
            if contingent_consideration is None
            else contingent_consideration
        )
        raw = (consideration + contingent
               + nci + self.previously_held_interest_fv
               - fina + deferred_tax)
        return self.currency_id.round(raw) if self.currency_id else raw

    @api.depends('consideration_transferred', 'nci_amount',
                 'previously_held_interest_fv', 'fv_identifiable_net_assets',
                 'identifiable_net_assets', 'asset_line_ids', 'deferred_tax',
                 'contingent_consideration_initial_fv')
    def _compute_goodwill(self):
        for c in self:
            # A deferred tax liability on the fair-value step-up increases the
            # net liabilities assumed, lowering identifiable net assets and so
            # raising goodwill by the same amount; a deferred tax asset lowers
            # goodwill (IAS 12.66). With no tax rate the deferred tax is nil.
            # Contingent consideration enters at acquisition-date fair value
            # as part of the consideration transferred (IFRS 3.39); with none
            # entered the formula is unchanged.
            raw = c._goodwill_raw(
                c.consideration_transferred, c.nci_amount,
                c._fv_identifiable_net_assets(), c.deferred_tax)
            c.goodwill = max(raw, 0.0)
            c.bargain_purchase_gain = max(-raw, 0.0)

    # Fields locked once the combination is recognised and its entry posted, so
    # a recognised acquisition cannot be retro-edited out from under its move.
    _FROZEN_AFTER_RECOGNISED = (
        'acquiree_name', 'consideration_transferred', 'nci_measurement',
        'nci_pct',
        'nci_amount', 'previously_held_interest_fv',
        'fv_identifiable_net_assets', 'acquisition_date',
        'goodwill_account_id', 'clearing_account_id', 'gain_account_id',
        'nci_account_id', 'journal_id', 'asset_line_ids',
        'tax_rate', 'deferred_tax_account_id',
        'previously_held_interest_carrying', 'remeasure_gain_account_id',
        'contingent_consideration_initial_fv', 'contingent_classification',
        'contingent_account_id',
        'contingent_pnl_account_id', 'company_id',
    )

    _ACCOUNT_REFERENCE_FIELDS = (
        'goodwill_account_id', 'clearing_account_id', 'gain_account_id',
        'nci_account_id', 'deferred_tax_account_id',
        'remeasure_gain_account_id', 'contingent_account_id',
        'contingent_pnl_account_id',
    )

    _SERVER_EVIDENCE_FIELDS = frozenset({
        'move_id', 'reversal_move_id', 'reversed_at', 'reversed_by_id',
    })

    _LEDGER_STATES = frozenset({'recognised', 'reversed'})

    def _eh_lock(self):
        ids = sorted(set(self.ids))
        if ids:
            self.env.cr.execute(
                "SELECT id FROM eh_business_combination "
                "WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                [ids],
            )
            self.invalidate_recordset()
        return self

    def _eh_check_caller_scope(self, operation='write'):
        if self.env.su:
            return
        self._eh_check_access(operation)
        if any(combo.company_id != self.env.company for combo in self):
            raise AccessError(_(
                "Business-combination actions must run in the record's exact "
                "company."))

    def _eh_validate_posting_scope(self, extra_accounts=None):
        """Validate journal/accounts under the caller before any elevation."""
        self.ensure_one()
        self._eh_check_caller_scope('write')
        if self.journal_id:
            if (self.journal_id.company_id != self.company_id
                    or self.journal_id.type != 'general'):
                raise AccessError(_(
                    "The posting journal must be a general journal in the "
                    "combination company."))
            self.journal_id._eh_check_access('read')
        accounts = self.env['account.account']
        for field_name in self._ACCOUNT_REFERENCE_FIELDS:
            accounts |= self[field_name]
        accounts |= extra_accounts or self.env['account.account']
        def _matches_company(account):
            if 'company_ids' in account._fields:
                return self.company_id in account.company_ids
            return account.company_id == self.company_id
        if any(not _matches_company(account) for account in accounts):
            raise AccessError(_(
                "Every posting account must belong to the combination "
                "company."))
        if accounts:
            accounts._eh_check_access('read')

    @api.model_create_multi
    def create(self, vals_list):
        if (any(set(vals) & self._SERVER_EVIDENCE_FIELDS
                for vals in vals_list)
                and not (self.env.su
                         and self.env.context.get('eh_bizcombo_engine'))):
            raise AccessError(_(
                "Recognition and reversal evidence is owned by the "
                "combination engine."))
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.business.combination') or '/'
            # Under the proportionate basis NCI is measured by the compute, not
            # entered; drop any supplied amount so the compute drives it.
            if vals.get('nci_measurement') == 'proportionate':
                vals.pop('nci_amount', None)
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su:
            self._eh_check_access('write')
        self._eh_lock()
        evidence = set(vals) & self._SERVER_EVIDENCE_FIELDS
        if evidence:
            if not (self.env.su
                    and self.env.context.get('eh_bizcombo_engine')):
                raise AccessError(_(
                    "Recognition and reversal evidence is owned by the "
                    "combination engine."))
            for combo in self:
                for field_name in evidence:
                    existing = combo[field_name]
                    new_value = vals.get(field_name) or False
                    existing_value = existing.id if hasattr(
                        existing, 'id') else existing
                    if existing_value and existing_value != new_value:
                        raise AccessError(_(
                            "Combination posting evidence is immutable."))
        # A measurement-period adjustment restates the recognised amounts of
        # a posted combination retrospectively (IFRS 3.45-49). Its apply
        # action posts the balancing entry first and then writes the revised
        # amounts under this context key; every other writer stays frozen.
        if self.env.su and self.env.context.get('eh_mp_restate'):
            return super().write(vals)
        # Switching to the proportionate basis, or changing its drivers, must
        # let the compute re-derive NCI rather than keep a stale manual value.
        if vals.get('nci_measurement') == 'proportionate':
            vals.pop('nci_amount', None)
        frozen = [f for f in self._FROZEN_AFTER_RECOGNISED if f in vals]
        if frozen:
            for c in self:
                # action_recognise / action_recognise_ppa flip state to
                # 'recognised' in their own write; allow that transition but
                # block edits once the record is already recognised.
                if c.state in self._LEDGER_STATES:
                    raise UserError(_(
                        "Combination %s is recognised and posted; %s cannot be "
                        "changed. Cancel and create a new combination instead.",
                        c.display_name, ', '.join(frozen)))
        return super().write(vals)

    def unlink(self):
        for c in self:
            if c.state in self._LEDGER_STATES or c.move_id \
                    or c.reversal_move_id:
                raise UserError(_(
                    "Combination %s carries immutable recognition evidence "
                    "and cannot be deleted.", c.display_name))
        return super().unlink()

    def _remeasurement_legs(self):
        """Step-acquisition remeasurement legs (IFRS 3.42).

        A previously-held interest is remeasured to acquisition-date fair
        value with the gain or loss in profit or loss. The clearing account
        carries the counterpart: it was credited with the interest at fair
        value, so debiting the gain (or crediting the loss) back leaves the
        net clearing movement at the actual carrying amount derecognised
        plus the consideration paid. Empty when no carrying amount was
        entered, preserving the prior behaviour byte for byte.
        """
        self.ensure_one()
        currency = self.currency_id
        gain = currency.round(self.remeasurement_gain)
        if currency.is_zero(gain):
            return []
        account = self.remeasure_gain_account_id or self.gain_account_id
        if not account:
            raise UserError(_(
                "Configure the step remeasurement account (or the bargain "
                "purchase gain account it falls back to) first."))
        if not self.clearing_account_id:
            raise UserError(_(
                "Configure the acquisition clearing account first."))
        if gain > 0:
            return [
                (self.clearing_account_id, gain, 0.0,
                 _("Step acquisition remeasurement %s", self.name)),
                (account, 0.0, gain,
                 _("Step acquisition remeasurement gain %s", self.name)),
            ]
        return [
            (account, -gain, 0.0,
             _("Step acquisition remeasurement loss %s", self.name)),
            (self.clearing_account_id, 0.0, -gain,
             _("Step acquisition remeasurement %s", self.name)),
        ]

    def action_recognise(self):
        self.ensure_one()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can recognise goodwill."))
        # Prove the caller owns this exact-company record before waiting, then
        # validate the posting references only after the row lock has made the
        # configuration stable.
        self._eh_check_caller_scope('write')
        self._eh_lock()
        self._eh_validate_posting_scope()
        # Run the state transition as su so the guarded 'state' write is
        # accepted; env.user (checked above) is preserved for audit stamps.
        self = self._eh_workflow_action().with_context(
            eh_bizcombo_engine=True)
        if self.state != 'draft':
            raise UserError(_("Only a draft combination can be recognised."))
        self._validate_measurement_inputs()
        if (not self.currency_id.is_zero(self.nci_amount)
                or not self.currency_id.is_zero(
                    self.previously_held_interest_fv)
                or not self.currency_id.is_zero(
                    self.previously_held_interest_carrying)):
            raise UserError(_(
                "Use the full purchase price allocation when non-controlling "
                "interest or a previously-held interest exists. The simple "
                "goodwill entry cannot evidence those IFRS 3 components "
                "separately."))
        if not self.journal_id:
            raise UserError(_("Configure the journal first."))
        currency = self.currency_id
        contingent = currency.round(self.contingent_consideration_initial_fv)
        if not currency.is_zero(contingent) and not self.contingent_account_id:
            raise UserError(_(
                "Configure the contingent consideration account first."))
        lines = []
        if not currency.is_zero(self.goodwill):
            if not self.goodwill_account_id or not self.clearing_account_id:
                raise UserError(_(
                    "Configure the goodwill and acquisition clearing accounts "
                    "first."))
            lines.append(
                (self.goodwill_account_id, self.goodwill, 0.0,
                 _("Goodwill %s", self.name)))
        elif not currency.is_zero(self.bargain_purchase_gain):
            if not self.gain_account_id or not self.clearing_account_id:
                raise UserError(_(
                    "Configure the bargain purchase gain and acquisition "
                    "clearing accounts first."))
            lines.append(
                (self.gain_account_id, 0.0, self.bargain_purchase_gain,
                 _("Bargain purchase gain %s", self.name)))
        elif currency.is_zero(contingent) \
                and currency.is_zero(self.remeasurement_gain):
            raise UserError(_(
                "Goodwill and any bargain purchase gain are both nil; nothing "
                "to post."))
        # Contingent consideration is recognised as its own liability or
        # equity credit at acquisition-date fair value (IFRS 3.39-.40); the
        # clearing account carries the remaining counterpart, so records
        # without contingent consideration post the exact prior entry.
        if not currency.is_zero(contingent):
            lines.append(
                (self.contingent_account_id, 0.0, contingent,
                 _("Contingent consideration %s", self.name)))
        clearing_credit = currency.round(
            self.goodwill - self.bargain_purchase_gain - contingent)
        if not currency.is_zero(clearing_credit):
            if not self.clearing_account_id:
                raise UserError(_(
                    "Configure the acquisition clearing account first."))
            if clearing_credit > 0:
                lines.append(
                    (self.clearing_account_id, 0.0, clearing_credit,
                     _("Acquisition clearing %s", self.name)))
            else:
                lines.append(
                    (self.clearing_account_id, -clearing_credit, 0.0,
                     _("Acquisition clearing %s", self.name)))
        lines.extend(self._remeasurement_legs())
        self._post_move(lines)
        self.state = 'recognised'
        return True

    def action_recognise_ppa(self):
        """Post the full purchase price allocation entry (IFRS 3.18-.34).

        Requires identifiable-asset lines. Debits each identifiable asset and
        goodwill, credits each liability, the consideration transferred (via
        the acquisition clearing account), the non-controlling interest, and
        any bargain purchase gain. The entry balances by construction.
        """
        self.ensure_one()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can recognise goodwill."))
        self._eh_check_caller_scope('write')
        self._eh_lock()
        # Identifiable lines share the combination lock. Re-read them after
        # acquiring it so an account/fair-value edit that committed while this
        # worker waited cannot be posted from a stale prefetch.
        self.asset_line_ids.invalidate_recordset()
        self._eh_validate_posting_scope(
            self.asset_line_ids.mapped('account_id'))
        # Run the state transition as su so the guarded 'state' write is
        # accepted; env.user (checked above) is preserved for audit stamps.
        self = self._eh_workflow_action().with_context(
            eh_bizcombo_engine=True)
        if self.state != 'draft':
            raise UserError(_("Only a draft combination can be recognised."))
        self._validate_measurement_inputs()
        if not self.journal_id:
            raise UserError(_("Configure the journal first."))
        if not self.asset_line_ids:
            raise UserError(_(
                "Add identifiable asset and liability lines before posting a "
                "purchase price allocation."))
        currency = self.currency_id
        for line in self.asset_line_ids:
            if not line.account_id:
                raise UserError(_(
                    "Line %s has no account.", line.name or ''))
        if not self.clearing_account_id:
            raise UserError(_("Configure the acquisition clearing account."))
        if not currency.is_zero(self.nci_amount) and not self.nci_account_id:
            raise UserError(_(
                "Configure the non-controlling interest account."))
        if not currency.is_zero(self.deferred_tax) \
                and not self.deferred_tax_account_id:
            raise UserError(_(
                "Configure the deferred tax account for the fair-value "
                "step-up."))

        legs = []
        # Dr identifiable assets, Cr liabilities, each at fair value.
        for line in self.asset_line_ids:
            if line.is_liability:
                legs.append((line.account_id, 0.0, line.fair_value,
                             _("Liability assumed %s", self.name)))
            else:
                legs.append((line.account_id, line.fair_value, 0.0,
                             _("Asset acquired %s", self.name)))
        # Cr deferred tax liability (or Dr deferred tax asset) on the
        # fair-value step-up; goodwill already carries this amount so the
        # entry stays balanced by construction (IAS 12.19/24, .66).
        if not currency.is_zero(self.deferred_tax):
            if self.deferred_tax > 0:
                legs.append((self.deferred_tax_account_id, 0.0,
                             self.deferred_tax,
                             _("Deferred tax liability %s", self.name)))
            else:
                legs.append((self.deferred_tax_account_id,
                             -self.deferred_tax, 0.0,
                             _("Deferred tax asset %s", self.name)))
        # Dr goodwill or Cr bargain purchase gain.
        if not currency.is_zero(self.goodwill):
            if not self.goodwill_account_id:
                raise UserError(_("Configure the goodwill account."))
            legs.append((self.goodwill_account_id, self.goodwill, 0.0,
                         _("Goodwill %s", self.name)))
        elif not currency.is_zero(self.bargain_purchase_gain):
            if not self.gain_account_id:
                raise UserError(_(
                    "Configure the bargain purchase gain account."))
            legs.append((self.gain_account_id, 0.0,
                         self.bargain_purchase_gain,
                         _("Bargain purchase gain %s", self.name)))
        # Keep consideration and the previously-held interest as distinct
        # IFRS 3 components even where both use the controlled acquisition
        # clearing account. Separate move lines preserve auditability.
        consideration_credit = currency.round(
            self.consideration_transferred)
        if not currency.is_zero(consideration_credit):
            legs.append((self.clearing_account_id, 0.0,
                         consideration_credit,
                         _("Consideration transferred %s", self.name)))
        prior_interest = currency.round(self.previously_held_interest_fv)
        if not currency.is_zero(prior_interest):
            legs.append((self.clearing_account_id, 0.0, prior_interest,
                         _("Previously-held interest at fair value %s",
                           self.name)))
        # Cr contingent consideration at acquisition-date fair value: part of
        # the consideration in goodwill (IFRS 3.39), carried as its own
        # liability or equity line per its classification (IFRS 3.40) so a
        # later liability remeasurement has a balance to move against.
        contingent = currency.round(self.contingent_consideration_initial_fv)
        if not currency.is_zero(contingent):
            if not self.contingent_account_id:
                raise UserError(_(
                    "Configure the contingent consideration account first."))
            legs.append((self.contingent_account_id, 0.0, contingent,
                         _("Contingent consideration %s", self.name)))
        # Cr non-controlling interest.
        if not currency.is_zero(self.nci_amount):
            legs.append((self.nci_account_id, 0.0, self.nci_amount,
                         _("Non-controlling interest %s", self.name)))
        # Step-acquisition remeasurement of the previously-held interest to
        # fair value, gain or loss to profit or loss (IFRS 3.42).
        legs.extend(self._remeasurement_legs())
        self._post_move(legs)
        self.state = 'recognised'
        return True

    def action_close_measurement_period(self):
        """Close the measurement period (IFRS 3.45).

        After closing, information about facts and circumstances at the
        acquisition date is accounted for prospectively (liability-classified
        contingent consideration keeps remeasuring through profit or loss,
        IFRS 3.58); no further measurement-period adjustment can be applied.
        """
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can close the measurement "
                "period."))
        self._eh_check_caller_scope('write')
        self._eh_lock()
        # Run the guarded write to measurement_period_closed as su.
        self = self._eh_workflow_action().with_context(
            eh_bizcombo_engine=True)
        for c in self:
            if c.state != 'recognised':
                raise UserError(_(
                    "Only a recognised combination has a measurement period "
                    "to close."))
            if c.measurement_period_closed:
                raise UserError(_(
                    "The measurement period of %s is already closed.",
                    c.display_name))
            c.measurement_period_closed = True
        return True

    def action_reverse(self, accounting_date=None):
        """Post an immutable counter-entry for a recognised combination.

        IFRS 3 downstream entries are separate pieces of accounting evidence.
        They must be unwound through their own source records first; otherwise
        reversing the acquisition entry would leave later goodwill and
        contingent-consideration movements standing without their basis.
        """
        self.ensure_one()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can reverse a business "
                "combination."))
        self._eh_check_caller_scope('write')
        self._eh_lock()
        self.adjustment_ids.invalidate_recordset(['state'])
        self.contingent_remeasure_ids.invalidate_recordset(['state'])
        if self.state != 'recognised' or not self.move_id:
            raise UserError(_(
                "Only a recognised combination with its original entry can "
                "be reversed."))
        active_adjustments = self.adjustment_ids.filtered(
            lambda adjustment: adjustment.state == 'applied')
        active_remeasurements = self.contingent_remeasure_ids.filtered(
            lambda remeasurement: remeasurement.state == 'applied')
        if active_adjustments or active_remeasurements:
            raise UserError(_(
                "Reverse all applied measurement-period adjustments and "
                "contingent-consideration remeasurements before reversing "
                "the acquisition entry."))
        move = self.move_id
        if (
            move.eh_business_combination_id != self
            or move.company_id != self.company_id
            or move.state != 'posted'
            or move.move_type != 'entry'
            or not move.eh_sealed
            or move.eh_legacy_unverified_seal
        ):
            raise UserError(_(
                "The recognition move failed its verified sealed provenance "
                "check and will not be reversed."))
        requested_date = (
            fields.Date.to_date(accounting_date)
            if accounting_date else fields.Date.context_today(self)
        )
        if accounting_date and requested_date < move.date:
            raise UserError(_(
                "The reversal date cannot precede the original recognition "
                "date %s.", move.date))
        reversal_date = max(requested_date, move.date)
        reversal = move.sudo(False)._eh_reverse_with_verified_capability([{
            'date': reversal_date,
            'journal_id': move.journal_id.id,
            'ref': _("Business combination reversal %s", self.name),
        }], cancel=False)
        reversal.sudo().with_context(
            eh_bizcombo_engine=True,
        )._eh_write_sealed_metadata(
            {'eh_business_combination_reversal_id': self.id},
            {'eh_business_combination_reversal_id'},
        )
        reversal._eh_post_verified_reversal()
        self._eh_seal_reversal(reversal)
        self._eh_workflow_action().with_context(
            eh_bizcombo_engine=True,
        ).write({
            'state': 'reversed',
            'reversal_move_id': reversal.id,
            'reversed_at': fields.Datetime.now(),
            'reversed_by_id': self.env.user.id,
        })
        return True

    def action_cancel(self):
        self._eh_check_caller_scope('write')
        self._eh_lock()
        self = self._eh_workflow_action().with_context(
            eh_bizcombo_engine=True)
        for c in self:
            if c.state in c._LEDGER_STATES:
                raise UserError(_(
                    "A recognised or reversed combination is permanent audit "
                    "evidence and cannot be cancelled."))
            if c.state != 'draft':
                raise UserError(_(
                    "Only a draft combination can be cancelled."))
            c.state = 'cancelled'

    def action_view_reversal(self):
        self.ensure_one()
        if not self.reversal_move_id:
            raise UserError(_("No reversal entry has been posted."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move', 'res_id': self.reversal_move_id.id,
            'view_mode': 'form', 'views': [(False, 'form')],
        }

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No entry has been posted yet."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move', 'res_id': self.move_id.id,
            'view_mode': 'form', 'views': [(False, 'form')],
        }

    def _post_move(self, legs):
        lines = [(0, 0, {
            'name': label, 'account_id': account.id,
            'debit': debit, 'credit': credit,
        }) for account, debit, credit, label in legs]
        move = self.env['account.move']._eh_create_sealed({
            'move_type': 'entry',
            'date': self.acquisition_date,
            'journal_id': self.journal_id.id,
            'ref': self.name,
            'eh_sealed': True,
            'line_ids': lines,
        })
        move.sudo().with_context(
            eh_bizcombo_engine=True,
        )._eh_write_sealed_metadata(
            {'eh_business_combination_id': self.id},
            {'eh_business_combination_id'},
        )
        move.action_post()
        self.sudo().with_context(eh_bizcombo_engine=True).write({
            'move_id': move.id,
        })
        return move


class EhBusinessCombinationAsset(models.Model):
    _name = 'eh.business.combination.asset'
    _description = "Identifiable asset or liability (IFRS 3.18)"
    _order = 'is_liability, id'

    combination_id = fields.Many2one(
        'eh.business.combination', required=True, ondelete='cascade',
        index=True)
    company_id = fields.Many2one(
        related='combination_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='combination_id.currency_id', store=True, readonly=True)
    name = fields.Char(required=True)
    account_id = fields.Many2one(
        'account.account', string="Account", required=True,
        check_company=True)
    fair_value = fields.Monetary(
        currency_field='currency_id',
        help="Fair value of the identifiable asset acquired or liability "
             "assumed at the acquisition date (IFRS 3.18).")
    tax_base = fields.Monetary(
        currency_field='currency_id',
        help="Tax base of the item (IAS 12.7-8): the amount deductible or "
             "taxable for tax purposes. The fair-value step-up is fair value "
             "less tax base; leave at zero to treat the whole fair value as a "
             "temporary difference.")
    is_liability = fields.Boolean(
        string="Liability",
        help="Tick for a liability assumed; leave blank for an asset "
             "acquired.")

    @api.constrains('fair_value')
    def _check_nonnegative_fair_value(self):
        for line in self:
            if line.fair_value < 0.0:
                raise ValidationError(_(
                    "Fair value cannot be negative. Use the Liability flag "
                    "to control whether the amount posts as a credit."))

    def _check_not_recognised(self, action):
        # A measurement-period adjustment restates line fair values on a
        # recognised combination after posting the balancing entry
        # (IFRS 3.45-49); it is the only writer allowed through the freeze.
        if self.env.su and self.env.context.get('eh_mp_restate'):
            return
        for line in self:
            if line.combination_id.state == 'recognised':
                raise UserError(_(
                    "Combination %s is recognised and posted; its "
                    "identifiable asset and liability lines cannot be %s. "
                    "Cancel and create a new combination instead.",
                    line.combination_id.display_name, action))

    @api.model_create_multi
    def create(self, vals_list):
        # New lines feeding a recognised (frozen) combination would shift its
        # stored goodwill without a move; block them like edits and deletes.
        combinations = self.env['eh.business.combination'].browse([
            vals.get('combination_id') for vals in vals_list
            if vals.get('combination_id')])
        combinations._eh_check_caller_scope('read')
        combinations._eh_lock()
        for vals in vals_list:
            combination = self.env['eh.business.combination'].browse(
                vals.get('combination_id'))
            if combination and combination.state == 'recognised':
                raise UserError(_(
                    "Combination %s is recognised and posted; identifiable "
                    "asset and liability lines cannot be added. Cancel and "
                    "create a new combination instead.",
                    combination.display_name))
        return super().create(vals_list)

    def write(self, vals):
        combinations = self.mapped('combination_id')
        if vals.get('combination_id'):
            combinations |= self.env['eh.business.combination'].browse(
                vals['combination_id'])
        combinations._eh_check_caller_scope('read')
        combinations._eh_lock()
        self._check_not_recognised(_("changed"))
        if any(combo.state == 'recognised' for combo in combinations) and not (
                self.env.su and self.env.context.get('eh_mp_restate')):
            raise UserError(_(
                "Identifiable lines cannot be reparented to a recognised "
                "combination."))
        return super().write(vals)

    def unlink(self):
        self.mapped('combination_id')._eh_lock()
        self._check_not_recognised(_("deleted"))
        return super().unlink()
