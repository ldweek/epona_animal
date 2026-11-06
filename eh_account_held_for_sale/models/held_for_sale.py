# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.held.for.sale: a non-current asset or disposal group held for sale.

On classification the item is remeasured to the lower of its carrying amount
and fair value less costs to sell, posting any write-down; depreciation then
ceases (IFRS 5.15, 25). On sale the disposal posts proceeds against the
carrying amount with the resulting gain or loss.
"""

import math

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, MissingError, UserError, ValidationError

from odoo.addons.eh_account_assets_pro.models.accounting_integrity import (
    _eh_record_matches_company,
)

# carrying_amount is the ledger-derived figure: it must equal the posted
# remeasurement and so cannot be edited by hand once the item leaves draft.
# The model's own actions set it through the 'eh_hfs_internal' context flag.
# fair_value_less_costs stays editable while held: it is the manager's input
# to the sanctioned Remeasure action, not a posted figure.
_LOCKED_MEASURE_FIELDS = frozenset({'carrying_amount'})
_FROZEN_STATES = frozenset({'held', 'sold', 'cancelled'})
_FROZEN_INPUT_FIELDS = frozenset({
    'company_id', 'classification_date', 'asset_id',
    'asset_account_id', 'impairment_account_id', 'proceeds_account_id',
    'gain_loss_account_id', 'journal_id',
})
_TERMINAL_INPUT_FIELDS = frozenset({
    'fair_value_less_costs', 'remeasurement_date', 'sale_date', 'proceeds',
    'declassification_date', 'declassification_adjusted_carrying_amount',
    'declassification_recoverable_amount', 'declassification_basis',
    'declassification_measurement_completed',
    'asset_reclassification_correction_date',
    'asset_reclassification_correction_basis',
})
_DISCONTINUED_EVIDENCE_FIELDS = frozenset({
    'is_discontinued', 'discontinued_pl_account_ids',
})
_HFS_MOVE_LINK_CAPABILITY = object()
_HFS_ASSET_TRANSITION_CAPABILITY = object()
_DISCONTINUED_LINE_CONTEXT_KEY = 'eh_discontinued_line_capability'
_DISCONTINUED_LINE_CAPABILITY = object()


class EhHeldForSale(models.Model):
    _name = 'eh.held.for.sale'
    _description = "Asset held for sale (IFRS 5)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'classification_date desc, id desc'
    _rec_name = 'name'

    # State is a control point: without this guard a plain user could
    # RPC-write it straight to 'held'/'sold', skipping action_classify /
    # action_sell and their journal entries. Only the guarded workflow-action
    # capability supplied by eh.workflow.guard may change it.
    _eh_guarded_fields = (
        'state', 'currency_id', 'depreciation_ceased',
        'asset_paused_by_hfs', 'writedown',
        'discontinued_tag_id', 'discontinued_evidence_account_ids',
        'discontinued_evidence_move_line_ids',
        'discontinued_scope_quarantined', 'discontinued_scope_quarantine_note',
        'preclassification_carrying_amount',
        'declassification_target_amount', 'declassification_move_id',
        'declassified_at', 'declassified_by_id',
        'declassification_evidence_quarantined',
        'declassification_evidence_quarantine_note',
        'asset_reclassification_move_id',
        'asset_reclassification_reversal_move_id',
        'asset_reclassification_quarantined',
        'asset_reclassification_quarantine_note',
        'legacy_group_reference',
    )
    _eh_create_guarded_fields = (
        'state', 'currency_id', 'depreciation_ceased',
        'asset_paused_by_hfs', 'writedown', 'discontinued_tag_id',
        'discontinued_evidence_account_ids',
        'discontinued_evidence_move_line_ids',
        'discontinued_scope_quarantined', 'discontinued_scope_quarantine_note',
        'preclassification_carrying_amount',
        'declassification_target_amount', 'declassification_move_id',
        'declassified_at', 'declassified_by_id',
        'declassification_evidence_quarantined',
        'declassification_evidence_quarantine_note',
        'asset_reclassification_move_id',
        'asset_reclassification_reversal_move_id',
        'asset_reclassification_quarantined',
        'asset_reclassification_quarantine_note',
        'legacy_group_reference',
    )

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    description = fields.Char(
        help="The asset or disposal group, e.g. 'Retail division - "
             "Northgate store'.")
    state = fields.Selection(
        [('draft', "Draft"), ('held', "Held for sale"),
         ('sold', "Sold"), ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    classification_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)
    remeasurement_date = fields.Date(
        string="Remeasurement Date", copy=False, tracking=True,
        help="Accounting date for the next held-for-sale remeasurement. "
             "When empty, the action uses today's date.")
    sale_date = fields.Date(
        string="Sale Date", copy=False, tracking=True,
        help="Accounting date for the disposal. When empty, the sale action "
             "uses today's date.")
    extension_12m = fields.Boolean(
        string="12-Month Extension (IFRS 5.9)", tracking=True,
        help="The sale has not completed within one year of classification "
             "because of events or circumstances beyond the entity's "
             "control, and the entity remains committed to its plan to "
             "sell. Setting this suppresses the overdue flag; the item is "
             "never auto-declassified (disclose-first design).")
    overdue_12m = fields.Boolean(
        string="Overdue (12 Months)",
        compute='_compute_overdue_12m', search='_search_overdue_12m',
        help="The item has been held for sale for more than twelve months "
             "without the IFRS 5.9 extension. Flag only: review whether "
             "the held-for-sale criteria are still met and either record "
             "the extension or cease classification.")
    is_discontinued = fields.Boolean(
        string="Discontinued operation",
        help="Scopes the operation for the module's IFRS 5.33 reporting API. "
             "Select exact posted P&L journal items and seal the scope; the "
             "module does not build or modify a financial-statement layout.")
    discontinued_tag_id = fields.Many2one(
        'account.account.tag', readonly=True, copy=False,
        string="Discontinued Operations Scope Marker",
        help="Per-company audit marker proving that an exact journal-item "
             "scope was sealed. It is not attached to chart accounts and "
             "does not imply a financial-statements integration.")
    discontinued_pl_account_ids = fields.Many2many(
        'account.account', 'eh_held_for_sale_disc_account_rel',
        'item_id', 'account_id', string="Discontinued P&L Accounts",
        domain="[('account_type', 'in', ['income', 'income_other', "
               "'expense', 'expense_depreciation', 'expense_direct_cost'])]",
        help="Legacy account-level workpaper hints. Exact journal-item "
             "evidence below, not chart-account metadata, controls the "
             "reported discontinued-operation scope.")
    discontinued_evidence_account_ids = fields.Many2many(
        'account.account', 'eh_hfs_disc_evidence_account_rel',
        'item_id', 'account_id', string="Frozen Discontinued P&L Scope",
        readonly=True, copy=False,
        help="Server-owned snapshot of the accounts approved when Tag "
             "Discontinued P&L ran. Reporting reads this immutable scope, "
             "not mutable account-tag membership.")
    discontinued_candidate_move_line_ids = fields.Many2many(
        'account.move.line', 'eh_hfs_disc_candidate_move_line_rel',
        'item_id', 'line_id', string="Candidate Discontinued Journal Items",
        copy=False,
        domain="[('company_id', '=', company_id), "
               "('parent_state', '=', 'posted'), "
               "('account_id.account_type', 'in', "
               "['income', 'income_other', 'expense', "
               "'expense_depreciation', 'expense_direct_cost'])]",
        help=(
            "Exact posted P&L journal items belonging to this discontinued "
            "operation. Account-wide tagging cannot distinguish continuing "
            "activity that shares an account, so reporting never infers scope "
            "from the selected accounts."
        ),
    )
    discontinued_evidence_move_line_ids = fields.Many2many(
        'account.move.line', 'eh_hfs_disc_evidence_move_line_rel',
        'item_id', 'line_id', string="Frozen Discontinued Journal Items",
        readonly=True, copy=False,
        help="Immutable exact journal-item scope used by IFRS 5.33 reporting.",
    )
    discontinued_scope_quarantined = fields.Boolean(
        readonly=True, copy=False, index=True,
    )
    discontinued_scope_quarantine_note = fields.Text(readonly=True, copy=False)
    legacy_group_reference = fields.Char(
        readonly=True, copy=False,
        help="Upgrade-only audit note preserving the former loose disposal-"
             "group reference. New group measurements are represented only "
             "by eh.disposal.group.line, which is the single carrying record.")
    depreciation_ceased = fields.Boolean(
        readonly=True, copy=False,
        help="Depreciation stops while the asset is held for sale "
             "(IFRS 5.25).")

    # ---- optional link to the fixed-asset engine ----
    # When an eh.asset is linked, classification drives its carrying amount
    # from the asset's ledger-derived net book value (not a hand-keyed
    # figure) and pauses the asset so the depreciation cron ceases charging
    # it (IFRS 5.25). Leaving this blank keeps the standalone, hand-keyed
    # behaviour unchanged.
    asset_id = fields.Many2one(
        'eh.asset', string="Linked Asset", tracking=True,
        ondelete='restrict', copy=False, check_company=True,
        domain="[('company_id', '=', company_id), "
               "('state', 'in', ['running', 'paused'])]",
        help="Fixed asset this classification covers. On classification the "
             "carrying amount is seeded from the asset's net book value and "
             "the asset's depreciation is paused so it is not charged twice "
             "(IFRS 5.15, 5.25). Leave blank for a standalone, hand-keyed "
             "item.")
    asset_paused_by_hfs = fields.Boolean(
        readonly=True, copy=False,
        help="Set when this classification paused the linked asset, so a "
             "reversal or cancellation only resumes an asset it paused "
             "itself (not one the user had already paused).")
    asset_reclassification_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
        check_company=True,
        help=(
            "Sealed IFRS 5.38 entry that moved the linked asset's gross "
            "cost, accumulated depreciation and accumulated impairment out "
            "of PPE and presented its net carrying amount as held for sale."
        ),
    )
    asset_reclassification_reversal_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
        check_company=True,
        help=(
            "Sealed exact reversal of the IFRS 5.38 presentation entry on "
            "sale or cease-classification."
        ),
    )
    asset_reclassification_quarantined = fields.Boolean(
        readonly=True, copy=False, index=True,
    )
    asset_reclassification_quarantine_note = fields.Text(
        readonly=True, copy=False,
    )
    asset_reclassification_correction_date = fields.Date(
        copy=False,
        help=(
            "Current-period accounting date for the manager-reviewed IFRS "
            "5.38 correction of a legacy linked classification."
        ),
    )
    asset_reclassification_correction_basis = fields.Text(
        copy=False,
        help=(
            "Required workpaper reference supporting a legacy IFRS 5.38 "
            "current-period correction. Historical entries are never "
            "fabricated or backdated by the upgrade."
        ),
    )

    preclassification_carrying_amount = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Server-owned carrying amount immediately before IFRS 5 measurement.",
    )
    declassification_date = fields.Date(
        string="Cease-classification Date", copy=False,
        help="Date the IFRS 5 held-for-sale criteria ceased to be met.",
    )
    declassification_adjusted_carrying_amount = fields.Monetary(
        string="Carrying Amount Had IFRS 5 Not Applied",
        currency_field='currency_id', copy=False,
        help=(
            "IFRS 5.27(a) carrying amount before classification adjusted for "
            "depreciation, amortisation or revaluation that would have been "
            "recognised had the asset never been classified held for sale."
        ),
    )
    declassification_recoverable_amount = fields.Monetary(
        string="Recoverable Amount on Ceasing Classification",
        currency_field='currency_id', copy=False,
        help="IFRS 5.27(b) recoverable amount at the decision date.",
    )
    declassification_measurement_completed = fields.Boolean(
        string="IFRS 5.27 Measurement Completed",
        copy=False,
        help=(
            "Explicit manager attestation that both IFRS 5.27 amounts were "
            "measured. Required because zero is a valid measured amount and "
            "cannot otherwise be distinguished from an omitted input."
        ),
    )
    declassification_basis = fields.Text(
        string="Cease-classification Measurement Basis", copy=False,
        help=(
            "Required evidence supporting the hypothetical adjusted carrying "
            "amount and recoverable amount used by IFRS 5.27."
        ),
    )
    declassification_target_amount = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
    )
    declassification_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
        check_company=True,
    )
    declassified_at = fields.Datetime(readonly=True, copy=False)
    declassified_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
    )
    declassification_evidence_quarantined = fields.Boolean(
        readonly=True, copy=False, index=True,
    )
    declassification_evidence_quarantine_note = fields.Text(
        readonly=True, copy=False,
    )

    carrying_amount = fields.Monetary(
        currency_field='currency_id', required=True, tracking=True,
        help="Carrying amount immediately before classification.")
    fair_value_less_costs = fields.Monetary(
        string="Fair Value Less Costs to Sell",
        currency_field='currency_id', required=True, tracking=True)
    writedown = fields.Monetary(
        compute='_compute_writedown', store=True, currency_field='currency_id',
        help="Excess of carrying amount over fair value less costs to sell.")
    proceeds = fields.Monetary(
        currency_field='currency_id',
        help="Net proceeds received on sale.")

    # ---- accounts ----
    asset_account_id = fields.Many2one(
        'account.account', string="Asset Account", tracking=True)
    impairment_account_id = fields.Many2one(
        'account.account', string="Impairment Expense Account", tracking=True,
        domain="[('account_type', 'in', ['expense', 'expense_depreciation'])]")
    proceeds_account_id = fields.Many2one(
        'account.account', string="Proceeds Account", tracking=True,
        domain="[('account_type', 'in', "
               "['asset_cash', 'asset_receivable', 'asset_current'])]")
    gain_loss_account_id = fields.Many2one(
        'account.account', string="Gain / Loss on Disposal Account",
        tracking=True,
        domain="[('account_type', 'in', ['income_other', 'expense'])]")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        domain="[('type', '=', 'general'), ('company_id', '=', company_id)]")

    move_ids = fields.One2many('account.move', 'eh_held_for_sale_id')
    move_count = fields.Integer(compute='_compute_move_count')

    notes = fields.Text()

    _check_carrying = models.Constraint(
        'CHECK (carrying_amount >= 0)', 'Carrying amount cannot be negative.')

    @api.constrains(
        'carrying_amount', 'fair_value_less_costs', 'proceeds',
        'declassification_adjusted_carrying_amount',
        'declassification_recoverable_amount',
    )
    def _check_finite_amounts(self):
        for item in self:
            values = (
                item.carrying_amount, item.fair_value_less_costs,
                item.proceeds, item.declassification_adjusted_carrying_amount,
                item.declassification_recoverable_amount,
            )
            if not all(math.isfinite(value or 0.0) for value in values):
                raise ValidationError(_(
                    "Held-for-sale measurement amounts must be finite."
                ))

    @api.depends('carrying_amount', 'fair_value_less_costs')
    def _compute_writedown(self):
        for item in self:
            item.writedown = max(
                item.carrying_amount - item.fair_value_less_costs, 0.0)

    def _compute_move_count(self):
        for item in self:
            item.move_count = len(item.move_ids)

    @api.depends('state', 'classification_date', 'extension_12m')
    def _compute_overdue_12m(self):
        today = fields.Date.context_today(self)
        for item in self:
            item.overdue_12m = bool(
                item.state == 'held'
                and not item.extension_12m
                and item.classification_date
                and item.classification_date + relativedelta(years=1)
                < today)

    def _search_overdue_12m(self, operator, value):
        # Overdue iff held, no extension, and the classification
        # anniversary has passed (classification_date < today - 1 year).
        if isinstance(value, (list, tuple)):
            value = True in value
        if operator in ('!=', 'not in'):
            value = not value
        elif operator not in ('=', 'in'):
            raise NotImplementedError(
                "Unsupported operator %r on overdue_12m" % operator)
        cutoff = fields.Date.context_today(self) - relativedelta(years=1)
        if value:
            return [('state', '=', 'held'), ('extension_12m', '=', False),
                    ('classification_date', '<', cutoff)]
        return ['|', '|', ('state', '!=', 'held'),
                ('extension_12m', '=', True),
                ('classification_date', '>=', cutoff)]

    @api.onchange('asset_id')
    def _onchange_asset_id(self):
        """Pre-fill the carrying amount from the linked asset's net book
        value so the user does not hand-key a figure that would double the
        carrying value. Only pre-fills in draft; the authoritative seed
        still happens in action_classify off the ledger at posting time.
        """
        for item in self:
            if item.asset_id and item.state == 'draft':
                item.carrying_amount = item.currency_id.round(
                    item.asset_id.net_book_value)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._eh_assert_company_allowed(
                vals.get('company_id') or self.env.company.id)
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.held.for.sale') or '/'
        items = super().create(vals_list)
        items._eh_validate_references()
        return items

    def write(self, vals):
        if vals and not self.env.su:
            self._eh_check_access('write')
        self._eh_validate_references(vals)
        self._eh_lock_for_transition(vals)
        self._eh_validate_references(vals)
        if (
            _LOCKED_MEASURE_FIELDS.intersection(vals)
            and not (self.env.su and self.env.context.get('eh_hfs_internal'))
            and any(item.state in _FROZEN_STATES for item in self)
        ):
            raise UserError(_(
                "The carrying amount is locked once the item is classified "
                "as held for sale; it must equal the posted remeasurement. "
                "Set a new fair value less costs to sell and use Remeasure to "
                "record a subsequent change (IFRS 5.15)."))
        if not (self.env.su and self.env.context.get('eh_hfs_internal')):
            frozen = self.filtered(
                lambda item: item.state in _FROZEN_STATES)
            touched = sorted(_FROZEN_INPUT_FIELDS.intersection(vals))
            if frozen and touched:
                raise UserError(_(
                    "Classified held-for-sale identity and posting fields "
                    "(%(fields)s) are immutable. Record a new classification "
                    "or correction instead of rewriting ledger evidence.",
                    fields=', '.join(touched)))
            terminal = self.filtered(
                lambda item: item.state in ('sold', 'cancelled'))
            terminal_touched = sorted(
                _TERMINAL_INPUT_FIELDS.intersection(vals))
            if terminal and terminal_touched:
                raise UserError(_(
                    "Completed held-for-sale event inputs (%(fields)s) are "
                    "immutable. Record an explicit correction instead.",
                    fields=', '.join(terminal_touched)))
            if {
                'asset_reclassification_correction_date',
                'asset_reclassification_correction_basis',
            }.intersection(vals):
                self._check_manager()
            tagged = self.filtered('discontinued_tag_id')
            tag_touched = sorted(
                _DISCONTINUED_EVIDENCE_FIELDS.intersection(vals))
            if tagged and tag_touched:
                raise UserError(_(
                    "Discontinued-operation scope is frozen after accounts "
                    "are tagged. Create an explicit correction instead."))
        # The state of a held / sold item is itself a control point: resetting
        # it to draft would silently lift the carrying-amount freeze above. A
        # raw ORM state write moving OUT of a frozen state without the
        # sanctioned-transition context flag must be manager-gated, so a plain
        # user cannot un-freeze a GL-backed item. The sanctioned actions
        # (sell / cancel) set the flag after their own manager check and move
        # handling; classify moves INTO 'held' and is unaffected.
        if 'state' in vals and not (
                self.env.su and self.env.context.get('eh_hfs_state_change')):
            crossing = self.filtered(
                lambda item: item.state in _FROZEN_STATES
                and item.state != vals['state'])
            if crossing:
                crossing._check_manager()
        result = super().write(vals)
        self._eh_validate_references()
        return result

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        posted = self.filtered(
            lambda item: item.state != 'draft' or item.move_ids)
        if posted:
            raise UserError(_(
                "A held-for-sale item that has been classified or sold cannot "
                "be deleted; it carries a posted GL movement. Cancel it "
                "(EH Accounting Manager only) instead."))
        return super().unlink()

    # ---- actions ----

    def action_classify(self):
        self.ensure_one()
        self = self._eh_prepare_action()
        if self.state != 'draft':
            raise UserError(_(
                "Only a draft item can be classified as held for sale."))
        self._validate_accounts(['asset', 'impairment'])
        currency = self.currency_id
        # When linked to a fixed asset, seed the carrying amount from the
        # asset's ledger-derived net book value and cease its depreciation
        # so the two records cannot double-count the carrying value
        # (IFRS 5.15, 5.25). The write-down below then measures off that
        # ledger figure rather than a hand-keyed one.
        if self.asset_id:
            self.asset_id._eh_assert_available_for_hfs_claim()
            self.asset_id._eh_accrue_depreciation_for_hfs(
                self.classification_date)
            self._eh_seed_from_asset()
            self._eh_cease_asset_depreciation()
        self.with_context(eh_hfs_internal=True).write({
            'preclassification_carrying_amount': self.carrying_amount,
        })
        if self.asset_id:
            # IFRS 5.38 requires separate balance-sheet presentation, not
            # merely an impairment credit to an HFS account while gross cost
            # and accumulated depreciation remain in PPE.
            self._eh_post_asset_reclassification(self.classification_date)
        writedown = currency.round(self.writedown)
        if writedown > 0:
            if self.asset_id:
                # Linked asset: route the write-down through the asset's own
                # impairment engine so the eh.asset net book value falls by the
                # same amount and the two subledgers reconcile (IFRS 5.15). One
                # journal entry, sharing the held-for-sale accounts, keeps the
                # asset ledger and the carrying amount from diverging.
                self._eh_post_asset_impairment(writedown, is_reversal=False)
            else:
                self._post_move([
                    (self.impairment_account_id, writedown, 0.0,
                     _("Held-for-sale write-down %s", self.name)),
                    (self.asset_account_id, 0.0, writedown,
                     _("Remeasure to FVLCTS %s", self.name)),
                ])
            self.with_context(eh_hfs_internal=True).write(
                {'carrying_amount': self.carrying_amount - writedown})
        self.write({'state': 'held', 'depreciation_ceased': True})
        return True

    def action_remeasure(self):
        """Subsequent remeasurement of a held-for-sale item (IFRS 5.15).

        On each reporting date remeasure to the lower of the current carrying
        amount and fair value less costs to sell. A fall posts a further
        write-down; a subsequent rise reverses previously recognised
        write-downs, but not above the carrying amount before classification
        (IFRS 5.21-22). This is the only sanctioned path to move the carrying
        amount once the item is held for sale.
        """
        self.ensure_one()
        self = self._eh_prepare_action()
        if self.state != 'held':
            raise UserError(_(
                "Only an item held for sale can be remeasured."))
        self._eh_assert_asset_reclassification_ready()
        self._validate_accounts(['asset', 'impairment'])
        event_date = self._eh_event_date(
            self.remeasurement_date, _("remeasurement"))
        currency = self.currency_id
        carrying = currency.round(self.carrying_amount)
        fvlcts = currency.round(self.fair_value_less_costs)
        delta = currency.round(fvlcts - carrying)
        if not delta:
            raise UserError(_(
                "The carrying amount already equals fair value less costs to "
                "sell; there is nothing to remeasure on %s.", self.name))
        if delta < 0:
            # Further write-down: DR impairment, CR asset.
            amount = -delta
            if self.asset_id:
                self._eh_post_asset_impairment(
                    amount, is_reversal=False, event_date=event_date)
            else:
                self._post_move([
                    (self.impairment_account_id, amount, 0.0,
                     _("Held-for-sale remeasurement write-down %s", self.name)),
                    (self.asset_account_id, 0.0, amount,
                     _("Remeasure to FVLCTS %s", self.name)),
                ], date=event_date)
        else:
            # Reversal of a prior write-down: DR asset, CR impairment.
            # IFRS 5.22 caps the gain so the carrying amount is not raised
            # above the amount that would have been carried had the item never
            # been written down, i.e. the reversal cannot exceed the cumulative
            # write-downs previously recognised. Anything above that cap is not
            # posted and the carrying amount is held at the ceiling.
            cap = self._cumulative_writedown()
            amount = currency.round(min(delta, cap))
            if amount <= 0:
                raise UserError(_(
                    "The reversal is fully limited by IFRS 5.22: fair value "
                    "less costs to sell may not lift the carrying amount above "
                    "its cumulative write-downs on %s.", self.name))
            if self.asset_id:
                self._eh_post_asset_impairment(
                    amount, is_reversal=True, event_date=event_date)
            else:
                self._post_move([
                    (self.asset_account_id, amount, 0.0,
                     _("Held-for-sale write-down reversal %s", self.name)),
                    (self.impairment_account_id, 0.0, amount,
                     _("Reverse impairment %s", self.name)),
                ], date=event_date)
            capped_carrying = currency.round(carrying + amount)
            self.with_context(eh_hfs_internal=True).write(
                {'carrying_amount': capped_carrying,
                 'remeasurement_date': event_date})
            return True
        self.with_context(eh_hfs_internal=True).write(
            {'carrying_amount': fvlcts,
             'remeasurement_date': event_date})
        return True

    def _cumulative_writedown(self):
        """Net write-down still recognised on this item, in company currency.

        Every classification/remeasurement write-down debits the impairment
        account and every reversal credits it, all on moves linked to this
        item. The net debit on that account across the item's own posted moves
        is therefore the cumulative write-down available to reverse under
        IFRS 5.22.
        """
        self.ensure_one()
        lines = self.move_ids.filtered(
            lambda m: m.state == 'posted'
        ).line_ids.filtered(
            lambda line: line.account_id == self.impairment_account_id)
        return self.currency_id.round(
            sum(lines.mapped('debit')) - sum(lines.mapped('credit')))

    def action_sell(self):
        self.ensure_one()
        self = self._eh_prepare_action()
        if self.state != 'held':
            raise UserError(_(
                "Only an item held for sale can be sold."))
        self._eh_assert_asset_reclassification_ready()
        currency = self.currency_id
        proceeds = currency.round(self.proceeds)
        sale_date = self._eh_event_date(self.sale_date, _("sale"))
        if self.asset_id:
            # Linked fixed asset: derecognise it through the asset engine so
            # its cost, accumulated depreciation and accumulated impairment
            # (including the held-for-sale write-downs, routed through the
            # asset's own impairment engine) are all reversed, gain/loss is
            # booked, and the eh.asset moves to 'disposed'. The standalone
            # carrying-derecognition below would leave the asset paused with a
            # live net book value and its gross cost / accumulated GL balances
            # never reversed.
            self._eh_reverse_asset_reclassification(sale_date)
            self._eh_dispose_linked_asset(proceeds, sale_date)
            self.with_context(
                eh_hfs_internal=True, eh_hfs_state_change=True).write(
                {'state': 'sold', 'carrying_amount': 0.0,
                 'asset_paused_by_hfs': False, 'sale_date': sale_date})
            return True
        self._validate_accounts(['asset', 'proceeds', 'gain_loss'])
        carrying = currency.round(self.carrying_amount)
        gain_loss = currency.round(proceeds - carrying)
        lines = []
        if proceeds:
            lines.append((self.proceeds_account_id, proceeds, 0.0,
                          _("Disposal proceeds %s", self.name)))
        if carrying:
            lines.append((self.asset_account_id, 0.0, carrying,
                          _("Derecognise asset %s", self.name)))
        if gain_loss > 0:
            lines.append((self.gain_loss_account_id, 0.0, gain_loss,
                          _("Gain on disposal %s", self.name)))
        elif gain_loss < 0:
            lines.append((self.gain_loss_account_id, -gain_loss, 0.0,
                          _("Loss on disposal %s", self.name)))
        if not lines:
            raise UserError(_("Nothing to post on sale of %s.", self.name))
        self._post_move(lines, date=sale_date)
        self.with_context(
            eh_hfs_internal=True, eh_hfs_state_change=True).write(
            {'state': 'sold', 'carrying_amount': 0.0,
             'sale_date': sale_date})
        return True

    def action_cancel(self):
        for original in self:
            item = original._eh_prepare_action()
            if item.state == 'sold':
                raise UserError(_("A sold item cannot be cancelled."))
            if item.state == 'draft':
                item.with_context(
                    eh_hfs_internal=True, eh_hfs_state_change=True,
                ).write({
                    'state': 'cancelled',
                    'depreciation_ceased': False,
                })
                continue

            item._eh_assert_asset_reclassification_ready()

            # IFRS 5.27 is a measurement event, not a bare workflow reset:
            # recognise the difference between current HFS carrying amount
            # and the lower of (a) the amount had IFRS 5 never applied and
            # (b) recoverable amount. The manager must retain the documented
            # basis; the resulting JE (or exact zero adjustment) is stamped.
            if not (item.declassification_basis or '').strip():
                raise UserError(_(
                    "Document the IFRS 5.27 adjusted-carrying and recoverable-"
                    "amount basis before ceasing classification on %s.",
                    item.display_name,
                ))
            if not item.declassification_measurement_completed:
                raise UserError(_(
                    "Complete and attest both IFRS 5.27 measurements before "
                    "ceasing classification on %s. Zero is allowed only "
                    "when explicitly measured.",
                    item.display_name,
                ))
            adjusted = item.declassification_adjusted_carrying_amount or 0.0
            recoverable = item.declassification_recoverable_amount or 0.0
            if not all(math.isfinite(value) and value >= 0 for value in (
                    adjusted, recoverable)):
                raise UserError(_(
                    "IFRS 5.27 adjusted carrying and recoverable amounts must "
                    "be finite and non-negative."
                ))
            event_date = item._eh_event_date(
                item.declassification_date, _("cease-classification"),
            )
            currency = item.currency_id
            target = currency.round(min(adjusted, recoverable))
            current = currency.round(item.carrying_amount)
            delta = currency.round(target - current)
            move = self.env['account.move']
            if delta > 0:
                cap = item._cumulative_writedown()
                if currency.compare_amounts(delta, cap) > 0:
                    raise UserError(_(
                        "The cease-classification uplift %(delta).2f exceeds "
                        "the %(cap).2f HFS write-down evidenced on %(item)s. "
                        "The adjusted basis would reverse unrelated history.",
                        delta=delta, cap=cap, item=item.display_name,
                    ))
            if delta:
                if item.asset_id:
                    impairment = item._eh_post_asset_impairment(
                        abs(delta), is_reversal=delta > 0,
                        event_date=event_date,
                    )
                    move = impairment.move_id
                else:
                    item._validate_accounts(['asset', 'impairment'])
                    if delta > 0:
                        legs = [
                            (item.asset_account_id, delta, 0.0,
                             _("IFRS 5.27 cease-classification adjustment %s",
                               item.name)),
                            (item.impairment_account_id, 0.0, delta,
                             _("IFRS 5.27 adjustment income %s", item.name)),
                        ]
                    else:
                        legs = [
                            (item.impairment_account_id, -delta, 0.0,
                             _("IFRS 5.27 adjustment expense %s", item.name)),
                            (item.asset_account_id, 0.0, -delta,
                             _("IFRS 5.27 cease-classification adjustment %s",
                               item.name)),
                        ]
                    move = item._post_move(legs, date=event_date)
            if item.asset_id:
                reversal = item._eh_reverse_asset_reclassification(event_date)
                transfer = item._eh_transfer_hfs_asset_impairment(event_date)
                move = transfer or reversal or move
                item.asset_id.invalidate_recordset(['net_book_value'])
                if currency.compare_amounts(
                        item.asset_id.net_book_value, target) != 0:
                    raise UserError(_(
                        "The linked fixed-asset carrying amount did not tie to "
                        "the IFRS 5.27 target; the transaction was rolled back."
                    ))
                item._eh_resume_asset(event_date)
            # Sanctioned exit out of 'held': flag the state write so the
            # state-reset gate in write() lets this manager-driven transition
            # through (a plain raw reset to draft stays blocked).
            item.with_context(
                eh_hfs_internal=True, eh_hfs_state_change=True).write(
                {
                    'state': 'cancelled',
                    'carrying_amount': target,
                    'depreciation_ceased': False,
                    'declassification_date': event_date,
                    'declassification_target_amount': target,
                    'declassification_move_id': move.id if move else False,
                    'declassified_at': fields.Datetime.now(),
                    'declassified_by_id': self.env.user.id,
                    'declassification_evidence_quarantined': False,
                    'declassification_evidence_quarantine_note': False,
                })
        return True

    def action_remediate_asset_reclassification(self):
        """Post a current-period IFRS 5.38 correction for legacy records.

        The upgrade cannot safely invent a historical journal entry or its
        accounting date.  It therefore quarantines affected active records;
        a manager supplies the correction date and workpaper basis here.
        """
        self.ensure_one()
        self = self._eh_prepare_action()
        if self.state != 'held' or not self.asset_id:
            raise UserError(_(
                "Only an active linked held-for-sale classification can be "
                "remediated for IFRS 5.38 presentation."))
        if self.asset_reclassification_move_id:
            raise UserError(_(
                "The linked asset already has sealed IFRS 5.38 "
                "reclassification evidence."))
        if not (self.asset_reclassification_correction_basis or '').strip():
            raise UserError(_(
                "Document the current-period IFRS 5.38 correction basis and "
                "workpaper reference first."))
        event_date = self._eh_event_date(
            self.asset_reclassification_correction_date,
            _("IFRS 5.38 correction"),
        )
        move = self._eh_post_asset_reclassification(event_date)
        self.with_context(eh_hfs_internal=True).write({
            'asset_reclassification_correction_date': event_date,
            'asset_reclassification_quarantined': False,
            'asset_reclassification_quarantine_note': False,
        })
        self.message_post(body=_(
            "Legacy IFRS 5.38 presentation remediated by sealed "
            "current-period entry %(move)s on %(date)s. Basis: %(basis)s",
            move=move.display_name,
            date=event_date,
            basis=self.asset_reclassification_correction_basis,
        ))
        return True

    def action_tag_discontinued(self):
        """Seal exact journal-item scope for IFRS 5.33 reporting.

        Candidate read access is checked in the caller's original environment
        before the workflow elevates. Elevation is then limited to immutable
        scope metadata; chart-account tags are never mutated.
        """
        self.ensure_one()
        caller_candidates = self.discontinued_candidate_move_line_ids
        if not self.env.su:
            caller_candidates._eh_check_access('read')
        self = self._eh_prepare_action()
        if not self.is_discontinued:
            raise UserError(_(
                "Flag %s as a discontinued operation first.",
                self.display_name))
        candidates = self.env['account.move.line'].browse(
            caller_candidates.ids)
        if not candidates:
            raise UserError(_(
                "Select the exact posted P&L journal items of discontinued "
                "operation %s first.", self.display_name))
        invalid = candidates.filtered(lambda line: (
            line.company_id != self.company_id
            or line.parent_state != 'posted'
            or line.account_id.account_type not in (
                'income', 'income_other', 'expense',
                'expense_depreciation', 'expense_direct_cost',
            )
            or (line.eh_discontinued_hfs_ids
                and line.eh_discontinued_hfs_ids != self)
            or line.eh_discontinued_group_ids
        ))
        if invalid:
            raise UserError(_(
                "Discontinued scope must contain unclaimed posted P&L lines "
                "in the same company. Current-period and comparative-period "
                "journal items may predate classification; reporting applies "
                "the requested reporting dates. Invalid journal items: %s.",
                ', '.join(map(str, invalid.ids)),
            ))
        # IFRS 5.33/34 is a continuing disclosure obligation, not a one-shot
        # classification-date snapshot.  Preserve every previously approved
        # journal item and add the newly reviewed candidates.  Omitting an old
        # line from the staging picker can therefore never erase audit
        # evidence that has already fed a published discontinued-operations
        # report.
        evidence = self.discontinued_evidence_move_line_ids | candidates
        accounts = (
            self.discontinued_evidence_account_ids
            | evidence.mapped('account_id')
        )
        tag = self.env['eh.disposal.group']._eh_discontinued_tag(
            self.company_id, create=True)
        self.with_context(eh_hfs_internal=True).write({
            'discontinued_tag_id': tag.id,
            'discontinued_evidence_account_ids': [
                (6, 0, accounts.ids),
            ],
            'discontinued_evidence_move_line_ids': [
                (6, 0, evidence.ids),
            ],
            'discontinued_scope_quarantined': False,
            'discontinued_scope_quarantine_note': False,
        })
        return True

    def action_view_moves(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('eh_held_for_sale_id', '=', self.id)],
        }

    # ---- helpers ----

    @api.model
    def _eh_assert_company_allowed(self, company_id):
        if (
            not self.env.su
            and company_id
            and int(company_id) not in self.env.companies.ids
        ):
            raise AccessError(_(
                "Held-for-sale records may only use a company enabled in "
                "the caller's current company context."))

    @api.model
    def _eh_resolve_reference(self, model_name, record_id, access='read'):
        record = self.env[model_name].browse(record_id) if record_id else False
        if not record:
            return record
        try:
            if not self.env.su:
                record._eh_check_access(access)
            record = record.exists()
        except (AccessError, MissingError):
            raise AccessError(_(
                "A held-for-sale accounting reference is unavailable to "
                "the caller.")) from None
        if not record:
            raise AccessError(_(
                "A held-for-sale accounting reference is unavailable to "
                "the caller."))
        return record

    def _eh_validate_references(self, vals=None):
        vals = vals or {}
        account_fields = (
            'asset_account_id', 'impairment_account_id',
            'proceeds_account_id', 'gain_loss_account_id',
        )
        for item in self:
            company_id = vals.get('company_id', item.company_id.id)
            self._eh_assert_company_allowed(company_id)
            company = self._eh_resolve_reference(
                'res.company', company_id, access='read')
            journal_id = vals.get('journal_id', item.journal_id.id)
            journal = self._eh_resolve_reference(
                'account.journal', journal_id, access='read')
            if journal and journal.company_id != company:
                raise ValidationError(_(
                    "Held-for-sale journal must belong to its company."))
            for field_name in account_fields:
                account_id = vals.get(field_name, item[field_name].id)
                account = self._eh_resolve_reference(
                    'account.account', account_id, access='read')
                if account and not _eh_record_matches_company(account, company):
                    raise ValidationError(_(
                        "Held-for-sale posting accounts must be available "
                        "to its company."))
            asset_id = vals.get('asset_id', item.asset_id.id)
            asset = self._eh_resolve_reference(
                'eh.asset', asset_id, access='write')
            if asset and asset.company_id != company:
                raise ValidationError(_(
                    "Linked fixed asset must belong to the held-for-sale "
                    "company."))
            declass_move_id = vals.get(
                'declassification_move_id', item.declassification_move_id.id,
            )
            declass_move = self._eh_resolve_reference(
                'account.move', declass_move_id, access='read',
            )
            if declass_move and (
                    declass_move.company_id != company
                    or declass_move.eh_held_for_sale_id != item):
                raise ValidationError(_(
                    "Cease-classification evidence must be an exact journal "
                    "entry owned by this held-for-sale item."
                ))
            reclass_move_id = vals.get(
                'asset_reclassification_move_id',
                item.asset_reclassification_move_id.id,
            )
            reclass_move = self._eh_resolve_reference(
                'account.move', reclass_move_id, access='read',
            )
            if reclass_move and (
                    reclass_move.company_id != company
                    or reclass_move.eh_held_for_sale_id != item
                    or reclass_move.state != 'posted'):
                raise ValidationError(_(
                    "IFRS 5.38 reclassification evidence must be an exact "
                    "posted journal entry owned by this held-for-sale item."
                ))
            reversal_move_id = vals.get(
                'asset_reclassification_reversal_move_id',
                item.asset_reclassification_reversal_move_id.id,
            )
            reversal_move = self._eh_resolve_reference(
                'account.move', reversal_move_id, access='read',
            )
            if reversal_move and (
                    reversal_move.company_id != company
                    or reversal_move.eh_held_for_sale_id != item
                    or reversal_move.state != 'posted'
                    or reversal_move == reclass_move):
                raise ValidationError(_(
                    "IFRS 5.38 reversal evidence must be a distinct exact "
                    "posted journal entry owned by this held-for-sale item."
                ))
            accounts = (
                item.discontinued_pl_account_ids
                | item.discontinued_evidence_account_ids
            )
            if 'discontinued_pl_account_ids' in vals:
                # Many2many commands are safest validated after ORM applies
                # them; actions always revalidate current stored membership.
                accounts = self.env['account.account'].browse()
            for account in accounts:
                self._eh_resolve_reference(
                    'account.account', account.id, access='read')
                if not _eh_record_matches_company(account, company):
                    raise ValidationError(_(
                        "Discontinued-operation accounts must be available "
                        "to the held-for-sale company."))
        return self

    def _eh_lock_for_transition(self, vals=None):
        vals = vals or {}
        asset_ids = set(self.mapped('asset_id').ids)
        if vals.get('asset_id'):
            asset_ids.add(int(vals['asset_id']))
        self.flush_recordset()
        if self.ids:
            self.env.cr.execute(
                "SELECT id FROM eh_held_for_sale WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(sorted(self.ids)),),
            )
        if asset_ids:
            self.env.cr.execute(
                "SELECT id FROM eh_asset WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(sorted(asset_ids)),),
            )
        self.invalidate_recordset()
        return self

    def _eh_prepare_action(self):
        self.ensure_one()
        if not self.env.su:
            self._eh_check_access('write')
        self._check_manager()
        self._eh_validate_references()
        self._eh_lock_for_transition()
        self._eh_validate_references()
        return self._eh_workflow_action()

    def _eh_event_date(self, value, label):
        """Return a non-backdated accounting date for a later event."""
        self.ensure_one()
        event_date = value or fields.Date.context_today(self)
        prior_dates = [self.classification_date]
        prior_dates += self.move_ids.filtered(
            lambda move: move.state == 'posted').mapped('date')
        latest = max(date for date in prior_dates if date)
        if event_date < latest:
            raise UserError(_(
                "The %(label)s date %(date)s precedes existing posted "
                "held-for-sale evidence dated %(latest)s on %(item)s. "
                "Post a current-period correction instead.",
                label=label, date=event_date, latest=latest,
                item=self.display_name))
        return event_date

    def _eh_seed_from_asset(self):
        """Set the carrying amount to the linked asset's net book value.

        The asset's net_book_value is the ledger-derived carrying amount
        (cost less accumulated depreciation, impairment and revaluation),
        so classification measures off it rather than a hand-keyed figure
        (IFRS 5.15).
        """
        self.ensure_one()
        nbv = self.currency_id.round(self.asset_id.net_book_value)
        self.with_context(eh_hfs_internal=True).write(
            {'carrying_amount': nbv})

    def _eh_cease_asset_depreciation(self):
        """Pause the linked asset via its own API so the monthly cron
        skips it while it is held for sale (IFRS 5.25).

        Only a running asset is paused; an asset already paused is left as
        is but not stamped, so a later resume does not reactivate an asset
        the user had deliberately paused. A disposed or fully depreciated
        asset cannot be classified.
        """
        self.ensure_one()
        asset = self.asset_id
        if asset.state == 'running':
            asset.action_pause()
            self.with_context(eh_hfs_internal=True).write({
                'asset_paused_by_hfs': True,
            })
        elif asset.state == 'paused':
            self.with_context(eh_hfs_internal=True).write({
                'asset_paused_by_hfs': False,
            })
        else:
            raise UserError(_(
                "Asset %s cannot be classified as held for sale from state "
                "'%s'; only a running or paused asset can be held for sale.",
                asset.display_name, asset.state))

    def _eh_assert_asset_reclassification_ready(self):
        """Fail closed when a legacy linked classification has no IFRS 5.38
        presentation entry. Standalone records need no such entry."""
        self.ensure_one()
        if self.asset_id and (
                self.asset_reclassification_quarantined
                or not self.asset_reclassification_move_id):
            raise UserError(_(
                "Linked asset %(asset)s has no reviewed IFRS 5.38 "
                "reclassification evidence. Enter a current-period "
                "correction date and basis, then use Remediate IFRS 5.38 "
                "before remeasuring, selling or ceasing classification.",
                asset=self.asset_id.display_name,
            ))
        return True

    def _eh_asset_impairment_by_account(self):
        """Return net posted asset impairment grouped by actual contra.

        This mirrors the asset disposal engine's basis so the classification
        entry clears precisely the same gross/contra presentation that will
        later be restored and derecognised.
        """
        self.ensure_one()
        result = {}
        asset = self.asset_id
        for impairment in asset.impairment_ids.filtered(
                lambda event: event.state == 'posted'):
            account = (
                impairment.accumulated_account_id
                or asset.accumulated_depreciation_account_id
            )
            amount = -impairment.amount if impairment.is_reversal \
                else impairment.amount
            result[account] = result.get(account, 0.0) + amount
        return result

    def _eh_post_asset_reclassification(self, event_date):
        """Move gross PPE and its contras to one net HFS presentation.

        The move changes presentation only; it does not change the asset
        subledger carrying amount. Exact source balances are retained in the
        sealed lines and later reversed byte-for-byte on sale/declassification.
        """
        self.ensure_one()
        if not self.asset_id:
            return self.env['account.move']
        if self.asset_reclassification_move_id:
            raise UserError(_(
                "IFRS 5.38 reclassification is already posted for %s.",
                self.display_name,
            ))
        asset = self.asset_id
        asset._validate_posting_setup()
        if self.asset_account_id == asset.asset_account_id:
            raise UserError(_(
                "Held-for-sale account %(hfs)s must differ from the linked "
                "asset's PPE cost account %(ppe)s so IFRS 5.38 separate "
                "presentation is visible.",
                hfs=self.asset_account_id.display_name,
                ppe=asset.asset_account_id.display_name,
            ))
        currency = self.currency_id
        gross = currency.round(
            asset.acquisition_cost + asset.revaluation_adjustment)
        accumulated = currency.round(asset.total_depreciated)
        impairment_by_account = self._eh_asset_impairment_by_account()
        net_impairment = currency.round(sum(impairment_by_account.values()))
        carrying = currency.round(gross - accumulated - net_impairment)
        if currency.compare_amounts(carrying, asset.net_book_value) != 0:
            raise UserError(_(
                "The linked asset's gross cost and posted contra evidence do "
                "not reconcile to its subledger carrying amount; IFRS 5.38 "
                "reclassification was not posted."))
        if carrying < 0:
            raise UserError(_(
                "A linked asset with a negative carrying amount cannot be "
                "classified as held for sale."))

        legs = []
        if carrying:
            legs.append((
                self.asset_account_id, carrying, 0.0,
                _("IFRS 5.38 net asset held for sale %s", self.name),
            ))
        if accumulated:
            legs.append((
                asset.accumulated_depreciation_account_id,
                accumulated if accumulated > 0 else 0.0,
                -accumulated if accumulated < 0 else 0.0,
                _("IFRS 5.38 clear accumulated depreciation %s", self.name),
            ))
        for account, raw_amount in impairment_by_account.items():
            amount = currency.round(raw_amount)
            if amount:
                legs.append((
                    account,
                    amount if amount > 0 else 0.0,
                    -amount if amount < 0 else 0.0,
                    _("IFRS 5.38 clear accumulated impairment %s", self.name),
                ))
        if gross:
            legs.append((
                asset.asset_account_id,
                -gross if gross < 0 else 0.0,
                gross if gross > 0 else 0.0,
                _("IFRS 5.38 reclassify gross PPE %s", self.name),
            ))
        if not legs:
            raise UserError(_(
                "The linked asset has no carrying presentation to "
                "reclassify."))
        move = self._post_move(legs, date=event_date)
        self.with_context(eh_hfs_internal=True).write({
            'asset_reclassification_move_id': move.id,
            'asset_reclassification_quarantined': False,
            'asset_reclassification_quarantine_note': False,
        })
        return move

    def _eh_reverse_asset_reclassification(self, event_date):
        """Restore the exact pre-HFS presentation before exit/disposal."""
        self.ensure_one()
        source = self.asset_reclassification_move_id
        if not source:
            return self.env['account.move']
        if self.asset_reclassification_reversal_move_id:
            raise UserError(_(
                "The IFRS 5.38 reclassification has already been reversed."))
        if source.state != 'posted' or source.eh_held_for_sale_id != self:
            raise UserError(_(
                "IFRS 5.38 source evidence is not an exact posted entry "
                "owned by this held-for-sale record."))
        legs = [(
            line.account_id,
            line.credit,
            line.debit,
            _("Reverse IFRS 5.38 presentation %s", self.name),
        ) for line in source.line_ids if line.debit or line.credit]
        reversal = self._post_move(legs, date=event_date)
        self.with_context(eh_hfs_internal=True).write({
            'asset_reclassification_reversal_move_id': reversal.id,
        })
        return reversal

    def _eh_transfer_hfs_asset_impairment(self, event_date):
        """Move remaining HFS impairment to the normal asset contra on exit.

        A reversal plus equal new charge leaves P&L and net book value
        unchanged while preserving the immutable posted events. The asset
        disposal engine will subsequently clear the new contra account rather
        than reopening the HFS presentation.
        """
        self.ensure_one()
        asset = self.asset_id
        hfs_events = asset.impairment_ids.filtered(lambda event: (
            event.state == 'posted'
            and event.move_id.eh_held_for_sale_id == self
            and (event.accumulated_account_id
                 or asset.accumulated_depreciation_account_id)
                == self.asset_account_id
        ))
        amount = self.currency_id.round(sum(
            -event.amount if event.is_reversal else event.amount
            for event in hfs_events
        ))
        if not amount:
            return self.env['account.move']
        normal_contra = asset.accumulated_depreciation_account_id
        if not normal_contra or normal_contra == self.asset_account_id:
            raise UserError(_(
                "Configure a normal accumulated depreciation/impairment "
                "account distinct from the held-for-sale account before "
                "ceasing classification."))
        self._eh_post_asset_impairment(
            amount, is_reversal=True, event_date=event_date,
            accumulated_account=self.asset_account_id,
            reason=_("Transfer HFS impairment back to PPE on %s", self.name),
        )
        charge = self._eh_post_asset_impairment(
            amount, is_reversal=False, event_date=event_date,
            accumulated_account=normal_contra,
            reason=_("Restore PPE impairment presentation on %s", self.name),
        )
        return charge.move_id

    def _eh_post_asset_impairment(
            self, amount, is_reversal, event_date=None,
            accumulated_account=None, reason=None):
        """Post a write-down (or reversal) on the linked asset through its
        own eh.asset.impairment engine, sharing the held-for-sale accounts.

        Routing the write-down through the asset engine reduces the eh.asset
        net book value by the same amount, so the asset subledger and the
        held-for-sale carrying amount stay reconciled after classification and
        remeasurement (IFRS 5.15). The impairment record is fed the
        held-for-sale accounts (impairment expense as the P&L leg, the
        held-for-sale asset account as the contra) so the journal entry legs
        are the same accounts the standalone path would have used. The move it
        posts is tagged back to this record so the cumulative-write-down cap
        (IFRS 5.22), the move count and the entries view all still see it.
        """
        self.ensure_one()
        amount = self.currency_id.round(amount)
        impairment = self.env['eh.asset.impairment'].create({
            'asset_id': self.asset_id.id,
            'impairment_date': event_date or self.classification_date,
            'amount': amount,
            'is_reversal': is_reversal,
            'reason': reason or _(
                "Held-for-sale remeasurement (IFRS 5.15) %s", self.name),
            'impairment_account_id': self.impairment_account_id.id,
            'accumulated_account_id': (
                accumulated_account or self.asset_account_id).id,
            'journal_id': self.journal_id.id,
        })
        impairment.with_context(
            eh_hfs_asset_transition_capability=
                _HFS_ASSET_TRANSITION_CAPABILITY,
        ).action_post()
        impairment.move_id.with_context(
            eh_hfs_move_link_capability=_HFS_MOVE_LINK_CAPABILITY,
        )._eh_write_sealed_metadata(
            {'eh_held_for_sale_id': self.id},
            {'eh_held_for_sale_id'},
        )
        return impairment

    def _eh_dispose_linked_asset(self, proceeds, sale_date):
        """Dispose the linked eh.asset through its own dispose engine.

        Reverses the asset's gross cost, accumulated depreciation and
        accumulated impairment, books the proceeds and the gain/loss on the
        asset's disposal accounts, and moves the asset to 'disposed'. The eh
        manager group implies account.group_account_manager, so the wizard's
        own manager gate is satisfied; sudo() lets it run in one call.
        """
        self.ensure_one()
        if proceeds > 0 and not self.proceeds_account_id:
            raise UserError(_(
                "Configure a proceeds account on %s before selling a linked "
                "asset.", self.display_name))
        vals = {
            'asset_id': self.asset_id.id,
            'disposal_date': sale_date,
            'proceeds': proceeds,
        }
        if self.proceeds_account_id:
            vals['cash_account_id'] = self.proceeds_account_id.id
        self.env['eh.asset.dispose.wizard'].with_context(
            eh_hfs_asset_transition_capability=
                _HFS_ASSET_TRANSITION_CAPABILITY,
        ).sudo().create(vals).action_dispose()
        disposal_move = self.asset_id.disposal_move_id
        if not disposal_move or disposal_move.state != 'posted':
            raise UserError(_(
                "The linked asset disposal did not produce posted journal "
                "evidence; the held-for-sale sale was rolled back."))
        disposal_move.with_context(
            eh_hfs_move_link_capability=_HFS_MOVE_LINK_CAPABILITY,
        )._eh_write_sealed_metadata(
            {'eh_held_for_sale_id': self.id},
            {'eh_held_for_sale_id'},
        )
        return disposal_move

    def _eh_resume_asset(self, resume_date):
        """Resume a linked asset that this record paused on classification.

        Guarded by asset_paused_by_hfs so a cease-to-be-classified event or
        cancellation only reactivates an asset we paused ourselves, and only
        while it is still paused (IFRS 5.26 resumes depreciation once the
        asset no longer meets the held-for-sale criteria).
        """
        self.ensure_one()
        if (
            self.asset_paused_by_hfs
            and self.asset_id.state == 'paused'
            and not self.asset_id._eh_has_exclusive_accounting_owner(
                exclude_hfs_item=self)
        ):
            self.asset_id._eh_resume_after_hfs(resume_date)
        self.with_context(eh_hfs_internal=True).write({
            'asset_paused_by_hfs': False,
        })

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can post held-for-sale "
                "entries."))

    def _validate_accounts(self, needed):
        self.ensure_one()
        field_map = {
            'asset': ('asset_account_id', _("asset account")),
            'impairment': ('impairment_account_id', _("impairment account")),
            'proceeds': ('proceeds_account_id', _("proceeds account")),
            'gain_loss': ('gain_loss_account_id', _("gain / loss account")),
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

    def _post_move(self, legs, date=None):
        lines = [(0, 0, {
            'name': label, 'account_id': account.id,
            'debit': debit, 'credit': credit,
        }) for account, debit, credit, label in legs]
        move = self.env['account.move'].with_context(
            eh_hfs_move_link_capability=_HFS_MOVE_LINK_CAPABILITY,
        )._eh_create_sealed({
            'move_type': 'entry',
            'company_id': self.company_id.id,
            'date': date or self.classification_date,
            'journal_id': self.journal_id.id,
            'ref': self.name,
            'eh_held_for_sale_id': self.id,
            'eh_sealed': True,
            'line_ids': lines,
        })
        move.action_post()
        return move


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_held_for_sale_id = fields.Many2one(
        'eh.held.for.sale', string="Held for Sale", readonly=True,
        index=True, ondelete='restrict', copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        if any(vals.get('eh_held_for_sale_id') for vals in vals_list) and not (
            self.env.context.get('eh_hfs_move_link_capability')
            is _HFS_MOVE_LINK_CAPABILITY
        ):
            raise AccessError(_(
                "Held-for-sale journal provenance is engine-owned."))
        return super().create(vals_list)

    def write(self, vals):
        if 'eh_held_for_sale_id' in vals and not (
            self.env.context.get('eh_hfs_move_link_capability')
            is _HFS_MOVE_LINK_CAPABILITY
        ):
            raise AccessError(_(
                "Held-for-sale journal provenance is immutable."))
        return super().write(vals)

    def button_draft(self):
        scoped = self.line_ids._eh_discontinued_scoped_lines()
        if scoped:
            raise UserError(_(
                "A move containing frozen discontinued-operation journal "
                "items cannot be reset/cancelled in place. Post a reversal; "
                "the reversal inherits the exact operation scope."
            ))
        return super().button_draft()

    def _reverse_moves(self, default_values_list=None, cancel=False):
        scoped_self = self.with_context(**{
            _DISCONTINUED_LINE_CONTEXT_KEY: _DISCONTINUED_LINE_CAPABILITY,
        })
        return super(AccountMove, scoped_self)._reverse_moves(
            default_values_list=default_values_list, cancel=cancel,
        )


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    eh_discontinued_hfs_ids = fields.Many2many(
        'eh.held.for.sale', 'eh_hfs_disc_evidence_move_line_rel',
        'line_id', 'item_id', readonly=True, copy=True,
    )
    eh_discontinued_group_ids = fields.Many2many(
        'eh.disposal.group', 'eh_dg_disc_evidence_move_line_rel',
        'line_id', 'group_id', readonly=True, copy=True,
    )

    @api.private
    def _eh_discontinued_scoped_lines(self):
        """Return lines carrying frozen discontinued-operation evidence.

        Core accountants can create and synchronise ordinary journal items
        without access to the EH held-for-sale models.  Reading either
        evidence relation in their environment raises before this invariant
        guard can determine that an ordinary line has no evidence at all.
        Query only relation-table ownership; no held-for-sale/group record or
        field value is exposed.  Return matching line ids in caller
        environment so every subsequent write/unlink still runs with caller
        ACLs and record rules.
        """
        if not self:
            return self
        self.env.cr.execute("""
            SELECT line_id
              FROM eh_hfs_disc_evidence_move_line_rel
             WHERE line_id IN %s
            UNION
            SELECT line_id
              FROM eh_dg_disc_evidence_move_line_rel
             WHERE line_id IN %s
        """, (tuple(self.ids), tuple(self.ids)))
        scoped_ids = [row[0] for row in self.env.cr.fetchall()]
        return self.browse(scoped_ids)

    @api.model_create_multi
    def create(self, vals_list):
        evidence_fields = {
            'eh_discontinued_hfs_ids', 'eh_discontinued_group_ids',
        }
        if any(
                any(vals.get(field_name) for field_name in evidence_fields)
                for vals in vals_list) \
                and self.env.context.get(_DISCONTINUED_LINE_CONTEXT_KEY) \
                is not _DISCONTINUED_LINE_CAPABILITY:
            raise AccessError(_(
                "Discontinued-operation journal-item scope is engine-owned."
            ))
        return super().create(vals_list)

    def write(self, vals):
        evidence_fields = {
            'eh_discontinued_hfs_ids', 'eh_discontinued_group_ids',
        }
        if evidence_fields.intersection(vals) \
                and self.env.context.get(_DISCONTINUED_LINE_CONTEXT_KEY) \
                is not _DISCONTINUED_LINE_CAPABILITY:
            raise AccessError(_(
                "Discontinued-operation journal-item scope is immutable."
            ))
        material = {
            'move_id', 'account_id', 'date', 'debit', 'credit', 'balance',
            'amount_currency', 'currency_id', 'company_id', 'display_type',
        }
        if (material.intersection(vals)
                and self._eh_discontinued_scoped_lines()):
            raise UserError(_(
                "A frozen discontinued-operation journal item cannot be "
                "edited. Reverse its posted move to retain both entries."
            ))
        return super().write(vals)

    def unlink(self):
        if self._eh_discontinued_scoped_lines():
            raise UserError(_(
                "A frozen discontinued-operation journal item cannot be "
                "deleted. Reverse its posted move instead."
            ))
        return super().unlink()


class EhAsset(models.Model):
    """Make an active IFRS 5 classification the exclusive asset owner."""

    _inherit = 'eh.asset'

    def _eh_post_depreciation_through(self, event_date):
        """Do not restart depreciation inside a sanctioned IFRS 5 sale.

        The core disposal engine normally accrues IAS 16 depreciation through
        disposal. IFRS 5.25 is the explicit exception: an actively held asset
        stopped depreciating on classification. The unforgeable transition
        capability distinguishes the HFS-owned disposal path from an ordinary
        asset sale.
        """
        if (
            self.env.context.get('eh_hfs_asset_transition_capability')
            is _HFS_ASSET_TRANSITION_CAPABILITY
            and self._eh_hfs_owned_asset_ids()
        ):
            return self.env['eh.asset.depreciation.line']
        return super()._eh_post_depreciation_through(event_date)

    def _eh_accrue_depreciation_for_hfs(self, classification_date):
        """Post earned depreciation through the exact IFRS 5 event date.

        A running asset must not be paused at the prior month-end balance when
        classification occurs mid-period. The asset engine posts full due rows
        and an immutable event-date stub. A classification cannot be backdated
        behind depreciation that is already posted.
        """
        for asset in self:
            if asset.state != 'running':
                continue
            future_posted = asset.depreciation_line_ids.filtered(
                lambda line: line.is_posted
                and line.depreciation_date > classification_date)
            if future_posted:
                raise UserError(_(
                    "Held-for-sale classification on %(date)s precedes posted "
                    "depreciation evidence for %(asset)s dated %(latest)s. "
                    "Use a current-period classification or reverse the later "
                    "accounting evidence first.",
                    date=classification_date,
                    asset=asset.display_name,
                    latest=max(future_posted.mapped('depreciation_date')),
                ))
            asset._eh_post_depreciation_through(classification_date)
            asset.invalidate_recordset([
                'net_book_value', 'total_depreciated',
                'depreciation_line_ids',
            ])
        return True

    def _eh_resume_after_hfs(self, resume_date):
        """Resume prospectively, with no accumulated past-due forecast rows."""
        for asset in self:
            future = asset.depreciation_line_ids.filtered(
                lambda line: not line.is_posted)
            remaining_periods = len(future)
            if future:
                future.sudo().unlink()
            asset.invalidate_recordset([
                'net_book_value', 'total_depreciated',
                'depreciation_line_ids',
            ])
            asset.with_context(
                eh_hfs_asset_transition_capability=
                    _HFS_ASSET_TRANSITION_CAPABILITY,
            ).action_resume()
            if remaining_periods:
                asset._build_remaining_schedule(
                    remaining_periods, start_date=resume_date)
        return True

    def _eh_hfs_owner_records(self, exclude_item=False, exclude_group=False):
        self.ensure_one()
        Item = self.env['eh.held.for.sale'].sudo()
        Line = self.env['eh.disposal.group.line'].sudo()
        item_domain = [('asset_id', '=', self.id), ('state', '=', 'held')]
        if exclude_item:
            item_domain.append(('id', '!=', exclude_item.id))
        line_domain = [
            ('asset_id', '=', self.id), ('group_id.state', '=', 'held'),
        ]
        if exclude_group:
            line_domain.append(('group_id', '!=', exclude_group.id))
        return Item.search(item_domain), Line.search(line_domain)

    def _eh_hfs_owned_asset_ids(self):
        """Resolve active single/group HFS ownership in two batched queries."""
        asset_ids = set(self.ids)
        if not asset_ids:
            return set()
        items = self.env['eh.held.for.sale'].sudo().search([
            ('asset_id', 'in', tuple(asset_ids)), ('state', '=', 'held'),
        ])
        lines = self.env['eh.disposal.group.line'].sudo().search([
            ('asset_id', 'in', tuple(asset_ids)),
            ('group_id.state', '=', 'held'),
        ])
        return set(items.mapped('asset_id').ids) | set(
            lines.mapped('asset_id').ids)

    def _eh_ip_owner_records(self):
        self.ensure_one()
        if 'eh.investment.property' not in self.env:
            return False
        return self.env['eh.investment.property'].sudo().search([
            ('transfer_in_asset_id', '=', self.id),
            ('transfer_log_ids.direction', '=', 'in'),
        ])

    def _eh_has_exclusive_accounting_owner(
            self, exclude_hfs_item=False, exclude_hfs_group=False):
        self.ensure_one()
        items, lines = self._eh_hfs_owner_records(
            exclude_item=exclude_hfs_item,
            exclude_group=exclude_hfs_group,
        )
        return bool(items or lines or self._eh_ip_owner_records())

    def _eh_assert_available_for_hfs_claim(self):
        for asset in self:
            if asset.state not in ('running', 'paused'):
                raise UserError(_(
                    "Only a running or paused asset can be classified as "
                    "held for sale; %(asset)s is %(state)s.",
                    asset=asset.display_name, state=asset.state))
            if asset.lvp_pool_id:
                raise UserError(_(
                    "Asset %(asset)s belongs to low-value pool %(pool)s. "
                    "Its carrying amount is controlled at pool level and "
                    "cannot be classified individually as held for sale.",
                    asset=asset.display_name,
                    pool=asset.lvp_pool_id.display_name))
            if asset._eh_has_exclusive_accounting_owner():
                raise UserError(_(
                    "Asset %s is already controlled by another active "
                    "held-for-sale or investment-property workflow. Complete "
                    "or cancel that workflow before claiming it again.",
                    asset.display_name))
        return True

    def _eh_assert_not_hfs_owned(self):
        if (
            self.env.context.get('eh_hfs_asset_transition_capability')
            is _HFS_ASSET_TRANSITION_CAPABILITY
        ):
            return True
        owned_ids = self._eh_hfs_owned_asset_ids()
        for asset in self:
            if asset.id in owned_ids:
                raise UserError(_(
                    "Asset %s is controlled by an active held-for-sale "
                    "classification. Use that workflow's remeasure, cancel, "
                    "or sale action instead.", asset.display_name))
        return True

    def _eh_assert_external_master_write_allowed(self, field_names):
        result = super()._eh_assert_external_master_write_allowed(field_names)
        if (
            self.env.context.get('eh_hfs_asset_transition_capability')
            is not _HFS_ASSET_TRANSITION_CAPABILITY
        ):
            self._eh_assert_not_hfs_owned()
        return result

    def action_resume(self):
        if not self.env.su:
            self._eh_check_access('write')
        self._eh_lock_for_transition()
        self._eh_assert_not_hfs_owned()
        return super().action_resume()

    def action_set_to_draft(self):
        if not self.env.su:
            self._eh_check_access('write')
        self._eh_lock_for_transition()
        self._eh_assert_not_hfs_owned()
        return super().action_set_to_draft()


class EhAssetDepreciationLine(models.Model):
    _inherit = 'eh.asset.depreciation.line'

    def action_post(self):
        assets = self.mapped('asset_id')
        if not self.env.su:
            self._eh_check_access('write')
            assets._eh_check_access('write')
        assets._eh_lock_for_transition()
        assets._eh_assert_not_hfs_owned()
        return super().action_post()


class EhAssetImpairment(models.Model):
    _inherit = 'eh.asset.impairment'

    def action_post(self):
        assets = self.mapped('asset_id')
        if not self.env.su:
            self._eh_check_access('write')
            assets._eh_check_access('write')
        assets._eh_lock_for_transition()
        assets._eh_assert_not_hfs_owned()
        return super().action_post()

    def action_cancel(self):
        owned = self.filtered(
            lambda rec: rec.move_id and rec.move_id.eh_held_for_sale_id)
        if owned:
            raise UserError(_(
                "This impairment was posted by held-for-sale item %s and "
                "cannot be cancelled from the asset register. Remeasure or "
                "correct the owning held-for-sale workflow instead.",
                ', '.join(owned.move_id.eh_held_for_sale_id
                          .mapped('display_name'))))
        assets = self.mapped('asset_id')
        if not self.env.su:
            self._eh_check_access('write')
            assets._eh_check_access('write')
        assets._eh_lock_for_transition()
        assets._eh_assert_not_hfs_owned()
        return super().action_cancel()


class EhAssetRevalueWizard(models.TransientModel):
    _inherit = 'eh.asset.revalue.wizard'

    def action_revalue(self):
        self.ensure_one()
        self.asset_id._eh_check_access('write')
        self.asset_id._eh_lock_for_transition()
        self.asset_id._eh_assert_not_hfs_owned()
        return super().action_revalue()


class EhAssetDisposeWizard(models.TransientModel):
    _inherit = 'eh.asset.dispose.wizard'

    def action_dispose(self):
        self.ensure_one()
        self.asset_id._eh_check_access('write')
        self.asset_id._eh_lock_for_transition()
        self.asset_id._eh_assert_not_hfs_owned()
        return super().action_dispose()
