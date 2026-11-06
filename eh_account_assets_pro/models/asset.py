# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Fixed asset record.

State machine:

  draft -> running -> paused -> running -> ...
                              \\-> fully_depreciated
                              \\-> disposed

draft: schedule not yet generated and approved.
running: schedule exists, monthly cron auto posts due lines.
paused: schedule exists, cron skips this asset.
fully_depreciated: net book value reached salvage; cron skips.
disposed: terminated by disposal wizard; cron skips.
"""

import calendar
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .accounting_integrity import _eh_validate_accounting_company
from .account_move import (
    _ASSET_AUTOCREATE_CAPABILITY,
    _ASSET_AUTOCREATE_CONTEXT_KEY,
)


_ASSET_REVALUATION_CAPABILITY = object()
_ASSET_REVALUATION_CONTEXT_KEY = '_eh_asset_revaluation_capability'


class EhAsset(models.Model):
    _name = 'eh.asset'
    _description = "Fixed Asset"
    _inherit = [
        'mail.thread', 'mail.activity.mixin', 'eh.cron.batch.mixin',
        'eh.workflow.guard',
    ]
    _order = 'in_service_date desc, id desc'

    # Lifecycle and server-derived audit / ledger-link fields may only change
    # through this model's own actions and wizards. ``readonly`` is a client
    # hint, not an ORM/RPC security boundary: without this guard a caller
    # could forge a revaluation, disposal, pool transfer, or audit stamp while
    # bypassing the journal entry that is meant to support it.
    _eh_guarded_fields = (
        'state',
        'capitalised_at', 'capitalised_by_id', 'capitalisation_move_id',
        'lvp_pool_id', 'lvp_opening_value', 'lvp_allocation_date',
        'lvp_transfer_move_id', 'lvp_transferred_at', 'lvp_transferred_by_id',
        'revaluation_adjustment', 'revaluation_surplus',
        'revaluation_pl_decrease',
        'revaluation_move_ids',
        'annual_test_overdue',
        'recoverable_amount_latest', 'recoverable_amount_date',
        'currency_mismatch_quarantined', 'currency_mismatch_note',
        'activated_at', 'activated_by_id',
        'disposed_at', 'disposed_by_id', 'disposal_date',
        'disposal_proceeds', 'disposal_partner_id', 'disposal_move_id',
        'disposal_invoice_id', 'disposal_invoice_line_id',
        'invoice_line_id',
        'origin_source_quarantined', 'origin_source_quarantine_note',
        'origin_reversal_move_id', 'origin_reversed_at',
        'origin_reversed_by_id',
        'origin_quarantine_resolved_at',
        'origin_quarantine_resolved_by_id',
    )

    name = fields.Char(
        required=True, copy=False, default='/', tracking=True,
    )
    code = fields.Char(
        copy=False, tracking=True,
        help="Internal asset tag, e.g. ITHW-2026-0001.",
    )
    category_id = fields.Many2one(
        'eh.asset.category', string="Category", required=True,
        ondelete='restrict', tracking=True, check_company=True,
    )
    deferred_type = fields.Selection(
        [
            ('asset', "Fixed asset (depreciation)"),
            ('deferred_revenue', "Deferred revenue (recognition over time)"),
            ('deferred_expense', "Deferred expense (recognition over time)"),
        ],
        default='asset', required=True, tracking=True,
        help=(
            "Depreciation engine flavour. 'asset' is a regular fixed "
            "asset whose net book value declines as accumulated "
            "depreciation rises. 'deferred_revenue' recognises a "
            "pre-paid revenue balance into income over the schedule. "
            "'deferred_expense' recognises a pre-paid expense into the "
            "P&L over the schedule. The same schedule generator and "
            "lifecycle apply; only the journal entry posted on each "
            "line differs."
        ),
    )
    state = fields.Selection([
        ('draft', "Draft"),
        ('running', "Running"),
        ('paused', "Paused"),
        ('fully_depreciated', "Fully Depreciated"),
        ('disposed', "Disposed"),
    ], default='draft', required=True, tracking=True)

    # ---- acquisition ----
    partner_id = fields.Many2one(
        'res.partner', string="Vendor", tracking=True, index=True,
    )
    invoice_id = fields.Many2one(
        'account.move', string="Origin Bill",
        check_company=True,
        domain="[('move_type', '=', 'in_invoice')]",
    )
    invoice_line_id = fields.Many2one(
        'account.move.line', string="Origin Bill Line", readonly=True,
        copy=False, ondelete='restrict', check_company=True, index=True,
        help=(
            "Exact server-owned vendor-bill line that generated this asset. "
            "It is paired with the line's Generated Asset field and cannot "
            "be supplied or repointed through RPC."
        ),
    )
    origin_source_quarantined = fields.Boolean(
        readonly=True, copy=False, index=True, tracking=True,
        help=(
            "The posted origin bill was reversed. The original and reversal "
            "entries are retained, but asset accounting is frozen until an "
            "explicit correction workflow resolves the source."
        ),
    )
    origin_source_quarantine_note = fields.Text(readonly=True, copy=False)
    origin_reversal_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
        check_company=True,
    )
    origin_reversed_at = fields.Datetime(readonly=True, copy=False)
    origin_reversed_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
    )
    origin_correction_move_id = fields.Many2one(
        'account.move', string="Origin Correction Entry", copy=False,
        ondelete='restrict', check_company=True,
        help=(
            "Posted same-company correction entry supporting the decision to "
            "retain an asset after its exact originating bill line was "
            "credited. The manager resolution action validates that this entry "
            "touches one of the asset's accounting accounts."
        ),
    )
    origin_resolution_note = fields.Text(
        copy=False,
        help=(
            "Manager's documented accounting basis for retaining this asset "
            "after correction of its reversed source line."
        ),
    )
    origin_quarantine_resolved_at = fields.Datetime(
        readonly=True, copy=False,
    )
    origin_quarantine_resolved_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
    )
    acquisition_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
    )
    in_service_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
        help="Date the asset entered service. Drives the schedule start.",
    )
    acquisition_cost = fields.Monetary(required=True, tracking=True)
    salvage_value = fields.Monetary(default=0.0, tracking=True)

    # ---- depreciation parameters ----
    method = fields.Selection([
        ('straight_line', "Straight Line"),
        ('reducing_balance', "Reducing Balance"),
        ('prime_cost', "Prime Cost (AU tax)"),
        ('diminishing_value', "Diminishing Value (AU tax)"),
        ('manual', "Manual"),
    ], required=True, default='straight_line', tracking=True,
        help=(
            "Primary depreciation method posted to the GL. Straight "
            "Line and Reducing Balance are the standard accounting "
            "methods. Prime Cost and Diminishing Value are AU tax-"
            "compliant variants (factor 2.0 by default for DV per the "
            "post-2006 AU tax ruling). Use additional books for "
            "parallel methods (statutory + tax + IFRS)."
        ),
    )

    is_instant_write_off = fields.Boolean(
        string="Instant write-off",
        default=False,
        tracking=True,
        help=(
            "When set, the schedule writes off the entire depreciable "
            "amount in the first period regardless of useful life. "
            "Used by the AU instant-asset-write-off (currently AUD "
            "20,000 threshold for small business; check ATO for the "
            "current cap and end date) and similar fast-deduction "
            "regimes. Salvage value is honoured."
        ),
    )

    # ---- asset under construction ----
    is_under_construction = fields.Boolean(
        string="Under construction",
        default=False, tracking=True,
        help=(
            "When set, the asset is treated as work in progress. No "
            "depreciation schedule is generated and no JE posts. "
            "Acquisition cost accumulates on the configured AUC "
            "account until action_capitalise transfers the balance "
            "to the asset account, sets the in-service date, and "
            "starts depreciation. Use for projects that capitalise "
            "over multiple periods (e.g. a building under "
            "construction, software in development)."
        ),
    )
    auc_account_id = fields.Many2one(
        'account.account',
        string="AUC Holding Account",
        check_company=True,
        help=(
            "Balance-sheet account that carries the asset's cost "
            "while it is under construction. On capitalisation, the "
            "JE debits asset_account_id and credits this account."
        ),
    )
    capitalised_at = fields.Datetime(
        readonly=True, copy=False, tracking=True,
        help="Timestamp when the AUC was capitalised.",
    )
    capitalised_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
    )
    capitalisation_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
        check_company=True,
    )
    useful_life_months = fields.Integer(
        string="Useful Life (months)", required=True, tracking=True,
        default=lambda self: (
            self.env.company.eh_asset_default_useful_life_months or 60
        ),
    )
    declining_factor = fields.Float(default=2.0, tracking=True)
    prorate_first_period = fields.Boolean(default=True, tracking=True)
    prorata_mode = fields.Selection(
        [
            ('none', "Full first period"),
            ('daily', "By days in service"),
            ('half', "Half first period (mid-period convention)"),
        ],
        tracking=True,
        help="Overrides the day-based first-period proration when set. "
             "'none' charges a full first period; 'half' charges half (the "
             "mid-period / half-year convention used by some tax regimes); "
             "'daily' prorates by days in service. When blank, the Prorate "
             "First Period switch applies.",
    )

    # units of production parameters
    total_units = fields.Float(
        help="Total units expected over the asset life "
             "(units of production method).",
    )
    units_used = fields.Float(
        help="Cumulative units consumed. Drives next depreciation under "
             "units of production method.",
    )

    # ---- IAS 38 intangible assets ----
    asset_class = fields.Selection(
        [
            ('tangible', "Tangible (IAS 16)"),
            ('intangible', "Intangible (IAS 38)"),
        ],
        default='tangible', required=True, tracking=True,
        help=(
            "Measurement standard the asset falls under. Tangible "
            "assets (property, plant and equipment) follow IAS 16; "
            "intangible assets (software, licences, brands, goodwill, "
            "capitalised development) follow IAS 38, which adds the "
            "indefinite-life regime (no amortisation, mandatory annual "
            "impairment testing) and the development-cost "
            "capitalisation gate (IAS 38.57)."
        ),
    )
    is_indefinite_life = fields.Boolean(
        string="Indefinite useful life",
        default=False, tracking=True,
        help=(
            "IAS 38.107-108: an intangible asset with an indefinite "
            "useful life is NOT amortised. Instead IAS 36.10 requires "
            "an impairment test annually, and whenever there is an "
            "indication of impairment. Setting this flag blocks "
            "schedule generation and amortisation posting, and places "
            "the asset in the annual impairment-test population "
            "enforced by the IAS 36 annual-test cron."
        ),
    )
    dev_cost_capitalisation = fields.Boolean(
        string="Capitalised development cost",
        default=False, tracking=True,
        help=(
            "Marks an intangible asset arising from development. IAS "
            "38.57 permits capitalisation only when ALL six criteria "
            "are demonstrated; until every checklist item below is "
            "ticked this asset cannot leave draft. If any criterion "
            "cannot be demonstrated, IAS 38 requires the expenditure "
            "to be recognised as an EXPENSE when incurred (research "
            "and non-qualifying development are never capitalised, "
            "IAS 38.54)."
        ),
    )
    dev_technical_feasibility = fields.Boolean(
        string="Technical feasibility demonstrated",
        help=(
            "IAS 38.57(a): the technical feasibility of completing the "
            "intangible asset so that it will be available for use or "
            "sale."
        ),
    )
    dev_intention_complete = fields.Boolean(
        string="Intention to complete",
        help=(
            "IAS 38.57(b): the intention to complete the intangible "
            "asset and use or sell it."
        ),
    )
    dev_ability_use_sell = fields.Boolean(
        string="Ability to use or sell",
        help="IAS 38.57(c): the ability to use or sell the intangible asset.",
    )
    dev_probable_benefits = fields.Boolean(
        string="Probable future economic benefits",
        help=(
            "IAS 38.57(d): how the intangible asset will generate "
            "probable future economic benefits (existence of a market "
            "or, if for internal use, its usefulness)."
        ),
    )
    dev_resources_available = fields.Boolean(
        string="Adequate resources available",
        help=(
            "IAS 38.57(e): the availability of adequate technical, "
            "financial and other resources to complete the development "
            "and to use or sell the intangible asset."
        ),
    )
    dev_reliable_measurement = fields.Boolean(
        string="Expenditure reliably measurable",
        help=(
            "IAS 38.57(f): the ability to measure reliably the "
            "expenditure attributable to the intangible asset during "
            "its development."
        ),
    )

    # ---- IAS 36 annual-test governance ----
    annual_test_overdue = fields.Boolean(
        string="Annual test overdue",
        default=False, copy=False, tracking=True,
        help=(
            "Set by the IAS 36 annual-test cron when this goodwill or "
            "indefinite-life intangible asset has no impairment test "
            "evidence (a CGU test run, or a posted impairment event) "
            "dated inside the current fiscal year once the company's "
            "annual test month has been reached. Cleared automatically "
            "when a test posts. Surfaces the IAS 36.10 exception list "
            "in the asset list and filters."
        ),
    )
    recoverable_amount_latest = fields.Monetary(
        string="Latest recoverable amount",
        readonly=True, copy=False, tracking=True,
        help=(
            "Most recent recoverable-amount measurement linked to this "
            "asset: stamped by a CGU impairment test (the member's "
            "post-test carrying amount when the unit was written down "
            "to its recoverable amount) or by a hand-keyed impairment "
            "or reversal that states its recoverable amount. IAS 36 "
            "does not permit a revaluation uplift to carry the asset "
            "above its recoverable amount, so the revaluation wizard "
            "caps uplifts against this measurement."
        ),
    )
    recoverable_amount_date = fields.Date(
        string="Recoverable amount date",
        readonly=True, copy=False, tracking=True,
        help="Measurement date of the latest recoverable amount.",
    )

    # ---- accounts and journal ----
    asset_account_id = fields.Many2one(
        'account.account', string="Asset Account", check_company=True,
    )
    depreciation_account_id = fields.Many2one(
        'account.account', string="Depreciation Expense Account",
        check_company=True,
    )
    accumulated_depreciation_account_id = fields.Many2one(
        'account.account', string="Accumulated Depreciation Account",
        check_company=True,
    )
    disposal_gain_account_id = fields.Many2one(
        'account.account', string="Disposal Gain Account", check_company=True,
    )
    disposal_loss_account_id = fields.Many2one(
        'account.account', string="Disposal Loss Account", check_company=True,
    )
    revaluation_reserve_account_id = fields.Many2one(
        'account.account', string="Revaluation Reserve Account",
        check_company=True,
        domain="[('account_type', '=', 'equity')]",
        help=(
            "Equity reserve attributable to this asset. Revaluation and "
            "impairment workflows use it to route IAS 16/IAS 36 amounts "
            "through OCI before P&L where required."
        ),
    )
    journal_id = fields.Many2one(
        'account.journal', string="Depreciation Journal", check_company=True,
    )

    currency_id = fields.Many2one(
        'res.currency', required=True,
        default=lambda self: self.env.company.currency_id,
    )
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
    )
    currency_mismatch_quarantined = fields.Boolean(
        readonly=True, copy=False, tracking=True,
        help=(
            "Set by the upgrade audit when a legacy asset has ledger "
            "evidence denominated as if its foreign source currency were "
            "company currency. The historical record remains visible but "
            "read-only; its ledger must be reversed and the asset recreated "
            "in company currency."
        ),
    )
    currency_mismatch_note = fields.Text(readonly=True, copy=False)

    # ---- schedule and totals ----
    depreciation_line_ids = fields.One2many(
        'eh.asset.depreciation.line', 'asset_id', copy=False,
    )
    book_ids = fields.One2many(
        'eh.asset.book', 'asset_id', copy=True,
        help=(
            "Parallel depreciation books on this asset (tax, IFRS, "
            "management). Each book has independent method, useful "
            "life, salvage, and schedule. They are reporting-only; the "
            "primary asset schedule is the sole GL source of truth."
        ),
    )
    book_count = fields.Integer(
        compute='_compute_book_count', store=False,
        help="Number of additional depreciation books configured.",
    )

    # ---- IAS 16 component accounting ----
    parent_asset_id = fields.Many2one(
        'eh.asset', string="Parent Asset",
        ondelete='restrict', check_company=True,
        index=True,
        help=(
            "Parent asset this record is a component of. IAS 16 "
            "requires entities to depreciate significant components "
            "of an asset separately when their useful life or "
            "depreciation pattern differs from the parent (e.g. an "
            "aircraft engine vs the airframe, an HVAC system vs the "
            "building). The parent rolls up component NBV in display "
            "but each component carries its own schedule and posts "
            "its own depreciation."
        ),
    )
    component_ids = fields.One2many(
        'eh.asset', 'parent_asset_id',
        string="Components",
        help="Child components of this asset. Empty for leaf assets.",
    )
    component_count = fields.Integer(
        compute='_compute_component_totals', store=False,
        help="Number of direct child components.",
    )
    rolled_up_cost = fields.Monetary(
        compute='_compute_component_totals', store=False,
        currency_field='currency_id',
        help=(
            "This asset's acquisition_cost plus the sum of every "
            "component's acquisition_cost. Equals acquisition_cost "
            "for assets with no components."
        ),
    )
    rolled_up_nbv = fields.Monetary(
        compute='_compute_component_totals', store=False,
        currency_field='currency_id',
        help=(
            "This asset's net_book_value plus the sum of every "
            "component's net_book_value. Equals net_book_value "
            "for assets with no components."
        ),
    )

    # ---- IAS 36 impairment ----
    impairment_ids = fields.One2many(
        'eh.asset.impairment', 'asset_id', copy=False,
        help=(
            "History of impairment charges and reversals. Each row "
            "represents a separate impairment event with its own JE."
        ),
    )
    accumulated_impairment = fields.Monetary(
        compute='_compute_impairment_totals', store=False,
        currency_field='currency_id',
        help=(
            "Net total of impairment charges minus reversals on this "
            "asset. Reduces the carrying amount used for the NBV "
            "computation; cannot exceed the depreciable base."
        ),
    )

    # ---- IAS 36 cash-generating unit ----
    cgu_id = fields.Many2one(
        'eh.asset.cgu',
        string="Cash-Generating Unit",
        ondelete='set null', check_company=True,
        index=True,
        help=(
            "Optional cash-generating unit (CGU) this asset belongs to. "
            "IAS 36 tests recoverable amount at the level of the "
            "smallest group of assets that generates largely "
            "independent cash inflows when an individual asset cannot "
            "be tested on its own. When the CGU's impairment test "
            "recognises a loss, the shortfall is allocated pro-rata "
            "across the CGU's member assets (any goodwill first). Left "
            "blank the asset is tested and impaired individually as "
            "before; this grouping is opt-in."
        ),
    )
    cgu_impairment_floor = fields.Monetary(
        string="IAS 36.105 Individual Floor",
        currency_field='currency_id', default=0.0, tracking=True,
        help=(
            "Lowest carrying amount permitted when a CGU loss is allocated "
            "to this asset under IAS 36.105: the highest of its individually "
            "determinable fair value less costs of disposal, its individually "
            "determinable value in use, and zero. Reassess this input at each "
            "CGU test date; zero means no individually determinable floor."
        ),
    )
    is_goodwill = fields.Boolean(
        string="Goodwill",
        default=False,
        help=(
            "Marks this asset as goodwill allocated to a CGU. IAS "
            "36.104 requires an impairment loss on a CGU to be applied "
            "first to reduce the carrying amount of any goodwill, then "
            "pro-rata across the other assets of the unit. Off by "
            "default; setting it only affects the CGU allocation order."
        ),
    )

    # ---- AU low-value pool ----
    lvp_pool_id = fields.Many2one(
        'eh.asset.lvp.pool',
        string="Low-Value Pool",
        ondelete='restrict', check_company=True,
        help=(
            "When set, this asset has been transferred to a low-value "
            "pool and is depreciated as part of the pool rather than "
            "individually. The asset's own schedule is frozen; the "
            "pool's schedule drives all subsequent depreciation. "
            "AU sites use this for assets under the AUD 1,000 "
            "low-value threshold."
        ),
    )
    lvp_opening_value = fields.Monetary(
        string="Pool Opening Adjustable Value",
        readonly=True, copy=False, currency_field='currency_id',
        help=(
            "Net book value captured at the moment this asset was "
            "transferred into its low-value pool (the 'opening adjustable "
            "value' that was reclassified into the pool asset account). "
            "The pool depreciates and reports on this base, not the gross "
            "acquisition cost, so the subledger stays aligned with the GL "
            "for an asset transferred in already partly depreciated."
        ),
    )
    lvp_allocation_date = fields.Date(
        string="Pool Allocation Date",
        readonly=True, copy=False,
        help=(
            "Date this asset was allocated (transferred) into its low-"
            "value pool. Drives the ATO first-year vs subsequent-year "
            "rate: the year of allocation attracts the 18.75% first-year "
            "rate, later years the 37.5% rate. Recorded independently of "
            "the in-service date so an asset transferred in a later year "
            "is rated on the correct base year and is not depreciated in "
            "the pool before it was ever transferred in."
        ),
    )
    lvp_transfer_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
        check_company=True,
        help=(
            "Sealed reclassification entry that moved this asset's carrying "
            "amount into the low-value-pool account. Blank for an explicitly "
            "tax-only pool."
        ),
    )
    lvp_transferred_at = fields.Datetime(readonly=True, copy=False)
    lvp_transferred_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
    )
    total_depreciated = fields.Monetary(
        compute='_compute_totals', store=True,
    )
    net_book_value = fields.Monetary(
        compute='_compute_totals', store=True,
    )
    revaluation_adjustment = fields.Monetary(
        readonly=True, tracking=True,
        help=(
            "Cumulative signed revaluation applied to the carrying amount "
            "(positive = uplift, negative = downward revaluation). Held "
            "separately from acquisition_cost so historical cost, and the "
            "IAS 36.117 depreciated-cost ceiling derived from it, stay "
            "intact. net_book_value includes this adjustment."
        ),
    )
    revaluation_surplus = fields.Monetary(
        readonly=True, tracking=True,
        help=(
            "Cumulative revaluation surplus held in equity (the credited "
            "revaluation reserve balance attributable to this asset, per "
            "IAS 16.39-41). An uplift increases it; a subsequent downward "
            "revaluation first reverses it (IAS 16.40) and only the excess "
            "hits P&L; on disposal any remaining balance is recycled "
            "directly to retained earnings (IAS 16.41), never through P&L. "
            "Never negative."
        ),
    )
    revaluation_pl_decrease = fields.Monetary(
        readonly=True, tracking=True,
        help=(
            "Cumulative revaluation decrease previously recognised in P&L "
            "(the excess of a downward revaluation that could not be absorbed "
            "by the revaluation surplus, per IAS 16.40). A subsequent upward "
            "revaluation must first reverse this in P&L (credit to income) up "
            "to this balance before any remainder is credited to the "
            "revaluation surplus (IAS 16.39). Never negative."
        ),
    )
    revaluation_move_ids = fields.Many2many(
        'account.move', 'eh_asset_revaluation_move_rel',
        'asset_id', 'move_id', string="Revaluation Entries",
        readonly=True, copy=False, check_company=True,
        help="Sealed journal entries supporting this asset's revaluations.",
    )
    next_post_date = fields.Date(compute='_compute_next_post')

    # ---- audit ----
    activated_at = fields.Datetime(readonly=True, tracking=True)
    activated_by_id = fields.Many2one('res.users', readonly=True)
    disposed_at = fields.Datetime(readonly=True, tracking=True)
    disposed_by_id = fields.Many2one('res.users', readonly=True)
    disposal_date = fields.Date(readonly=True, tracking=True)
    disposal_proceeds = fields.Monetary(readonly=True, tracking=True)
    disposal_partner_id = fields.Many2one('res.partner', readonly=True)
    disposal_move_id = fields.Many2one(
        'account.move', readonly=True, ondelete='restrict', check_company=True)
    disposal_invoice_id = fields.Many2one(
        'account.move', string="Asset Sale Invoice", readonly=True,
        copy=False, ondelete='restrict', check_company=True,
        help=(
            "Posted customer invoice that recognised the sale proceeds and "
            "output tax. The disposal entry clears its selected net revenue "
            "line instead of duplicating receivable or tax legs."
        ),
    )
    disposal_invoice_line_id = fields.Many2one(
        'account.move.line', string="Asset Sale Invoice Line", readonly=True,
        copy=False, ondelete='restrict', check_company=True,
    )

    notes = fields.Text()

    _check_acquisition_cost = models.Constraint(
        'CHECK (acquisition_cost > 0)',
        'Acquisition cost must be positive.',
    )
    _check_salvage_le_cost = models.Constraint(
        'CHECK (salvage_value >= 0)',
        'Salvage value cannot be negative.',
    )
    _check_useful_life = models.Constraint(
        'CHECK (useful_life_months > 0)',
        'Useful life must be greater than zero.',
    )
    _one_asset_per_origin_line = models.Constraint(
        'unique(invoice_line_id)',
        'A vendor-bill line can generate only one fixed asset.',
    )
    _one_asset_per_disposal_invoice_line = models.Constraint(
        'unique(disposal_invoice_line_id)',
        'A customer-invoice line can support only one asset disposal.',
    )

    @api.constrains('cgu_impairment_floor')
    def _check_cgu_impairment_floor(self):
        for asset in self:
            if asset.cgu_impairment_floor < 0:
                raise ValidationError(_(
                    "The IAS 36.105 individual impairment floor cannot be "
                    "negative on %(asset)s.", asset=asset.display_name,
                ))

    @api.constrains('revaluation_reserve_account_id')
    def _check_revaluation_reserve_is_equity(self):
        for asset in self.filtered('revaluation_reserve_account_id'):
            if asset.revaluation_reserve_account_id.account_type != 'equity':
                raise ValidationError(_(
                    "Revaluation Reserve Account on %(asset)s must be an "
                    "equity account.", asset=asset.display_name,
                ))

    # ---- compute ----

    @api.depends(
        'acquisition_cost', 'revaluation_adjustment',
        'depreciation_line_ids.amount', 'depreciation_line_ids.is_posted',
        'impairment_ids.amount', 'impairment_ids.is_reversal',
        'impairment_ids.state',
    )
    def _compute_totals(self):
        for asset in self:
            posted = asset.depreciation_line_ids.filtered(lambda l: l.is_posted)
            asset.total_depreciated = sum(posted.mapped('amount'))
            posted_impairments = asset.impairment_ids.filtered(
                lambda i: i.state == 'posted'
            )
            charges = sum(
                posted_impairments
                .filtered(lambda i: not i.is_reversal)
                .mapped('amount'),
            )
            reversals = sum(
                posted_impairments
                .filtered(lambda i: i.is_reversal)
                .mapped('amount'),
            )
            net_impairment = charges - reversals
            asset.net_book_value = (
                asset.acquisition_cost
                - asset.total_depreciated
                - net_impairment
                + asset.revaluation_adjustment
            )

    @api.depends('depreciation_line_ids.is_posted', 'depreciation_line_ids.depreciation_date')
    def _compute_next_post(self):
        for asset in self:
            unposted = asset.depreciation_line_ids.filtered(lambda l: not l.is_posted)
            asset.next_post_date = (
                min(unposted.mapped('depreciation_date')) if unposted else False
            )

    @api.depends('book_ids')
    def _compute_book_count(self):
        for asset in self:
            asset.book_count = len(asset.book_ids)

    @api.depends(
        'component_ids', 'component_ids.acquisition_cost',
        'component_ids.net_book_value',
        'acquisition_cost', 'net_book_value',
    )
    def _compute_component_totals(self):
        for asset in self:
            children = asset.component_ids
            asset.component_count = len(children)
            asset.rolled_up_cost = (
                (asset.acquisition_cost or 0.0)
                + sum(children.mapped('acquisition_cost'))
            )
            asset.rolled_up_nbv = (
                (asset.net_book_value or 0.0)
                + sum(children.mapped('net_book_value'))
            )

    @api.depends('impairment_ids', 'impairment_ids.amount',
                 'impairment_ids.is_reversal', 'impairment_ids.state')
    def _compute_impairment_totals(self):
        for asset in self:
            posted_impairments = asset.impairment_ids.filtered(
                lambda i: i.state == 'posted'
            )
            charges = sum(
                posted_impairments
                .filtered(lambda i: not i.is_reversal)
                .mapped('amount'),
            )
            reversals = sum(
                posted_impairments
                .filtered(lambda i: i.is_reversal)
                .mapped('amount'),
            )
            asset.accumulated_impairment = charges - reversals

    def action_view_components(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Components"),
            'res_model': 'eh.asset',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('parent_asset_id', '=', self.id)],
            'context': {'default_parent_asset_id': self.id},
        }

    def action_view_impairments(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Impairment History"),
            'res_model': 'eh.asset.impairment',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('asset_id', '=', self.id)],
            'context': {'default_asset_id': self.id},
        }

    def action_open_impairment_wizard(self):
        self.ensure_one()
        if self.state not in ('running', 'paused'):
            raise UserError(_(
                "Impairment requires a running or paused asset.",
            ))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Record Impairment"),
            'res_model': 'eh.asset.impairment',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_asset_id': self.id},
        }

    def action_resolve_reversed_origin_after_correction(self):
        """Release source quarantine only against reviewed ledger evidence."""
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH accounting manager can resolve a reversed-origin "
                "asset quarantine."
            ))
        self._eh_check_access('write')
        self._eh_lock_for_transition()
        for asset in self:
            if not asset.origin_source_quarantined:
                raise UserError(_(
                    "Asset %(asset)s is not in reversed-origin quarantine.",
                    asset=asset.display_name,
                ))
            note = (asset.origin_resolution_note or '').strip()
            correction = asset.origin_correction_move_id
            if not note:
                raise UserError(_(
                    "Document the accounting basis for retaining the asset."
                ))
            if not correction or correction.state != 'posted':
                raise UserError(_(
                    "Select a posted correction entry before releasing the "
                    "asset quarantine."
                ))
            if correction.company_id != asset.company_id:
                raise UserError(_(
                    "The correction entry and asset must belong to the same "
                    "company."
                ))
            if (asset.origin_reversal_move_id
                    and correction.date < asset.origin_reversal_move_id.date):
                raise UserError(_(
                    "The correction entry cannot predate the source reversal."
                ))
            relevant_accounts = (
                asset.asset_account_id
                | asset.depreciation_account_id
                | asset.accumulated_depreciation_account_id
                | asset.disposal_gain_account_id
                | asset.disposal_loss_account_id
            )
            if not correction.line_ids.filtered(
                    lambda line: line.account_id in relevant_accounts):
                raise UserError(_(
                    "The correction entry does not touch any configured "
                    "account of %(asset)s; it cannot support release of the "
                    "quarantine.", asset=asset.display_name,
                ))
            asset.with_context({
                _ASSET_AUTOCREATE_CONTEXT_KEY: _ASSET_AUTOCREATE_CAPABILITY,
            }).write({
                'origin_source_quarantined': False,
                'origin_quarantine_resolved_at': fields.Datetime.now(),
                'origin_quarantine_resolved_by_id': self.env.user.id,
            })
            asset.message_post(body=_(
                "Reversed-origin quarantine resolved by %(user)s against "
                "posted correction %(move)s. Basis: %(note)s",
                user=self.env.user.display_name,
                move=correction.display_name,
                note=note,
            ))
        return True

    def action_discard_reversed_origin_draft(self):
        """Remove a source-reversed generated asset that has no own ledger."""
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH accounting manager can discard a generated asset."
            ))
        self._eh_check_access('unlink')
        self._eh_lock_for_transition()
        for asset in self:
            if not asset.origin_source_quarantined:
                raise UserError(_(
                    "Asset %(asset)s is not in reversed-origin quarantine.",
                    asset=asset.display_name,
                ))
            if asset._eh_has_ledger_evidence():
                raise UserError(_(
                    "Asset %(asset)s has its own ledger evidence. Post and "
                    "select a correction entry, then use Resolve Quarantine; "
                    "it cannot be discarded.", asset=asset.display_name,
                ))
            origin_line = asset.invoice_line_id
            engine_context = {
                _ASSET_AUTOCREATE_CONTEXT_KEY: _ASSET_AUTOCREATE_CAPABILITY,
            }
            if origin_line and origin_line.eh_asset_id == asset:
                origin_line.with_context(engine_context).write({
                    'eh_asset_id': False,
                })
            asset.with_context(engine_context).unlink()
        return True

    def action_view_books(self):
        """Open the book list filtered to this asset."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Depreciation Books"),
            'res_model': 'eh.asset.book',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('asset_id', '=', self.id)],
            'context': {'default_asset_id': self.id},
        }

    # ---- onchange ----

    @api.onchange('category_id')
    def _onchange_category(self):
        if not self.category_id:
            return
        cat = self.category_id
        self.method = cat.method
        self.useful_life_months = cat.useful_life_months
        self.salvage_value = cat.salvage_rate * (self.acquisition_cost or 0.0)
        self.declining_factor = cat.declining_factor
        self.prorate_first_period = cat.prorate_first_period
        self.prorata_mode = cat.prorata_mode
        self.asset_account_id = cat.asset_account_id
        self.depreciation_account_id = cat.depreciation_account_id
        self.accumulated_depreciation_account_id = cat.accumulated_depreciation_account_id
        self.disposal_gain_account_id = cat.disposal_gain_account_id
        self.disposal_loss_account_id = cat.disposal_loss_account_id
        self.revaluation_reserve_account_id = (
            cat.revaluation_reserve_account_id
        )
        self.journal_id = cat.journal_id

    @api.onchange('acquisition_date')
    def _onchange_acquisition_date_default_in_service(self):
        """Default in_service_date to acquisition_date when empty.

        Most assets enter service on the day they are acquired; the
        common path is "buy a laptop today, start using it today".
        Pre-fill saves the user a duplicate date entry. We only fill
        when in_service_date is empty so a deferred-deployment case
        (asset in storage for weeks) is preserved.
        """
        for rec in self:
            if rec.acquisition_date and not rec.in_service_date:
                rec.in_service_date = rec.acquisition_date

    @api.onchange('acquisition_cost', 'salvage_value')
    def _onchange_cost_warn_negative_depreciable(self):
        """Warn when salvage exceeds cost (would yield negative
        depreciable base). The constraint blocks the save anyway,
        but a live warning during edit is friendlier than a UserError
        on Save.
        """
        for rec in self:
            if (rec.acquisition_cost
                    and rec.salvage_value
                    and rec.salvage_value > rec.acquisition_cost):
                return {
                    'warning': {
                        'title': _("Salvage exceeds cost"),
                        'message': _(
                            "Salvage value %(salvage).2f is greater than "
                            "acquisition cost %(cost).2f. The depreciable "
                            "base would be negative; saving will be blocked.",
                            salvage=rec.salvage_value,
                            cost=rec.acquisition_cost,
                        ),
                    }
                }

    @api.constrains('salvage_value', 'acquisition_cost')
    def _check_salvage(self):
        for asset in self:
            if asset.salvage_value > asset.acquisition_cost:
                raise ValidationError(_(
                    "Salvage value cannot exceed acquisition cost.",
                ))

    @api.constrains('acquisition_date', 'in_service_date')
    def _check_service_date_not_before_acquisition(self):
        for asset in self:
            if (asset.acquisition_date and asset.in_service_date
                    and asset.in_service_date < asset.acquisition_date):
                raise ValidationError(_(
                    "In-service date %(service)s cannot precede acquisition "
                    "date %(acquisition)s on %(asset)s.",
                    service=asset.in_service_date,
                    acquisition=asset.acquisition_date,
                    asset=asset.display_name,
                ))

    @api.constrains('asset_class', 'is_indefinite_life',
                    'dev_cost_capitalisation')
    def _check_ias38_class_flags(self):
        """The IAS 38 regimes only exist for intangible assets."""
        for asset in self:
            if asset.is_indefinite_life and asset.asset_class != 'intangible':
                raise ValidationError(_(
                    "Only an intangible asset (IAS 38) can have an "
                    "indefinite useful life. Tangible assets under IAS 16 "
                    "are always depreciated over a finite useful life.",
                ))
            if (asset.dev_cost_capitalisation
                    and asset.asset_class != 'intangible'):
                raise ValidationError(_(
                    "The development-cost capitalisation checklist (IAS "
                    "38.57) applies to intangible assets only. Set the "
                    "asset class to Intangible (IAS 38) first.",
                ))

    @api.constrains('is_indefinite_life', 'depreciation_line_ids')
    def _check_indefinite_no_schedule(self):
        """IAS 38.107: an indefinite-life intangible is not amortised.

        Blocks flipping is_indefinite_life on an asset that already
        carries schedule lines; the mirror guard on the line model
        blocks creating lines under an indefinite-life asset.
        """
        for asset in self:
            if asset.is_indefinite_life and asset.depreciation_line_ids:
                raise ValidationError(_(
                    "%(asset)s has an indefinite useful life; IAS 38.107 "
                    "prohibits amortising it, so it cannot carry a "
                    "depreciation schedule. Remove the schedule lines "
                    "(or clear the indefinite-life flag after a "
                    "finite-life reassessment per IAS 38.109).",
                    asset=asset.display_name,
                ))

    def _eh_is_indefinite_intangible(self):
        self.ensure_one()
        return self.asset_class == 'intangible' and self.is_indefinite_life

    _IAS38_57_CRITERIA = (
        ('dev_technical_feasibility', "technical feasibility (IAS 38.57(a))"),
        ('dev_intention_complete', "intention to complete (IAS 38.57(b))"),
        ('dev_ability_use_sell', "ability to use or sell (IAS 38.57(c))"),
        ('dev_probable_benefits',
         "probable future economic benefits (IAS 38.57(d))"),
        ('dev_resources_available',
         "adequate resources available (IAS 38.57(e))"),
        ('dev_reliable_measurement',
         "reliable measurement of expenditure (IAS 38.57(f))"),
    )

    def _check_ias38_dev_gate(self):
        """Capitalisation gate for development costs (IAS 38.57).

        An intangible flagged dev_cost_capitalisation cannot leave
        draft until all six IAS 38.57 criteria are ticked. When any
        criterion cannot be demonstrated, IAS 38 requires the
        expenditure to be expensed as incurred, not capitalised.
        """
        for asset in self:
            if not asset.dev_cost_capitalisation:
                continue
            missing = [
                label for field_name, label in self._IAS38_57_CRITERIA
                if not asset[field_name]
            ]
            if missing:
                raise UserError(_(
                    "%(asset)s is a capitalised development cost but the "
                    "IAS 38.57 capitalisation criteria are not all "
                    "demonstrated. Missing: %(missing)s. IAS 38 permits "
                    "capitalising development expenditure only when every "
                    "criterion is met; otherwise recognise it as an "
                    "expense when incurred.",
                    asset=asset.display_name,
                    missing='; '.join(missing),
                ))

    # ---- create ----

    @api.model_create_multi
    def create(self, vals_list):
        if any('invoice_line_id' in vals for vals in vals_list) \
                and self.env.context.get(_ASSET_AUTOCREATE_CONTEXT_KEY) \
                is not _ASSET_AUTOCREATE_CAPABILITY:
            raise UserError(_(
                "An exact origin bill line is server-owned provenance and "
                "can only be supplied by vendor-bill posting."
            ))
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                seq = self.env['ir.sequence'].next_by_code('eh.asset') or '/'
                vals['name'] = seq
        assets = super().create(vals_list)
        assets.env['res.company'].sudo()._eh_bump_move_version(
            assets.mapped('company_id.id'),
        )
        return assets

    # Measurement inputs that determine the schedule and the applicable
    # IAS 16 / IAS 38 regime. Once any ledger evidence exists they are frozen:
    # changing one in place would make the asset master describe a different
    # measurement from the sealed entries and immutable schedule rows it
    # already produced. A prospective reassessment must therefore be recorded
    # through a dedicated accounting workflow, not a direct master-data edit.
    _FROZEN_AFTER_POST = (
        'category_id', 'partner_id', 'invoice_id', 'invoice_line_id',
        'parent_asset_id', 'cgu_id',
        'deferred_type',
        'acquisition_date', 'in_service_date',
        'acquisition_cost', 'salvage_value',
        'method', 'useful_life_months', 'declining_factor',
        'prorate_first_period', 'prorata_mode',
        'is_instant_write_off', 'is_under_construction',
        'total_units', 'units_used',
        'asset_class', 'is_indefinite_life',
        'dev_cost_capitalisation',
        'dev_technical_feasibility', 'dev_intention_complete',
        'dev_ability_use_sell', 'dev_probable_benefits',
        'dev_resources_available', 'dev_reliable_measurement',
        'is_goodwill',
        'cgu_impairment_floor',
        'auc_account_id', 'asset_account_id', 'depreciation_account_id',
        'accumulated_depreciation_account_id', 'disposal_gain_account_id',
        'disposal_loss_account_id', 'revaluation_reserve_account_id',
        'journal_id',
        'company_id', 'currency_id',
    )

    _ACCOUNTING_COMPANY_FIELDS = (
        'auc_account_id', 'asset_account_id', 'depreciation_account_id',
        'accumulated_depreciation_account_id', 'disposal_gain_account_id',
        'disposal_loss_account_id', 'revaluation_reserve_account_id',
        'journal_id',
    )

    @api.constrains('currency_id', 'company_id')
    def _check_company_currency(self):
        for asset in self:
            if (asset.currency_id and asset.company_id
                    and asset.currency_id != asset.company_id.currency_id):
                raise ValidationError(_(
                    "Asset %(asset)s must use %(currency)s, the currency of "
                    "company %(company)s. Assets Pro posts its monetary "
                    "figures directly to company-currency debit/credit "
                    "columns and does not implement an FX subledger.",
                    asset=asset.display_name,
                    currency=asset.company_id.currency_id.display_name,
                    company=asset.company_id.display_name,
                ))

    @api.constrains(
        'company_id', 'category_id', 'invoice_id', 'invoice_line_id',
        'origin_reversal_move_id', 'origin_correction_move_id',
        'disposal_invoice_id', 'disposal_invoice_line_id', 'parent_asset_id',
        'cgu_id', 'lvp_pool_id', 'auc_account_id', 'asset_account_id',
        'depreciation_account_id', 'accumulated_depreciation_account_id',
        'disposal_gain_account_id', 'disposal_loss_account_id',
        'revaluation_reserve_account_id', 'journal_id',
    )
    def _check_relationship_company(self):
        _eh_validate_accounting_company(
            self, self._ACCOUNTING_COMPANY_FIELDS,
        )
        for asset in self:
            company = asset.company_id
            if asset.category_id and asset.category_id.company_id != company:
                raise ValidationError(_(
                    "Asset %(asset)s and category %(category)s must belong to "
                    "the same company.",
                    asset=asset.display_name,
                    category=asset.category_id.display_name,
                ))
            if asset.invoice_id:
                if asset.invoice_id.company_id != company:
                    raise ValidationError(_(
                        "Asset %(asset)s and origin bill must belong to the "
                        "same company.", asset=asset.display_name,
                    ))
                if asset.invoice_id.move_type != 'in_invoice':
                    raise ValidationError(_(
                        "Asset %(asset)s origin must be a vendor bill.",
                        asset=asset.display_name,
                    ))
            line = asset.invoice_line_id
            if line:
                if not asset.invoice_id or line.move_id != asset.invoice_id:
                    raise ValidationError(_(
                        "Asset %(asset)s origin line must belong to its exact "
                        "origin vendor bill.", asset=asset.display_name,
                    ))
                if line.company_id != company or line.display_type != 'product':
                    raise ValidationError(_(
                        "Asset %(asset)s origin must be a product line in the "
                        "same company.", asset=asset.display_name,
                    ))
                if line.eh_asset_id and line.eh_asset_id != asset:
                    raise ValidationError(_(
                        "Origin bill line %(line)s already points to a "
                        "different generated asset.", line=line.display_name,
                    ))
            reversal = asset.origin_reversal_move_id
            if reversal and (
                    reversal.company_id != company
                    or reversal.reversed_entry_id != asset.invoice_id
                    or reversal.state != 'posted'):
                raise ValidationError(_(
                    "Asset origin reversal must be a posted reversal of the "
                    "exact same-company origin vendor bill."
                ))
            correction = asset.origin_correction_move_id
            if correction and correction.company_id != company:
                raise ValidationError(_(
                    "Asset origin-correction evidence must belong to the same "
                    "company as the asset."
                ))
            disposal_line = asset.disposal_invoice_line_id
            if disposal_line and (
                    not asset.disposal_invoice_id
                    or disposal_line.move_id != asset.disposal_invoice_id
                    or disposal_line.company_id != company):
                raise ValidationError(_(
                    "Asset sale evidence must be an exact line of the linked "
                    "same-company customer invoice."
                ))
            parent = asset.parent_asset_id
            if parent and (
                    parent.company_id != company
                    or parent.currency_id != asset.currency_id):
                raise ValidationError(_(
                    "Asset %(asset)s and its parent must use the same company "
                    "and currency.", asset=asset.display_name,
                ))
            cgu = asset.cgu_id
            if cgu and (
                    cgu.company_id != company
                    or cgu.currency_id != asset.currency_id):
                raise ValidationError(_(
                    "Asset %(asset)s and CGU %(cgu)s must use the same company "
                    "and currency.",
                    asset=asset.display_name, cgu=cgu.display_name,
                ))
            pool = asset.lvp_pool_id
            if pool and pool.company_id != company:
                raise ValidationError(_(
                    "Asset %(asset)s and low-value pool %(pool)s must belong "
                    "to the same company.",
                    asset=asset.display_name, pool=pool.display_name,
                ))
            seen = {asset.id} if asset.id else set()
            ancestor = parent
            while ancestor:
                if ancestor.id in seen:
                    raise ValidationError(_(
                        "Asset component hierarchy cannot contain a cycle."
                    ))
                seen.add(ancestor.id)
                ancestor = ancestor.parent_asset_id

    def _eh_validate_company_currency(self):
        """Fail closed before any GL or measurement side effect."""
        self._check_relationship_company()
        for asset in self:
            if asset.origin_source_quarantined:
                raise UserError(_(
                    "Asset %(asset)s is frozen because origin bill %(bill)s "
                    "was reversed by %(reversal)s. Original and reversal GL "
                    "evidence is retained; resolve it through an explicit "
                    "asset correction workflow before any further posting.",
                    asset=asset.display_name,
                    bill=asset.invoice_id.display_name,
                    reversal=asset.origin_reversal_move_id.display_name,
                ))
            if asset.currency_mismatch_quarantined:
                raise UserError(_(
                    "Asset %(asset)s is quarantined because its legacy "
                    "currency %(source)s differs from company currency "
                    "%(company)s after ledger entries were recorded. "
                    "Existing history was not rewritten. Reverse the linked "
                    "entries and recreate the asset in company currency.",
                    asset=asset.display_name,
                    source=asset.currency_id.display_name,
                    company=asset.company_id.currency_id.display_name,
                ))
            if asset.currency_id != asset.company_id.currency_id:
                raise UserError(_(
                    "Asset %(asset)s uses %(source)s but company %(company)s "
                    "posts in %(currency)s. Correct the asset currency "
                    "before computing or posting any accounting workflow.",
                    asset=asset.display_name,
                    source=asset.currency_id.display_name,
                    company=asset.company_id.display_name,
                    currency=asset.company_id.currency_id.display_name,
                ))
        return True

    def _eh_has_ledger_evidence(self):
        self.ensure_one()
        return bool(
            self.disposal_move_id
            or self.capitalisation_move_id
            or self.capitalised_at
            or self.revaluation_move_ids
            or self.lvp_transfer_move_id
            or self.lvp_pool_id
            or self.lvp_transferred_at
            or self.revaluation_adjustment
            or self.revaluation_surplus
            or self.revaluation_pl_decrease
            or any(
                line.is_posted or line.move_id
                for line in self.depreciation_line_ids
            )
            or any(
                line.is_posted or line.move_id or line.reversal_move_id
                for book in self.book_ids
                for line in book.line_ids
            )
            or any(
                impairment.state == 'posted' or impairment.move_id
                for impairment in self.impairment_ids
            )
        )

    def _eh_accounting_evidence_dates(self):
        """Return every known dated event that constrains chronology.

        Each accounting workflow must compare against the same closure. A
        local list of only primary depreciation dates lets later impairment,
        revaluation, parallel-book, AUC, pool, HFS, or IAS 40 evidence be
        backdated underneath a sealed entry.
        """
        self.ensure_one()
        dates = {
            value for value in (self.acquisition_date, self.in_service_date)
            if value
        }

        def add_move(move):
            for entry in move:
                if entry and entry.date and entry.state in ('posted', 'cancel'):
                    dates.add(entry.date)

        add_move(self.invoice_id)
        add_move(self.origin_reversal_move_id)
        add_move(self.origin_correction_move_id)
        add_move(self.disposal_invoice_id)
        add_move(self.capitalisation_move_id)
        add_move(self.disposal_move_id)
        add_move(self.lvp_transfer_move_id)
        add_move(self.revaluation_move_ids)
        if self.lvp_allocation_date:
            dates.add(self.lvp_allocation_date)
        if self.disposal_date:
            dates.add(self.disposal_date)
        for line in self.depreciation_line_ids:
            if line.is_posted or line.move_id:
                if line.depreciation_date:
                    dates.add(line.depreciation_date)
                add_move(line.move_id)
        for book in self.book_ids:
            for line in book.line_ids:
                if line.is_posted or line.move_id or line.reversal_move_id:
                    if line.depreciation_date:
                        dates.add(line.depreciation_date)
                    add_move(line.move_id)
                    add_move(line.reversal_move_id)
        for event in self.impairment_ids:
            if event.state in ('posted', 'cancelled') \
                    or event.move_id or event.reversal_move_id:
                if event.impairment_date:
                    dates.add(event.impairment_date)
                add_move(event.move_id)
                add_move(event.reversal_move_id)

        # Optional owner modules extend the same asset lifecycle. Resolve
        # their evidence dynamically so Assets Pro remains installable alone.
        if 'eh.held.for.sale' in self.env:
            items = self.env['eh.held.for.sale'].sudo().search([
                ('asset_id', '=', self.id),
            ])
            for item in items:
                if item.classification_date:
                    dates.add(item.classification_date)
                add_move(item.move_ids)
        if 'eh.disposal.group.line' in self.env:
            group_lines = self.env['eh.disposal.group.line'].sudo().search([
                ('asset_id', '=', self.id),
            ])
            for group in group_lines.mapped('group_id'):
                if group.classification_date:
                    dates.add(group.classification_date)
                add_move(group.move_ids)
        if 'eh.investment.property.transfer' in self.env:
            transfers = self.env['eh.investment.property.transfer'].sudo().search([
                ('source_asset_id', '=', self.id),
            ])
            dates.update(filter(None, transfers.mapped('date')))
            add_move(transfers.mapped('move_id'))
            add_move(transfers.mapped('remeasure_move_id'))
        if 'eh.investment.property' in self.env:
            properties = self.env['eh.investment.property'].sudo().search([
                ('transfer_in_asset_id', '=', self.id),
            ])
            dates.update(filter(None, properties.mapped('acquisition_date')))
            add_move(properties.mapped('move_ids'))
        return sorted(dates)

    def _eh_latest_accounting_evidence_date(self):
        self.ensure_one()
        dates = self._eh_accounting_evidence_dates()
        return max(dates) if dates else False

    def _eh_lock_for_transition(self):
        """Serialise asset postings and lifecycle transitions."""
        if not self.ids:
            return self
        if not self.env.su:
            self._eh_check_access('write')
        self.env.cr.execute(
            'SELECT id FROM eh_asset WHERE id IN %s ORDER BY id FOR UPDATE',
            (tuple(self.ids),),
        )
        self.invalidate_recordset([
            'state', 'currency_id', 'company_id',
            'currency_mismatch_quarantined', 'depreciation_line_ids',
            'book_ids', 'impairment_ids', 'net_book_value',
            'total_depreciated', 'revaluation_adjustment',
            'revaluation_surplus', 'revaluation_pl_decrease',
            'recoverable_amount_latest', 'recoverable_amount_date',
            'disposal_move_id', 'lvp_pool_id',
            'lvp_transfer_move_id',
            'capitalisation_move_id', 'revaluation_move_ids',
            'invoice_id', 'invoice_line_id',
            'origin_source_quarantined', 'origin_reversal_move_id',
            'origin_correction_move_id', 'origin_resolution_note',
            'origin_quarantine_resolved_at',
        ])
        return self

    def write(self, vals):
        vals = dict(vals or {})
        provenance_fields = {
            'invoice_line_id', 'origin_source_quarantined',
            'origin_source_quarantine_note', 'origin_reversal_move_id',
            'origin_reversed_at', 'origin_reversed_by_id',
            'origin_quarantine_resolved_at',
            'origin_quarantine_resolved_by_id',
        }
        if provenance_fields.intersection(vals) \
                and self.env.context.get(_ASSET_AUTOCREATE_CONTEXT_KEY) \
                is not _ASSET_AUTOCREATE_CAPABILITY:
            raise AccessError(_(
                "Origin-line and source-reversal evidence is server-owned "
                "and can only be changed by the vendor-bill asset engine."
            ))
        resolution_inputs = {
            'origin_correction_move_id', 'origin_resolution_note',
        }.intersection(vals)
        if resolution_inputs and self.filtered(
                lambda asset: not asset.origin_source_quarantined
                or asset.origin_quarantine_resolved_at):
            raise UserError(_(
                "Origin-correction evidence can only be entered while the "
                "asset is in unresolved source quarantine."
            ))
        sensitive = (
            set(self._FROZEN_AFTER_POST)
            | set(self._ACCOUNTING_COMPANY_FIELDS)
            | set(self._eh_guarded_fields)
            | {'category_id', 'partner_id', 'invoice_id', 'invoice_line_id'}
        ).intersection(vals)
        if vals and not self.env.su:
            self._eh_check_access('write')
        # Canonical lock order for CGU-sensitive changes is CGU -> asset.
        # action_test_now and impairment posting use the same order, avoiding
        # an asset/CGU deadlock while making membership immutable for the
        # duration of a test.
        if {'cgu_id', 'company_id', 'currency_id'} & set(vals):
            cgus = self.mapped('cgu_id')
            if vals.get('cgu_id'):
                cgus |= self.env['eh.asset.cgu'].browse(vals['cgu_id'])
            if not self.env.su:
                cgus._eh_check_access('write')
            cgus._eh_lock_for_test()
            cgus._eh_validate_company_currency()
        if sensitive:
            self._eh_lock_for_transition()
            self._eh_assert_external_master_write_allowed(sensitive)
        if vals and self.filtered('currency_mismatch_quarantined'):
            raise UserError(_(
                "A currency-quarantined asset is read-only because changing "
                "its source would detach existing ledger history. Reverse "
                "the linked entries and recreate it in company currency.",
            ))
        frozen = [f for f in self._FROZEN_AFTER_POST if f in vals]
        # A revaluation action may bind the previously-empty reserve account
        # while it stamps the sealed move that proves that exact account.
        # The unforgeable in-process capability keeps the normal RPC/ORM
        # freeze intact; a client-supplied context value can never be the
        # module-local object by identity.
        if (frozen == ['revaluation_reserve_account_id']
                and self.env.context.get(_ASSET_REVALUATION_CONTEXT_KEY)
                is _ASSET_REVALUATION_CAPABILITY):
            frozen = []
        if frozen:
            posted = self.filtered(
                lambda asset: asset._eh_has_ledger_evidence(),
            )
            if posted:
                raise UserError(_(
                    "Measurement inputs (%(fields)s) are frozen once this "
                    "asset has ledger evidence; its accounting basis cannot "
                    "be rewritten underneath sealed entries. Use the relevant "
                    "revaluation, impairment, disposal, or policy-change "
                    "workflow instead.",
                    fields=', '.join(frozen)))
        company_ids = set(self.mapped('company_id.id'))
        result = super().write(vals)
        if vals:
            company_ids.update(self.mapped('company_id.id'))
            self.env['res.company'].sudo()._eh_bump_move_version(company_ids)
        return result

    def _eh_assert_external_master_write_allowed(self, field_names):
        """Extension hook for exclusive HFS / IAS 40 ownership.

        The base register has no dependency on those modules. Owners inherit
        this hook and reject direct master-data mutation while their lifecycle
        is active, after the canonical asset row lock has been acquired.
        """
        return True

    def unlink(self):
        # An asset that has posted depreciation, or a disposal move, carries
        # a posting-move link (its lines' JEs and the disposal_move_id);
        # deleting the master would orphan a posted GL entry. Block it. A
        # draft asset with no posted line and no disposal move stays deletable.
        if not self.env.su:
            self._eh_check_access('unlink')
        posted = self.filtered(
            lambda asset: asset._eh_has_ledger_evidence(),
        )
        if posted:
            raise UserError(_(
                "An asset with posted primary/book depreciation or a "
                "disposal entry cannot be deleted; its journal entries "
                "would be orphaned. Dispose of it instead."))
        company_ids = set(self.mapped('company_id.id'))
        result = super().unlink()
        if company_ids:
            self.env['res.company'].sudo()._eh_bump_move_version(company_ids)
        return result

    # ---- transitions ----

    def action_compute_schedule(self):
        """Generate (or regenerate) the depreciation schedule.

        Only allowed in draft. Wipes any existing draft (unposted) lines.
        Posted lines are preserved and the schedule resumes from the
        last posted line.
        """
        self._eh_lock_for_transition()
        self = self._eh_workflow_action()
        for asset in self:
            asset._eh_validate_company_currency()
            if asset.state not in ('draft',):
                raise UserError(_(
                    "Schedule can only be (re)generated while the asset "
                    "is in draft state.",
                ))
            if asset.is_under_construction:
                raise UserError(_(
                    "Asset %s is under construction; capitalise it "
                    "before computing the schedule.",
                ) % asset.display_name)
            if asset.lvp_pool_id:
                raise UserError(_(
                    "Asset %(asset)s belongs to low-value pool %(pool)s; its "
                    "individual schedule cannot be recomputed.",
                    asset=asset.display_name,
                    pool=asset.lvp_pool_id.display_name,
                ))
            if asset._eh_is_indefinite_intangible():
                raise UserError(_(
                    "%s has an indefinite useful life; IAS 38.107 "
                    "prohibits amortisation, so no schedule is generated. "
                    "The asset is instead subject to a mandatory annual "
                    "impairment test (IAS 36.10).",
                ) % asset.display_name)
            asset._wipe_unposted_lines()
            asset._build_schedule()

    def action_capitalise(self, capitalisation_date=None):
        """Capitalise an asset under construction.

        Sets in_service_date to capitalisation_date (defaults to today),
        flips is_under_construction off, posts a balanced JE moving
        the carrying amount from auc_account_id to asset_account_id
        when both are configured, generates the schedule, and
        activates the asset.

        Refuses to run when:
          * is_under_construction is False (nothing to capitalise).
          * acquisition_cost is zero (no carrying amount to transfer).
        """
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an accounting manager can capitalise an asset under "
                "construction. This posting is a segregation-of-duties "
                "control point.",
            ))
        self._eh_lock_for_transition()
        self = self._eh_workflow_action()
        for asset in self:
            asset._eh_validate_company_currency()
            if asset.state != 'draft':
                raise UserError(_(
                    "Only a draft asset under construction can be "
                    "capitalised; %(asset)s is %(state)s.",
                    asset=asset.display_name, state=asset.state,
                ))
            if not asset.is_under_construction:
                raise UserError(_(
                    "Asset %s is not under construction.",
                ) % asset.display_name)
            if asset.capitalised_at or asset.capitalisation_move_id:
                raise UserError(_(
                    "Asset %s already carries capitalisation evidence and "
                    "cannot be capitalised a second time.",
                ) % asset.display_name)
            if not asset.acquisition_cost:
                raise UserError(_(
                    "Asset %s has no carrying amount to capitalise.",
                ) % asset.display_name)
            asset._check_ias38_dev_gate()
            cap_date = (
                capitalisation_date
                or fields.Date.context_today(self)
            )
            cap_date = fields.Date.to_date(cap_date)
            if cap_date < asset.acquisition_date:
                raise UserError(_(
                    "Capitalisation date %(date)s cannot precede the "
                    "acquisition date %(acquisition)s on %(asset)s.",
                    date=cap_date, acquisition=asset.acquisition_date,
                    asset=asset.display_name,
                ))
            asset.in_service_date = cap_date
            asset.is_under_construction = False
            # Post the AUC -> asset transfer JE when both accounts and
            # a journal are configured. Sites that prefer to reclassify
            # the carrying amount manually can leave auc_account_id
            # blank; in that case we skip the JE and rely on the user
            # to make the reclassification entry by hand.
            capitalisation_move = self.env['account.move']
            if asset.auc_account_id and asset.asset_account_id and asset.journal_id:
                capitalisation_move = asset._eh_post_auc_capitalisation_move(
                    cap_date,
                )
            # Generate schedule + activate so the cron starts posting.
            # An indefinite-life intangible (IAS 38.107) carries no
            # amortisation schedule; it activates schedule-less and is
            # covered by the annual impairment-test cron instead.
            asset._wipe_unposted_lines()
            if not asset._eh_is_indefinite_intangible():
                asset._build_schedule()
                asset._validate_posting_setup()
            asset.write({
                'state': 'running',
                'activated_at': fields.Datetime.now(),
                'activated_by_id': self.env.user.id,
                'capitalised_at': fields.Datetime.now(),
                'capitalised_by_id': self.env.user.id,
                'capitalisation_move_id': (
                    capitalisation_move.id if capitalisation_move else False
                ),
            })
            asset.message_post(body=_(
                "Capitalised on %(date)s. Depreciation schedule "
                "generated; the cron will post due lines from the "
                "next pass.",
                date=cap_date,
            ))
        return True

    def _eh_post_auc_capitalisation_move(self, cap_date):
        """Move the carrying amount from AUC holding to the asset account."""
        self.ensure_one()
        self._eh_validate_company_currency()
        label = _("Capitalisation %s") % self.display_name
        move = self.env['account.move']._eh_create_sealed({
            'move_type': 'entry',
            'eh_sealed': True,
            'journal_id': self.journal_id.id,
            'date': cap_date,
            'ref': self.name,
            'line_ids': [
                (0, 0, {
                    'name': label,
                    'account_id': self.asset_account_id.id,
                    'debit': self.acquisition_cost,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': label,
                    'account_id': self.auc_account_id.id,
                    'debit': 0.0,
                    'credit': self.acquisition_cost,
                }),
            ],
        })
        move.action_post()
        return move

    def action_activate(self):
        self._eh_lock_for_transition()
        self = self._eh_workflow_action()
        for asset in self:
            asset._eh_validate_company_currency()
            if asset.state != 'draft':
                raise UserError(_(
                    "Only draft assets can be activated.",
                ))
            if asset.is_under_construction:
                raise UserError(_(
                    "Asset %s is under construction; capitalise it "
                    "first via action_capitalise.",
                ) % asset.display_name)
            if asset.lvp_pool_id:
                raise UserError(_(
                    "Asset %(asset)s belongs to low-value pool %(pool)s and "
                    "cannot activate an individual depreciation schedule.",
                    asset=asset.display_name,
                    pool=asset.lvp_pool_id.display_name,
                ))
            asset._check_ias38_dev_gate()
            if asset._eh_is_indefinite_intangible():
                # IAS 38.107: no amortisation schedule; the asset runs
                # schedule-less under the annual impairment-test regime.
                pass
            else:
                if not asset.depreciation_line_ids:
                    asset._build_schedule()
                asset._validate_posting_setup()
            asset.write({
                'state': 'running',
                'activated_at': fields.Datetime.now(),
                'activated_by_id': self.env.user.id,
            })

    def action_pause(self):
        self._eh_lock_for_transition()
        self = self._eh_workflow_action()
        for asset in self:
            if asset.state != 'running':
                raise UserError(_(
                    "Only running assets can be paused.",
                ))
            asset.state = 'paused'

    def action_resume(self):
        self._eh_lock_for_transition()
        self = self._eh_workflow_action()
        for asset in self:
            asset._eh_validate_company_currency()
            if asset.state != 'paused':
                raise UserError(_(
                    "Only paused assets can be resumed.",
                ))
            if asset.lvp_pool_id:
                raise UserError(_(
                    "Asset %(asset)s belongs to low-value pool %(pool)s and "
                    "cannot resume individual depreciation.",
                    asset=asset.display_name,
                    pool=asset.lvp_pool_id.display_name,
                ))
            asset.state = 'running'

    def action_set_to_draft(self):
        self._eh_lock_for_transition()
        self = self._eh_workflow_action()
        for asset in self:
            if asset.state == 'disposed':
                raise UserError(_(
                    "Disposed assets cannot return to draft.",
                ))
            if asset._eh_has_ledger_evidence():
                raise UserError(_(
                    "Cannot return to draft once the asset has ledger or "
                    "capitalisation/pool-transfer evidence.",
                ))
            asset.state = 'draft'

    def action_open_revalue_wizard(self):
        self.ensure_one()
        if self.state not in ('running', 'paused'):
            raise UserError(_(
                "Revaluation requires a running or paused asset.",
            ))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'eh.asset.revalue.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_asset_id': self.id},
        }

    def action_open_dispose_wizard(self):
        self.ensure_one()
        if self.deferred_type != 'asset':
            raise UserError(_(
                "Deferred revenue and deferred expense records are recognition "
                "schedules, not fixed assets. They cannot use IAS 16 asset "
                "disposal accounting. Correct, finish, or explicitly reverse "
                "the recognition schedule instead."
            ))
        if self.state in ('disposed',):
            raise UserError(_(
                "Asset is already disposed.",
            ))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'eh.asset.dispose.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_asset_id': self.id},
        }

    def action_open_lvp_transfer_wizard(self):
        self.ensure_one()
        self._eh_check_access('read')
        if self.lvp_pool_id:
            raise UserError(_(
                "Asset %(asset)s is already in low-value pool %(pool)s.",
                asset=self.display_name,
                pool=self.lvp_pool_id.display_name,
            ))
        if self.deferred_type != 'asset' or self.is_under_construction:
            raise UserError(_(
                "Only a capitalised fixed-asset record can enter a low-value "
                "pool; deferred recognition and AUC schedules are excluded."
            ))
        if self.state not in ('draft', 'running', 'paused'):
            raise UserError(_(
                "Only a draft, running, or paused asset can enter a low-value "
                "pool; %(asset)s is %(state)s.",
                asset=self.display_name,
                state=self.state,
            ))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Transfer Asset to Low-Value Pool"),
            'res_model': 'eh.asset.lvp.transfer.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {
                'default_company_id': self.company_id.id,
                'default_asset_id': self.id,
            },
        }

    def action_post_due_lines(self):
        """Force post all depreciation lines whose date is today or earlier."""
        today = fields.Date.context_today(self)
        for asset in self:
            if asset.state not in ('running',):
                continue
            due = asset.depreciation_line_ids.filtered(
                lambda l: not l.is_posted and l.depreciation_date <= today,
            ).sorted('depreciation_date')
            for line in due:
                line.action_post()
            asset._maybe_mark_fully_depreciated()

    # ---- helpers ----

    def _wipe_unposted_lines(self):
        self.ensure_one()
        unposted = self.depreciation_line_ids.filtered(lambda l: not l.is_posted)
        unposted.unlink()

    def _validate_posting_setup(self):
        """Verify the accounts and journal needed to post this schedule.

        Deferred revenue and deferred expense assets require both an
        asset/liability holding account (asset_account_id) and a P/L
        recognition account (depreciation_account_id), but not an
        accumulated_depreciation_account_id since the balance sheet
        leg is the holding account itself, not a contra account.
        """
        self.ensure_one()
        self._eh_validate_company_currency()
        missing = []
        if not self.journal_id:
            missing.append(_("Depreciation Journal"))
        if self.deferred_type == 'asset':
            if not self.depreciation_account_id:
                missing.append(_("Depreciation Expense Account"))
            if not self.accumulated_depreciation_account_id:
                missing.append(_("Accumulated Depreciation Account"))
        elif self.deferred_type == 'deferred_revenue':
            if not self.asset_account_id:
                missing.append(_("Deferred Revenue Liability Account"))
            if not self.depreciation_account_id:
                missing.append(_("Revenue Recognition Account"))
        elif self.deferred_type == 'deferred_expense':
            if not self.asset_account_id:
                missing.append(_("Prepaid Expense Asset Account"))
            if not self.depreciation_account_id:
                missing.append(_("Expense Recognition Account"))
        if missing:
            raise UserError(_(
                "Asset %(asset)s is missing posting setup: %(missing)s.",
                asset=self.display_name,
                missing=", ".join(missing),
            ))

    def _build_schedule(self):
        self.ensure_one()
        Line = self.env['eh.asset.depreciation.line'].sudo()
        rows = self._generate_schedule_rows()
        if rows:
            Line.create([{
                'asset_id': self.id,
                'sequence': row['sequence'],
                'depreciation_date': row['date'],
                'amount': row['amount'],
                'accumulated': row['accumulated'],
                'remaining_value': row['remaining'],
            } for row in rows])

    def _generate_schedule_rows(self):
        """Compute the depreciation schedule as a list of dicts.

        Output:
          [{sequence, date, amount, accumulated, remaining}, ...]
        Pure function: does not write to DB.

        is_instant_write_off short-circuits to a single line for the
        full depreciable amount on the in-service date's month-end,
        regardless of method or useful life.
        """
        self.ensure_one()
        if self.method == 'manual':
            return []
        depreciable = self.acquisition_cost - self.salvage_value
        if depreciable <= 0:
            return []
        if self.is_instant_write_off:
            return self._schedule_instant_write_off(depreciable)
        if self.method in ('straight_line', 'prime_cost'):
            return self._schedule_straight_line(depreciable)
        if self.method in ('reducing_balance', 'diminishing_value'):
            return self._schedule_reducing_balance(depreciable)
        if self.method == 'units_of_production':
            return self._schedule_uop(depreciable)
        return []

    def _schedule_instant_write_off(self, depreciable):
        """One-shot full write-off in the first period.

        AU instant-asset-write-off and similar fast-deduction regimes
        depreciate the entire eligible amount in the year of acquisition.
        We post a single line at the in-service month-end so the GL
        impact is immediate; the cron picks it up on its next pass.
        """
        amount = self.currency_id.round(depreciable)
        return [{
            'sequence': 1,
            'date': self._first_period_date(),
            'amount': amount,
            'accumulated': amount,
            'remaining': self.currency_id.round(
                self.acquisition_cost - amount,
            ),
        }]

    def _schedule_straight_line(self, depreciable):
        rows = []
        months = self.useful_life_months
        per_period = depreciable / months
        period_fractions = self._schedule_period_fractions(months)
        accumulated = 0.0
        period_date = self._first_period_date()
        for n, period_fraction in enumerate(period_fractions, start=1):
            if n == len(period_fractions):
                amount = depreciable - accumulated
            else:
                amount = per_period * period_fraction
            amount = self.currency_id.round(amount)
            accumulated = self.currency_id.round(accumulated + amount)
            remaining = self.currency_id.round(
                self.acquisition_cost - accumulated,
            )
            rows.append({
                'sequence': n,
                'date': period_date,
                'amount': amount,
                'accumulated': accumulated,
                'remaining': remaining,
            })
            period_date = self._next_period_end(period_date)
        return rows

    def _schedule_reducing_balance(self, depreciable):
        rows = []
        months = self.useful_life_months
        years = max(1, months / 12.0)
        sl_rate_per_year = 1.0 / years
        rate_per_year = self.declining_factor * sl_rate_per_year
        rate_per_period = rate_per_year / 12.0
        period_fractions = self._schedule_period_fractions(months)
        # Floor switch: when straight line on remaining balance exceeds
        # reducing balance, switch to straight line for the rest.
        accumulated = 0.0
        nbv = self.acquisition_cost
        period_date = self._first_period_date()
        for n, period_fraction in enumerate(period_fractions, start=1):
            remaining_periods = sum(period_fractions[n - 1:])
            sl_amount = max(
                0.0,
                (nbv - self.salvage_value) / remaining_periods,
            )
            rb_amount = (
                max(0.0, (nbv - self.salvage_value)) * rate_per_period
            )
            amount = max(rb_amount, sl_amount) * period_fraction
            if n == len(period_fractions):
                amount = depreciable - accumulated
            amount = max(0.0, self.currency_id.round(amount))
            if accumulated + amount > depreciable:
                amount = self.currency_id.round(depreciable - accumulated)
            accumulated = self.currency_id.round(accumulated + amount)
            nbv = self.acquisition_cost - accumulated
            rows.append({
                'sequence': n,
                'date': period_date,
                'amount': amount,
                'accumulated': accumulated,
                'remaining': self.currency_id.round(nbv),
            })
            period_date = self._next_period_end(period_date)
            if accumulated >= depreciable:
                break
        return rows

    def _schedule_uop(self, depreciable):
        # Units of production depreciation is a preview method: usage
        # recording is not yet implemented, so a schedule cannot be
        # generated. Block activation with a clear message rather than
        # emitting a zero amount placeholder schedule.
        raise UserError(_(
            "Units of Production depreciation is a preview method and is "
            "not yet available. Choose Straight Line or Reducing Balance "
            "for the primary GL book, or use an additional book for a "
            "parallel method."
        ))

    def _first_period_date(self):
        d = self.in_service_date
        return self._month_end(d)

    @staticmethod
    def _month_end(d):
        last = calendar.monthrange(d.year, d.month)[1]
        return date(d.year, d.month, last)

    def _next_period_end(self, d):
        nxt = d + relativedelta(months=1)
        return self._month_end(nxt)

    def _first_period_prorated_amount(self, full_period_amount):
        """Compute the prorated first period amount based on days in service."""
        d = self.in_service_date
        last = calendar.monthrange(d.year, d.month)[1]
        days_in_service = last - d.day + 1
        return full_period_amount * (days_in_service / float(last))

    def _first_period_amount(self, base):
        """First-period charge for the effective prorata mode.

        Mode resolves from prorata_mode when set, otherwise from the
        legacy prorate_first_period switch (True -> daily, False -> none),
        so existing assets keep their schedule unchanged.
        """
        mode = self.prorata_mode or (
            'daily' if self.prorate_first_period else 'none')
        if mode == 'none':
            return base
        if mode == 'half':
            return base / 2.0
        return self._first_period_prorated_amount(base)

    def _schedule_period_fractions(self, months):
        """Return calendar-period fractions spanning exact useful life.

        A prorated first calendar month consumes only its day/half-period
        fraction of useful life.  Keep normal full monthly charges after it,
        then emit a final stub for remaining fraction.  Folding shortfall into
        nominal last month would overstate that month's depreciation.
        """
        self.ensure_one()
        first_fraction = max(0.0, min(1.0, self._first_period_amount(1.0)))
        remaining = max(0.0, float(months) - first_fraction)
        full_periods = int(remaining)
        final_fraction = remaining - full_periods
        fractions = [first_fraction] + [1.0] * full_periods
        if final_fraction > 1e-12:
            fractions.append(final_fraction)
        return fractions

    def _build_remaining_schedule(self, periods, start_date=None):
        """Rebuild future charges without changing the depreciation method.

        IAS 36.63 and IAS 16.61 require prospective allocation on the revised
        carrying amount using the asset's existing systematic consumption
        pattern. Straight-line / prime-cost assets therefore remain linear;
        reducing-balance / diminishing-value assets keep their declining rate
        and the existing straight-line floor switch. Sequence numbers continue
        from immutable posted evidence and the final row lands exactly on
        salvage after currency rounding.
        """
        self.ensure_one()
        Line = self.env['eh.asset.depreciation.line'].sudo()
        depreciable = self.net_book_value - self.salvage_value
        if depreciable <= 0 or periods <= 0:
            return
        posted = self.depreciation_line_ids.filtered(lambda l: l.is_posted)
        last_seq = max(posted.mapped('sequence')) if posted else 0
        anchors = list(posted.mapped('depreciation_date'))
        if start_date:
            anchors.append(start_date)
        last_date = max(anchors) if anchors else self.in_service_date
        accumulated_after_posted = sum(posted.mapped('amount'))
        period_date = self._next_period_end(last_date)
        carrying = self.net_book_value
        rate_per_period = (
            (self.declining_factor or 0.0) / self.useful_life_months
            if self.method in ('reducing_balance', 'diminishing_value')
            else 0.0
        )
        rows = []
        allocated = 0.0
        for i in range(1, periods + 1):
            if i == periods:
                amount = depreciable - allocated
            elif rate_per_period:
                headroom = max(0.0, carrying - self.salvage_value)
                reducing = headroom * rate_per_period
                straight_line_floor = headroom / (periods - i + 1)
                amount = max(reducing, straight_line_floor)
            else:
                amount = depreciable / periods
            amount = max(0.0, self.currency_id.round(amount))
            amount = min(amount, self.currency_id.round(depreciable - allocated))
            allocated = self.currency_id.round(allocated + amount)
            carrying = self.currency_id.round(carrying - amount)
            accumulated_after_posted = self.currency_id.round(
                accumulated_after_posted + amount,
            )
            rows.append({
                'asset_id': self.id,
                'sequence': last_seq + i,
                'depreciation_date': period_date,
                'amount': amount,
                'accumulated': accumulated_after_posted,
                'remaining_value': carrying,
            })
            period_date = self._next_period_end(period_date)
        if rows:
            Line.create(rows)

    def _eh_post_depreciation_through(self, event_date):
        """Post earned depreciation, including a final event-date stub.

        IAS 16.55 stops depreciation on derecognition, not at the preceding
        schedule boundary. Full rows due through ``event_date`` post first. If
        the event falls inside the next schedule period, an immutable prorated
        row is then created and posted at the exact event date. Future forecast
        rows remain unposted for the calling disposal workflow to remove.
        """
        self.ensure_one()
        if self.state not in ('running', 'paused'):
            return self.env['eh.asset.depreciation.line']
        due = self.depreciation_line_ids.filtered(
            lambda line: not line.is_posted
            and line.depreciation_date <= event_date,
        ).sorted(lambda line: (line.depreciation_date, line.sequence))
        for line in due:
            line.action_post()

        future = self.depreciation_line_ids.filtered(
            lambda line: not line.is_posted
            and line.depreciation_date > event_date,
        ).sorted(lambda line: (line.depreciation_date, line.sequence))
        if not future:
            return due
        next_line = future[0]
        posted = self.depreciation_line_ids.filtered('is_posted')
        period_start = (
            max(posted.mapped('depreciation_date')) + timedelta(days=1)
            if posted else self.in_service_date
        )
        if event_date < period_start:
            return due
        span_days = (next_line.depreciation_date - period_start).days + 1
        earned_days = (event_date - period_start).days + 1
        if span_days <= 0 or earned_days <= 0:
            return due
        stub_amount = self.currency_id.round(
            next_line.amount * min(1.0, earned_days / float(span_days)),
        )
        self.invalidate_recordset(['net_book_value', 'total_depreciated'])
        available = self.currency_id.round(
            max(0.0, self.net_book_value - self.salvage_value),
        )
        stub_amount = min(stub_amount, available)
        if self.currency_id.is_zero(stub_amount):
            return due
        all_sequences = self.depreciation_line_ids.mapped('sequence')
        accumulated = self.currency_id.round(
            sum(posted.mapped('amount')) + stub_amount,
        )
        stub = self.env['eh.asset.depreciation.line'].sudo().create({
            'asset_id': self.id,
            'sequence': (max(all_sequences) if all_sequences else 0) + 1,
            'depreciation_date': event_date,
            'amount': stub_amount,
            'accumulated': accumulated,
            'remaining_value': self.currency_id.round(
                self.net_book_value - stub_amount,
            ),
            'is_event_accrual': True,
        })
        stub.action_post()
        return due | stub

    def _maybe_mark_fully_depreciated(self):
        self = self._eh_workflow_action()
        for asset in self:
            if asset.state != 'running':
                continue
            unposted = asset.depreciation_line_ids.filtered(
                lambda l: not l.is_posted,
            )
            if not unposted and asset.net_book_value <= asset.salvage_value:
                asset.state = 'fully_depreciated'

    # ---- IAS 36 impairment helpers ----

    def _eh_rebuild_after_impairment(self):
        """Re-amortise remaining depreciation on the post-event carrying
        amount, per IAS 36.63.

        After an impairment loss (or its reversal) is recognised, the
        depreciation charge must be adjusted in future periods to allocate
        the asset's revised carrying amount, less residual value, on a
        systematic basis over its remaining useful life. We wipe the
        unposted lines and rebuild them over the same number of remaining
        periods on the current (post-event) net book value, reusing the
        same re-amortisation the revaluation wizard uses.

        Applies to regular depreciating fixed assets only. Manual, units of
        production, instant write-off, pooled, and deferred items keep
        their schedule untouched.
        """
        self.ensure_one()
        if self.deferred_type != 'asset':
            return
        if self.method in ('manual', 'units_of_production'):
            return
        if self.is_instant_write_off or self.lvp_pool_id:
            return
        unposted = self.depreciation_line_ids.filtered(
            lambda l: not l.is_posted,
        )
        remaining_periods = len(unposted)
        if remaining_periods <= 0:
            return
        self.invalidate_recordset(['net_book_value'])
        self._wipe_unposted_lines()
        self._build_remaining_schedule(remaining_periods)

    def _ias36_depreciated_cost(self, as_of_date=None):
        """Carrying amount the asset would have had if no impairment had
        ever been recognised: original cost less the depreciation that
        would have accrued, over the same number of elapsed (posted)
        periods, on the original cost base. IAS 36.117 caps an impairment
        reversal at this depreciated historical cost.

        Aligned on the count of posted periods (not on the calendar) so an
        asset whose depreciation cron is behind is not falsely treated as
        more depreciated than it is.

        Manual and units-of-production assets have no engine-generated
        schedule to replay, so the ceiling falls back to a HYPOTHETICAL
        straight line over the asset's useful life (see
        _ias36_hypothetical_sl_cost); as_of_date anchors the elapsed-time
        measurement for that fallback (defaults to today).
        """
        self.ensure_one()
        posted = self.depreciation_line_ids.filtered(lambda l: l.is_posted)
        posted_total = sum(posted.mapped('amount'))
        if self.method in ('manual', 'units_of_production'):
            # _generate_schedule_rows returns nothing for manual and
            # raises for units of production; both route to the
            # hypothetical straight-line fallback.
            return self._ias36_hypothetical_sl_cost(as_of_date, posted_total)
        rows = self._generate_schedule_rows()
        if not rows:
            return self._ias36_hypothetical_sl_cost(as_of_date, posted_total)
        hypothetical_accum = sum(r['amount'] for r in rows[:len(posted)])
        return self.currency_id.round(
            self.acquisition_cost - hypothetical_accum,
        )

    def _ias36_hypothetical_sl_cost(self, as_of_date, posted_total):
        """IAS 36.117 ceiling for assets with no engine schedule.

        IAS 36.117 caps a reversal at the carrying amount that "would
        have been determined (net of amortisation or depreciation) had
        no impairment loss been recognised". A manual-method asset has
        no engine schedule from which to replay that hypothetical, and
        the naive fallback (raw cost less whatever happens to have been
        posted) lets an asset with little or no posted depreciation
        reverse all the way back to full cost, over-reversing relative
        to ANY systematic depreciation basis.

        The defensible proxy is a hypothetical straight line over the
        asset's stated useful life (the default systematic basis of
        IAS 16.62 / IAS 38.97): elapsed month-end periods since the
        in-service date, times (cost - salvage) / useful_life_months.
        The ceiling is the LOWER of that hypothetical depreciated cost
        and cost less actually posted depreciation (a reversal must
        never restore depreciation that has genuinely been charged),
        floored at salvage value.
        """
        self.ensure_one()
        as_of = as_of_date or fields.Date.context_today(self)
        months = self.useful_life_months or 0
        depreciable = self.acquisition_cost - self.salvage_value
        if months <= 0 or depreciable <= 0:
            hypothetical = self.acquisition_cost
        else:
            elapsed = max(0, min(months, self._eh_elapsed_month_ends(as_of)))
            hypothetical = (
                self.acquisition_cost - depreciable * (elapsed / float(months))
            )
        actual = self.acquisition_cost - posted_total
        return self.currency_id.round(
            max(self.salvage_value, min(hypothetical, actual)),
        )

    def _eh_elapsed_month_ends(self, as_of):
        """Number of monthly periods elapsed under the module's month-end
        schedule convention: counts month-end dates from the in-service
        month end through as_of, inclusive only when as_of itself is a
        month end (a mid-month date has not completed its period)."""
        self.ensure_one()
        start = self._month_end(self.in_service_date)
        if as_of < start:
            return 0
        elapsed = (as_of.year - start.year) * 12 + (as_of.month - start.month)
        if as_of == self._month_end(as_of):
            elapsed += 1
        return elapsed

    # ---- cron ----

    @api.model
    def _cron_post_due(self, batch_size=200):
        today = fields.Date.context_today(self)
        due_lines = self.env['eh.asset.depreciation.line']._search([
            ('is_posted', '=', False),
            ('depreciation_date', '<=', today),
        ])
        domain = [
            ('state', '=', 'running'),
            ('depreciation_line_ids', 'in', due_lines),
        ]
        assets = self._eh_rotating_due_batch(
            domain,
            batch_size,
            'eh_account_assets_pro.asset_post_due_cursor',
        )

        # Per-asset savepoint via the shared batch mixin so one bad asset
        # does not poison the cursor and abort the rest of the batch.
        def _post_asset(asset):
            due = asset.depreciation_line_ids.filtered(
                lambda l: not l.is_posted
                and l.depreciation_date <= today,
            ).sorted('depreciation_date')
            for line in due:
                line.action_post()
            asset._maybe_mark_fully_depreciated()

        return self._eh_for_each_savepoint(
            assets, _post_asset, log_label="Auto post",
        )

    @api.model
    def _eh_rotating_due_batch(self, domain, batch_size, cursor_key):
        """Return one due-only keyset page and rotate past failed records."""
        size = max(1, int(batch_size or 200))
        params = self.env['ir.config_parameter'].sudo()
        try:
            cursor = max(0, int(params.get_param(cursor_key, '0') or 0))
        except (TypeError, ValueError):
            cursor = 0
        assets = self.search(
            list(domain) + [('id', '>', cursor)],
            order='id', limit=size,
        )
        if not assets and cursor:
            cursor = 0
            assets = self.search(domain, order='id', limit=size)
        params.set_param(cursor_key, str(assets[-1].id if assets else 0))
        return assets

    # ---- IAS 36 annual-test cron ----

    @api.model
    def _cron_ias36_annual_test(self, as_of=None, batch_size=500):
        """Enforce the IAS 36.10 annual impairment-test mandate.

        Population: running/paused goodwill assets and indefinite-life
        intangibles (the assets IAS 36.10 requires to be tested
        annually irrespective of indicators). Once the company's annual
        test month (Settings, default December) has been reached inside
        the current fiscal year, any population asset with no test
        evidence dated in that fiscal year is flagged
        annual_test_overdue and receives a to-do activity; the flag
        clears automatically when a CGU test runs or an impairment
        event posts (and on the next cron pass after either).

        ``as_of`` overrides "today" for deterministic testing.
        """
        today = fields.Date.to_date(as_of) if as_of else (
            fields.Date.context_today(self)
        )
        assets = self.search([
            ('state', 'in', ['running', 'paused']),
            '|',
            ('is_goodwill', '=', True),
            '&',
            ('asset_class', '=', 'intangible'),
            ('is_indefinite_life', '=', True),
        ], limit=batch_size)

        def _review(asset):
            fy = asset.company_id.compute_fiscalyear_dates(today)
            trigger = asset._eh_annual_test_trigger_date(fy)
            tested = asset._eh_ias36_tested_in(
                fy['date_from'], fy['date_to'],
            )
            overdue = today >= trigger and not tested
            if asset.annual_test_overdue != overdue:
                asset._eh_workflow_write({
                    'annual_test_overdue': overdue,
                })
            if overdue:
                asset._eh_schedule_annual_test_activity(fy)

        self._eh_for_each_savepoint(
            assets, _review, log_label="IAS 36 annual test",
        )

    def _eh_annual_test_trigger_date(self, fy):
        """First day of the company's annual test month inside the
        fiscal year [fy['date_from'], fy['date_to']]."""
        self.ensure_one()
        month = self.company_id.eh_ias36_annual_test_month or 12
        candidate = date(fy['date_from'].year, month, 1)
        if candidate < fy['date_from']:
            candidate = date(fy['date_from'].year + 1, month, 1)
        return min(candidate, fy['date_to'])

    def _eh_ias36_tested_in(self, date_from, date_to):
        """Impairment-test evidence for this asset inside a date range:
        the asset's CGU ran a test (passed or impaired) in the range,
        or an impairment event (charge or reversal) posted in the
        range. Both funnel through the segregation-of-duties posting
        gates, so either is auditable evidence a test was performed."""
        self.ensure_one()
        cgu = self.cgu_id
        if (cgu and cgu.last_test_date
                and date_from <= cgu.last_test_date <= date_to):
            return True
        return bool(self.impairment_ids.filtered(
            lambda i: i.state == 'posted'
            and date_from <= i.impairment_date <= date_to,
        ))

    def _eh_schedule_annual_test_activity(self, fy):
        """One open to-do per asset (module idiom: activity, not email)
        prompting the annual test; deduped on the summary."""
        self.ensure_one()
        summary = _("IAS 36 annual impairment test overdue")
        existing = self.env['mail.activity'].search_count([
            ('res_model', '=', self._name),
            ('res_id', '=', self.id),
            ('summary', '=', summary),
        ])
        if existing:
            return
        self.activity_schedule(
            'mail.mail_activity_data_todo',
            summary=summary,
            note=_(
                "IAS 36.10 requires goodwill and indefinite-life "
                "intangible assets to be tested for impairment "
                "annually, irrespective of indicators. No impairment "
                "test dated in the fiscal year %(date_from)s to "
                "%(date_to)s has been recorded for this asset. Run the "
                "cash-generating unit test (or record a hand-keyed "
                "impairment assessment) to clear the flag.",
                date_from=fy['date_from'], date_to=fy['date_to'],
            ),
            user_id=(self.activated_by_id or self.env.user).id,
        )

    # ---- record helpers ----

    @api.depends('code', 'name')
    def _compute_display_name(self):
        for asset in self:
            if asset.code and asset.name and asset.name != asset.code:
                asset.display_name = "%s / %s" % (asset.code, asset.name)
            else:
                asset.display_name = asset.code or asset.name or ''
