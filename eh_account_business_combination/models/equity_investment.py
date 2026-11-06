# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.equity.investment: an investment in an associate or joint venture carried
under the equity method (IAS 28).

The carrying amount starts at cost and rolls forward for the investor's share
of the investee's profit (or loss), less dividends received and any
impairment (IAS 28.10-11). Each pick-up, dividend and impairment posts a
balanced entry.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


_MIN_DISPOSAL_PERCENTAGE = 0.0
_MAX_DISPOSAL_PERCENTAGE = 100.0


class EhEquityInvestment(models.Model):
    _name = 'eh.equity.investment'
    _description = "Equity-method investment (IAS 28)"
    _inherit = [
        'mail.thread', 'mail.activity.mixin', 'eh.workflow.guard',
        'eh.gl.reversal',
    ]
    _order = 'id desc'
    _rec_name = 'name'

    # State advances only through this record's own actions (run under sudo),
    # never a direct RPC write that would skip an action and its posted entry.
    _eh_guarded_fields = ('state', 'reversed_at', 'reversed_by_id')
    _eh_create_guarded_fields = _eh_guarded_fields

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    investee_name = fields.Char(required=True, tracking=True)
    kind = fields.Selection(
        [('associate', "Associate"), ('joint_venture', "Joint venture")],
        default='associate', required=True)
    state = fields.Selection(
        [('draft', "Draft"), ('active', "Active"),
         ('disposed', "Disposed"), ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    ownership_pct = fields.Float(
        digits=(7, 4), required=True, default=25.0,
        help="Investor's ownership / share of the investee (percentage).")
    cost_of_investment = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Initial cost recognised on acquisition.")
    initial_recognition_method = fields.Selection(
        [
            ('post_cost', "Post acquisition at cost"),
            ('already_recognised', "Already recognised on ledger"),
        ],
        required=True, default='post_cost', tracking=True,
        help="Post acquisition at cost creates the initial debit to the "
             "investment and credit to cash/receivable. Already recognised "
             "adopts a proved posted journal entry and never fabricates a "
             "second cash movement.")
    opening_move_id = fields.Many2one(
        'account.move', string="Existing Recognition Entry", copy=False,
        check_company=True, ondelete='restrict',
        domain="[('company_id', '=', company_id), ('state', '=', 'posted')]",
        help="Posted source entry whose net debit to the configured investment "
             "account exactly equals opening cost. Required for the Already "
             "recognised on ledger method and retained as adoption evidence.")
    activation_date = fields.Date(
        readonly=True, copy=False, tracking=True,
        help="Accounting date on which the equity-method subledger was "
             "activated or adopted.")
    carrying_amount = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id', tracking=True,
        help="Equity-method carrying amount, rolled forward from cost.")
    cumulative_share_of_profit = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id')
    cumulative_dividends = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id')
    cumulative_impairment = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id')
    unrecognised_loss = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id', tracking=True,
        help="Investor share of associate losses not recognised after the "
             "equity-method investment reached nil. Future shares of profit "
             "must absorb this balance before recognition (IAS 28.39).",
    )
    current_ownership_pct = fields.Float(
        string="Current Ownership %", digits=(7, 4), readonly=True,
        copy=False, tracking=True,
        help="Ownership percentage remaining after controlled partial "
             "disposals. It drives later equity-method pickups.")
    cumulative_disposed_carrying = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Memorandum roll-forward of carrying amount derecognised through "
             "all disposal actions.")
    cumulative_disposal_proceeds = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Memorandum roll-forward of proceeds recognised through all "
             "disposal actions.")
    cumulative_retained_interest_fv = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        string="Cumulative Retained-interest FV",
        help="Fair value recognised for interests retained when significant "
             "influence or joint control ceased.")
    cumulative_oci_recycled = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Signed cumulative OCI/CTA amount reclassified on disposal; a "
             "positive amount is a reserve credit recycled to profit or loss.")
    cumulative_unrecognised_loss_closed = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Memorandum amount of previously unrecognised IAS 28 losses "
             "closed out for disposed interests.")
    last_disposal_date = fields.Date(readonly=True, copy=False, tracking=True)

    # ---- period inputs ----
    investee_profit = fields.Monetary(
        currency_field='currency_id',
        help="Investee's profit or loss for the period; the share picked up "
             "is this at the ownership percentage.")
    dividend_received = fields.Monetary(currency_field='currency_id')
    impairment_amount = fields.Monetary(currency_field='currency_id')
    disposal_proceeds = fields.Monetary(
        currency_field='currency_id',
        help="Fair value of consideration received on disposing of the "
             "investment (IAS 28.22).")
    disposal_treatment = fields.Selection(
        [
            ('continue_equity_method',
             "Partial disposal - continue equity method"),
            ('cease_equity_method',
             "Cease equity method / full disposal"),
        ],
        required=True, default='cease_equity_method',
        help="Continue applies proportional derecognition while significant "
             "influence or joint control remains. Cease derecognises the full "
             "equity-method balance and measures any retained interest at "
             "fair value (IAS 28.22).")
    disposal_percentage = fields.Float(
        string="Holding Disposed %", digits=(7, 4), default=100.0,
        help="Percentage of the current holding disposed by this action, not "
             "percentage points of the investee.")
    retained_interest_fair_value = fields.Monetary(
        currency_field='currency_id',
        help="Fair value of any interest retained when the equity method "
             "ceases (IAS 28.22). Leave nil on a full disposal or while the "
             "equity method continues.")
    oci_recycling_amount = fields.Monetary(
        string="OCI / CTA Recycling", currency_field='currency_id',
        help="Signed reserve amount reclassified on disposal. Positive debits "
             "the OCI/CTA reserve and credits disposal profit or loss; "
             "negative reverses those sides.")
    accounting_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
        help="Effective accounting date used by the next equity-method "
             "posting. Set this to the reporting-period end for a share-of-"
             "profit or loss pickup; entries are rejected if Odoo cannot "
             "post on this exact date.")

    # ---- accounts ----
    investment_account_id = fields.Many2one(
        'account.account', string="Investment Account", tracking=True,
        check_company=True,
        domain="[('account_type', 'in', "
               "['asset_non_current', 'asset_fixed'])]")
    share_of_profit_account_id = fields.Many2one(
        'account.account', string="Share of Profit Account", tracking=True,
        check_company=True,
        domain="[('account_type', 'in', ['income', 'income_other', "
               "'expense', 'expense_other', 'expense_depreciation', "
               "'expense_direct_cost'])]")
    cash_account_id = fields.Many2one(
        'account.account', string="Cash / Receivable Account", tracking=True,
        check_company=True,
        domain="[('account_type', 'in', "
               "['asset_cash', 'asset_receivable', 'asset_current'])]")
    impairment_account_id = fields.Many2one(
        'account.account', string="Impairment Account", tracking=True,
        check_company=True,
        domain="[('account_type', 'in', ['expense', 'expense_other', "
               "'expense_depreciation', 'expense_direct_cost'])]")
    disposal_gain_loss_account_id = fields.Many2one(
        'account.account', string="Disposal Gain / Loss Account",
        check_company=True,
        tracking=True,
        domain="[('account_type', 'in', ['income', 'income_other', "
               "'expense', 'expense_other', 'expense_depreciation', "
               "'expense_direct_cost'])]",
        help="Profit or loss account for the gain or loss on disposal "
             "(IAS 28.22).")
    retained_interest_account_id = fields.Many2one(
        'account.account', string="Retained Interest Account",
        check_company=True, tracking=True,
        domain="[('account_type', 'in', "
               "['asset_current', 'asset_non_current', 'asset_fixed'])]",
        help="Financial-asset account debited for retained-interest fair value "
             "when the equity method ceases.")
    oci_reserve_account_id = fields.Many2one(
        'account.account', string="OCI / CTA Reserve Account",
        check_company=True, tracking=True,
        domain="[('account_type', 'in', ['equity', 'equity_unaffected'])]",
        help="Equity reserve reclassified to profit or loss on disposal.")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        check_company=True,
        domain="[('type', '=', 'general'), ('company_id', '=', company_id)]")

    move_ids = fields.One2many('account.move', 'eh_equity_investment_id')
    reversal_move_ids = fields.One2many(
        'account.move', 'eh_equity_investment_reversal_id',
        string="Reversal Entries")
    move_count = fields.Integer(compute='_compute_move_count')
    reversed_at = fields.Datetime(readonly=True, copy=False, tracking=True)
    reversed_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False, string="Reversed By")

    notes = fields.Text()

    @api.constrains('cost_of_investment', 'disposal_percentage',
                    'retained_interest_fair_value')
    def _check_disposal_inputs(self):
        for investment in self:
            if investment.cost_of_investment < 0.0:
                raise ValidationError(_(
                    "Investment cost cannot be negative."))
            if not (_MIN_DISPOSAL_PERCENTAGE
                    < investment.disposal_percentage
                    <= _MAX_DISPOSAL_PERCENTAGE):
                raise ValidationError(_(
                    "Holding disposed percentage must be above 0 and no "
                    "more than 100."))
            if investment.retained_interest_fair_value < 0.0:
                raise ValidationError(_(
                    "Retained-interest fair value cannot be negative."))

    _check_ownership = models.Constraint(
        'CHECK (ownership_pct >= 0 AND ownership_pct <= 100)',  # noqa: gcclog-hardcode IAS 28 ownership percentage uses fixed 0-100 scale
        'Ownership percentage must be between 0 and 100.')

    _ENGINE_ANCHORS = (
        'carrying_amount', 'cumulative_share_of_profit',
        'cumulative_dividends', 'cumulative_impairment',
        'unrecognised_loss', 'current_ownership_pct', 'activation_date',
        'cumulative_disposed_carrying', 'cumulative_disposal_proceeds',
        'cumulative_retained_interest_fv', 'cumulative_oci_recycled',
        'cumulative_unrecognised_loss_closed', 'last_disposal_date',
        'reversed_at', 'reversed_by_id',
    )
    _FROZEN_CONFIG = (
        'company_id', 'investee_name', 'kind', 'ownership_pct',
        'cost_of_investment', 'initial_recognition_method',
        'opening_move_id', 'investment_account_id',
        'share_of_profit_account_id', 'cash_account_id',
        'impairment_account_id', 'disposal_gain_loss_account_id',
        'retained_interest_account_id', 'oci_reserve_account_id', 'journal_id',
    )

    def _eh_lock(self):
        ids = sorted(set(self.ids))
        if ids:
            self.env.cr.execute(
                "SELECT id FROM eh_equity_investment WHERE id = ANY(%s) "
                "ORDER BY id FOR UPDATE",
                [ids],
            )
            self.invalidate_recordset()
        return self

    def _eh_check_caller_scope(self, operation='write'):
        if self.env.su:
            return
        self._eh_check_access(operation)
        if any(inv.company_id != self.env.company for inv in self):
            raise AccessError(_(
                "Equity-method actions must run in the investment's exact "
                "company."))

    def _compute_move_count(self):
        for inv in self:
            inv.move_count = len(
                inv.move_ids | inv.reversal_move_ids | inv.opening_move_id)

    def _equity_method_percentage(self):
        """Return current ownership, with an upgrade-safe legacy fallback."""
        self.ensure_one()
        if self.current_ownership_pct > 0.0:
            return self.current_ownership_pct
        if self.state == 'active':
            return self.ownership_pct
        return 0.0

    def _validate_opening_move(self):
        self.ensure_one()
        move = self.opening_move_id
        if not move:
            raise UserError(_(
                "Select the posted entry that already recognised this "
                "investment."))
        move.invalidate_recordset(['state', 'company_id', 'line_ids'])
        if move.state != 'posted' or move.company_id != self.company_id:
            raise UserError(_(
                "The existing recognition entry must be posted in the "
                "investment company."))
        duplicate = self.search([
            ('id', '!=', self.id),
            ('opening_move_id', '=', move.id),
            ('state', '!=', 'cancelled'),
        ], limit=1)
        if duplicate:
            raise UserError(_(
                "Existing recognition entry %s is already linked to %s.",
                move.display_name, duplicate.display_name))
        reversals = self.env['account.move'].sudo().search([
            ('reversed_entry_id', '=', move.id),
            ('state', '=', 'posted'),
        ], limit=1)
        if reversals:
            raise UserError(_(
                "Existing recognition entry %s has already been reversed.",
                move.display_name))
        investment_lines = move.line_ids.filtered(
            lambda line: line.account_id == self.investment_account_id)
        recognised = self.currency_id.round(sum(
            investment_lines.mapped('balance')))
        expected = self.currency_id.round(self.cost_of_investment)
        if self.currency_id.compare_amounts(recognised, expected):
            raise UserError(_(
                "Existing recognition entry %(move)s carries %(recognised)s "
                "on the investment account; expected exact opening cost "
                "%(expected)s.",
                move=move.display_name,
                recognised=recognised,
                expected=expected,
            ))

    @api.model_create_multi
    def create(self, vals_list):
        if (set().union(*(set(vals) for vals in vals_list or [{}]))
                & set(self._ENGINE_ANCHORS)
                and not (self.env.su
                         and self.env.context.get('eh_equity_engine'))):
            raise AccessError(_(
                "Equity-method carrying and cumulative balances are owned by "
                "the posting engine."))
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.equity.investment') or '/'
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su:
            self._eh_check_access('write')
        self._eh_lock()
        if (set(vals) & set(self._ENGINE_ANCHORS)
                and not (self.env.su
                         and self.env.context.get('eh_equity_engine'))):
            raise AccessError(_(
                "Equity-method carrying and cumulative balances are owned by "
                "the posting engine."))
        frozen = set(vals) & set(self._FROZEN_CONFIG)
        if frozen and any(inv.state != 'draft' for inv in self):
            raise UserError(_(
                "Investment configuration is frozen after activation: %s.",
                ', '.join(sorted(frozen))))
        return super().write(vals)

    # ---- actions ----

    def action_activate(self):
        self._check_manager()
        self._eh_check_caller_scope('write')
        self._eh_lock()
        for inv in self:
            if not inv.currency_id.is_zero(inv.cost_of_investment):
                needed = ['investment']
                if inv.initial_recognition_method == 'post_cost':
                    needed.append('cash')
                inv._validate_accounts(needed)
        self = self._eh_workflow_action().with_context(
            eh_equity_engine=True)
        for inv in self:
            if inv.state != 'draft':
                raise UserError(_("Only draft investments can be activated."))
            currency = inv.currency_id
            cost = currency.round(inv.cost_of_investment)
            # Recognise the investment at cost on the ledger (IAS 28.10).
            # Nothing else feeds the investment account (unlike a fixed asset
            # created from a vendor bill), so if the acquisition is not posted
            # here the investment never reaches the balance sheet and a later
            # disposal derecognises a carrying amount that was never
            # recognised, leaving a phantom credit on the investment account.
            if (inv.initial_recognition_method == 'already_recognised'
                    and currency.is_zero(cost)):
                raise UserError(_(
                    "Already-recognised adoption requires a positive opening "
                    "cost and its posted source entry."))
            if inv.initial_recognition_method == 'already_recognised':
                inv._validate_opening_move()
            elif not currency.is_zero(cost):
                inv._validate_accounts(['investment', 'cash'])
                inv._post_move([
                    (inv.investment_account_id, cost, 0.0,
                     _("Investment in associate at cost %s", inv.name)),
                    (inv.cash_account_id, 0.0, cost,
                     _("Consideration paid %s", inv.name)),
                ])
            inv.write({
                'state': 'active',
                'carrying_amount': cost,
                'current_ownership_pct': inv.ownership_pct,
                'activation_date': inv.accounting_date,
            })
        return True

    def action_pickup_profit(self):
        self.ensure_one()
        self._check_manager()
        self._eh_check_caller_scope('write')
        self._eh_lock()
        self._require_active()
        self._validate_accounts(['investment', 'share_of_profit'])
        self = self._eh_workflow_action().with_context(
            eh_equity_engine=True)
        currency = self.currency_id
        gross_share = currency.round(
            self.investee_profit * self._equity_method_percentage() / 100.0
        )
        if currency.is_zero(gross_share):
            raise UserError(_(
                "The share of profit is nil; nothing to pick up."))
        share = gross_share
        if gross_share > 0:
            absorbed = currency.round(min(
                gross_share,
                max(self.unrecognised_loss, 0.0),
            ))
            if not currency.is_zero(absorbed):
                self.unrecognised_loss = currency.round(
                    self.unrecognised_loss - absorbed
                )
                share = currency.round(gross_share - absorbed)
            if currency.is_zero(share):
                # IAS 28.39: profit first absorbs losses that were not
                # recognised after the investment reached nil. No ledger
                # pickup occurs until that balance is exhausted.
                self.investee_profit = 0.0
                return True
            legs = [
                (self.investment_account_id, share, 0.0,
                 _("Share of profit %s", self.name)),
                (self.share_of_profit_account_id, 0.0, share,
                 _("Share of profit %s", self.name)),
            ]
        else:
            # IAS 28.38: discontinue recognising the share of loss once the
            # investment reaches nil. Recognise only the loss that brings the
            # carrying amount to zero; floor it there.
            full_loss = -gross_share
            recognised_loss = min(full_loss, max(self.carrying_amount, 0.0))
            recognised_loss = currency.round(recognised_loss)
            unrecognised = currency.round(full_loss - recognised_loss)
            if not currency.is_zero(unrecognised):
                self.unrecognised_loss = currency.round(
                    self.unrecognised_loss + unrecognised
                )
            if currency.is_zero(recognised_loss):
                self.investee_profit = 0.0
                return True
            # The amount actually recognised may be less than the full share of
            # loss; keep the roll-forward and the entry consistent.
            share = -recognised_loss
            legs = [
                (self.share_of_profit_account_id, recognised_loss, 0.0,
                 _("Share of loss %s", self.name)),
                (self.investment_account_id, 0.0, recognised_loss,
                 _("Share of loss %s", self.name)),
            ]
        self._post_move(legs)
        self.carrying_amount = currency.round(self.carrying_amount + share)
        self.cumulative_share_of_profit = currency.round(
            self.cumulative_share_of_profit + share
        )
        self.investee_profit = 0.0
        return True

    def action_record_dividend(self):
        self.ensure_one()
        self._check_manager()
        self._eh_check_caller_scope('write')
        self._eh_lock()
        self._require_active()
        self._validate_accounts(['investment', 'cash'])
        self = self._eh_workflow_action().with_context(
            eh_equity_engine=True)
        currency = self.currency_id
        amount = currency.round(self.dividend_received)
        if currency.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_("Enter a positive dividend received."))
        if currency.compare_amounts(amount, self.carrying_amount) > 0:
            raise UserError(_(
                "The dividend cannot exceed the investment's carrying amount. "
                "Account for any excess distribution separately after review."
            ))
        # Dividends reduce the carrying amount, they are not income.
        self._post_move([
            (self.cash_account_id, amount, 0.0,
             _("Dividend received %s", self.name)),
            (self.investment_account_id, 0.0, amount,
             _("Dividend reduces investment %s", self.name)),
        ])
        self.carrying_amount -= amount
        self.cumulative_dividends += amount
        self.dividend_received = 0.0
        return True

    def action_impair(self):
        self.ensure_one()
        self._check_manager()
        self._eh_check_caller_scope('write')
        self._eh_lock()
        self._require_active()
        self._validate_accounts(['investment', 'impairment'])
        self = self._eh_workflow_action().with_context(
            eh_equity_engine=True)
        currency = self.currency_id
        amount = currency.round(self.impairment_amount)
        if currency.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_("Enter a positive impairment amount."))
        if currency.compare_amounts(amount, self.carrying_amount) > 0:
            raise UserError(_(
                "The impairment cannot exceed the investment's carrying "
                "amount."
            ))
        self._post_move([
            (self.impairment_account_id, amount, 0.0,
             _("Impairment of associate %s", self.name)),
            (self.investment_account_id, 0.0, amount,
             _("Impairment reduces investment %s", self.name)),
        ])
        self.carrying_amount -= amount
        self.cumulative_impairment += amount
        self.impairment_amount = 0.0
        return True

    def action_dispose(self):
        """Post partial disposal or cessation of equity-method accounting.

        A continuing-interest disposal derecognises a proportional carrying
        amount and retains the residual IAS 28 balance. When significant
        influence or joint control ceases, the full equity-method balance is
        derecognised and any retained interest is recognised at fair value
        (IAS 28.22). Signed OCI/CTA recycling posts as a separate balanced
        pair, while cumulative memorandum fields preserve every roll-forward.
        """
        self.ensure_one()
        self._check_manager()
        self._eh_check_caller_scope('write')
        self._eh_lock()
        self._require_active()
        needed = ['investment', 'cash', 'disposal']
        if not self.currency_id.is_zero(self.oci_recycling_amount):
            needed.append('oci')
        if not self.currency_id.is_zero(self.retained_interest_fair_value):
            needed.append('retained')
        self._validate_accounts(needed)
        # Run the state transition as su so the guarded 'state' write is
        # accepted; env.user (checked above) is preserved for audit stamps.
        self = self._eh_workflow_action().with_context(
            eh_equity_engine=True)
        currency = self.currency_id
        proceeds = currency.round(self.disposal_proceeds)
        if currency.compare_amounts(proceeds, 0.0) < 0:
            raise UserError(_("Disposal proceeds cannot be negative."))
        percentage = self.disposal_percentage
        if not (_MIN_DISPOSAL_PERCENTAGE
                < percentage <= _MAX_DISPOSAL_PERCENTAGE):
            raise UserError(_(
                "Holding disposed percentage must be above 0 and no more "
                "than 100."))
        treatment = self.disposal_treatment
        current_ownership = self._equity_method_percentage()
        if current_ownership <= 0.0:
            raise UserError(_(
                "No current equity-method ownership remains to dispose."))
        retained_fv = currency.round(self.retained_interest_fair_value)
        oci_recycling = currency.round(self.oci_recycling_amount)
        carrying = currency.round(self.carrying_amount)
        if treatment == 'continue_equity_method':
            if percentage >= _MAX_DISPOSAL_PERCENTAGE:
                raise UserError(_(
                    "A continuing equity-method disposal must leave part of "
                    "the current holding."))
            if not currency.is_zero(retained_fv):
                raise UserError(_(
                    "Retained-interest fair value applies only when the "
                    "equity method ceases."))
            carrying_disposed = currency.round(
                carrying * percentage / 100.0)
        else:
            carrying_disposed = carrying
            if percentage < _MAX_DISPOSAL_PERCENTAGE and currency.compare_amounts(
                    retained_fv, 0.0) <= 0:
                raise UserError(_(
                    "Enter a positive retained-interest fair value when the "
                    "equity method ceases but part of the holding remains."))
            if (percentage >= _MAX_DISPOSAL_PERCENTAGE
                    and not currency.is_zero(retained_fv)):
                raise UserError(_(
                    "A full disposal cannot recognise a retained interest."))
        gain_loss = currency.round(
            proceeds + retained_fv - carrying_disposed)

        legs = []
        # Dr cash / receivable for the proceeds received.
        if not currency.is_zero(proceeds):
            legs.append((self.cash_account_id, proceeds, 0.0,
                         _("Disposal proceeds %s", self.name)))
        if not currency.is_zero(retained_fv):
            legs.append((self.retained_interest_account_id, retained_fv, 0.0,
                         _("Retained interest at fair value %s", self.name)))
        # Cr investment for the proportion/full carrying amount derecognised.
        if currency.compare_amounts(carrying_disposed, 0.0) > 0:
            legs.append((self.investment_account_id, 0.0, carrying_disposed,
                         _("Derecognise investment %s", self.name)))
        elif currency.compare_amounts(carrying_disposed, 0.0) < 0:
            legs.append((self.investment_account_id, -carrying_disposed, 0.0,
                         _("Derecognise investment %s", self.name)))
        # Balancing gain (Cr income) or loss (Dr expense) to profit or loss.
        if currency.compare_amounts(gain_loss, 0.0) > 0:
            legs.append((self.disposal_gain_loss_account_id, 0.0, gain_loss,
                         _("Gain on disposal %s", self.name)))
        elif currency.compare_amounts(gain_loss, 0.0) < 0:
            legs.append((self.disposal_gain_loss_account_id, -gain_loss, 0.0,
                         _("Loss on disposal %s", self.name)))
        # Reclassify cumulative OCI/CTA separately from the disposal gain or
        # loss calculation. Positive means a credit reserve recycled to P&L.
        if currency.compare_amounts(oci_recycling, 0.0) > 0:
            legs.extend([
                (self.oci_reserve_account_id, oci_recycling, 0.0,
                 _("OCI / CTA recycled %s", self.name)),
                (self.disposal_gain_loss_account_id, 0.0, oci_recycling,
                 _("OCI / CTA reclassification %s", self.name)),
            ])
        elif currency.compare_amounts(oci_recycling, 0.0) < 0:
            legs.extend([
                (self.disposal_gain_loss_account_id, -oci_recycling, 0.0,
                 _("OCI / CTA reclassification %s", self.name)),
                (self.oci_reserve_account_id, 0.0, -oci_recycling,
                 _("OCI / CTA recycled %s", self.name)),
            ])
        if legs:
            self._post_move(legs)
        remaining_ownership = round(
            current_ownership * (1.0 - percentage / 100.0), 4)
        unrecognised_closed = currency.round(
            self.unrecognised_loss * (
                percentage / 100.0
                if treatment == 'continue_equity_method' else 1.0))
        continuing = treatment == 'continue_equity_method'
        self.write({
            'carrying_amount': currency.round(
                carrying - carrying_disposed) if continuing else 0.0,
            'current_ownership_pct': remaining_ownership,
            'unrecognised_loss': currency.round(
                self.unrecognised_loss - unrecognised_closed
            ) if continuing else 0.0,
            'cumulative_disposed_carrying': currency.round(
                self.cumulative_disposed_carrying + carrying_disposed),
            'cumulative_disposal_proceeds': currency.round(
                self.cumulative_disposal_proceeds + proceeds),
            'cumulative_retained_interest_fv': currency.round(
                self.cumulative_retained_interest_fv + retained_fv),
            'cumulative_oci_recycled': currency.round(
                self.cumulative_oci_recycled + oci_recycling),
            'cumulative_unrecognised_loss_closed': currency.round(
                self.cumulative_unrecognised_loss_closed
                + unrecognised_closed),
            'last_disposal_date': self.accounting_date,
            'disposal_proceeds': 0.0,
            'retained_interest_fair_value': 0.0,
            'oci_recycling_amount': 0.0,
            'disposal_percentage': 100.0,
            'state': 'active' if continuing else 'disposed',
        })
        return True

    def action_cancel(self):
        self._check_manager()
        self._eh_check_caller_scope('write')
        self._eh_lock()
        self = self._eh_workflow_action().with_context(
            eh_equity_engine=True)
        for inv in self:
            if inv.opening_move_id:
                raise UserError(_(
                    "The adopted opening entry is external ledger evidence. "
                    "Dispose the investment or correct that entry through "
                    "its owning workflow."))
            if inv.move_ids:
                raise UserError(_(
                    "Use Reverse and Cancel to counter the posted entries of "
                    "%s.",
                    inv.display_name))
            if inv.state not in ('draft', 'active'):
                raise UserError(_(
                    "Only a draft or zero-cost active investment can be "
                    "cancelled."))
            inv.write({
                'state': 'cancelled',
                'current_ownership_pct': 0.0,
            })

    def action_reverse_and_cancel(self, accounting_date=None):
        """Counter every investment entry, then close the subledger record.

        The investment model stores cumulative carrying anchors rather than a
        transaction subledger capable of safely reopening one historical
        pickup in isolation. The controlled correction is therefore an
        all-or-nothing cancellation: reverse every verified source entry in
        reverse posting order, preserve all originals and counter-entries,
        and zero the now-cancelled carrying anchors.
        """
        self.ensure_one()
        self._check_manager()
        self._eh_check_caller_scope('write')
        self._eh_lock()
        if self.opening_move_id:
            raise UserError(_(
                "An adopted opening entry is external ledger evidence and "
                "cannot be reversed by this subledger. Dispose the investment "
                "or correct the source entry through its owning workflow."))
        self.move_ids.invalidate_recordset([
            'state', 'eh_sealed', 'eh_legacy_unverified_seal',
        ])
        if self.state not in ('active', 'disposed') or not self.move_ids:
            raise UserError(_(
                "Only an active or disposed investment with posted entries "
                "can be reversed and cancelled."))
        originals = self.move_ids.sorted(key=lambda move: move.id, reverse=True)
        invalid = originals.filtered(lambda move: (
            move.eh_equity_investment_id != self
            or move.company_id != self.company_id
            or move.state != 'posted'
            or move.move_type != 'entry'
            or not move.eh_sealed
            or move.eh_legacy_unverified_seal
        ))
        if invalid:
            raise UserError(_(
                "At least one investment entry failed its verified sealed "
                "provenance check; no cancellation was posted."))
        existing_reversals = self.env['account.move'].sudo().search([
            ('reversed_entry_id', 'in', originals.ids),
        ], limit=1)
        if existing_reversals:
            raise UserError(_(
                "An investment entry already has a reversal. Review the "
                "immutable trail before attempting cancellation."))
        latest_original_date = max(originals.mapped('date'))
        requested_date = (
            fields.Date.to_date(accounting_date)
            if accounting_date else fields.Date.context_today(self)
        )
        if accounting_date and requested_date < latest_original_date:
            raise UserError(_(
                "The cancellation date cannot precede the latest investment "
                "entry date %s.", latest_original_date))
        reversal_date = max(requested_date, latest_original_date)
        for original in originals:
            reversal = original.sudo(False) \
                ._eh_reverse_with_verified_capability([{
                    'date': reversal_date,
                    'journal_id': original.journal_id.id,
                    'ref': _("Equity investment cancellation %s", self.name),
                }], cancel=False)
            reversal.sudo().with_context(
                eh_bizcombo_engine=True,
            )._eh_write_sealed_metadata(
                {'eh_equity_investment_reversal_id': self.id},
                {'eh_equity_investment_reversal_id'},
            )
            reversal._eh_post_verified_reversal()
            self._eh_seal_reversal(reversal)
        self._eh_workflow_action().with_context(
            eh_equity_engine=True,
        ).write({
            'state': 'cancelled',
            'carrying_amount': 0.0,
            'cumulative_share_of_profit': 0.0,
            'cumulative_dividends': 0.0,
            'cumulative_impairment': 0.0,
            'unrecognised_loss': 0.0,
            'current_ownership_pct': 0.0,
            'cumulative_disposed_carrying': 0.0,
            'cumulative_disposal_proceeds': 0.0,
            'cumulative_retained_interest_fv': 0.0,
            'cumulative_oci_recycled': 0.0,
            'cumulative_unrecognised_loss_closed': 0.0,
            'last_disposal_date': False,
            'investee_profit': 0.0,
            'dividend_received': 0.0,
            'impairment_amount': 0.0,
            'disposal_proceeds': 0.0,
            'reversed_at': fields.Datetime.now(),
            'reversed_by_id': self.env.user.id,
        })
        return True

    def unlink(self):
        for investment in self:
            if (investment.move_ids or investment.reversal_move_ids
                    or investment.opening_move_id):
                raise UserError(_(
                    "Investment %s carries immutable journal evidence and "
                    "cannot be deleted.", investment.display_name))
        return super().unlink()

    def action_view_moves(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': ['|', '|',
                       ('id', '=', self.opening_move_id.id),
                       ('eh_equity_investment_id', '=', self.id),
                       ('eh_equity_investment_reversal_id', '=', self.id)],
        }

    # ---- helpers ----

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can post equity-method "
                "entries."))

    def _require_active(self):
        if self.state != 'active':
            raise UserError(_(
                "The investment must be active. Activate it to recognise it "
                "at cost first."))

    def _validate_accounts(self, needed):
        self.ensure_one()
        field_map = {
            'investment': ('investment_account_id', _("investment account")),
            'share_of_profit': ('share_of_profit_account_id',
                                 _("share of profit account")),
            'cash': ('cash_account_id', _("cash / receivable account")),
            'impairment': ('impairment_account_id', _("impairment account")),
            'disposal': ('disposal_gain_loss_account_id',
                         _("disposal gain / loss account")),
            'retained': ('retained_interest_account_id',
                         _("retained interest account")),
            'oci': ('oci_reserve_account_id', _("OCI / CTA reserve account")),
        }
        missing = []
        if not self.journal_id:
            missing.append(_("journal"))
        for key in needed:
            fname, label = field_map[key]
            if not self[fname]:
                missing.append(label)
        if missing:
            raise UserError(_(
                "Configure the %s on %s first.",
                ', '.join(missing), self.display_name))
        if self.journal_id:
            if (self.journal_id.company_id != self.company_id
                    or self.journal_id.type != 'general'):
                raise AccessError(_(
                    "The equity-method journal must be a general journal in "
                    "the investment company."))
            self.journal_id._eh_check_access('read')
        accounts = self.env['account.account']
        for key in needed:
            accounts |= self[field_map[key][0]]
        def _matches_company(account):
            if 'company_ids' in account._fields:
                return self.company_id in account.company_ids
            return account.company_id == self.company_id
        if any(not _matches_company(account) for account in accounts):
            raise AccessError(_(
                "Every equity-method posting account must belong to the "
                "investment company."))
        if accounts:
            accounts._eh_check_access('read')

    def _post_move(self, legs):
        posting_date = fields.Date.to_date(self.accounting_date)
        if not posting_date:
            raise UserError(_(
                "Set an explicit accounting date before posting an equity-"
                "method entry."))
        lines = [(0, 0, {
            'name': label, 'account_id': account.id,
            'debit': debit, 'credit': credit,
        }) for account, debit, credit, label in legs]
        move = self.env['account.move']._eh_create_sealed({
            'move_type': 'entry',
            'date': posting_date,
            'journal_id': self.journal_id.id,
            'ref': self.name,
            'eh_sealed': True,
            'line_ids': lines,
        })
        move.sudo().with_context(
            eh_bizcombo_engine=True,
        )._eh_write_sealed_metadata(
            {'eh_equity_investment_id': self.id},
            {'eh_equity_investment_id'},
        )
        move.action_post()
        if move.state != 'posted' or move.date != posting_date:
            raise UserError(_(
                "Equity-method entry %(move)s did not post on requested date "
                "%(requested)s (stored date: %(stored)s). Choose an open "
                "accounting date or obtain an authorised lock-date exception.",
                move=move.display_name,
                requested=posting_date,
                stored=move.date or _("not posted"),
            ))
        return move


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_equity_investment_id = fields.Many2one(
        'eh.equity.investment', string="Equity Investment", readonly=True,
        index=True, ondelete='restrict', copy=False)
    eh_equity_investment_reversal_id = fields.Many2one(
        'eh.equity.investment', string="Equity Investment Reversal",
        readonly=True, index=True, ondelete='restrict', copy=False)
    eh_business_combination_id = fields.Many2one(
        'eh.business.combination', string="Business Combination",
        readonly=True, index=True, ondelete='restrict', copy=False)
    eh_business_combination_reversal_id = fields.Many2one(
        'eh.business.combination', string="Business Combination Reversal",
        readonly=True, index=True, ondelete='restrict', copy=False)
    eh_bizcombo_adjustment_id = fields.Many2one(
        'eh.bizcombo.adjustment', string="Combination Adjustment",
        readonly=True, index=True, ondelete='restrict', copy=False)
    eh_bizcombo_adjustment_reversal_id = fields.Many2one(
        'eh.bizcombo.adjustment', string="Combination Adjustment Reversal",
        readonly=True, index=True, ondelete='restrict', copy=False)
    eh_bizcombo_remeasure_id = fields.Many2one(
        'eh.bizcombo.contingent.remeasure', string="Contingent Remeasurement",
        readonly=True, index=True, ondelete='restrict', copy=False)
    eh_bizcombo_remeasure_reversal_id = fields.Many2one(
        'eh.bizcombo.contingent.remeasure',
        string="Contingent Remeasurement Reversal", readonly=True,
        index=True, ondelete='restrict', copy=False)

    _BIZCOMBO_PROVENANCE_FIELDS = (
        'eh_equity_investment_id', 'eh_equity_investment_reversal_id',
        'eh_business_combination_id',
        'eh_business_combination_reversal_id',
        'eh_bizcombo_adjustment_id',
        'eh_bizcombo_adjustment_reversal_id',
        'eh_bizcombo_remeasure_id',
        'eh_bizcombo_remeasure_reversal_id',
    )

    @api.model_create_multi
    def create(self, vals_list):
        supplied = any(
            any(vals.get(field) for field in self._BIZCOMBO_PROVENANCE_FIELDS)
            for vals in vals_list)
        if supplied and not (
                self.env.su and self.env.context.get('eh_bizcombo_engine')):
            raise AccessError(_(
                "Business-combination move provenance is server-owned."))
        return super().create(vals_list)

    def write(self, vals):
        provenance = set(vals) & set(self._BIZCOMBO_PROVENANCE_FIELDS)
        if provenance:
            if not (self.env.su
                    and self.env.context.get('eh_bizcombo_engine')):
                raise AccessError(_(
                    "Business-combination move provenance is server-owned."))
            for move in self:
                for field_name in provenance:
                    existing = move[field_name]
                    new_id = vals.get(field_name) or False
                    if existing and existing.id != new_id:
                        raise AccessError(_(
                            "Business-combination move provenance is "
                            "immutable."))
        return super().write(vals)
