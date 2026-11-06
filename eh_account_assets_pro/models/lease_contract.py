# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
IFRS 16 lease contract (lessee and basic lessor accounting).

Lessee default: recognises a Right Of Use (ROU) asset and a lease
liability at lease commencement, then runs the schedule:

* Each period: interest = liability_opening * periodic_rate; payment is
  split into interest and principal; liability balance decreases.
* ROU asset is depreciated straight line over the lease term - or over
  the underlying asset's useful life when a purchase option is
  reasonably certain (IFRS 16.32).

Recognition exemptions (IFRS 16.5-8): a short-term lease (term,
including reasonably-certain extensions, of 12 months or less and no
purchase option) or a low-value lease (underlying asset at or below the
company threshold when new) may elect out of ROU/liability recognition;
the schedule then recognises the lease payments as a straight-line
expense (equal fixed payments, so the per-period expense equals the
payment) and posts no opening entry.

Term options (IFRS 16.18-19/27): reasonably-certain extension options
extend the term used for the schedule; reasonably-certain termination
penalties and purchase prices are included in the liability and settle
with the final period's payment.

Lease / non-lease component split (IFRS 16.13-16): payment_service_pct
carves the service share out of each payment; only the lease share
builds the liability and ROU, the service share posts straight to
expense each period.

Basic lessor accounting (IFRS 16.67-77, 81): lessor_mode 'operating'
recognises rental income straight line with the underlying asset kept
on the books; 'finance' derecognises to a net investment (PV of the
payments at the rate implicit in the lease, entered in the rate field)
and splits each receipt into interest income and principal recovery.

Manufacturer / dealer finance lessor (IFRS 16.71-74): when
lessor_dealer is set on a finance lease the commencement entry also
recognises selling profit or loss. The net investment is the PV of the
lease payments PLUS the PV of the unguaranteed residual value, both at
the rate implicit in the lease; selling revenue is the lower of the
fair value of the asset and the PV of the lease payments at a market
rate; cost of sale is the carrying amount of the underlying asset less
the PV of the unguaranteed residual value; selling profit (revenue less
cost of sale) posts to P&L at commencement. Interest income then
accrues on the net investment (which amortises down to the unguaranteed
residual, recovered when the asset returns, not through the receipts).

State machine:

  draft -> active -> modified -> active -> ...
                              \\-> terminated
                              \\-> ended (term completed)
"""

import calendar
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .accounting_integrity import _eh_validate_accounting_company

CADENCE_MONTHS = {
    'monthly': 1,
    'quarterly': 3,
    'semi_annual': 6,
    'annual': 12,
}


class EhLeaseContract(models.Model):
    _name = 'eh.lease.contract'
    _description = "Lease Contract (IFRS 16)"
    _inherit = [
        'mail.thread', 'mail.activity.mixin', 'eh.cron.batch.mixin',
        'eh.workflow.guard',
    ]
    _order = 'commencement_date desc, id desc'

    # Workflow, derived measurement, journal-link, and audit fields are
    # server-owned. ``readonly`` only protects the web form; this guard also
    # closes direct ORM/RPC writes that could forge activation, modification,
    # or termination without the supporting entries.
    _eh_guarded_fields = (
        'state', 'rou_initial_value', 'liability_initial_value',
        'activated_at', 'activated_by_id', 'opening_move_id',
        'terminated_at', 'terminated_by_id', 'termination_date',
        'termination_move_id', 'modification_count', 'last_modified_at',
        'modification_move_ids',
        'current_measurement_term_months',
        'currency_mismatch_quarantined', 'currency_mismatch_note',
    )

    name = fields.Char(
        required=True, copy=False, default='/', tracking=True,
    )
    reference = fields.Char(
        copy=False, tracking=True,
        help="External lease reference, e.g. landlord contract number.",
    )
    state = fields.Selection([
        ('draft', "Draft"),
        ('active', "Active"),
        ('modified', "Modified"),
        ('terminated', "Terminated"),
        ('ended', "Ended"),
    ], default='draft', required=True, tracking=True)

    lessor_id = fields.Many2one(
        'res.partner', string="Lessor", required=True, tracking=True,
        index=True,
    )
    commencement_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
    )
    term_months = fields.Integer(
        required=True, tracking=True,
        default=lambda self: (
            self.env.company.eh_lease_default_term_months or 36
        ),
        help="Total lease term in months.",
    )
    current_measurement_term_months = fields.Integer(
        string="Current Measurement Term (months)", readonly=True,
        copy=False, tracking=True,
        help=(
            "Remaining/revised term used by the latest IFRS 16 measurement. "
            "Unlike Total Lease Term, this audit field changes on a lease "
            "modification and never rewrites the original contract term."
        ),
    )
    cadence = fields.Selection([
        ('monthly', "Monthly"),
        ('quarterly', "Quarterly"),
        ('semi_annual', "Semi Annual"),
        ('annual', "Annual"),
    ], required=True, default='monthly', tracking=True)
    payment_timing = fields.Selection([
        ('advance', "In Advance"),
        ('arrears', "In Arrears"),
    ], required=True, default='advance', tracking=True)

    payment_amount = fields.Monetary(required=True, tracking=True)
    incremental_borrowing_rate = fields.Float(
        string="IBR (annual %)", required=True, default=5.0, tracking=True,
        help=(
            "Effective annual discount rate as a percentage (not a nominal "
            "APR). Lessee: the rate "
            "implicit in the lease when readily determinable, else the "
            "incremental borrowing rate (IFRS 16.26). Finance lessor: "
            "the rate implicit in the lease (IFRS 16.68)."
        ),
    )
    initial_direct_costs = fields.Monetary(default=0.0, tracking=True)
    prepaid_lease_payments = fields.Monetary(default=0.0, tracking=True)

    # ---- IFRS 16.5-8 recognition exemptions ----
    exemption = fields.Selection(
        [
            ('none', "None (recognise ROU / liability)"),
            ('short_term', "Short-term lease (IFRS 16.6, term <= 12m)"),
            ('low_value', "Low-value asset (IFRS 16.6, B3-B8)"),
        ],
        required=True, default='none', tracking=True,
        help=(
            "Recognition exemption election. An exempt lease posts NO "
            "ROU asset and NO lease liability; its payments are "
            "recognised as an expense on a straight-line basis over "
            "the term (IFRS 16.6). Short-term requires a term of 12 "
            "months or less INCLUDING reasonably-certain extensions "
            "and no reasonably-certain purchase option (IFRS 16.5, "
            "18); low-value requires the underlying asset's value when "
            "new to be at or below the company threshold."
        ),
    )
    underlying_asset_value = fields.Monetary(
        tracking=True,
        help=(
            "Value of the underlying asset when new (IFRS 16.B3-B8: "
            "assessed on an absolute basis, regardless of the lessee's "
            "size). Required for the low-value exemption; compared "
            "against the company's low-value threshold."
        ),
    )
    exemption_election_note = fields.Text(
        string="Exemption election (per class)",
        help=(
            "IFRS 16.8: the short-term election is made by CLASS of "
            "underlying asset (the low-value election is lease-by-"
            "lease). Document here the class this lease belongs to and "
            "the election covering it, so the class-level policy is "
            "auditable from the contract."
        ),
    )

    # ---- IFRS 16.13-16 lease / non-lease component split ----
    payment_service_pct = fields.Float(
        string="Service (non-lease) share %",
        default=0.0, tracking=True, digits=(5, 2),
        help=(
            "Percentage of each payment that pays for non-lease "
            "components (services: maintenance, utilities, supplies). "
            "IFRS 16.13-16: consideration is allocated on relative "
            "stand-alone prices; only the lease component builds the "
            "liability and ROU, the service share posts straight to "
            "expense each period. 0 keeps the whole payment in the "
            "lease component (including under the IFRS 16.15 practical "
            "expedient of not separating)."
        ),
    )
    component_allocation_note = fields.Text(
        string="Component allocation basis",
        help=(
            "Stand-alone price evidence behind the service percentage "
            "(IFRS 16.14: relative stand-alone price allocation; "
            "observable prices, or estimates maximising observable "
            "inputs)."
        ),
    )

    # ---- IFRS 16.18-19 term options ----
    option_ids = fields.One2many(
        'eh.lease.option', 'lease_id', copy=True,
        string="Term Options",
        help=(
            "Extension, termination and purchase options. Only options "
            "flagged reasonably certain enter the term and liability "
            "measurement."
        ),
    )
    effective_term_months = fields.Integer(
        compute='_compute_effective_term_months',
        help=(
            "Lease term used for the schedule: the base term plus the "
            "months of every reasonably-certain extension option "
            "(IFRS 16.18)."
        ),
    )
    underlying_useful_life_months = fields.Integer(
        tracking=True,
        help=(
            "Useful life of the underlying asset in months. Required "
            "when a purchase option is reasonably certain: the ROU "
            "asset is then depreciated over this useful life instead "
            "of the lease term (IFRS 16.32)."
        ),
    )

    # ---- IFRS 16.67-77 lessor accounting ----
    lessor_mode = fields.Selection(
        [
            ('none', "Lessee (default)"),
            ('operating', "Lessor - operating lease"),
            ('finance', "Lessor - finance lease"),
        ],
        required=True, default='none', tracking=True,
        help=(
            "Accounting perspective for this contract. Lessee is the "
            "default ROU/liability model. Lessor - operating keeps the "
            "underlying asset on the books and recognises rental "
            "income straight line (IFRS 16.81). Lessor - finance "
            "derecognises the underlying asset into a net investment "
            "(the PV of the payments at the rate implicit in the "
            "lease, IFRS 16.67-68) and splits every receipt into "
            "interest income and principal recovery."
        ),
    )

    # ---- IFRS 16.71-74 manufacturer / dealer finance lessor ----
    lessor_dealer = fields.Boolean(
        string="Manufacturer / dealer lessor",
        default=False, tracking=True,
        help=(
            "IFRS 16.71-74: a manufacturer or dealer lessor recognises "
            "selling profit or loss at commencement of a finance lease. "
            "The net investment is the PV of the lease payments plus the "
            "PV of the unguaranteed residual value; selling revenue is "
            "the lower of the asset's fair value and the PV of the lease "
            "payments; cost of sale is the carrying amount less the PV of "
            "the unguaranteed residual; selling profit posts to P&L at "
            "commencement. Only available on a finance-lessor contract."
        ),
    )
    fair_value_of_asset = fields.Monetary(
        string="Fair value of underlying asset",
        default=0.0, tracking=True,
        help=(
            "IFRS 16.71: fair value of the underlying asset at "
            "commencement. Selling revenue is capped at the lower of "
            "this and the PV of the lease payments (a below-market rate "
            "restricts the revenue a dealer lessor may recognise)."
        ),
    )
    carrying_amount_of_asset = fields.Monetary(
        string="Carrying amount (cost) of asset",
        default=0.0, tracking=True,
        help=(
            "IFRS 16.71-72: carrying amount (cost) of the underlying "
            "asset. Cost of sale is this carrying amount less the PV of "
            "the unguaranteed residual value."
        ),
    )
    unguaranteed_residual_value = fields.Monetary(
        string="Unguaranteed residual value",
        default=0.0, tracking=True,
        help=(
            "IFRS 16.71-74: the portion of the residual value of the "
            "underlying asset the lessor is NOT guaranteed to recover "
            "(undiscounted amount at end of term). Its present value is "
            "included in the net investment (IFRS 16.70(b)) and excluded "
            "from cost of sale (IFRS 16.72). The net investment "
            "receivable amortises down to this residual, recovered when "
            "the asset returns, not through the lease receipts."
        ),
    )
    dealer_revenue_account_id = fields.Many2one(
        'account.account', string="Selling Revenue Account",
        check_company=True,
        domain="[('account_type', 'in', ['income', 'income_other'])]",
        help=(
            "IFRS 16.71: P&L account credited with the dealer lessor's "
            "selling revenue (lower of fair value and PV of the lease "
            "payments) at commencement."
        ),
    )
    dealer_cost_of_sale_account_id = fields.Many2one(
        'account.account', string="Cost of Sale Account",
        check_company=True,
        domain="[('account_type', 'in', "
               "['expense', 'expense_direct_cost'])]",
        help=(
            "IFRS 16.71-72: P&L account debited with the dealer lessor's "
            "cost of sale (carrying amount less PV of the unguaranteed "
            "residual value) at commencement."
        ),
    )

    # ---- accounts ----
    # The lessee ROU / liability accounts are enforced per mode at
    # activation (_validate_lease_setup), not with required=True: an
    # exempt lease posts only expense and cash, and a lessor contract
    # posts income / net-investment legs, so hard-requiring the ROU
    # block would force meaningless configuration on those contracts.
    rou_asset_account_id = fields.Many2one(
        'account.account', string="ROU Asset Account",
        check_company=True,
        domain="[('account_type', 'in', ['asset_fixed', 'asset_non_current'])]",
    )
    lease_liability_account_id = fields.Many2one(
        'account.account', string="Lease Liability Account",
        check_company=True,
        domain="[('account_type', 'in', ['liability_current', 'liability_non_current'])]",
    )
    interest_expense_account_id = fields.Many2one(
        'account.account', string="Interest Expense Account",
        check_company=True,
        domain="[('account_type', '=', 'expense')]",
    )
    rou_depreciation_account_id = fields.Many2one(
        'account.account', string="ROU Depreciation Account",
        check_company=True,
        domain="[('account_type', '=', 'expense_depreciation')]",
    )
    rou_accumulated_depreciation_account_id = fields.Many2one(
        'account.account', string="ROU Accumulated Depreciation",
        check_company=True,
        domain="[('account_type', 'in', ['asset_fixed', 'asset_non_current'])]",
    )
    cash_account_id = fields.Many2one(
        'account.account', string="Cash / Payables Account", required=True,
        check_company=True,
        domain="[('account_type', 'in', "
               "['asset_cash', 'liability_payable', 'liability_current'])]",
    )
    lease_expense_account_id = fields.Many2one(
        'account.account', string="Lease / Service Expense Account",
        check_company=True,
        domain="[('account_type', 'in', ['expense', 'expense_direct_cost'])]",
        help=(
            "P&L account for lease expense that bypasses the ROU model: "
            "the straight-line expense of an exempt (short-term / "
            "low-value) lease, and the service (non-lease component) "
            "share of each payment when a component split is set."
        ),
    )
    lessor_income_account_id = fields.Many2one(
        'account.account', string="Rental Income Account",
        check_company=True,
        domain="[('account_type', 'in', ['income', 'income_other'])]",
        help=(
            "Operating-lessor rental income account; credited straight "
            "line each period (IFRS 16.81)."
        ),
    )
    lessor_interest_income_account_id = fields.Many2one(
        'account.account', string="Interest Income Account",
        check_company=True,
        domain="[('account_type', 'in', ['income', 'income_other'])]",
        help=(
            "Finance-lessor interest income account; credited with the "
            "constant periodic return on the net investment "
            "(IFRS 16.75)."
        ),
    )
    net_investment_account_id = fields.Many2one(
        'account.account', string="Net Investment Account",
        check_company=True,
        domain="[('account_type', 'in', "
               "['asset_receivable', 'asset_current', 'asset_non_current', "
               "'asset_fixed'])]",
        help=(
            "Finance-lessor receivable carrying the net investment in "
            "the lease (IFRS 16.67). Debited at commencement with the "
            "PV of the payments; credited with the principal portion "
            "of every receipt."
        ),
    )
    lessor_counterpart_account_id = fields.Many2one(
        'account.account', string="Asset Derecognition Account",
        check_company=True,
        help=(
            "Counterpart credited when the net investment is "
            "recognised at commencement of a finance lease (the "
            "carrying amount of the underlying asset derecognised, or "
            "a clearing account when derecognition is posted "
            "separately)."
        ),
    )
    journal_id = fields.Many2one(
        'account.journal', string="Lease Journal", required=True,
        check_company=True,
        domain="[('type', '=', 'general')]",
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
            "Set by the upgrade audit when a legacy lease has ledger "
            "evidence whose source currency differs from company currency. "
            "The record remains visible but read-only until its ledger is "
            "reversed and the lease is recreated in company currency."
        ),
    )
    currency_mismatch_note = fields.Text(readonly=True, copy=False)

    # ---- computed at activation ----
    rou_initial_value = fields.Monetary(readonly=True, tracking=True)
    liability_initial_value = fields.Monetary(readonly=True, tracking=True)
    activated_at = fields.Datetime(readonly=True, tracking=True)
    activated_by_id = fields.Many2one('res.users', readonly=True)
    opening_move_id = fields.Many2one(
        'account.move', readonly=True, ondelete='restrict', check_company=True,
    )

    # ---- termination ----
    terminated_at = fields.Datetime(readonly=True, tracking=True)
    terminated_by_id = fields.Many2one('res.users', readonly=True)
    termination_date = fields.Date(readonly=True, tracking=True)
    termination_move_id = fields.Many2one(
        'account.move', readonly=True, ondelete='restrict', check_company=True,
    )

    # ---- modification audit ----
    modification_count = fields.Integer(readonly=True, default=0)
    last_modified_at = fields.Datetime(readonly=True, tracking=True)
    modification_move_ids = fields.Many2many(
        'account.move', 'eh_lease_modification_move_rel',
        'lease_id', 'move_id', string="Modification Entries",
        readonly=True, copy=False, check_company=True,
        help="Sealed journal entries produced by lease modifications.",
    )

    # ---- schedule ----
    schedule_line_ids = fields.One2many(
        'eh.lease.schedule.line', 'lease_id', copy=False,
    )

    # ---- totals ----
    total_paid = fields.Monetary(compute='_compute_totals', store=True)
    total_interest = fields.Monetary(compute='_compute_totals', store=True)
    total_principal = fields.Monetary(compute='_compute_totals', store=True)
    liability_balance = fields.Monetary(compute='_compute_totals', store=True)

    notes = fields.Text()

    _check_term_positive = models.Constraint(
        'CHECK (term_months > 0)',
        'Lease term must be greater than zero.',
    )
    _check_payment_positive = models.Constraint(
        'CHECK (payment_amount > 0)',
        'Payment amount must be positive.',
    )

    @api.constrains(
        'incremental_borrowing_rate', 'initial_direct_costs',
        'prepaid_lease_payments', 'underlying_asset_value',
        'fair_value_of_asset', 'carrying_amount_of_asset',
        'unguaranteed_residual_value',
    )
    def _check_nonnegative_measurements(self):
        for lease in self:
            values = (
                lease.incremental_borrowing_rate,
                lease.initial_direct_costs,
                lease.prepaid_lease_payments,
                lease.underlying_asset_value,
                lease.fair_value_of_asset,
                lease.carrying_amount_of_asset,
                lease.unguaranteed_residual_value,
            )
            if any(value < 0 for value in values):
                raise ValidationError(_(
                    "Lease rates, costs, prepayments, values, and residuals "
                    "cannot be negative."
                ))

    # ---- compute ----

    @api.depends(
        'schedule_line_ids.is_posted',
        'schedule_line_ids.payment_amount',
        'schedule_line_ids.interest',
        'schedule_line_ids.principal',
        'schedule_line_ids.liability_close',
    )
    def _compute_totals(self):
        for lease in self:
            posted = lease.schedule_line_ids.filtered(lambda l: l.is_posted)
            lease.total_paid = sum(posted.mapped('payment_amount'))
            lease.total_interest = sum(posted.mapped('interest'))
            lease.total_principal = sum(posted.mapped('principal'))
            if posted:
                last = max(posted, key=lambda l: l.sequence)
                lease.liability_balance = last.liability_close
            else:
                lease.liability_balance = lease.liability_initial_value

    @api.depends('term_months', 'option_ids.option_type',
                 'option_ids.extension_months',
                 'option_ids.reasonably_certain')
    def _compute_effective_term_months(self):
        for lease in self:
            extensions = sum(
                lease.option_ids
                .filtered(lambda o: o.option_type == 'extension'
                          and o.reasonably_certain)
                .mapped('extension_months'),
            )
            lease.effective_term_months = lease.term_months + extensions

    # ---- constraints ----

    @api.constrains('exemption', 'term_months', 'underlying_asset_value',
                    'option_ids', 'lessor_mode',
                    'initial_direct_costs', 'prepaid_lease_payments')
    def _check_exemption(self):
        for lease in self:
            if lease.exemption == 'none':
                continue
            if lease.lessor_mode != 'none':
                raise ValidationError(_(
                    "The IFRS 16.5 recognition exemptions are LESSEE "
                    "elections; a lessor contract cannot be exempt.",
                ))
            certain_purchase = lease.option_ids.filtered(
                lambda o: o.option_type == 'purchase'
                and o.reasonably_certain,
            )
            if certain_purchase:
                raise ValidationError(_(
                    "A lease with a reasonably-certain purchase option "
                    "transfers the underlying asset and does not "
                    "qualify for a recognition exemption (IFRS 16.5, "
                    "Appendix A definition of a short-term lease).",
                ))
            if lease.exemption == 'short_term':
                if lease.effective_term_months > 12:
                    raise ValidationError(_(
                        "The short-term exemption requires a lease term "
                        "of 12 months or less INCLUDING reasonably-"
                        "certain extension options (IFRS 16.18); this "
                        "lease's effective term is %(term)s months.",
                        term=lease.effective_term_months,
                    ))
            if lease.exemption == 'low_value':
                threshold = (
                    lease.company_id.eh_lease_low_value_threshold or 5000.0
                )
                if lease.underlying_asset_value <= 0:
                    raise ValidationError(_(
                        "The low-value exemption requires the value of "
                        "the underlying asset when new (IFRS 16.B3-B8).",
                    ))
                if lease.underlying_asset_value > threshold:
                    raise ValidationError(_(
                        "The underlying asset's value when new "
                        "(%(value).2f) exceeds the company's low-value "
                        "threshold (%(threshold).2f); the low-value "
                        "exemption is not available.",
                        value=lease.underlying_asset_value,
                        threshold=threshold,
                    ))
            if (lease.initial_direct_costs or 0.0) or (
                    lease.prepaid_lease_payments or 0.0):
                raise ValidationError(_(
                    "An exempt lease recognises no ROU asset, so there "
                    "is nothing to capitalise initial direct costs or "
                    "prepayments into; expense them directly and leave "
                    "both fields at zero.",
                ))

    @api.constrains('payment_service_pct', 'lessor_mode')
    def _check_service_pct(self):
        for lease in self:
            if not (0.0 <= lease.payment_service_pct < 100.0):
                raise ValidationError(_(
                    "The service (non-lease) share must be at least 0 "
                    "and below 100 percent; at 100 percent there is no "
                    "lease component and the contract is a service "
                    "agreement, not a lease.",
                ))
            if lease.payment_service_pct and lease.lessor_mode != 'none':
                raise ValidationError(_(
                    "The lease / non-lease component split is a lessee "
                    "measurement feature; set the service share to zero "
                    "on a lessor contract.",
                ))

    @api.constrains('lessor_mode', 'initial_direct_costs',
                    'prepaid_lease_payments')
    def _check_lessor_mode(self):
        for lease in self:
            if lease.lessor_mode == 'none':
                continue
            if (lease.initial_direct_costs or 0.0) or (
                    lease.prepaid_lease_payments or 0.0):
                raise ValidationError(_(
                    "Initial direct costs and prepaid payments are "
                    "lessee ROU inputs; leave them at zero on a lessor "
                    "contract (lessor initial direct costs are outside "
                    "this basic lessor scope).",
                ))

    @api.constrains('lessor_dealer', 'lessor_mode', 'fair_value_of_asset',
                    'carrying_amount_of_asset')
    def _check_dealer(self):
        for lease in self:
            if not lease.lessor_dealer:
                continue
            if lease.lessor_mode != 'finance':
                raise ValidationError(_(
                    "The manufacturer / dealer selling-profit model "
                    "(IFRS 16.71-74) is a FINANCE-lessor feature; set the "
                    "accounting mode to Lessor - finance lease first.",
                ))
            if (lease.fair_value_of_asset or 0.0) <= 0:
                raise ValidationError(_(
                    "A manufacturer / dealer finance lease needs the fair "
                    "value of the underlying asset (IFRS 16.71).",
                ))
            if (lease.carrying_amount_of_asset or 0.0) <= 0:
                raise ValidationError(_(
                    "A manufacturer / dealer finance lease needs the "
                    "carrying amount (cost) of the underlying asset "
                    "(IFRS 16.71-72).",
                ))

    # ---- measurement helpers (components / options) ----

    def _lease_component_payment(self):
        """Lease-component share of each contractual payment: the full
        payment less the service (non-lease) share (IFRS 16.13-16)."""
        self.ensure_one()
        pct = (self.payment_service_pct or 0.0) / 100.0
        return self.currency_id.round(self.payment_amount * (1.0 - pct))

    def _service_component_payment(self):
        """Service (non-lease component) share of each payment; the
        residual of the rounded lease share so the two always sum back
        to the contractual payment exactly."""
        self.ensure_one()
        return self.currency_id.round(
            self.payment_amount - self._lease_component_payment(),
        )

    def _end_of_term_balloon(self):
        """Reasonably-certain purchase price plus reasonably-certain
        termination penalty, both settled with the final period's
        payment (IFRS 16.27(d)/(e))."""
        self.ensure_one()
        certain = self.option_ids.filtered('reasonably_certain')
        balloon = (
            sum(certain.filtered(lambda o: o.option_type == 'purchase')
                .mapped('purchase_price'))
            + sum(certain.filtered(lambda o: o.option_type == 'termination')
                  .mapped('termination_penalty'))
        )
        return self.currency_id.round(balloon)

    def _has_certain_purchase_option(self):
        self.ensure_one()
        return bool(self.option_ids.filtered(
            lambda o: o.option_type == 'purchase' and o.reasonably_certain,
        ))

    def _rou_depreciation_months(self):
        """Months over which the ROU asset depreciates: the underlying
        asset's useful life when a purchase option is reasonably
        certain (IFRS 16.32), else the effective lease term."""
        self.ensure_one()
        if self._has_certain_purchase_option():
            return self.underlying_useful_life_months
        return self.effective_term_months

    # ---- create ----

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                seq = self.env['ir.sequence'].next_by_code('eh.lease.contract') or '/'
                vals['name'] = seq
        return super().create(vals_list)

    @api.constrains('currency_id', 'company_id')
    def _check_company_currency(self):
        for lease in self:
            if (lease.currency_id and lease.company_id
                    and lease.currency_id != lease.company_id.currency_id):
                raise ValidationError(_(
                    "Lease %(lease)s must use %(currency)s, the currency of "
                    "company %(company)s. Assets Pro posts lease monetary "
                    "figures directly to company-currency debit/credit "
                    "columns and does not implement an FX subledger.",
                    lease=lease.display_name,
                    currency=lease.company_id.currency_id.display_name,
                    company=lease.company_id.display_name,
                ))

    _ACCOUNTING_COMPANY_FIELDS = (
        'dealer_revenue_account_id', 'dealer_cost_of_sale_account_id',
        'rou_asset_account_id', 'lease_liability_account_id',
        'interest_expense_account_id', 'rou_depreciation_account_id',
        'rou_accumulated_depreciation_account_id', 'cash_account_id',
        'lease_expense_account_id', 'lessor_income_account_id',
        'lessor_interest_income_account_id', 'net_investment_account_id',
        'lessor_counterpart_account_id', 'journal_id',
    )

    @api.constrains(
        'company_id', 'dealer_revenue_account_id',
        'dealer_cost_of_sale_account_id', 'rou_asset_account_id',
        'lease_liability_account_id', 'interest_expense_account_id',
        'rou_depreciation_account_id',
        'rou_accumulated_depreciation_account_id', 'cash_account_id',
        'lease_expense_account_id', 'lessor_income_account_id',
        'lessor_interest_income_account_id', 'net_investment_account_id',
        'lessor_counterpart_account_id', 'journal_id',
    )
    def _check_accounting_company(self):
        _eh_validate_accounting_company(
            self, self._ACCOUNTING_COMPANY_FIELDS,
        )

    def _eh_validate_company_currency(self):
        self._check_accounting_company()
        for lease in self:
            if lease.currency_mismatch_quarantined:
                raise UserError(_(
                    "Lease %(lease)s is quarantined because its legacy "
                    "currency %(source)s differs from company currency "
                    "%(company)s after ledger entries were recorded. "
                    "Existing history was not rewritten. Reverse the linked "
                    "entries and recreate the lease in company currency.",
                    lease=lease.display_name,
                    source=lease.currency_id.display_name,
                    company=lease.company_id.currency_id.display_name,
                ))
            if lease.currency_id != lease.company_id.currency_id:
                raise UserError(_(
                    "Lease %(lease)s uses %(source)s but company %(company)s "
                    "posts in %(currency)s. Correct the lease currency "
                    "before computing or posting any accounting workflow.",
                    lease=lease.display_name,
                    source=lease.currency_id.display_name,
                    company=lease.company_id.display_name,
                    currency=lease.company_id.currency_id.display_name,
                ))
        return True

    def _eh_has_ledger_evidence(self):
        self.ensure_one()
        return bool(
            self.opening_move_id
            or self.termination_move_id
            or self.modification_move_ids
            or self.modification_count
            or any(
                line.is_posted or line.move_id
                for line in self.schedule_line_ids
            )
        )

    _FROZEN_AFTER_BOOKING = (
        'lessor_id', 'commencement_date', 'term_months', 'cadence',
        'payment_timing', 'payment_amount', 'incremental_borrowing_rate',
        'initial_direct_costs', 'prepaid_lease_payments', 'exemption',
        'underlying_asset_value', 'exemption_election_note',
        'payment_service_pct', 'component_allocation_note',
        'underlying_useful_life_months', 'lessor_mode', 'lessor_dealer',
        'fair_value_of_asset', 'carrying_amount_of_asset',
        'unguaranteed_residual_value', 'dealer_revenue_account_id',
        'dealer_cost_of_sale_account_id', 'rou_asset_account_id',
        'lease_liability_account_id', 'interest_expense_account_id',
        'rou_depreciation_account_id',
        'rou_accumulated_depreciation_account_id', 'cash_account_id',
        'lease_expense_account_id', 'lessor_income_account_id',
        'lessor_interest_income_account_id', 'net_investment_account_id',
        'lessor_counterpart_account_id', 'journal_id', 'company_id',
        'currency_id',
    )

    def _eh_lock_for_transition(self):
        if not self.ids:
            return self
        if not self.env.su:
            self._eh_check_access('write')
        self.env.cr.execute(
            'SELECT id FROM eh_lease_contract WHERE id IN %s '
            'ORDER BY id FOR UPDATE',
            (tuple(self.ids),),
        )
        self.invalidate_recordset([
            'state', 'company_id', 'currency_id',
            'currency_mismatch_quarantined', 'schedule_line_ids',
            'opening_move_id', 'termination_move_id',
            'modification_move_ids', 'modification_count',
            'rou_initial_value', 'liability_initial_value',
            'liability_balance',
        ])
        return self

    def write(self, vals):
        if vals and not self.env.su:
            self._eh_check_access('write')
        if vals and self.filtered('currency_mismatch_quarantined'):
            raise UserError(_(
                "A currency-quarantined lease is read-only because changing "
                "its source would detach existing ledger history. Reverse "
                "the linked entries and recreate it in company currency.",
            ))
        frozen = [
            field_name for field_name in self._FROZEN_AFTER_BOOKING
            if field_name in vals
        ]
        if frozen:
            self._eh_lock_for_transition()
        if frozen and not self.env.su and self.filtered(
                lambda lease: lease.state != 'draft'):
            raise UserError(_(
                "Lease measurement and posting fields (%(fields)s) are frozen "
                "after activation. Use the modification or termination "
                "workflow instead of editing the contract in place.",
                fields=', '.join(frozen),
            ))
        if ({'company_id', 'currency_id'} & set(vals)
                and self.filtered(
                    lambda lease: lease._eh_has_ledger_evidence())):
            raise UserError(_(
                "Lease company and currency are frozen once ledger evidence "
                "exists."
            ))
        draft_schedules = self.filtered(
            lambda lease: lease.state == 'draft' and lease.schedule_line_ids,
        ) if frozen and not self.env.su else self.browse([])
        result = super().write(vals)
        if draft_schedules:
            draft_schedules.mapped('schedule_line_ids').sudo().unlink()
        return result

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        booked = self.filtered(
            lambda lease: lease.state != 'draft'
            or lease.opening_move_id
            or lease.termination_move_id
            or any(
                line.is_posted or line.move_id
                for line in lease.schedule_line_ids
            )
        )
        if booked:
            raise UserError(_(
                "An activated, terminated, or otherwise booked lease cannot "
                "be deleted; its schedule and journal-entry links are "
                "permanent audit evidence. Archive it instead.",
            ))
        return super().unlink()

    # ---- transitions ----

    def action_compute_schedule(self):
        self._eh_lock_for_transition()
        self = self._eh_workflow_action()
        for lease in self:
            lease._eh_validate_company_currency()
            if lease.state != 'draft':
                raise UserError(_(
                    "Schedule can only be computed in draft state.",
                ))
            lease._wipe_unposted_schedule()
            lease._build_schedule()

    def action_activate(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an accounting manager can activate a lease and post its "
                "opening entry. This posting is a segregation-of-duties "
                "control point.",
            ))
        self._eh_lock_for_transition()
        self = self._eh_workflow_action()
        for lease in self:
            lease._eh_validate_company_currency()
            if lease.state != 'draft':
                raise UserError(_(
                    "Only draft leases can be activated.",
                ))
            lease._validate_lease_setup()
            if not lease.schedule_line_ids:
                lease._build_schedule()
            opening_move = lease._post_opening_entry()
            lease.write({
                'state': 'active',
                'activated_at': fields.Datetime.now(),
                'activated_by_id': self.env.user.id,
                'opening_move_id': opening_move.id if opening_move else False,
                'current_measurement_term_months': (
                    lease.effective_term_months
                ),
            })

    def _validate_lease_setup(self):
        """Mode-aware posting-setup validation, replacing blanket
        required=True on the lessee ROU block: each accounting mode
        needs a different set of accounts."""
        self.ensure_one()
        self._eh_validate_company_currency()
        missing = []
        if self.exemption != 'none':
            if not self.lease_expense_account_id:
                missing.append(_("Lease / Service Expense Account"))
        elif self.lessor_mode == 'operating':
            if not self.lessor_income_account_id:
                missing.append(_("Rental Income Account"))
        elif self.lessor_mode == 'finance':
            if not self.net_investment_account_id:
                missing.append(_("Net Investment Account"))
            if not self.lessor_interest_income_account_id:
                missing.append(_("Interest Income Account"))
            if not self.lessor_counterpart_account_id:
                missing.append(_("Asset Derecognition Account"))
            if self.lessor_dealer:
                if not self.dealer_revenue_account_id:
                    missing.append(_("Selling Revenue Account"))
                if not self.dealer_cost_of_sale_account_id:
                    missing.append(_("Cost of Sale Account"))
        else:
            if not self.rou_asset_account_id:
                missing.append(_("ROU Asset Account"))
            if not self.lease_liability_account_id:
                missing.append(_("Lease Liability Account"))
            if not self.interest_expense_account_id:
                missing.append(_("Interest Expense Account"))
            if not self.rou_depreciation_account_id:
                missing.append(_("ROU Depreciation Account"))
            if not self.rou_accumulated_depreciation_account_id:
                missing.append(_("ROU Accumulated Depreciation"))
            if (self.payment_service_pct
                    and not self.lease_expense_account_id):
                missing.append(_("Lease / Service Expense Account"))
        if missing:
            raise UserError(_(
                "Lease %(lease)s is missing posting setup: %(missing)s.",
                lease=self.display_name,
                missing=", ".join(missing),
            ))

    def action_open_modify_wizard(self):
        self.ensure_one()
        if self.state not in ('active', 'modified'):
            raise UserError(_(
                "Only active leases can be modified.",
            ))
        self._check_remeasurement_supported(_("modified"))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'eh.lease.modify.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_lease_id': self.id},
        }

    def action_open_terminate_wizard(self):
        self.ensure_one()
        if self.state not in ('active', 'modified'):
            raise UserError(_(
                "Only active leases can be terminated.",
            ))
        self._check_remeasurement_supported(_("terminated early"))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'eh.lease.terminate.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_lease_id': self.id},
        }

    def action_post_due_lines(self):
        today = fields.Date.context_today(self)
        for lease in self:
            if lease.state not in ('active', 'modified'):
                continue
            due = lease.schedule_line_ids.filtered(
                lambda l: not l.is_posted and l.period_date <= today,
            ).sorted('sequence')
            for line in due:
                line.action_post()
            lease._maybe_mark_ended()

    # ---- helpers ----

    def _check_remeasurement_supported(self, verb):
        """The modification / early-termination wizards remeasure the
        lessee ROU-liability model. Exempt (expense-only) and lessor
        contracts, and leases whose measurement includes reasonably-
        certain options, are outside their arithmetic; block with a
        clear path instead of silently mis-measuring."""
        self.ensure_one()
        if self.exemption != 'none':
            raise UserError(_(
                "An exempt (short-term / low-value) lease has no ROU or "
                "liability to remeasure, so it cannot be %(verb)s through "
                "this wizard. Adjust the remaining expense rows in draft "
                "of a replacement contract, or end the schedule by "
                "posting its remaining rows.",
                verb=verb,
            ))
        if self.lessor_mode != 'none':
            raise UserError(_(
                "Lessor contracts cannot be %(verb)s through the lessee "
                "remeasurement wizard in this basic lessor scope.",
                verb=verb,
            ))
        certain = self.option_ids.filtered('reasonably_certain')
        if certain:
            raise UserError(_(
                "This lease's measurement includes reasonably-certain "
                "options (%(count)s). The remeasurement wizard rebuilds "
                "the schedule from plain term / payment / rate inputs "
                "and would drop the option amounts; reassess the "
                "options in a replacement contract instead.",
                count=len(certain),
            ))

    def _eh_post_accrued_through(self, event_date):
        """Post full due rows plus a measured event-date ROU/interest stub.

        Monthly ROU rows make consumption independent of payment cadence. When
        a modification or termination falls inside the next month, the earned
        ROU fraction is posted at the event date. For arrears payments, accrued
        effective interest through that date is capitalised into the liability
        without inventing a cash payment; advance schedules already recognise
        the cadence interest with their payment row.
        """
        self.ensure_one()
        due = self.schedule_line_ids.filtered(
            lambda line: not line.is_posted and line.period_date <= event_date,
        ).sorted(lambda line: (line.period_date, line.sequence))
        for line in due:
            line.action_post()
        if self.state not in ('active', 'modified'):
            return due

        unposted = self.schedule_line_ids.filtered(
            lambda line: not line.is_posted and line.period_date > event_date,
        )
        posted = self.schedule_line_ids.filtered('is_posted')
        next_rou = unposted.filtered(
            lambda line: line.rou_amount > 0,
        ).sorted(lambda line: (line.period_date, line.sequence))[:1]
        rou_stub = 0.0
        if next_rou:
            posted_rou_dates = posted.filtered(
                lambda line: line.rou_amount > 0,
            ).mapped('period_date')
            # Schedule dates are period-end boundaries. At commencement no
            # time has elapsed; one day after a posted month-end represents
            # one earned day of the next period. Using exclusive boundaries
            # avoids creating a fictitious one-day stub on commencement.
            rou_start = (
                max(posted_rou_dates)
                if posted_rou_dates else self.commencement_date
            )
            if rou_start < event_date < next_rou.period_date:
                span = (next_rou.period_date - rou_start).days
                elapsed = (event_date - rou_start).days
                rou_stub = self.currency_id.round(
                    next_rou.rou_amount * elapsed / float(span),
                )

        interest_stub = 0.0
        if self.payment_timing == 'arrears':
            next_interest = unposted.filtered(
                lambda line: line.interest > 0,
            ).sorted(lambda line: (line.period_date, line.sequence))[:1]
            if next_interest:
                posted_payment_dates = posted.filtered(
                    lambda line: line.payment_amount > 0,
                ).mapped('period_date')
                interest_start = (
                    max(posted_payment_dates)
                    if posted_payment_dates else self.commencement_date
                )
                if interest_start < event_date < next_interest.period_date:
                    span = (next_interest.period_date - interest_start).days
                    elapsed = (event_date - interest_start).days
                    opening = self.currency_id.round(
                        self._liability_balance_after_last_post(),
                    )
                    if opening > 0 and span > 0:
                        full_factor = 1.0 + (
                            next_interest.interest / opening
                        )
                        interest_stub = self.currency_id.round(
                            opening * (
                                full_factor ** (elapsed / float(span)) - 1.0
                            ),
                        )

        if (self.currency_id.is_zero(rou_stub)
                and self.currency_id.is_zero(interest_stub)):
            return due
        liability_open = self.currency_id.round(
            self._liability_balance_after_last_post(),
        )
        all_sequences = self.schedule_line_ids.mapped('sequence')
        accumulated_rou = self.currency_id.round(
            sum(posted.mapped('rou_amount')) + rou_stub,
        )
        stub = self.env['eh.lease.schedule.line'].sudo().create({
            'lease_id': self.id,
            'sequence': (max(all_sequences) if all_sequences else 0) + 1,
            'period_date': event_date,
            'liability_open': liability_open,
            'payment_amount': 0.0,
            'service_amount': 0.0,
            'interest': interest_stub,
            'principal': 0.0,
            'liability_close': self.currency_id.round(
                liability_open + interest_stub,
            ),
            'rou_amount': rou_stub,
            'rou_accumulated': accumulated_rou,
            'is_event_accrual': True,
        })
        stub.action_post()
        return due | stub

    def _wipe_unposted_schedule(self):
        self.ensure_one()
        unposted = self.schedule_line_ids.filtered(lambda l: not l.is_posted)
        unposted.unlink()

    def _periodic_rate(self):
        self.ensure_one()
        annual = self.incremental_borrowing_rate / 100.0
        period_months = CADENCE_MONTHS[self.cadence]
        # Convert the effective annual rate to the cadence-equivalent rate via
        # (1+annual)^(period_months/12) - 1.
        return (1.0 + annual) ** (period_months / 12.0) - 1.0

    def _number_of_periods(self):
        self.ensure_one()
        period_months = CADENCE_MONTHS[self.cadence]
        term = self.effective_term_months
        if term % period_months:
            raise UserError(_(
                "Term (%(term)s months, including reasonably-certain "
                "extensions) must be a whole multiple of the cadence "
                "(%(months)s months).",
                term=term, months=period_months,
            ))
        return int(term // period_months)

    def _present_value_of_payments(self):
        """Present value of N equal lease-component payments at periodic
        rate r, plus the present value of any end-of-term balloon (a
        reasonably-certain purchase price or termination penalty,
        IFRS 16.27(d)/(e)) discounted over the full N periods."""
        self.ensure_one()
        n = self._number_of_periods()
        r = self._periodic_rate()
        pmt = self._lease_component_payment()
        if r == 0:
            pv = pmt * n
        else:
            pv = pmt * (1.0 - (1.0 + r) ** (-n)) / r
            if self.payment_timing == 'advance':
                pv = pv * (1.0 + r)
        balloon = self._end_of_term_balloon()
        if balloon:
            pv += balloon / (1.0 + r) ** n if r else balloon
        return pv

    # ---- IFRS 16.71-74 manufacturer / dealer lessor measurement ----

    def _pv_unguaranteed_residual(self):
        """Present value of the unguaranteed residual value at the rate
        implicit in the lease over the full term (IFRS 16.70(b))."""
        self.ensure_one()
        residual = self.unguaranteed_residual_value or 0.0
        if not residual:
            return 0.0
        n = self._number_of_periods()
        r = self._periodic_rate()
        return residual / (1.0 + r) ** n if r else residual

    def _dealer_measurement(self):
        """IFRS 16.71-74 commencement measurement for a manufacturer /
        dealer finance lessor. Returns a dict of the four rounded
        figures:

        * net_investment = PV(lease payments) + PV(unguaranteed residual)
        * revenue        = lower of fair value and PV(lease payments)
        * cost_of_sale   = carrying amount - PV(unguaranteed residual)
        * selling_profit = revenue - cost_of_sale

        The revenue is discounted at 'a market rate of interest'; this
        module uses the rate implicit already entered (the common case
        where that rate reflects the market), so PV(lease payments) is
        the annuity PV of the lease-component payments.
        """
        self.ensure_one()
        pv_payments = self._present_value_of_payments()
        pv_residual = self._pv_unguaranteed_residual()
        revenue = self.currency_id.round(
            min(self.fair_value_of_asset or 0.0, pv_payments),
        )
        cost_of_sale = self.currency_id.round(
            (self.carrying_amount_of_asset or 0.0) - pv_residual,
        )
        selling_profit = self.currency_id.round(revenue - cost_of_sale)
        # The payment component of the net investment equals the
        # recognised selling revenue: when the rate is at or above market
        # (fair value >= PV of payments) revenue IS the PV of payments; a
        # below-market rate restricts BOTH the revenue and the receivable
        # to the fair value (IFRS 16.71-72), which keeps the commencement
        # entry balanced.
        net_investment = self.currency_id.round(revenue + pv_residual)
        return {
            'pv_payments': self.currency_id.round(pv_payments),
            'pv_residual': self.currency_id.round(pv_residual),
            'net_investment': net_investment,
            'revenue': revenue,
            'cost_of_sale': cost_of_sale,
            'selling_profit': selling_profit,
        }

    def _validate_rou_depreciation_span(self):
        """A reasonably-certain purchase option depreciates the ROU over
        the underlying asset's useful life (IFRS 16.32); validate the
        inputs make a well-formed schedule."""
        self.ensure_one()
        if not self._has_certain_purchase_option():
            return
        period_months = CADENCE_MONTHS[self.cadence]
        useful = self.underlying_useful_life_months
        if useful <= 0:
            raise UserError(_(
                "A reasonably-certain purchase option requires the "
                "underlying asset's useful life (in months): the ROU "
                "asset depreciates over that life, not the lease term "
                "(IFRS 16.32).",
            ))
        if useful < self.effective_term_months:
            raise UserError(_(
                "The underlying asset's useful life (%(life)s months) "
                "cannot be shorter than the lease term (%(term)s "
                "months).",
                life=useful, term=self.effective_term_months,
            ))
        if (useful - self.effective_term_months) % period_months:
            raise UserError(_(
                "The useful life must exceed the term by a whole "
                "multiple of the cadence (%(months)s months) so the "
                "post-term depreciation rows align to periods.",
                months=period_months,
            ))

    def _eh_create_monthly_rou_schedule(
            self, payment_rows, opening_liability, rou_total, rou_months,
            first_rou_date, rou_accumulated_start=0.0, sequence_start=0):
        """Persist one chronological row per monthly ROU charge/payment date.

        IFRS 16.32 depreciation follows the passage of monthly consumption,
        independently of whether cash is paid monthly, quarterly or annually.
        Payment dates retain the cadence amortisation arithmetic; intervening
        rows carry an unchanged liability and only the monthly ROU charge.
        """
        self.ensure_one()
        by_date = {}
        for payment in payment_rows:
            values = dict(payment)
            period_date = values.pop('period_date')
            values['_has_payment_event'] = True
            by_date[period_date] = values

        per_month = rou_total / rou_months if rou_months else 0.0
        allocated_rou = 0.0
        rou_date = self._month_end(first_rou_date)
        for month_index in range(1, rou_months + 1):
            if month_index == rou_months:
                rou_amount = rou_total - allocated_rou
            else:
                rou_amount = per_month
            rou_amount = self.currency_id.round(max(0.0, rou_amount))
            allocated_rou = self.currency_id.round(
                allocated_rou + rou_amount,
            )
            values = by_date.setdefault(rou_date, {})
            values['rou_amount'] = rou_amount
            rou_date = self._next_period_date(rou_date, 1)

        running_liability = self.currency_id.round(opening_liability)
        rou_accumulated = self.currency_id.round(rou_accumulated_start)
        create_vals = []
        for offset, period_date in enumerate(sorted(by_date), start=1):
            values = by_date[period_date]
            is_payment = values.pop('_has_payment_event', False)
            rou_amount = values.pop('rou_amount', 0.0)
            if is_payment:
                liability_open = values['liability_open']
                liability_close = values['liability_close']
                running_liability = liability_close
            else:
                liability_open = running_liability
                liability_close = running_liability
            rou_accumulated = self.currency_id.round(
                rou_accumulated + rou_amount,
            )
            create_vals.append({
                'lease_id': self.id,
                'sequence': sequence_start + offset,
                'period_date': period_date,
                'liability_open': liability_open,
                'payment_amount': values.get('payment_amount', 0.0),
                'service_amount': values.get('service_amount', 0.0),
                'interest': values.get('interest', 0.0),
                'principal': values.get('principal', 0.0),
                'liability_close': liability_close,
                'rou_amount': rou_amount,
                'rou_accumulated': rou_accumulated,
            })
        if create_vals:
            self.env['eh.lease.schedule.line'].sudo().create(create_vals)
        return create_vals

    def _build_schedule(self):
        self.ensure_one()
        if self.exemption != 'none':
            return self._build_exempt_schedule()
        if self.lessor_mode == 'operating':
            return self._build_operating_lessor_schedule()
        # Lessee ROU/liability model and finance-lessor net investment
        # share the same amortisation arithmetic; the finance lessor
        # simply carries no ROU (the liability fields carry the net
        # investment receivable) and posts income legs instead.
        is_finance_lessor = self.lessor_mode == 'finance'
        self._validate_rou_depreciation_span()
        Line = self.env['eh.lease.schedule.line'].sudo()
        n = self._number_of_periods()
        r = self._periodic_rate()
        pmt = self._lease_component_payment()
        service_pmt = (
            0.0 if is_finance_lessor else self._service_component_payment()
        )

        # Manufacturer / dealer finance lessor: the net investment
        # includes the PV of the unguaranteed residual, and the
        # receivable amortises down to the UNDISCOUNTED residual
        # (recovered when the asset returns), not to zero
        # (IFRS 16.70(b)/.74).
        is_dealer = is_finance_lessor and self.lessor_dealer
        residual_terminal = (
            self.unguaranteed_residual_value or 0.0 if is_dealer else 0.0
        )
        if is_dealer:
            # Net investment = recognised selling revenue + PV of the
            # unguaranteed residual (IFRS 16.71-74); the receivable opens
            # here and amortises to the undiscounted residual.
            liability = self._dealer_measurement()['net_investment']
        else:
            liability = self._present_value_of_payments()
        rou_initial = 0.0 if is_finance_lessor else (
            liability
            + (self.initial_direct_costs or 0.0)
            + (self.prepaid_lease_payments or 0.0)
        )

        self.write({
            'liability_initial_value': self.currency_id.round(liability),
            'rou_initial_value': self.currency_id.round(rou_initial),
        })

        period_months = CADENCE_MONTHS[self.cadence]
        period_date = self._first_period_date()
        running = liability
        rou_accumulated = 0.0
        rou_months = self._rou_depreciation_months()
        rou_per_month = (
            rou_initial / rou_months if rou_months else 0.0
        )
        # Rows carrying ROU depreciation: the payment rows, plus - when
        # a reasonably-certain purchase option stretches depreciation
        # over the useful life (IFRS 16.32) - depreciation-only rows
        # after the payments end.
        extra_rou_rows = 0
        if rou_initial and rou_months > self.effective_term_months:
            extra_rou_rows = int(
                (rou_months - self.effective_term_months) // period_months,
            )
        total_rows = n + extra_rou_rows

        balloon = self._end_of_term_balloon()
        # An advance annuity pays its N regular instalments at t0..t(N-1),
        # while a purchase price / termination penalty remains payable at
        # the END of the term, tN.  Keep that end-of-term amount outstanding
        # after the last regular payment, then settle it on its own auditable
        # row at tN.  Arrears payments already fall at t1..tN, so their final
        # row settles the balloon together with the regular instalment.
        advance_balloon = (
            balloon if self.payment_timing == 'advance' else 0.0
        )
        rows = self._compute_amortisation_rows(
            opening_liability=running, n=n, r=r, pmt=pmt,
            terminal_balance=self.currency_id.round(
                residual_terminal + advance_balloon,
            ),
        )
        if not is_finance_lessor:
            payment_rows = []
            payment_date = period_date
            for row in rows:
                payment_rows.append({
                    'period_date': payment_date,
                    **row,
                    'service_amount': service_pmt,
                })
                payment_date = self._next_period_date(
                    payment_date, period_months,
                )
            if advance_balloon:
                payment_rows.append({
                    'period_date': payment_date,
                    'liability_open': self.currency_id.round(
                        residual_terminal + advance_balloon,
                    ),
                    'payment_amount': advance_balloon,
                    'service_amount': 0.0,
                    'interest': 0.0,
                    'principal': advance_balloon,
                    'liability_close': self.currency_id.round(
                        residual_terminal,
                    ),
                })
            self._eh_create_monthly_rou_schedule(
                payment_rows=payment_rows,
                opening_liability=liability,
                rou_total=self.currency_id.round(rou_initial),
                rou_months=rou_months,
                # The opening recognition at commencement is not itself a
                # month of consumption. The first full monthly ROU charge is
                # due one month later, irrespective of payment timing.
                first_rou_date=self._next_period_date(
                    self._month_end(self.commencement_date), 1,
                ),
            )
            return
        for n_idx, row in enumerate(rows, start=1):
            is_last_rou = (n_idx == total_rows)
            if is_last_rou:
                rou_amount = rou_initial - rou_accumulated
            else:
                rou_amount = rou_per_month * period_months
            rou_amount = self.currency_id.round(max(0.0, rou_amount))
            rou_accumulated = self.currency_id.round(
                rou_accumulated + rou_amount
            )

            Line.create({
                'lease_id': self.id,
                'sequence': n_idx,
                'period_date': period_date,
                'liability_open': row['liability_open'],
                'payment_amount': row['payment_amount'],
                'service_amount': service_pmt,
                'interest': row['interest'],
                'principal': row['principal'],
                'liability_close': row['liability_close'],
                'rou_amount': rou_amount,
                'rou_accumulated': rou_accumulated,
            })
            period_date = self._next_period_date(period_date, period_months)

        # Advance-payment balloon settlement at tN.  No further interest is
        # recognised here: the last regular row accrued the t(N-1)..tN
        # interest and closed at the contractual balloon (plus any dealer
        # residual that remains recoverable through return of the asset).
        sequence_offset = 0
        if advance_balloon:
            sequence_offset = 1
            Line.create({
                'lease_id': self.id,
                'sequence': n + 1,
                'period_date': period_date,
                'liability_open': self.currency_id.round(
                    residual_terminal + advance_balloon,
                ),
                'payment_amount': advance_balloon,
                'service_amount': 0.0,
                'interest': 0.0,
                'principal': advance_balloon,
                'liability_close': self.currency_id.round(residual_terminal),
                'rou_amount': 0.0,
                'rou_accumulated': rou_accumulated,
            })

        # IFRS 16.32 tail: depreciation-only rows after the last payment.
        for k_idx in range(n + 1, total_rows + 1):
            if k_idx == total_rows:
                rou_amount = rou_initial - rou_accumulated
            else:
                rou_amount = rou_per_month * period_months
            rou_amount = self.currency_id.round(max(0.0, rou_amount))
            rou_accumulated = self.currency_id.round(
                rou_accumulated + rou_amount
            )
            Line.create({
                'lease_id': self.id,
                'sequence': k_idx + sequence_offset,
                'period_date': period_date,
                'liability_open': 0.0,
                'payment_amount': 0.0,
                'service_amount': 0.0,
                'interest': 0.0,
                'principal': 0.0,
                'liability_close': 0.0,
                'rou_amount': rou_amount,
                'rou_accumulated': rou_accumulated,
            })
            period_date = self._next_period_date(period_date, period_months)

    def _build_exempt_schedule(self):
        """IFRS 16.6: an exempt (short-term / low-value) lease recognises
        its payments as an expense on a straight-line basis over the
        term. The module supports equal fixed payments, so the
        straight-line per-period expense equals the payment; each row
        posts Dr Lease Expense / Cr Cash and carries no liability, no
        interest and no ROU."""
        self.ensure_one()
        Line = self.env['eh.lease.schedule.line'].sudo()
        n = self._number_of_periods()
        pmt = self.payment_amount
        self.write({
            'liability_initial_value': 0.0,
            'rou_initial_value': 0.0,
        })
        period_months = CADENCE_MONTHS[self.cadence]
        period_date = self._first_period_date()
        for n_idx in range(1, n + 1):
            Line.create({
                'lease_id': self.id,
                'sequence': n_idx,
                'period_date': period_date,
                'liability_open': 0.0,
                'payment_amount': pmt,
                'service_amount': 0.0,
                'interest': 0.0,
                'principal': 0.0,
                'liability_close': 0.0,
                'rou_amount': 0.0,
                'rou_accumulated': 0.0,
            })
            period_date = self._next_period_date(period_date, period_months)

    def _build_operating_lessor_schedule(self):
        """IFRS 16.81: an operating lessor recognises lease payments as
        income on a straight-line basis; the underlying asset stays on
        the books (its depreciation continues in the asset register).
        Each row posts Dr Cash / Cr Rental Income."""
        self.ensure_one()
        Line = self.env['eh.lease.schedule.line'].sudo()
        n = self._number_of_periods()
        pmt = self.payment_amount
        self.write({
            'liability_initial_value': 0.0,
            'rou_initial_value': 0.0,
        })
        period_months = CADENCE_MONTHS[self.cadence]
        period_date = self._first_period_date()
        for n_idx in range(1, n + 1):
            Line.create({
                'lease_id': self.id,
                'sequence': n_idx,
                'period_date': period_date,
                'liability_open': 0.0,
                'payment_amount': pmt,
                'service_amount': 0.0,
                'interest': 0.0,
                'principal': 0.0,
                'liability_close': 0.0,
                'rou_amount': 0.0,
                'rou_accumulated': 0.0,
            })
            period_date = self._next_period_date(period_date, period_months)

    def _compute_amortisation_rows(self, opening_liability, n, r, pmt,
                                   terminal_balance=0.0):
        """Return amortisation rows whose schedule and journal posting agree.

        Invariant: principal + interest == payment_amount on every row, and
        liability_close == liability_open - principal. The journal entry
        posts Dr Liability=principal, Dr Interest=interest, Cr Cash=pmt,
        which balances exactly. The last row trues up so that the closing
        liability lands on the terminal balance (zero by default; the
        unguaranteed residual value for a manufacturer / dealer finance
        lessor, whose net investment amortises down to the residual
        recovered when the asset returns, not through the receipts,
        IFRS 16.70(b)/.74) regardless of fixed payment rounding.
        """
        rows = []
        running = opening_liability
        for n_idx in range(1, n + 1):
            is_last = (n_idx == n)
            if self.payment_timing == 'advance':
                # In advance: payment first, interest accrues on the
                # post-payment balance over the period and is recapitalised.
                # principal[i] = pmt - interest[i]
                # interest[i] = (running - pmt) * r
                # liability_close[i] = running - principal[i]
                if is_last and not terminal_balance:
                    interest_raw = 0.0
                    principal_raw = running
                    period_pmt_raw = running
                elif is_last:
                    # Dealer residual: recover everything above the
                    # terminal balance, interest on the post-payment base.
                    interest_raw = max(0.0, (running - pmt) * r)
                    principal_raw = running - terminal_balance
                    period_pmt_raw = principal_raw + interest_raw
                else:
                    interest_raw = max(0.0, (running - pmt) * r)
                    principal_raw = pmt - interest_raw
                    period_pmt_raw = pmt
            else:
                # In arrears: interest accrues on the opening balance and
                # is settled at period end with the payment.
                if is_last:
                    interest_raw = running * r
                    principal_raw = running - terminal_balance
                    period_pmt_raw = principal_raw + interest_raw
                else:
                    interest_raw = max(0.0, running * r)
                    principal_raw = pmt - interest_raw
                    period_pmt_raw = pmt

            interest = self.currency_id.round(max(0.0, interest_raw))
            period_pmt = self.currency_id.round(max(0.0, period_pmt_raw))
            # Re-derive principal from rounded values so the journal entry
            # balances exactly: principal + interest == payment_amount.
            principal = self.currency_id.round(period_pmt - interest)
            liability_close = self.currency_id.round(
                max(0.0, running - principal)
            )
            rows.append({
                'liability_open': self.currency_id.round(running),
                'payment_amount': period_pmt,
                'interest': interest,
                'principal': principal,
                'liability_close': liability_close,
            })
            running = liability_close
        return rows

    def _first_period_date(self):
        self.ensure_one()
        period_months = CADENCE_MONTHS[self.cadence]
        if self.payment_timing == 'advance':
            return self._month_end(self.commencement_date)
        d = self.commencement_date + relativedelta(months=period_months)
        return self._month_end(d)

    @staticmethod
    def _month_end(d):
        last = calendar.monthrange(d.year, d.month)[1]
        return date(d.year, d.month, last)

    def _next_period_date(self, current, months):
        nxt = current + relativedelta(months=months)
        return self._month_end(nxt)

    def _post_opening_entry(self):
        self.ensure_one()
        # IFRS 16.6: an exempt lease recognises no ROU asset and no
        # liability; there is no opening entry, expense posts per period.
        if self.exemption != 'none':
            return None
        # IFRS 16.81: an operating lessor keeps the underlying asset on
        # its books; income posts per period, no opening entry.
        if self.lessor_mode == 'operating':
            return None
        # IFRS 16.67: a finance lessor recognises the net investment in
        # the lease at commencement.
        if self.lessor_mode == 'finance':
            # IFRS 16.71-74: a manufacturer / dealer lessor recognises
            # selling profit or loss at commencement:
            #   Dr Net investment (PV payments + PV unguaranteed residual)
            #   Dr Cost of sale (carrying amount - PV unguaranteed residual)
            #     Cr Selling revenue (lower of fair value, PV of payments)
            #     Cr Asset derecognition (carrying amount of the asset)
            if self.lessor_dealer:
                m = self._dealer_measurement()
                move = self.env['account.move']._eh_create_sealed({
                    'move_type': 'entry',
                    'eh_sealed': True,
                    'date': self.commencement_date,
                    'journal_id': self.journal_id.id,
                    'ref': _("Dealer finance lease %s", self.display_name),
                    'line_ids': [
                        (0, 0, {
                            'name': _("Net investment %s",
                                      self.display_name),
                            'account_id': self.net_investment_account_id.id,
                            'debit': m['net_investment'],
                            'credit': 0.0,
                        }),
                        (0, 0, {
                            'name': _("Cost of sale %s", self.display_name),
                            'account_id': (
                                self.dealer_cost_of_sale_account_id.id
                            ),
                            'debit': m['cost_of_sale'],
                            'credit': 0.0,
                        }),
                        (0, 0, {
                            'name': _("Selling revenue %s",
                                      self.display_name),
                            'account_id': self.dealer_revenue_account_id.id,
                            'debit': 0.0,
                            'credit': m['revenue'],
                        }),
                        (0, 0, {
                            'name': _("Underlying asset derecognition %s",
                                      self.display_name),
                            'account_id': (
                                self.lessor_counterpart_account_id.id
                            ),
                            'debit': 0.0,
                            'credit': self.currency_id.round(
                                self.carrying_amount_of_asset or 0.0,
                            ),
                        }),
                    ],
                })
                move.action_post()
                return move
            # Simple (non-dealer) finance lessor: Dr Net Investment (PV of
            # payments), Cr Asset Derecognition counterpart.
            move = self.env['account.move']._eh_create_sealed({
                'move_type': 'entry',
                'eh_sealed': True,
                'date': self.commencement_date,
                'journal_id': self.journal_id.id,
                'ref': _("Finance lease net investment %s", self.display_name),
                'line_ids': [
                    (0, 0, {
                        'name': _("Net investment %s", self.display_name),
                        'account_id': self.net_investment_account_id.id,
                        'debit': self.liability_initial_value,
                        'credit': 0.0,
                    }),
                    (0, 0, {
                        'name': _("Underlying asset derecognition %s",
                                  self.display_name),
                        'account_id': self.lessor_counterpart_account_id.id,
                        'debit': 0.0,
                        'credit': self.liability_initial_value,
                    }),
                ],
            })
            move.action_post()
            return move
        # Lessee: Dr ROU Asset, Cr Lease Liability, plus initial direct
        # costs and prepaid lease payments (already capitalised into ROU).
        idc = self.initial_direct_costs or 0.0
        prepaid = self.prepaid_lease_payments or 0.0
        rou = self.rou_initial_value
        liab = self.liability_initial_value
        lines = [
            (0, 0, {
                'name': _("ROU asset opening %s", self.display_name),
                'account_id': self.rou_asset_account_id.id,
                'debit': rou,
                'credit': 0.0,
            }),
            (0, 0, {
                'name': _("Lease liability opening %s", self.display_name),
                'account_id': self.lease_liability_account_id.id,
                'debit': 0.0,
                'credit': liab,
            }),
        ]
        cash_credit = idc + prepaid
        if cash_credit > 0:
            lines.append((0, 0, {
                'name': _("Initial direct costs and prepayments %s", self.display_name),
                'account_id': self.cash_account_id.id,
                'debit': 0.0,
                'credit': self.currency_id.round(cash_credit),
            }))
        move = self.env['account.move']._eh_create_sealed({
            'move_type': 'entry',
            'eh_sealed': True,
            'date': self.commencement_date,
            'journal_id': self.journal_id.id,
            'ref': _("Lease opening %s", self.display_name),
            'line_ids': lines,
        })
        move.action_post()
        return move

    def _maybe_mark_ended(self):
        self = self._eh_workflow_action()
        for lease in self:
            if lease.state not in ('active', 'modified'):
                continue
            unposted = lease.schedule_line_ids.filtered(
                lambda l: not l.is_posted,
            )
            if not unposted:
                lease.state = 'ended'

    def _remaining_periods_from(self, anchor_date):
        """Count unposted schedule lines whose period_date >= anchor_date."""
        self.ensure_one()
        return len(self.schedule_line_ids.filtered(
            lambda l: not l.is_posted and l.period_date >= anchor_date,
        ))

    def _liability_balance_after_last_post(self):
        self.ensure_one()
        posted = self.schedule_line_ids.filtered(
            lambda l: l.is_posted,
        ).sorted('sequence')
        if posted:
            return posted[-1].liability_close
        return self.liability_initial_value

    def _rou_carrying_amount(self):
        self.ensure_one()
        posted = self.schedule_line_ids.filtered(lambda l: l.is_posted)
        accumulated = sum(posted.mapped('rou_amount'))
        return self.rou_initial_value - accumulated

    # ---- cron ----

    @api.model
    def _cron_post_due(self, batch_size=200):
        today = fields.Date.context_today(self)
        due_lines = self.env['eh.lease.schedule.line']._search([
            ('is_posted', '=', False),
            ('period_date', '<=', today),
        ])
        base_domain = [
            ('state', 'in', ['active', 'modified']),
            ('schedule_line_ids', 'in', due_lines),
        ]
        leases = self._eh_rotating_due_batch(
            base_domain,
            batch_size,
            'eh_account_assets_pro.lease_post_due_cursor',
        )

        def _post_lease(lease):
            due = lease.schedule_line_ids.filtered(
                lambda l: not l.is_posted and l.period_date <= today,
            ).sorted('sequence')
            for line in due:
                line.action_post()
            lease._maybe_mark_ended()

        return self._eh_for_each_savepoint(
            leases, _post_lease, log_label="Lease auto post",
        )

    @api.model
    def _eh_rotating_due_batch(self, domain, batch_size, cursor_key):
        """Return one keyset page and persist its last id.

        Due filtering prevents unrelated active records consuming the batch.
        Persisted keyset rotation prevents a permanently failing low-id page
        starving later due records on every daily run.
        """
        size = max(1, int(batch_size or 200))
        params = self.env['ir.config_parameter'].sudo()
        try:
            cursor = max(0, int(params.get_param(cursor_key, '0') or 0))
        except (TypeError, ValueError):
            cursor = 0
        records = self.search(
            list(domain) + [('id', '>', cursor)],
            order='id', limit=size,
        )
        if not records and cursor:
            cursor = 0
            records = self.search(domain, order='id', limit=size)
        params.set_param(cursor_key, str(records[-1].id if records else 0))
        return records

    @api.depends('name', 'lessor_id', 'lessor_id.display_name')
    def _compute_display_name(self):
        for lease in self:
            if lease.lessor_id:
                lease.display_name = "%s / %s" % (
                    lease.name or '', lease.lessor_id.display_name,
                )
            else:
                lease.display_name = lease.name or ''
