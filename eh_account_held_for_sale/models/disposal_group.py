# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.disposal.group: a disposal group held for sale (IFRS 5).

A disposal group is a set of assets to be disposed of together in a single
transaction, plus the liabilities directly associated with those assets that
will be transferred with them (IFRS 5 Appendix A). Measurement happens at
GROUP level: on classification the group is remeasured to the lower of its
aggregate carrying amount and its fair value less costs to sell, and any
write-down is allocated to the members in the IFRS 5.23 order (which applies
IAS 36.104-105):

* first against any goodwill members;
* then pro rata, by carrying amount, over the members inside the IFRS 5
  measurement scope;
* never below a member's own fair-value floor when one is recorded (a
  member is not written below the highest of its fair value less costs of
  disposal and zero, IAS 36.105); the excess re-prorates over the
  remaining scope members;
* never to members OUTSIDE the measurement scope (financial assets,
  inventories carried at NRV, deferred tax assets, IFRS 5.5): flag those
  lines out of scope and they receive no allocation;
* never to liability members (they are derecognised on sale, not written
  down).

If every scope member reaches its floor before the shortfall is fully
allocated, the residual stays unallocated per IAS 36.105 (it is logged on
the record; no member is forced below its floor).

Members can reference a fixed asset (eh.asset). On classification such a
member seeds its carrying amount from the asset's ledger-derived net book
value, the asset is paused so depreciation ceases (IFRS 5.25), and the
member's share of any write-down is recorded against the asset through an
eh.asset.impairment row attached to the single group journal entry, so the
asset subledger and the group stay reconciled (IFRS 5.15).

A subsequent rise in fair value less costs to sell reverses previous
write-downs, capped at the cumulative write-down recognised on the group's
non-goodwill members (IFRS 5.22; goodwill write-downs are never reversed,
IAS 36.124).

12-month rule (IFRS 5.9): the sale is expected to complete within one year
of classification. This module is disclose-first by design: a held group
past its classification anniversary is flagged (overdue_12m) in the list
and the filter, but never auto-declassified; the extension flag records
the IFRS 5.9 exception (delay caused by events beyond the entity's
control while it remains committed to the plan).

Discontinued operations reporting service (IFRS 5.33): a manager seals exact
posted P&L journal-item identities for a group or single held-for-sale record.
The reporting API also includes P&L lines from the record's own sealed
remeasurement and disposal moves automatically. It exposes a pre-tax amount and
an explicitly tax-adjusted result; it does not build a financial-statement
layout or mutate chart-account tags. The contracts exposed here are:

    env['eh.disposal.group'].eh_discontinued_pl_amount(
        period_from, period_to, company=company)

which returns the pre-tax posted P&L total (credit minus debit, profit positive),
and ``eh_discontinued_result(..., tax_effect=amount)`` which returns separate
pre-tax, tax-effect, and post-tax values.
"""

import math

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, MissingError, UserError, ValidationError

from odoo.addons.eh_account_assets_pro.models.accounting_integrity import (
    _eh_record_matches_company,
)

from .held_for_sale import _HFS_ASSET_TRANSITION_CAPABILITY

_FROZEN_STATES = frozenset({'held', 'sold', 'cancelled'})
_GROUP_FROZEN_INPUT_FIELDS = frozenset({
    'company_id', 'classification_date', 'asset_account_id',
    'impairment_account_id', 'proceeds_account_id',
    'gain_loss_account_id', 'journal_id',
})
_GROUP_TERMINAL_INPUT_FIELDS = frozenset({
    'fair_value_less_costs', 'remeasurement_date', 'sale_date', 'proceeds',
    'declassification_date', 'declassification_basis',
})
_DISCONTINUED_EVIDENCE_FIELDS = frozenset({
    'is_discontinued',
})
_DG_MOVE_LINK_CAPABILITY = object()
_P_AND_L_ACCOUNT_TYPES = (
    'income', 'income_other', 'expense',
    'expense_depreciation', 'expense_direct_cost',
)

# Member measurement fields frozen once the group leaves draft: the group
# write-down was allocated over these values and posted, so re-shaping the
# membership afterwards would desync the lines from the ledger. The group's
# own actions write through the 'eh_dg_internal' context flag.
_LINE_MEASURE_FIELDS = frozenset({
    'carrying_amount', 'is_liability', 'is_goodwill', 'in_scope',
    'name', 'fair_value_floor', 'asset_id', 'group_id',
    'account_id', 'pl_account_id',
})
# Allocation stamps are engine output at every state, never user input.
_LINE_INTERNAL_FIELDS = frozenset({
    'allocated_writedown', 'cumulative_writedown',
    'asset_paused_by_group', 'company_id', 'currency_id',
    'preclassification_carrying_amount', 'declassification_target_amount',
})
_LINE_DECLASS_INPUT_FIELDS = frozenset({
    'declassification_adjusted_carrying_amount',
    'declassification_recoverable_amount',
    'declassification_measurement_completed',
})


class EhDisposalGroup(models.Model):
    _name = 'eh.disposal.group'
    _description = "Disposal group held for sale (IFRS 5)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'classification_date desc, id desc'
    _rec_name = 'name'

    # State is a control point: without this guard a plain user could
    # RPC-write it straight to 'held'/'sold', skipping action_classify /
    # action_sell and the single allocated journal entry. Only the group's
    # guarded workflow-action capability may change it.
    _eh_guarded_fields = (
        'state', 'currency_id', 'depreciation_ceased',
        'carrying_amount', 'writedown', 'cumulative_writedown',
        'discontinued_tag_id', 'discontinued_evidence_account_ids',
        'discontinued_evidence_move_line_ids',
        'discontinued_scope_quarantined', 'discontinued_scope_quarantine_note',
        'declassification_move_ids', 'declassified_at',
        'declassified_by_id',
        'declassification_evidence_quarantined',
        'declassification_evidence_quarantine_note',
        'asset_reclassification_move_id',
        'asset_reclassification_reversal_move_id',
        'asset_reclassification_quarantined',
        'asset_reclassification_quarantine_note',
    )
    _eh_create_guarded_fields = _eh_guarded_fields

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    description = fields.Char(
        help="The disposal group, e.g. 'Retail division - Northgate "
             "cluster'.")
    state = fields.Selection(
        [('draft', "Draft"), ('held', "Held for sale"),
         ('sold', "Sold"), ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True,
        copy=False)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    classification_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)
    remeasurement_date = fields.Date(
        string="Remeasurement Date", copy=False, tracking=True,
        help="Accounting date for the next disposal-group remeasurement. "
             "When empty, the action uses today's date.")
    sale_date = fields.Date(
        string="Sale Date", copy=False, tracking=True,
        help="Accounting date for the group disposal. When empty, the sale "
             "action uses today's date.")
    declassification_date = fields.Date(
        string="Cease-classification Date", copy=False,
        help="Date the disposal group ceased meeting the IFRS 5 criteria.",
    )
    declassification_basis = fields.Text(
        string="Cease-classification Measurement Basis", copy=False,
        help=(
            "Required group-level evidence supporting each member's IFRS "
            "5.27 hypothetical adjusted carrying and recoverable amount."
        ),
    )
    declassification_move_ids = fields.Many2many(
        'account.move', 'eh_dg_declassification_move_rel',
        'group_id', 'move_id', readonly=True, copy=False,
        string="Cease-classification Entries", check_company=True,
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
    asset_reclassification_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
        check_company=True,
        help=(
            "Sealed IFRS 5.38 entry that moved every linked member's gross "
            "cost and accumulated contras out of PPE and presented each net "
            "carrying amount in the disposal group's held-for-sale account."
        ),
    )
    asset_reclassification_reversal_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
        check_company=True,
        help=(
            "Sealed exact reversal of the group's IFRS 5.38 presentation "
            "entry on sale or cease-classification."
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
            "Current-period accounting date for manager-reviewed IFRS 5.38 "
            "correction of a legacy linked disposal group."
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
    extension_12m = fields.Boolean(
        string="12-Month Extension (IFRS 5.9)", tracking=True,
        help="The sale has not completed within one year of classification "
             "because of events or circumstances beyond the entity's "
             "control, and the entity remains committed to its plan to "
             "sell. Setting this suppresses the overdue flag; the group is "
             "never auto-declassified (disclose-first design).")
    overdue_12m = fields.Boolean(
        string="Overdue (12 Months)",
        compute='_compute_overdue_12m', search='_search_overdue_12m',
        help="The group has been held for sale for more than twelve months "
             "without the IFRS 5.9 extension. Flag only: review whether "
             "the held-for-sale criteria are still met and either record "
             "the extension or cease classification.")
    is_discontinued = fields.Boolean(
        string="Discontinued operation", tracking=True,
        help="Scopes the operation for the module's IFRS 5.33 reporting API. "
             "Seal exact posted P&L journal items; the module does not build "
             "or modify a financial-statement layout.")
    discontinued_tag_id = fields.Many2one(
        'account.account.tag', readonly=True, copy=False,
        string="Discontinued Operations Scope Marker",
        help="Per-company audit marker proving that an exact journal-item "
             "scope was sealed. It is not attached to chart accounts and "
             "does not imply a financial-statements integration.")
    discontinued_evidence_account_ids = fields.Many2many(
        'account.account', 'eh_dg_disc_evidence_account_rel',
        'group_id', 'account_id', string="Frozen Discontinued P&L Scope",
        readonly=True, copy=False,
        help="Server-owned account summary derived from the exact approved "
             "journal items. Journal-item identity, not account metadata, is "
             "the authoritative reporting scope.")
    discontinued_candidate_move_line_ids = fields.Many2many(
        'account.move.line', 'eh_dg_disc_candidate_move_line_rel',
        'group_id', 'line_id', string="Candidate Discontinued Journal Items",
        copy=False,
        domain="[('company_id', '=', company_id), "
               "('parent_state', '=', 'posted'), "
               "('account_id.account_type', 'in', "
               "['income', 'income_other', 'expense', "
               "'expense_depreciation', 'expense_direct_cost'])]",
        help=(
            "Exact posted P&L journal items belonging to this discontinued "
            "operation. Shared accounts are never treated as sufficient "
            "operation identity."
        ),
    )
    discontinued_evidence_move_line_ids = fields.Many2many(
        'account.move.line', 'eh_dg_disc_evidence_move_line_rel',
        'group_id', 'line_id', string="Frozen Discontinued Journal Items",
        readonly=True, copy=False,
    )
    discontinued_scope_quarantined = fields.Boolean(
        readonly=True, copy=False, index=True,
    )
    discontinued_scope_quarantine_note = fields.Text(readonly=True, copy=False)
    depreciation_ceased = fields.Boolean(
        readonly=True, copy=False,
        help="Depreciation stops on every member while the group is held "
             "for sale (IFRS 5.25).")

    line_ids = fields.One2many(
        'eh.disposal.group.line', 'group_id', string="Members", copy=True)

    carrying_amount = fields.Monetary(
        compute='_compute_carrying_amount', store=True,
        currency_field='currency_id',
        help="Aggregate carrying amount of the group: asset members less "
             "directly associated liability members. Derived from the "
             "member lines; asset-linked lines are seeded from the asset's "
             "net book value on classification.")
    fair_value_less_costs = fields.Monetary(
        string="Fair Value Less Costs to Sell",
        currency_field='currency_id', required=True, tracking=True,
        help="Fair value less costs to sell of the disposal group as a "
             "whole (IFRS 5.15 measures the group, not its members).")
    writedown = fields.Monetary(
        compute='_compute_writedown', store=True,
        currency_field='currency_id',
        help="Excess of the group carrying amount over its fair value "
             "less costs to sell.")
    cumulative_writedown = fields.Monetary(
        compute='_compute_cumulative_writedown',
        currency_field='currency_id',
        help="Net write-down currently recognised across the member lines "
             "(including goodwill). Reversals are capped at the "
             "non-goodwill portion (IFRS 5.22, IAS 36.124).")
    proceeds = fields.Monetary(
        currency_field='currency_id',
        help="Net proceeds received on sale of the group.")

    # ---- accounts ----
    asset_account_id = fields.Many2one(
        'account.account', string="Asset Account (Fallback)", tracking=True,
        help="Balance-sheet account used for a member line that does not "
             "set its own member account.")
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

    move_ids = fields.One2many('account.move', 'eh_disposal_group_id')
    move_count = fields.Integer(compute='_compute_move_count')

    notes = fields.Text()

    # ---- computes ----

    @api.depends('line_ids.carrying_amount', 'line_ids.is_liability')
    def _compute_carrying_amount(self):
        for group in self:
            group.carrying_amount = group.currency_id.round(sum(
                -line.carrying_amount if line.is_liability
                else line.carrying_amount
                for line in group.line_ids))

    @api.depends('carrying_amount', 'fair_value_less_costs')
    def _compute_writedown(self):
        for group in self:
            group.writedown = max(
                group.carrying_amount - group.fair_value_less_costs, 0.0)

    @api.depends('line_ids.cumulative_writedown')
    def _compute_cumulative_writedown(self):
        for group in self:
            group.cumulative_writedown = group.currency_id.round(
                sum(group.line_ids.mapped('cumulative_writedown')))

    def _compute_move_count(self):
        for group in self:
            group.move_count = len(group.move_ids)

    @api.constrains('fair_value_less_costs', 'proceeds')
    def _check_finite_group_amounts(self):
        for group in self:
            if not all(math.isfinite(value or 0.0) for value in (
                    group.fair_value_less_costs, group.proceeds)):
                raise ValidationError(_(
                    "Disposal-group measurement amounts must be finite."
                ))

    @api.depends('state', 'classification_date', 'extension_12m')
    def _compute_overdue_12m(self):
        today = fields.Date.context_today(self)
        for group in self:
            group.overdue_12m = bool(
                group.state == 'held'
                and not group.extension_12m
                and group.classification_date
                and group.classification_date + relativedelta(years=1)
                < today)

    def _search_overdue_12m(self, operator, value):
        # Overdue iff held, no extension, and the classification anniversary
        # has passed: classification_date + 1 year < today, i.e.
        # classification_date < today - 1 year.
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

    # ---- ORM guards (mirroring the single-asset flow) ----

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._eh_assert_company_allowed(
                vals.get('company_id') or self.env.company.id)
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.disposal.group') or '/'
        groups = super().create(vals_list)
        groups._eh_validate_references()
        return groups

    def write(self, vals):
        if vals and not self.env.su:
            self._eh_check_access('write')
        self._eh_validate_references(vals)
        self._eh_lock_for_transition()
        self._eh_validate_references(vals)
        if not (self.env.su and self.env.context.get('eh_dg_internal')):
            frozen = self.filtered(
                lambda group: group.state in _FROZEN_STATES)
            touched = sorted(_GROUP_FROZEN_INPUT_FIELDS.intersection(vals))
            if frozen and touched:
                raise UserError(_(
                    "Classified disposal-group identity and posting fields "
                    "(%(fields)s) are immutable. Record a new classification "
                    "or correction instead of rewriting ledger evidence.",
                    fields=', '.join(touched)))
            terminal = self.filtered(
                lambda group: group.state in ('sold', 'cancelled'))
            terminal_touched = sorted(
                _GROUP_TERMINAL_INPUT_FIELDS.intersection(vals))
            if terminal and terminal_touched:
                raise UserError(_(
                    "Completed disposal-group event inputs (%(fields)s) are "
                    "immutable. Record an explicit correction instead.",
                    fields=', '.join(terminal_touched)))
            tagged = self.filtered('discontinued_tag_id')
            if tagged and _DISCONTINUED_EVIDENCE_FIELDS.intersection(vals):
                raise UserError(_(
                    "Discontinued-operation status is frozen after member "
                    "accounts are tagged."))
        # The state of a held / sold group is a control point: a raw ORM
        # state write moving OUT of a frozen state without the sanctioned-
        # transition flag must be manager-gated, so a plain user cannot
        # un-freeze a GL-backed group. The sanctioned actions (sell /
        # cancel) set the flag after their own manager check; classify
        # moves INTO 'held' and is unaffected.
        if 'state' in vals and not (
                self.env.su and self.env.context.get('eh_dg_state_change')):
            crossing = self.filtered(
                lambda group: group.state in _FROZEN_STATES
                and group.state != vals['state'])
            if crossing:
                crossing._check_manager()
        result = super().write(vals)
        self._eh_validate_references()
        return result

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        posted = self.filtered(
            lambda group: group.state != 'draft' or group.move_ids)
        if posted:
            raise UserError(_(
                "A disposal group that has been classified or sold cannot "
                "be deleted; it carries a posted GL movement. Cancel it "
                "(EH Accounting Manager only) instead."))
        return super().unlink()

    # ---- actions ----

    def action_classify(self):
        """Classify the group as held for sale (IFRS 5.15, 5.23, 5.25).

        Seeds asset-linked members from their ledger net book value,
        pauses those assets (depreciation ceases on every member), then
        remeasures the group to the lower of carrying amount and fair
        value less costs to sell. A shortfall posts ONE journal entry with
        a per-member impairment leg pair, allocated goodwill-first then
        pro rata over the scope members, floor-capped (IFRS 5.23 applying
        IAS 36.104-105).
        """
        self.ensure_one()
        self = self._eh_prepare_action()
        if self.state != 'draft':
            raise UserError(_(
                "Only a draft disposal group can be classified as held "
                "for sale."))
        if not self.line_ids:
            raise UserError(_(
                "Add at least one member line to %s before classifying "
                "it.", self.display_name))
        if not self.line_ids.filtered(lambda line: not line.is_liability):
            raise UserError(_(
                "A disposal group needs at least one asset member; %s "
                "only lists liabilities.", self.display_name))
        self._validate_accounts(['impairment'])
        self._validate_member_accounts()
        currency = self.currency_id
        asset_lines = self.line_ids.filtered('asset_id')
        asset_lines.mapped('asset_id')._eh_assert_available_for_hfs_claim()
        asset_lines.mapped('asset_id')._eh_accrue_depreciation_for_hfs(
            self.classification_date)
        for line in asset_lines:
            line._eh_seed_from_asset()
            line._eh_cease_asset_depreciation()
        for line in self.line_ids:
            line.with_context(eh_dg_internal=True).write({
                'preclassification_carrying_amount': line.carrying_amount,
            })
        if asset_lines:
            self._eh_post_asset_reclassification(self.classification_date)
        loss = currency.round(self.writedown)
        if loss > 0:
            plan, allocated, unallocated = self._eh_allocation_plan(loss)
            if plan:
                self._eh_post_writedown(plan, is_reversal=False)
            if unallocated > 0.005:
                # IAS 36.105: no member is written below its floor; the
                # residual shortfall stays unallocated. Disclose-first.
                self.message_post(body=_(
                    "Write-down of %(loss).2f could only be allocated for "
                    "%(alloc).2f: every in-scope member reached its "
                    "fair-value floor (IAS 36.105). The residual "
                    "%(residual).2f is not allocated and the group carries "
                    "above its fair value less costs to sell; review the "
                    "member floors and scope flags.",
                    loss=loss, alloc=allocated, residual=unallocated))
        self.write({'state': 'held', 'depreciation_ceased': True})
        return True

    def action_remeasure(self):
        """Subsequent group remeasurement (IFRS 5.15, 5.21-22).

        A fall in fair value less costs to sell posts a further allocated
        write-down. A rise reverses previous write-downs pro rata to current
        carrying amounts under IAS 36.122, capped at each non-goodwill
        member's cumulative write-down (IFRS 5.22; goodwill write-downs are
        never reversed, IAS 36.124).
        """
        self.ensure_one()
        # A subsequent remeasurement of an asset-linked member stamps the
        # asset's own eh.asset.impairment.state (a guarded field on
        # eh.workflow.guard) through _eh_attach_asset_impairment. Elevate the
        # action first so that sanctioned write runs under env.su and passes
        # the guard, mirroring action_classify / action_sell; without this a
        # real (non-superuser) manager hits AccessError and the whole
        # remeasurement rolls back (IFRS 5.21-22 cannot be booked).
        self = self._eh_prepare_action()
        if self.state != 'held':
            raise UserError(_(
                "Only a disposal group held for sale can be remeasured."))
        self._eh_assert_asset_reclassification_ready()
        self._validate_accounts(['impairment'])
        self._validate_member_accounts()
        currency = self.currency_id
        event_date = self._eh_event_date(
            self.remeasurement_date, _("remeasurement"))
        carrying = currency.round(self.carrying_amount)
        fvlcts = currency.round(self.fair_value_less_costs)
        delta = currency.round(fvlcts - carrying)
        if not delta:
            raise UserError(_(
                "The group carrying amount already equals fair value less "
                "costs to sell; there is nothing to remeasure on %s.",
                self.name))
        if delta < 0:
            plan, allocated, unallocated = self._eh_allocation_plan(-delta)
            if not plan:
                raise UserError(_(
                    "No further write-down can be allocated on %s: every "
                    "in-scope member is at its fair-value floor "
                    "(IAS 36.105).", self.name))
            self._eh_post_writedown(
                plan, is_reversal=False, event_date=event_date)
            if unallocated > 0.005:
                self.message_post(body=_(
                    "Remeasurement write-down allocated %(alloc).2f of "
                    "%(loss).2f; the residual %(residual).2f is blocked by "
                    "member fair-value floors (IAS 36.105).",
                    alloc=allocated, loss=-delta, residual=unallocated))
        else:
            plan, total = self._eh_reversal_plan(delta)
            if not plan:
                raise UserError(_(
                    "The reversal is fully limited on %s: a gain may not "
                    "exceed the cumulative write-down previously "
                    "recognised (IFRS 5.22), and goodwill write-downs are "
                    "never reversed (IAS 36.124).", self.name))
            self._eh_post_writedown(
                plan, is_reversal=True, event_date=event_date)
        self.with_context(eh_dg_internal=True).write({
            'remeasurement_date': event_date,
        })
        return True

    def action_sell(self):
        self.ensure_one()
        self = self._eh_prepare_action()
        if self.state != 'held':
            raise UserError(_(
                "Only a disposal group held for sale can be sold."))
        self._eh_assert_asset_reclassification_ready()
        self._validate_accounts(['proceeds', 'gain_loss'])
        self._validate_member_accounts()
        currency = self.currency_id
        proceeds = currency.round(self.proceeds)
        sale_date = self._eh_event_date(self.sale_date, _("sale"))
        # Asset-linked members are derecognised through the asset engine. The
        # purchaser takes both the cash consideration and the directly
        # associated liabilities, so allocate gross disposal consideration
        # (cash proceeds + liabilities transferred) over asset carrying amounts.
        # The residual group entry offsets the liability consideration through
        # the proceeds account. This preserves one exact group gain/loss while
        # giving every linked disposal move its consistent share and provenance.
        asset_members = self.line_ids.filtered(
            lambda l: l.asset_id and not l.is_liability)
        if asset_members:
            self._eh_reverse_asset_reclassification(sale_date)
        asset_carry = currency.round(sum(
            currency.round(l.carrying_amount)
            for l in self.line_ids.filtered(lambda l: not l.is_liability)))
        liability_carry = currency.round(sum(
            currency.round(l.carrying_amount)
            for l in self.line_ids.filtered('is_liability')))
        gross_consideration = currency.round(proceeds + liability_carry)
        remaining_proceeds = proceeds
        for line in asset_members:
            share = currency.round(
                gross_consideration
                * currency.round(line.carrying_amount)
                / asset_carry
            ) if asset_carry else 0.0
            line._eh_dispose_member_asset(share, sale_date)
            remaining_proceeds = currency.round(remaining_proceeds - share)

        rest = self.line_ids - asset_members
        legs = []
        if remaining_proceeds > 0:
            legs.append((self.proceeds_account_id, remaining_proceeds, 0.0,
                         _("Net disposal proceeds %s", self.name)))
        elif remaining_proceeds < 0:
            legs.append((self.proceeds_account_id, 0.0, -remaining_proceeds,
                         _("Liability consideration allocation %s", self.name)))
        rest_carry = 0.0
        for line in rest.sorted('id'):
            amount = currency.round(line.carrying_amount)
            if not amount:
                continue
            account = line._eh_member_account()
            if line.is_liability:
                # Derecognise the directly associated liability.
                legs.append((account, amount, 0.0,
                             _("Derecognise liability %s", line._eh_label())))
                rest_carry -= amount
            else:
                legs.append((account, 0.0, amount,
                             _("Derecognise asset %s", line._eh_label())))
                rest_carry += amount
        gain_loss = currency.round(remaining_proceeds - rest_carry)
        if gain_loss > 0:
            legs.append((self.gain_loss_account_id, 0.0, gain_loss,
                         _("Gain on disposal %s", self.name)))
        elif gain_loss < 0:
            legs.append((self.gain_loss_account_id, -gain_loss, 0.0,
                         _("Loss on disposal %s", self.name)))
        if legs:
            self._post_move(legs, date=sale_date)
        for line in self.line_ids:
            line.with_context(eh_dg_internal=True).write(
                {'carrying_amount': 0.0})
        self.with_context(
            eh_dg_internal=True, eh_dg_state_change=True,
        ).write({'state': 'sold', 'sale_date': sale_date})
        return True

    def action_cancel(self):
        for original in self:
            group = original._eh_prepare_action()
            if group.state == 'sold':
                raise UserError(_(
                    "A sold disposal group cannot be cancelled."))
            if group.state == 'draft':
                group.with_context(
                    eh_dg_internal=True, eh_dg_state_change=True,
                ).write({
                    'state': 'cancelled', 'depreciation_ceased': False,
                })
                continue
            group._eh_assert_asset_reclassification_ready()
            if not (group.declassification_basis or '').strip():
                raise UserError(_(
                    "Document the IFRS 5.27 member measurement basis before "
                    "ceasing classification on %s.", group.display_name,
                ))
            event_date = group._eh_event_date(
                group.declassification_date, _("cease-classification"),
            )
            currency = group.currency_id
            incomplete = group.line_ids.filtered(
                lambda line: (
                    not line.is_liability
                    and not line.declassification_measurement_completed
                )
            )
            if incomplete:
                raise UserError(_(
                    "Complete and attest both IFRS 5.27 measurements for "
                    "every asset member before ceasing classification. "
                    "Missing: %s",
                    ', '.join(line._eh_label() for line in incomplete),
                ))
            downward = {}
            upward = {}
            targets = {}
            for line in group.line_ids.sorted('id'):
                if line.is_liability:
                    targets[line] = currency.round(line.carrying_amount)
                    continue
                adjusted = (
                    line.declassification_adjusted_carrying_amount or 0.0
                )
                recoverable = line.declassification_recoverable_amount or 0.0
                if not all(math.isfinite(value) and value >= 0 for value in (
                        adjusted, recoverable)):
                    raise UserError(_(
                        "IFRS 5.27 amounts on member %s must be finite and "
                        "non-negative.", line._eh_label(),
                    ))
                target = currency.round(min(adjusted, recoverable))
                targets[line] = target
                delta = currency.round(target - line.carrying_amount)
                if delta < 0:
                    downward[line] = -delta
                elif delta > 0:
                    if line.is_goodwill:
                        raise UserError(_(
                            "Goodwill write-downs cannot be reversed when a "
                            "disposal group ceases classification (IAS 36.124)."
                        ))
                    if currency.compare_amounts(
                            delta, line.cumulative_writedown) > 0:
                        raise UserError(_(
                            "Member %(member)s needs an uplift of %(delta).2f "
                            "but has only %(cap).2f of HFS write-down evidence. "
                            "The inputs would reverse unrelated history.",
                            member=line._eh_label(), delta=delta,
                            cap=line.cumulative_writedown,
                        ))
                    upward[line] = delta
            group._validate_accounts(['impairment'])
            group._validate_member_accounts()
            moves = self.env['account.move']
            if downward:
                moves |= group._eh_post_writedown(
                    downward, is_reversal=False, event_date=event_date,
                )
            if upward:
                moves |= group._eh_post_writedown(
                    upward, is_reversal=True, event_date=event_date,
                )
            linked_lines = group.line_ids.filtered('asset_id')
            if linked_lines:
                moves |= group._eh_reverse_asset_reclassification(event_date)
            for line, target in targets.items():
                line.with_context(eh_dg_internal=True).write({
                    'declassification_target_amount': target,
                    'carrying_amount': target,
                })
                if line.asset_id:
                    moves |= group._eh_transfer_hfs_asset_impairment(
                        line, event_date,
                    )
                    line.asset_id.invalidate_recordset(['net_book_value'])
                    if currency.compare_amounts(
                            line.asset_id.net_book_value, target) != 0:
                        raise UserError(_(
                            "Linked asset %(asset)s does not tie to its IFRS "
                            "5.27 target; the transaction was rolled back.",
                            asset=line.asset_id.display_name,
                        ))
                    line._eh_resume_asset(event_date)
            group.with_context(
                eh_dg_internal=True, eh_dg_state_change=True).write(
                {
                    'state': 'cancelled',
                    'depreciation_ceased': False,
                    'declassification_date': event_date,
                    'declassification_move_ids': [(6, 0, moves.ids)],
                    'declassified_at': fields.Datetime.now(),
                    'declassified_by_id': self.env.user.id,
                    'declassification_evidence_quarantined': False,
                    'declassification_evidence_quarantine_note': False,
                })
        return True

    def action_remediate_asset_reclassification(self):
        """Post a reviewed current-period IFRS 5.38 group correction."""
        self.ensure_one()
        self = self._eh_prepare_action()
        if self.state != 'held' or not self.line_ids.filtered('asset_id'):
            raise UserError(_(
                "Only an active disposal group with linked assets can be "
                "remediated for IFRS 5.38 presentation."))
        if self.asset_reclassification_move_id:
            raise UserError(_(
                "The disposal group already has sealed IFRS 5.38 "
                "reclassification evidence."))
        if not (
            self.asset_reclassification_correction_basis or ''
        ).strip():
            raise UserError(_(
                "Document the current-period IFRS 5.38 correction basis and "
                "workpaper reference first."))
        event_date = self._eh_event_date(
            self.asset_reclassification_correction_date,
            _("IFRS 5.38 correction"),
        )
        move = self._eh_post_asset_reclassification(event_date)
        self.with_context(eh_dg_internal=True).write({
            'asset_reclassification_correction_date': event_date,
            'asset_reclassification_quarantined': False,
            'asset_reclassification_quarantine_note': False,
        })
        self.message_post(body=_(
            "Legacy disposal-group IFRS 5.38 presentation remediated by "
            "sealed current-period entry %(move)s on %(date)s. Basis: "
            "%(basis)s",
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
            or line.account_id.account_type not in _P_AND_L_ACCOUNT_TYPES
            or line.eh_discontinued_hfs_ids
            or (line.eh_discontinued_group_ids
                and line.eh_discontinued_group_ids != self)
        ))
        if invalid:
            raise UserError(_(
                "Discontinued scope must contain unclaimed posted P&L lines "
                "in the same company. Current-period and comparative-period "
                "journal items may predate classification; reporting applies "
                "the requested reporting dates. Invalid journal items: %s.",
                ', '.join(map(str, invalid.ids)),
            ))
        evidence = self.discontinued_evidence_move_line_ids | candidates
        accounts = (
            self.discontinued_evidence_account_ids
            | evidence.mapped('account_id')
        )
        tag = self._eh_discontinued_tag(self.company_id, create=True)
        self.with_context(eh_dg_internal=True).write({
            'discontinued_tag_id': tag.id,
            'discontinued_evidence_account_ids': [(6, 0, accounts.ids)],
            'discontinued_evidence_move_line_ids': [(6, 0, evidence.ids)],
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
            'domain': [('eh_disposal_group_id', '=', self.id)],
        }

    # ---- exact-scope discontinued-operations reporting service ----

    @api.model
    def _eh_discontinued_tag(self, company, create=False):
        """Find or create the company's immutable scope-seal marker.

        The account-tag model supplies a stable audited identifier, but the
        marker is deliberately not attached to accounts: exact journal-item
        identity is the only reporting scope.
        """
        Tag = self.env['account.account.tag'].sudo()
        linked = self.sudo().search([
            ('company_id', '=', company.id),
            ('discontinued_tag_id', '!=', False),
        ]).mapped('discontinued_tag_id')
        linked |= self.env['eh.held.for.sale'].sudo().search([
            ('company_id', '=', company.id),
            ('discontinued_tag_id', '!=', False),
        ]).mapped('discontinued_tag_id')
        tag = linked.sorted('id')[:1]
        if not tag and create:
            name = "EH Discontinued Operations [company:%s]" % company.id
            tag = Tag.search([
                ('name', '=', name), ('applicability', '=', 'accounts'),
                ('country_id', '=', False)], limit=1)
        if not tag and create:
            tag = Tag.create({'name': name, 'applicability': 'accounts'})
        return tag

    @api.model
    def _eh_discontinued_scope_lines(
            self, period_from, period_to, company=None):
        """Return exact manual and engine-owned P&L evidence for a period."""
        period_from = fields.Date.to_date(period_from)
        period_to = fields.Date.to_date(period_to)
        company = company or self.env.company
        if isinstance(company, int):
            company = self.env['res.company'].browse(company)
        if not period_from or not period_to or period_from > period_to:
            raise ValidationError(_(
                "Discontinued-operation reporting requires a valid inclusive "
                "date range."))
        if not self.env.su:
            company._eh_check_access('read')
            if company not in self.env.companies:
                raise AccessError(_(
                    "Discontinued-operation reporting is limited to active "
                    "companies."
                ))
        groups = self.sudo().search([
            ('company_id', '=', company.id),
            ('is_discontinued', '=', True),
        ])
        items = self.env['eh.held.for.sale'].sudo().search([
            ('company_id', '=', company.id),
            ('is_discontinued', '=', True),
        ])
        sealed_groups = groups.filtered('discontinued_tag_id')
        sealed_items = items.filtered('discontinued_tag_id')
        lines = sealed_groups.mapped('discontinued_evidence_move_line_ids')
        lines |= sealed_items.mapped('discontinued_evidence_move_line_ids')
        Line = self.env['account.move.line'].sudo()
        if groups:
            lines |= Line.search([
                ('move_id.eh_disposal_group_id', 'in', groups.ids),
            ])
        if items:
            lines |= Line.search([
                ('move_id.eh_held_for_sale_id', 'in', items.ids),
            ])
        lines = lines.filtered(lambda line: (
            line.parent_state == 'posted'
            and line.company_id == company
            and period_from <= line.date <= period_to
            and line.account_id.account_type in _P_AND_L_ACCOUNT_TYPES
        ))
        # The exact immutable journal-item identity is authoritative. Never
        # broaden back to every line on a shared P&L account: that would mix
        # continuing operations and pre-classification activity into IFRS
        # 5.33 presentation.
        if not lines:
            return lines
        lines.sudo(False)._eh_check_access('read')
        return self.env['account.move.line'].sudo().browse(lines.ids).filtered(
            lambda line: line.parent_state == 'posted'
        )

    @api.model
    def eh_discontinued_pl_amount(self, period_from, period_to, company=None):
        """Return the pre-tax discontinued-operation result, profit positive.

        This compatibility API is explicitly pre-tax. Use
        :meth:`eh_discontinued_result` when an IFRS 5.33(a) post-tax amount is
        required.
        """
        company = company or self.env.company
        if isinstance(company, int):
            company = self.env['res.company'].browse(company)
        lines = self._eh_discontinued_scope_lines(
            period_from, period_to, company=company)
        return company.currency_id.round(
            sum(lines.mapped('credit')) - sum(lines.mapped('debit')))

    @api.model
    def eh_discontinued_result(
            self, period_from, period_to, company=None, tax_effect=0.0):
        """Return separate pre-tax, tax-effect and post-tax IFRS 5.33 values.

        ``tax_effect`` is an explicitly measured income-tax expense (positive)
        or benefit (negative) for the same operation and period. The module does
        not infer tax from account names or rates. Its own remeasurement and
        disposal P&L lines are included automatically through sealed move
        provenance.
        """
        company = company or self.env.company
        if isinstance(company, int):
            company = self.env['res.company'].browse(company)
        if not isinstance(tax_effect, (int, float)) or not math.isfinite(
                tax_effect):
            raise ValidationError(_(
                "The discontinued-operation tax effect must be a finite "
                "company-currency amount."))
        pre_tax = self.eh_discontinued_pl_amount(
            period_from, period_to, company=company)
        tax_effect = company.currency_id.round(tax_effect)
        return {
            'pre_tax': pre_tax,
            'tax_effect': tax_effect,
            'post_tax': company.currency_id.round(pre_tax - tax_effect),
        }

    # ---- allocation engine (IFRS 5.23 / IAS 36.104-105) ----

    def _eh_prorate_capped(self, amount, members):
        """Allocate ``amount`` pro rata by weight over ``members``, capped.

        :param members: list of (line, weight, cap) tuples, in line-id
            order, weight > 0, cap the most that member may absorb.
        :return: ({line: allocated}, total_allocated). Deterministic:
            a member whose pro-rata share exceeds its cap takes the cap
            and drops out, and the excess re-prorates over the remaining
            members (IAS 36.105 reallocation); rounding residue (cents)
            lands on the first member with headroom.
        """
        currency = self.currency_id
        alloc = {}
        remaining = currency.round(amount)
        active = [(line, weight, currency.round(cap))
                  for (line, weight, cap) in members
                  if weight > 0 and currency.round(cap) > 0]
        # Cap-and-reallocate: whenever a share would breach a cap, that
        # member takes exactly its cap and the rest re-prorates.
        capped = True
        while active and remaining > 0.005 and capped:
            capped = False
            total_weight = sum(weight for _line, weight, _cap in active)
            for line, weight, cap in active:
                if remaining * weight / total_weight > cap + 0.005:
                    alloc[line] = cap
                    remaining = currency.round(remaining - cap)
                    active = [t for t in active if t[0] != line]
                    capped = True
                    break
        # Final pro-rata pass over the survivors, exact to the cent.
        if active and remaining > 0.005:
            total_weight = sum(weight for _line, weight, _cap in active)
            shares = {}
            for line, weight, cap in active:
                shares[line] = min(
                    currency.round(remaining * weight / total_weight), cap)
            residue = currency.round(remaining - sum(shares.values()))
            for line, _weight, cap in active:
                if abs(residue) < 0.005:
                    break
                if residue > 0:
                    room = currency.round(cap - shares[line])
                    step = min(residue, room)
                    if step > 0:
                        shares[line] = currency.round(shares[line] + step)
                        residue = currency.round(residue - step)
                else:
                    step = min(-residue, shares[line])
                    if step > 0:
                        shares[line] = currency.round(shares[line] - step)
                        residue = currency.round(residue + step)
            alloc.update({line: value
                          for line, value in shares.items() if value})
        return alloc, currency.round(sum(alloc.values()))

    def _eh_allocation_plan(self, loss):
        """IFRS 5.23 write-down allocation: goodwill first, then pro rata
        over in-scope non-goodwill members, floor-capped per member.

        :return: (plan {line: amount}, allocated, unallocated).
        """
        self.ensure_one()
        currency = self.currency_id
        scope = self.line_ids.filtered(
            lambda line: line.in_scope and not line.is_liability)
        goodwill = scope.filtered('is_goodwill').sorted('id')
        others = (scope - goodwill).sorted('id')

        def pool(lines):
            members = []
            for line in lines:
                cap = currency.round(
                    line.carrying_amount - max(line.fair_value_floor, 0.0))
                if line.carrying_amount > 0 and cap > 0:
                    members.append((line, line.carrying_amount, cap))
            return members

        plan = {}
        remaining = currency.round(loss)
        for members in (pool(goodwill), pool(others)):
            if remaining <= 0.005 or not members:
                continue
            alloc, total = self._eh_prorate_capped(remaining, members)
            for line, amount in alloc.items():
                plan[line] = currency.round(plan.get(line, 0.0) + amount)
            remaining = currency.round(remaining - total)
        allocated = currency.round(loss - remaining)
        return plan, allocated, remaining

    def _eh_reversal_plan(self, gain):
        """IFRS 5.22 reversal on IAS 36.122 carrying-amount weights.

        Eligible non-goodwill assets are weighted by current carrying amount,
        while each member remains capped at its own cumulative write-down.
        Goodwill is excluded entirely (IAS 36.124).

        :return: (plan {line: amount}, total).
        """
        self.ensure_one()
        currency = self.currency_id
        eligible = self.line_ids.filtered(
            lambda line: line.in_scope and not line.is_liability
            and not line.is_goodwill
            and line.cumulative_writedown > 0.005).sorted('id')
        members = [(line, line.carrying_amount,
                    line.cumulative_writedown) for line in eligible]
        capacity = currency.round(
            sum(line.cumulative_writedown for line in eligible))
        amount = min(currency.round(gain), capacity)
        if amount <= 0:
            return {}, 0.0
        return self._eh_prorate_capped(amount, members)

    # ---- linked-asset presentation and posting helpers ----

    def _eh_assert_asset_reclassification_ready(self):
        self.ensure_one()
        if self.line_ids.filtered('asset_id') and (
            self.asset_reclassification_quarantined
            or not self.asset_reclassification_move_id
        ):
            raise UserError(_(
                "Disposal group %(group)s has linked assets but no reviewed "
                "IFRS 5.38 reclassification evidence. Enter a current-period "
                "correction date and basis, then use Remediate IFRS 5.38 "
                "before remeasuring, selling or ceasing classification.",
                group=self.display_name,
            ))
        return True

    @api.model
    def _eh_asset_impairment_by_account(self, asset):
        result = {}
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
        """Present linked group members net in HFS, preserving gross detail."""
        self.ensure_one()
        asset_lines = self.line_ids.filtered('asset_id').sorted('id')
        if not asset_lines:
            return self.env['account.move']
        if self.asset_reclassification_move_id:
            raise UserError(_(
                "IFRS 5.38 reclassification is already posted for %s.",
                self.display_name,
            ))
        currency = self.currency_id
        legs = []
        for line in asset_lines:
            asset = line.asset_id
            asset._validate_posting_setup()
            hfs_account = line._eh_member_account()
            if hfs_account == asset.asset_account_id:
                raise UserError(_(
                    "Held-for-sale member account %(hfs)s for %(asset)s must "
                    "differ from the PPE cost account so IFRS 5.38 separate "
                    "presentation is visible.",
                    hfs=hfs_account.display_name,
                    asset=asset.display_name,
                ))
            gross = currency.round(
                asset.acquisition_cost + asset.revaluation_adjustment)
            accumulated = currency.round(asset.total_depreciated)
            impairment_by_account = self._eh_asset_impairment_by_account(asset)
            net_impairment = currency.round(
                sum(impairment_by_account.values()))
            carrying = currency.round(gross - accumulated - net_impairment)
            if currency.compare_amounts(carrying, asset.net_book_value) != 0:
                raise UserError(_(
                    "Linked asset %(asset)s does not reconcile from gross "
                    "cost and posted contra evidence to its subledger "
                    "carrying amount; IFRS 5.38 reclassification was not "
                    "posted.",
                    asset=asset.display_name,
                ))
            if carrying < 0:
                raise UserError(_(
                    "Linked asset %s has a negative carrying amount and "
                    "cannot be classified in a disposal group.",
                    asset.display_name,
                ))
            label = line._eh_label()
            if carrying:
                legs.append((
                    hfs_account, carrying, 0.0,
                    _("IFRS 5.38 net asset held for sale %s", label),
                ))
            if accumulated:
                legs.append((
                    asset.accumulated_depreciation_account_id,
                    accumulated if accumulated > 0 else 0.0,
                    -accumulated if accumulated < 0 else 0.0,
                    _("IFRS 5.38 clear accumulated depreciation %s", label),
                ))
            for account, raw_amount in impairment_by_account.items():
                amount = currency.round(raw_amount)
                if amount:
                    legs.append((
                        account,
                        amount if amount > 0 else 0.0,
                        -amount if amount < 0 else 0.0,
                        _("IFRS 5.38 clear accumulated impairment %s", label),
                    ))
            if gross:
                legs.append((
                    asset.asset_account_id,
                    -gross if gross < 0 else 0.0,
                    gross if gross > 0 else 0.0,
                    _("IFRS 5.38 reclassify gross PPE %s", label),
                ))
        if not legs:
            raise UserError(_(
                "The linked disposal-group assets have no carrying "
                "presentation to reclassify."))
        move = self._post_move(legs, date=event_date)
        self.with_context(eh_dg_internal=True).write({
            'asset_reclassification_move_id': move.id,
            'asset_reclassification_quarantined': False,
            'asset_reclassification_quarantine_note': False,
        })
        return move

    def _eh_reverse_asset_reclassification(self, event_date):
        self.ensure_one()
        source = self.asset_reclassification_move_id
        if not source:
            return self.env['account.move']
        if self.asset_reclassification_reversal_move_id:
            raise UserError(_(
                "The disposal group's IFRS 5.38 reclassification has already "
                "been reversed."))
        if source.state != 'posted' or source.eh_disposal_group_id != self:
            raise UserError(_(
                "IFRS 5.38 source evidence is not an exact posted entry "
                "owned by this disposal group."))
        legs = [(
            line.account_id,
            line.credit,
            line.debit,
            _("Reverse IFRS 5.38 group presentation %s", self.name),
        ) for line in source.line_ids if line.debit or line.credit]
        reversal = self._post_move(legs, date=event_date)
        self.with_context(eh_dg_internal=True).write({
            'asset_reclassification_reversal_move_id': reversal.id,
        })
        return reversal

    def _eh_post_asset_impairment_for_line(
            self, line, amount, is_reversal, event_date,
            accumulated_account, reason):
        self.ensure_one()
        impairment = self.env['eh.asset.impairment'].create({
            'asset_id': line.asset_id.id,
            'impairment_date': event_date,
            'amount': self.currency_id.round(amount),
            'is_reversal': is_reversal,
            'reason': reason,
            'impairment_account_id': self.impairment_account_id.id,
            'accumulated_account_id': accumulated_account.id,
            'journal_id': self.journal_id.id,
        })
        impairment.with_context(
            eh_hfs_asset_transition_capability=
                _HFS_ASSET_TRANSITION_CAPABILITY,
        ).action_post()
        impairment.move_id.with_context(
            eh_dg_move_link_capability=_DG_MOVE_LINK_CAPABILITY,
        )._eh_write_sealed_metadata(
            {'eh_disposal_group_id': self.id},
            {'eh_disposal_group_id'},
        )
        return impairment

    def _eh_transfer_hfs_asset_impairment(self, line, event_date):
        """Move one exiting member's remaining HFS contra back to PPE."""
        self.ensure_one()
        asset = line.asset_id
        hfs_account = line._eh_member_account()
        hfs_events = asset.impairment_ids.filtered(lambda event: (
            event.state == 'posted'
            and event.move_id.eh_disposal_group_id == self
            and (event.accumulated_account_id
                 or asset.accumulated_depreciation_account_id) == hfs_account
        ))
        amount = self.currency_id.round(sum(
            -event.amount if event.is_reversal else event.amount
            for event in hfs_events
        ))
        if not amount:
            return self.env['account.move']
        normal_contra = asset.accumulated_depreciation_account_id
        if not normal_contra or normal_contra == hfs_account:
            raise UserError(_(
                "Configure a normal accumulated depreciation/impairment "
                "account distinct from held-for-sale member account %(hfs)s "
                "before ceasing classification for %(asset)s.",
                hfs=hfs_account.display_name,
                asset=asset.display_name,
            ))
        reversal = self._eh_post_asset_impairment_for_line(
            line, amount, True, event_date, hfs_account,
            _("Transfer HFS impairment back to PPE on %s", line._eh_label()),
        )
        charge = self._eh_post_asset_impairment_for_line(
            line, amount, False, event_date, normal_contra,
            _("Restore PPE impairment presentation on %s", line._eh_label()),
        )
        return reversal.move_id | charge.move_id

    def _eh_post_writedown(self, plan, is_reversal, event_date=None):
        """Post ONE journal entry with a per-member leg pair for the
        allocated write-down (or its reversal), stamp the lines, and
        record the event on each linked asset's impairment subledger."""
        self.ensure_one()
        currency = self.currency_id
        legs = []
        for line in sorted(plan, key=lambda l: l.id):
            amount = plan[line]
            account = line._eh_member_account()
            if is_reversal:
                label = _("Held-for-sale write-down reversal %s",
                          line._eh_label())
                legs.append((account, amount, 0.0, label))
                legs.append((self.impairment_account_id, 0.0, amount, label))
            else:
                label = _("Held-for-sale write-down %s", line._eh_label())
                legs.append((self.impairment_account_id, amount, 0.0, label))
                legs.append((account, 0.0, amount, label))
        move = self._post_move(legs, date=event_date)
        sign = -1 if is_reversal else 1
        for line, amount in plan.items():
            line.with_context(eh_dg_internal=True).write({
                'carrying_amount': currency.round(
                    line.carrying_amount - sign * amount),
                'allocated_writedown': sign * amount,
                'cumulative_writedown': currency.round(
                    line.cumulative_writedown + sign * amount),
            })
            if line.asset_id:
                self._eh_attach_asset_impairment(
                    line, amount, is_reversal, move,
                    event_date=event_date)
        return move

    def _eh_attach_asset_impairment(
            self, line, amount, is_reversal, move, event_date=None):
        """Record a member's allocated write-down (or reversal) on the
        linked asset's own impairment subledger, attached to the single
        group journal entry.

        The single-asset flow routes a write-down through
        eh.asset.impairment.action_post, which posts its own entry. A
        group posts ONE entry for all members, whose legs for this member
        (impairment expense against the member account) are exactly what
        the engine would have posted, so here the impairment row is
        created in draft (its IAS 36 validation constraints run) and then
        marked posted against the group move instead of producing a
        second, duplicate entry. The asset's net book value falls by the
        allocated amount and the subledgers stay reconciled (IFRS 5.15);
        the remaining schedule is re-amortised per IAS 36.63 like every
        other impairment event.
        """
        self.ensure_one()
        impairment = self.env['eh.asset.impairment'].create({
            'asset_id': line.asset_id.id,
            'impairment_date': event_date or self.classification_date,
            'amount': self.currency_id.round(amount),
            'is_reversal': is_reversal,
            'reason': _(
                "Held-for-sale group remeasurement (IFRS 5.15, 5.23) %s",
                self.name),
            'impairment_account_id': self.impairment_account_id.id,
            'accumulated_account_id': line._eh_member_account().id,
            'journal_id': self.journal_id.id,
        })
        impairment._eh_validate_hfs_attachment()
        impairment.write({
            'state': 'posted',
            'move_id': move.id,
            'posted_at': fields.Datetime.now(),
            'posted_by_id': self.env.user.id,
        })
        line.asset_id._eh_rebuild_after_impairment()
        return impairment

    def _post_move(self, legs, date=None):
        lines = [(0, 0, {
            'name': label, 'account_id': account.id,
            'debit': debit, 'credit': credit,
        }) for account, debit, credit, label in legs]
        move = self.env['account.move'].with_context(
            eh_dg_move_link_capability=_DG_MOVE_LINK_CAPABILITY,
        )._eh_create_sealed({
            'move_type': 'entry',
            'company_id': self.company_id.id,
            'date': date or self.classification_date,
            'journal_id': self.journal_id.id,
            'ref': self.name,
            'eh_disposal_group_id': self.id,
            'eh_sealed': True,
            'line_ids': lines,
        })
        move.action_post()
        return move

    # ---- validation helpers ----

    @api.model
    def _eh_assert_company_allowed(self, company_id):
        if (
            not self.env.su
            and company_id
            and int(company_id) not in self.env.companies.ids
        ):
            raise AccessError(_(
                "Disposal groups may only use a company enabled in the "
                "caller's current company context."))

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
                "A disposal-group accounting reference is unavailable to "
                "the caller.")) from None
        if not record:
            raise AccessError(_(
                "A disposal-group accounting reference is unavailable to "
                "the caller."))
        return record

    def _eh_validate_references(self, vals=None):
        vals = vals or {}
        account_fields = (
            'asset_account_id', 'impairment_account_id',
            'proceeds_account_id', 'gain_loss_account_id',
        )
        for group in self:
            company_id = vals.get('company_id', group.company_id.id)
            self._eh_assert_company_allowed(company_id)
            company = self._eh_resolve_reference(
                'res.company', company_id, access='read')
            journal_id = vals.get('journal_id', group.journal_id.id)
            journal = self._eh_resolve_reference(
                'account.journal', journal_id, access='read')
            if journal and journal.company_id != company:
                raise ValidationError(_(
                    "Disposal-group journal must belong to its company."))
            for field_name in account_fields:
                account_id = vals.get(field_name, group[field_name].id)
                account = self._eh_resolve_reference(
                    'account.account', account_id, access='read')
                if account and not _eh_record_matches_company(account, company):
                    raise ValidationError(_(
                        "Disposal-group posting accounts must be available "
                        "to its company."))
            for account in group.discontinued_evidence_account_ids:
                self._eh_resolve_reference(
                    'account.account', account.id, access='read')
                if not _eh_record_matches_company(account, company):
                    raise ValidationError(_(
                        "Frozen discontinued-operation accounts must be "
                        "available to the disposal-group company."))
            for move in group.declassification_move_ids:
                self._eh_resolve_reference(
                    'account.move', move.id, access='read',
                )
                if (
                    move.company_id != company
                    or move.eh_disposal_group_id != group
                ):
                    raise ValidationError(_(
                        "Cease-classification entries must be exact journal "
                        "evidence owned by this disposal group."
                    ))
            reclass_move_id = vals.get(
                'asset_reclassification_move_id',
                group.asset_reclassification_move_id.id,
            )
            reclass_move = self._eh_resolve_reference(
                'account.move', reclass_move_id, access='read',
            )
            if reclass_move and (
                    reclass_move.company_id != company
                    or reclass_move.eh_disposal_group_id != group
                    or reclass_move.state != 'posted'):
                raise ValidationError(_(
                    "IFRS 5.38 group reclassification evidence must be an "
                    "exact posted entry owned by this disposal group."
                ))
            reversal_move_id = vals.get(
                'asset_reclassification_reversal_move_id',
                group.asset_reclassification_reversal_move_id.id,
            )
            reversal_move = self._eh_resolve_reference(
                'account.move', reversal_move_id, access='read',
            )
            if reversal_move and (
                    reversal_move.company_id != company
                    or reversal_move.eh_disposal_group_id != group
                    or reversal_move.state != 'posted'
                    or reversal_move == reclass_move):
                raise ValidationError(_(
                    "IFRS 5.38 group reversal evidence must be a distinct "
                    "exact posted entry owned by this disposal group."
                ))
            for line in group.line_ids:
                line._eh_validate_references(company=company)
        return self

    def _eh_lock_for_transition(self):
        group_ids = tuple(sorted(set(self.ids)))
        line_ids = tuple(sorted(set(self.mapped('line_ids').ids)))
        asset_ids = tuple(sorted(set(
            self.mapped('line_ids.asset_id').ids)))
        self.flush_recordset()
        if group_ids:
            self.env.cr.execute(
                "SELECT id FROM eh_disposal_group WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (group_ids,),
            )
        if line_ids:
            self.env.cr.execute(
                "SELECT id FROM eh_disposal_group_line WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (line_ids,),
            )
        if asset_ids:
            self.env.cr.execute(
                "SELECT id FROM eh_asset WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (asset_ids,),
            )
        self.invalidate_recordset()
        self.mapped('line_ids').invalidate_recordset()
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
        """Return a non-backdated accounting date for a later group event."""
        self.ensure_one()
        event_date = value or fields.Date.context_today(self)
        prior_dates = [self.classification_date]
        prior_dates += self.move_ids.filtered(
            lambda move: move.state == 'posted').mapped('date')
        latest = max(date for date in prior_dates if date)
        if event_date < latest:
            raise UserError(_(
                "The %(label)s date %(date)s precedes existing posted "
                "disposal-group evidence dated %(latest)s on %(group)s. "
                "Post a current-period correction instead.",
                label=label, date=event_date, latest=latest,
                group=self.display_name))
        return event_date

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can post disposal-group "
                "entries."))

    def _validate_accounts(self, needed):
        self.ensure_one()
        field_map = {
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

    def _validate_member_accounts(self):
        self.ensure_one()
        if self.asset_account_id:
            return
        unaccounted = self.line_ids.filtered(lambda line: not line.account_id)
        if unaccounted:
            raise UserError(_(
                "Set a member account on every line of %s (or a fallback "
                "Asset Account on the group). Missing on: %s.",
                self.display_name,
                ', '.join(line._eh_label() for line in unaccounted)))


class EhDisposalGroupLine(models.Model):
    _name = 'eh.disposal.group.line'
    _description = "Disposal group member (IFRS 5)"
    _order = 'group_id, id'
    _rec_name = 'name'

    group_id = fields.Many2one(
        'eh.disposal.group', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='group_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='group_id.currency_id', store=True, readonly=True)
    group_state = fields.Selection(
        related='group_id.state', readonly=True)

    name = fields.Char(
        help="Member description, e.g. 'Northgate store fit-out'. "
             "Defaults from the linked asset.")
    asset_id = fields.Many2one(
        'eh.asset', string="Linked Asset", ondelete='restrict', copy=False,
        check_company=True,
        domain="[('state', 'in', ['running', 'paused'])]",
        help="Fixed asset this member covers. On classification the "
             "carrying amount is seeded from the asset's net book value, "
             "the asset is paused so depreciation ceases (IFRS 5.25), and "
             "any allocated write-down is recorded on the asset's own "
             "impairment subledger so the two stay reconciled (IFRS "
             "5.15). Leave blank for a free carrying entry.")
    is_liability = fields.Boolean(
        string="Liability",
        help="A liability directly associated with the assets of the "
             "group, transferred in the same transaction (IFRS 5 "
             "Appendix A). Enter the liability's carrying amount as a "
             "positive number; it reduces the group carrying amount and "
             "never receives any write-down allocation.")
    is_goodwill = fields.Boolean(
        string="Goodwill",
        help="Goodwill member: an IFRS 5.23 write-down is applied first "
             "to goodwill (IAS 36.104) and a goodwill write-down is never "
             "reversed (IAS 36.124).")
    in_scope = fields.Boolean(
        string="In Measurement Scope", default=True,
        help="Member is inside the IFRS 5 measurement scope and absorbs "
             "its pro-rata share of a group write-down. Untick for "
             "members measured under their own standard (financial "
             "assets, inventories at net realisable value, deferred tax "
             "assets, IFRS 5.5): they stay in the group's carrying amount "
             "but receive no allocation.")
    carrying_amount = fields.Monetary(
        currency_field='currency_id', required=True,
        help="Member carrying amount (always entered positive; liability "
             "members reduce the group total). Asset-linked lines are "
             "re-seeded from the asset's ledger net book value on "
             "classification.")
    preclassification_carrying_amount = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Server-owned carrying amount immediately before IFRS 5 measurement.",
    )
    declassification_adjusted_carrying_amount = fields.Monetary(
        string="Carrying Amount Had IFRS 5 Not Applied",
        currency_field='currency_id', copy=False,
        help="Member amount under IFRS 5.27(a).",
    )
    declassification_recoverable_amount = fields.Monetary(
        string="Recoverable Amount on Ceasing Classification",
        currency_field='currency_id', copy=False,
        help="Member recoverable amount under IFRS 5.27(b).",
    )
    declassification_measurement_completed = fields.Boolean(
        string="IFRS 5.27 Measurement Completed",
        copy=False,
        help=(
            "Explicit attestation that both IFRS 5.27 member amounts were "
            "measured. Zero is valid only when this evidence flag is set."
        ),
    )
    declassification_target_amount = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
    )
    fair_value_floor = fields.Monetary(
        currency_field='currency_id',
        help="Optional floor: the member is never written below the "
             "higher of this amount and zero (IAS 36.105, applied by "
             "IFRS 5.23). Any blocked excess re-prorates over the "
             "remaining scope members. Leave at zero for no floor beyond "
             "zero itself.")
    allocated_writedown = fields.Monetary(
        currency_field='currency_id', readonly=True, copy=False,
        help="Signed allocation from the most recent group measurement "
             "event (negative for a reversal). Engine output.")
    cumulative_writedown = fields.Monetary(
        currency_field='currency_id', readonly=True, copy=False,
        help="Net write-down currently recognised on this member; the "
             "member's reversal cap (IFRS 5.22). Engine output.")
    account_id = fields.Many2one(
        'account.account', string="Member Account",
        help="Balance-sheet account carrying this member (the write-down "
             "credit leg for assets; the derecognition leg on sale). "
             "Falls back to the group's fallback asset account.")
    pl_account_id = fields.Many2one(
        'account.account', string="Discontinued P&L Account",
        domain="[('account_type', 'in', ['income', 'income_other', "
               "'expense', 'expense_depreciation', 'expense_direct_cost'])]",
        help="Legacy account-level workpaper hint for this member's "
             "operations. Exact sealed journal items, not account metadata, "
             "control IFRS 5.33 reporting scope.")
    asset_paused_by_group = fields.Boolean(
        readonly=True, copy=False,
        help="Set when the group's classification paused the linked "
             "asset, so cancellation only resumes an asset the group "
             "paused itself.")

    _check_carrying = models.Constraint(
        'CHECK (carrying_amount >= 0)',
        'Member carrying amounts are entered positive; flag liabilities '
        'with the Liability toggle instead of a negative amount.')
    _check_floor = models.Constraint(
        'CHECK (fair_value_floor >= 0)',
        'The fair-value floor cannot be negative.')
    _unique_asset_per_group = models.Constraint(
        'UNIQUE (group_id, asset_id)',
        'The same asset cannot appear twice in one disposal group.')

    @api.constrains(
        'carrying_amount', 'fair_value_floor',
        'declassification_adjusted_carrying_amount',
        'declassification_recoverable_amount',
    )
    def _check_finite_amounts(self):
        for line in self:
            if not all(math.isfinite(value or 0.0) for value in (
                    line.carrying_amount, line.fair_value_floor,
                    line.declassification_adjusted_carrying_amount,
                    line.declassification_recoverable_amount)):
                raise ValidationError(_(
                    "Disposal-group member measurement amounts must be finite."
                ))

    @api.constrains('is_liability', 'is_goodwill')
    def _check_liability_not_goodwill(self):
        for line in self:
            if line.is_liability and line.is_goodwill:
                raise ValidationError(_(
                    "A member cannot be both a liability and goodwill."))

    @api.constrains('asset_id', 'group_id')
    def _check_asset_company(self):
        for line in self:
            if line.asset_id and line.group_id \
                    and line.asset_id.company_id != line.group_id.company_id:
                raise ValidationError(_(
                    "Asset %s belongs to another company than disposal "
                    "group %s.", line.asset_id.display_name,
                    line.group_id.display_name))

    @api.onchange('asset_id')
    def _onchange_asset_id(self):
        for line in self:
            if line.asset_id:
                line.carrying_amount = line.currency_id.round(
                    line.asset_id.net_book_value)
                line.is_goodwill = line.asset_id.is_goodwill
                if not line.name:
                    line.name = line.asset_id.display_name

    # ---- ORM guards ----
    # The membership of a classified group backed a posted, allocated
    # write-down: adding, removing or re-measuring members afterwards would
    # desync the lines from the ledger, so the member set is frozen once
    # the group leaves draft. The group's own actions write through the
    # 'eh_dg_internal' context flag.

    @api.model_create_multi
    def create(self, vals_list):
        internal = self.env.su and self.env.context.get('eh_dg_internal')
        if not internal:
            for vals in vals_list:
                group = self.env['eh.disposal.group'].browse(
                    vals.get('group_id'))
                group._eh_check_access('write')
                self._eh_validate_values(vals)
                if group and group.state != 'draft':
                    raise UserError(_(
                        "Members cannot be added to %s once it has left "
                        "draft; the posted group measurement covered the "
                        "membership as classified.", group.display_name))
                if set(vals) & _LINE_INTERNAL_FIELDS:
                    raise UserError(_(
                        "The allocation fields are engine output and "
                        "cannot be hand-seeded."))
        groups = self.env['eh.disposal.group'].browse({
            vals.get('group_id') for vals in vals_list if vals.get('group_id')
        })
        groups._eh_lock_for_transition()
        groups.invalidate_recordset(['state', 'discontinued_tag_id'])
        if not internal:
            for vals in vals_list:
                self._eh_validate_values(vals)
                group = self.env['eh.disposal.group'].browse(
                    vals.get('group_id')).exists()
                if group.state != 'draft':
                    raise UserError(_(
                        "Members cannot be added to %s once it has left "
                        "draft; classification won the concurrent race.",
                        group.display_name))
                if group.discontinued_tag_id:
                    raise UserError(_(
                        "Members cannot be added to %s after its "
                        "discontinued-operation scope was frozen.",
                        group.display_name))
        lines = super().create(vals_list)
        lines._eh_validate_references()
        return lines

    def write(self, vals):
        internal = self.env.su and self.env.context.get('eh_dg_internal')
        if vals and not self.env.su:
            self._eh_check_access('write')
        for line in self:
            line._eh_validate_values(vals, current=line)
        self._eh_lock_for_write(vals)
        for line in self:
            line._eh_validate_values(vals, current=line)
        if not internal:
            if set(vals) & _LINE_INTERNAL_FIELDS:
                raise UserError(_(
                    "The allocation fields are engine output; they must "
                    "equal the posted group measurement and cannot be "
                    "edited."))
            if set(vals) & _LINE_MEASURE_FIELDS:
                frozen = self.filtered(
                    lambda line: line.group_id.state != 'draft')
                if frozen:
                    raise UserError(_(
                        "Member measurement fields are locked once the "
                        "group is classified; they must reconcile to the "
                        "posted group measurement. Use Remeasure on the "
                        "group to record a subsequent change (IFRS 5.15)."))
            if {'pl_account_id', 'group_id'} & set(vals):
                tagged = self.filtered('group_id.discontinued_tag_id')
                if tagged:
                    raise UserError(_(
                        "Member discontinued-operation scope is immutable "
                        "after its exact journal-item scope is sealed."))
            if set(vals) & _LINE_DECLASS_INPUT_FIELDS:
                terminal = self.filtered(
                    lambda line: line.group_id.state in ('sold', 'cancelled'),
                )
                if terminal:
                    raise UserError(_(
                        "IFRS 5.27 member inputs are immutable after the "
                        "group's accounting exit."
                    ))
        result = super().write(vals)
        self._eh_validate_references()
        return result

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        self._eh_lock_for_write()
        if not (self.env.su and self.env.context.get('eh_dg_internal')):
            tagged = self.filtered('group_id.discontinued_tag_id')
            if tagged:
                raise UserError(_(
                    "Members cannot be removed after the group's "
                    "discontinued-operation scope was frozen."))
            frozen = self.filtered(
                lambda line: line.group_id.state != 'draft')
            if frozen:
                raise UserError(_(
                    "Members cannot be removed from a classified disposal "
                    "group; the posted group measurement covered them."))
        return super().unlink()

    # ---- helpers ----

    @api.model
    def _eh_validate_values(self, vals, current=None):
        current = current or self.browse()
        group_id = vals.get(
            'group_id', current.group_id.id if current else False)
        group = self.env['eh.disposal.group'].browse(group_id)
        try:
            if group and not self.env.su:
                group._eh_check_access('write')
            group = group.exists()
        except (AccessError, MissingError):
            raise AccessError(_(
                "Disposal-group member references are unavailable to the "
                "caller.")) from None
        if not group:
            raise ValidationError(_(
                "Every disposal-group member needs an accessible group."))
        company = group.company_id
        group._eh_assert_company_allowed(company.id)
        for field_name, model_name, access in (
            ('asset_id', 'eh.asset', 'write'),
            ('account_id', 'account.account', 'read'),
            ('pl_account_id', 'account.account', 'read'),
        ):
            record_id = vals.get(
                field_name, current[field_name].id if current else False)
            record = group._eh_resolve_reference(
                model_name, record_id, access=access)
            if not record:
                continue
            if field_name == 'asset_id':
                valid = record.company_id == company
            else:
                valid = _eh_record_matches_company(record, company)
            if not valid:
                raise ValidationError(_(
                    "Disposal-group member assets and accounts must be "
                    "available to the group company."))
        return True

    def _eh_validate_references(self, company=None):
        for line in self:
            line._eh_validate_values({}, current=line)
            if company and line.group_id.company_id != company:
                raise ValidationError(_(
                    "Disposal-group member company does not match its "
                    "parent group."))
        return self

    def _eh_lock_for_write(self, vals=None):
        vals = vals or {}
        group_ids = set(self.mapped('group_id').ids)
        asset_ids = set(self.mapped('asset_id').ids)
        if vals.get('group_id'):
            group_ids.add(int(vals['group_id']))
        if vals.get('asset_id'):
            asset_ids.add(int(vals['asset_id']))
        if group_ids:
            self.env.cr.execute(
                "SELECT id FROM eh_disposal_group WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(sorted(group_ids)),),
            )
            self.env['eh.disposal.group'].browse(
                tuple(sorted(group_ids))).invalidate_recordset([
                    'state', 'discontinued_tag_id',
                ])
        if self.ids:
            self.env.cr.execute(
                "SELECT id FROM eh_disposal_group_line WHERE id IN %s "
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

    def _eh_label(self):
        self.ensure_one()
        return (self.asset_id.display_name or self.name
                or _("member %s", self.id))

    def _eh_member_account(self):
        self.ensure_one()
        return self.account_id or self.group_id.asset_account_id

    def _eh_seed_from_asset(self):
        """Seed the member carrying amount from the linked asset's
        ledger-derived net book value (IFRS 5.15)."""
        self.ensure_one()
        nbv = self.currency_id.round(self.asset_id.net_book_value)
        self.with_context(eh_dg_internal=True).write(
            {'carrying_amount': nbv})

    def _eh_dispose_member_asset(self, proceeds, sale_date):
        """Dispose this member's linked eh.asset through the asset engine on
        group sale, so its cost, accumulated depreciation and accumulated
        impairment are reversed and it moves to 'disposed' instead of staying
        paused. Proceeds is this member's pro-rata share of the group's sale
        gross consideration. Gain/loss books on the asset's own disposal
        accounts, and the resulting move is sealed back to the group.
        """
        self.ensure_one()
        if not self.asset_id:
            return
        vals = {
            'asset_id': self.asset_id.id,
            'disposal_date': sale_date,
            'proceeds': proceeds,
        }
        if self.group_id.proceeds_account_id:
            vals['cash_account_id'] = self.group_id.proceeds_account_id.id
        self.env['eh.asset.dispose.wizard'].with_context(
            eh_hfs_asset_transition_capability=
                _HFS_ASSET_TRANSITION_CAPABILITY,
        ).sudo().create(vals).action_dispose()
        disposal_move = self.asset_id.disposal_move_id
        if not disposal_move or disposal_move.state != 'posted':
            raise UserError(_(
                "Linked member %(asset)s did not produce posted disposal "
                "evidence; the group sale was rolled back.",
                asset=self.asset_id.display_name,
            ))
        disposal_move.with_context(
            eh_dg_move_link_capability=_DG_MOVE_LINK_CAPABILITY,
        )._eh_write_sealed_metadata(
            {'eh_disposal_group_id': self.group_id.id},
            {'eh_disposal_group_id'},
        )
        self.with_context(eh_dg_internal=True).write(
            {'asset_paused_by_group': False})
        return disposal_move

    def _eh_cease_asset_depreciation(self):
        """Pause the linked asset so the depreciation cron skips it while
        the group is held for sale (IFRS 5.25). Mirrors the single-asset
        flow: only a running asset is paused and stamped; an asset the
        user had already paused is left alone."""
        self.ensure_one()
        asset = self.asset_id
        if asset.state == 'running':
            asset.action_pause()
            self.with_context(eh_dg_internal=True).write(
                {'asset_paused_by_group': True})
        elif asset.state == 'paused':
            self.with_context(eh_dg_internal=True).write(
                {'asset_paused_by_group': False})
        else:
            raise UserError(_(
                "Asset %s cannot join a held-for-sale group from state "
                "'%s'; only a running or paused asset can be held for "
                "sale.", asset.display_name, asset.state))

    def _eh_resume_asset(self, resume_date):
        """Resume an asset this group paused on classification (IFRS
        5.26), and only such an asset."""
        self.ensure_one()
        if (
            self.asset_paused_by_group
            and self.asset_id.state == 'paused'
            and not self.asset_id._eh_has_exclusive_accounting_owner(
                exclude_hfs_group=self.group_id)
        ):
            self.asset_id._eh_resume_after_hfs(resume_date)
        self.with_context(eh_dg_internal=True).write(
            {'asset_paused_by_group': False})


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_disposal_group_id = fields.Many2one(
        'eh.disposal.group', string="Disposal Group", readonly=True,
        index=True, ondelete='restrict', copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        if any(vals.get('eh_disposal_group_id') for vals in vals_list) and not (
            self.env.context.get('eh_dg_move_link_capability')
            is _DG_MOVE_LINK_CAPABILITY
        ):
            raise AccessError(_(
                "Disposal-group journal provenance is engine-owned."))
        return super().create(vals_list)

    def write(self, vals):
        if 'eh_disposal_group_id' in vals and not (
            self.env.context.get('eh_dg_move_link_capability')
            is _DG_MOVE_LINK_CAPABILITY
        ):
            raise AccessError(_(
                "Disposal-group journal provenance is immutable."))
        return super().write(vals)


class EhAssetImpairment(models.Model):
    _inherit = 'eh.asset.impairment'

    def _eh_validate_hfs_attachment(self):
        """Apply the asset engine's fail-closed posting invariants before a
        group-owned event is attached to the group's single shared move."""
        for rec in self:
            asset = rec.asset_id
            asset._eh_validate_company_currency()
            if asset.state not in ('running', 'paused'):
                raise UserError(_(
                    "Impairment can only attach to a running or paused asset; "
                    "%(asset)s is %(state)s.",
                    asset=asset.display_name, state=asset.state))
            if asset.lvp_pool_id:
                raise UserError(_(
                    "Asset %(asset)s belongs to low-value pool %(pool)s. "
                    "An individual held-for-sale impairment would double "
                    "count carrying value controlled by the pool.",
                    asset=asset.display_name,
                    pool=asset.lvp_pool_id.display_name))
            latest = asset._eh_latest_accounting_evidence_date()
            if rec.impairment_date < latest:
                raise UserError(_(
                    "Held-for-sale impairment date %(date)s precedes "
                    "existing accounting evidence dated %(latest)s on "
                    "%(asset)s. Post a current-period correction instead.",
                    date=rec.impairment_date, latest=latest,
                    asset=asset.display_name))
            rec._check_reversal_cap()
            asset.invalidate_recordset(['net_book_value'])
            if (
                not rec.is_reversal
                and asset.currency_id.compare_amounts(
                    rec.amount, asset.net_book_value) > 0
            ):
                raise UserError(_(
                    "Held-for-sale impairment of %(amount).2f exceeds "
                    "%(carrying).2f carrying amount on %(asset)s.",
                    amount=rec.amount, carrying=asset.net_book_value,
                    asset=asset.display_name))
        return True

    def action_cancel(self):
        # An impairment row attached to a disposal group's single journal
        # entry shares that entry with the other members; cancelling it
        # standalone would draft the whole group move. The group flow owns
        # the correction path (Remeasure posts a capped reversal).
        grouped = self.filtered(
            lambda rec: rec.move_id and rec.move_id.eh_disposal_group_id)
        if grouped:
            raise UserError(_(
                "This impairment was posted by disposal group %s and "
                "shares its journal entry with the other members; it "
                "cannot be cancelled standalone. Remeasure the group "
                "instead (IFRS 5.15).",
                ', '.join(grouped.move_id.eh_disposal_group_id
                          .mapped('display_name'))))
        return super().action_cancel()
