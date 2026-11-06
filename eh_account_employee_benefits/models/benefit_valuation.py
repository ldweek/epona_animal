# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.benefit.valuation: one period's actuarial import for a defined benefit
plan, and the IAS 19 ledger mechanics computed from it.

Scope: the actuarial figures (obligation, plan assets, service cost,
actuarial gains and losses, asset ceiling) are IMPORTED INPUTS keyed from
the actuary's report. The module computes only what IAS 19 requires of the
ledger: interest at the discount rate, the P&L vs OCI routing, the asset
ceiling cap, the rollforward ties and one balanced sealed journal entry.

Sign conventions (documented once, used everywhere):

* actuarial_gain_loss_dbo: LOSS POSITIVE. A positive amount is an
  actuarial loss that increases the obligation and is debited to OCI.
* return_on_assets_excess: GAIN POSITIVE. A positive amount is actual
  return above the interest income component; it increases plan assets and
  is credited to OCI.
* oci_remeasurement: LOSS POSITIVE (debit to OCI) =
  actuarial_gain_loss_dbo - return_on_assets_excess + ceiling_effect_delta.
* settlement_gain_loss: GAIN POSITIVE = settlement_dbo_released -
  settlement_payment (IAS 19.109-112); a gain is credited to the service
  cost account (IAS 19.8 includes settlement results in service cost).
* net_liability = closing_dbo - closing_assets (positive = liability,
  negative = surplus before the asset ceiling).

Net interest simplification (IAS 19.123): net interest = discount rate x
OPENING balances (interest_cost = rate x opening DBO, interest_income =
rate x opening plan assets). Mid-year weighting of contributions and
benefit payments in the interest computation is a permitted refinement and
is OUT OF SCOPE here; likewise interest on the asset ceiling effect is not
split out of net interest, the whole change in the ceiling effect routes
to OCI (stated simplification, consistent with rate x opening balances).

Rollforward mechanics and rounding order (each step rounded to company
currency, mirrored by the golden-test oracles):

    interest_cost   = round(rate x opening_dbo)
    interest_income = round(rate x opening_assets)
    net_interest    = interest_cost - interest_income
    closing_dbo     = round(opening_dbo + current_service_cost
                            + past_service_cost + interest_cost
                            - benefits_paid + actuarial_gain_loss_dbo
                            - settlement_dbo_released)
    closing_assets  = round(opening_assets + interest_income
                            + contributions_employer
                            + return_on_assets_excess
                            - (benefits_paid + settlement_payment
                               when the plan is funded; an unfunded plan
                               pays both from employer cash))

closing_dbo and closing_assets are stored editable computes: the actuary's
reported closing figures may be keyed over them, and the ROLLFORWARD TIE
constraint refuses any keyed closing that does not reconcile to the
movement analysis within 0.01 (the audit-proof requirement). Opening
figures chain to the prior posted valuation under the same tolerance.

Asset ceiling (IAS 19.64): when the closing position is a surplus,
recognised asset = min(surplus, asset_ceiling); the unrecognisable excess
(ceiling_effect) is a valuation allowance credited to the plan asset
account, and its period change routes to OCI remeasurement, disclosed
separately in the ceiling schedule.

Worked table (golden example 1, funded plan, rate 5 pct,
contributions_posted_elsewhere = True):

    inputs   opening DBO 1,000,000 / assets 800,000; service cost 60,000;
             benefits paid 30,000; contributions 45,000; actuarial loss
             20,000; return excess +5,000
    derive   interest cost 50,000; interest income 40,000; net interest
             10,000; closing DBO 1,100,000; closing assets 860,000; net
             liability 240,000 (opening 200,000); OCI loss 15,000;
             P&L 70,000
    entry    Dr service cost           60,000
             Dr net interest           10,000
             Dr OCI remeasurement      15,000
             Dr plan assets            60,000   (movement 860k - 800k)
             Cr DBO liability         100,000   (movement 1,100k - 1,000k)
             Cr contribution clearing  45,000

The entry balances by the identity: delta DBO - delta assets =
service cost + net interest + remeasurement (pre-ceiling) - contributions
- settlement gain. Benefits paid by a funded plan appear on both movement
deltas and cancel, so they never touch employer cash here.
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

# Tolerance of the rollforward tie and opening chain checks: keyed figures
# must reconcile to the recomputed movement analysis within one cent.
TIE_TOLERANCE = 0.01 + 1e-9


class EhBenefitValuation(models.Model):
    _name = 'eh.benefit.valuation'
    _description = "Defined benefit valuation period (IAS 19)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'period_end desc, id desc'
    _check_company_auto = True
    # Block a direct RPC write of state (e.g. draft->posted) that would skip
    # action_post, its manager check, account validation and the journal
    # entry. Only the flagged actions may move state.
    _eh_guarded_fields = (
        'state', 'move_id', 'company_id', 'currency_id', 'plan_funded',
        'settlement_gain_loss', 'interest_cost', 'interest_income',
        'net_interest', 'service_cost_total', 'pnl_total', 'net_liability',
        'surplus', 'ceiling_effect', 'ceiling_effect_delta',
        'recognised_asset', 'recognised_net_position', 'oci_remeasurement',
        'provenance_quarantined', 'provenance_quarantine_note',
        'provenance_remediated_by_id', 'provenance_remediated_at',
    )

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('posted', "Posted"), ('reversed', "Reversed")],
        default='draft', required=True, tracking=True, index=True)

    plan_id = fields.Many2one(
        'eh.benefit.plan', required=True, index=True, ondelete='restrict',
        tracking=True, check_company=True)
    company_id = fields.Many2one(
        related='plan_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='plan_id.currency_id', store=True, readonly=True)
    plan_funded = fields.Boolean(related='plan_id.funded')

    period_end = fields.Date(required=True, tracking=True)

    # ---- opening position (chained to the prior posted valuation) ----
    opening_dbo = fields.Monetary(
        currency_field='currency_id', tracking=True,
        compute='_compute_opening', store=True, readonly=False,
        precompute=True, string="Opening DBO", aggregator=None,
        help="Defaults to the prior posted valuation's closing DBO and is "
             "constrained to equal it; editable only on the plan's first "
             "valuation.")
    opening_assets = fields.Monetary(
        currency_field='currency_id', tracking=True,
        compute='_compute_opening', store=True, readonly=False,
        precompute=True, string="Opening Plan Assets", aggregator=None,
        help="Defaults to the prior posted valuation's closing plan assets "
             "and is constrained to equal it; editable only on the plan's "
             "first valuation.")
    opening_ceiling_effect = fields.Monetary(
        currency_field='currency_id', tracking=True,
        compute='_compute_opening', store=True, readonly=False,
        precompute=True, string="Opening Ceiling Effect", aggregator=None,
        help="Cumulative asset ceiling allowance carried in from the prior "
             "posted valuation.")

    # ---- actuary inputs ----
    current_service_cost = fields.Monetary(
        currency_field='currency_id', tracking=True)
    past_service_cost = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Plan amendment or curtailment result: recognised in profit "
             "or loss IMMEDIATELY (IAS 19.103), never spread and never in "
             "OCI. May be negative when an amendment reduces benefits.")
    benefits_paid = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Benefits paid in the period. Funded plan: paid out of plan "
             "assets (reduces both the DBO and the assets, no employer "
             "cash leg). Unfunded plan: paid by the employer, credited to "
             "the plan's benefit payment account.")
    contributions_employer = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Employer contributions into the fund (funded plans only). "
             "The credit goes to the contribution clearing account when "
             "contributions are posted elsewhere (default), otherwise to "
             "the bank account.")
    discount_rate = fields.Float(
        digits=(12, 4), tracking=True, string="Discount Rate (%)",
        help="Annual rate per IAS 19.83 (high quality corporate bonds), as "
             "a percentage. Applied to the OPENING balances for net "
             "interest (IAS 19.123 simplified); mid-year cash-flow "
             "weighting is out of scope.")
    actuarial_gain_loss_dbo = fields.Monetary(
        currency_field='currency_id', tracking=True,
        string="Actuarial (Gain)/Loss on DBO",
        help="Demographic and financial remeasurement of the obligation. "
             "LOSS POSITIVE: a positive amount increases the DBO and is "
             "debited to OCI.")
    return_on_assets_excess = fields.Monetary(
        currency_field='currency_id', tracking=True,
        string="Return on Assets Above Interest",
        help="Actual return on plan assets minus the interest income "
             "component. GAIN POSITIVE: a positive amount increases plan "
             "assets and is credited to OCI.")

    # ---- settlements (IAS 19.109-112) ----
    settlement_dbo_released = fields.Monetary(
        currency_field='currency_id', tracking=True,
        string="Settlement: DBO Released",
        help="Obligation extinguished by the settlement.")
    settlement_payment = fields.Monetary(
        currency_field='currency_id', tracking=True,
        string="Settlement: Payment",
        help="Amount paid to settle. Funded plan: paid from plan assets; "
             "unfunded plan: paid by the employer.")
    settlement_gain_loss = fields.Monetary(
        compute='_compute_flows', store=True, currency_field='currency_id',
        string="Settlement Gain/(Loss)",
        help="DBO released minus payment, GAIN POSITIVE; part of service "
             "cost in profit or loss (IAS 19.8), computed, never keyed.")

    # ---- asset ceiling (IAS 19.64) ----
    apply_asset_ceiling = fields.Boolean(
        tracking=True, string="Apply Asset Ceiling",
        help="Tick when the actuary has determined the asset ceiling (the "
             "present value of refunds and contribution reductions "
             "available from the plan). A surplus is then recognised only "
             "up to that ceiling (IAS 19.64).")
    asset_ceiling = fields.Monetary(
        currency_field='currency_id', tracking=True,
        string="Asset Ceiling", aggregator=None,
        help="Present value of available refunds / future contribution "
             "reductions, per the actuary (IFRIC 14 basis).")

    # ---- computed ledger mechanics ----
    interest_cost = fields.Monetary(
        compute='_compute_flows', store=True, currency_field='currency_id',
        help="Discount rate x opening DBO (IAS 19.123 simplified).")
    interest_income = fields.Monetary(
        compute='_compute_flows', store=True, currency_field='currency_id',
        help="Discount rate x opening plan assets (IAS 19.123 simplified).")
    net_interest = fields.Monetary(
        compute='_compute_flows', store=True, currency_field='currency_id',
        string="Net Interest Cost/(Income)",
        help="Interest cost minus interest income; negative for a net "
             "asset position (income).")
    service_cost_total = fields.Monetary(
        compute='_compute_flows', store=True, currency_field='currency_id',
        help="Current plus past service cost (settlement results are shown "
             "on their own line of the same entry).")
    pnl_total = fields.Monetary(
        compute='_compute_flows', store=True, currency_field='currency_id',
        string="P&L Total",
        help="Service cost + net interest - settlement gain: everything "
             "IAS 19 routes to profit or loss. Remeasurements never appear "
             "here.")

    closing_dbo = fields.Monetary(
        currency_field='currency_id', tracking=True,
        compute='_compute_closing', store=True, readonly=False,
        precompute=True, string="Closing DBO", aggregator=None,
        help="Derived from the rollforward; the actuary's reported closing "
             "may be keyed here but must tie to the movement analysis "
             "within 0.01 or posting-time validation refuses it.")
    closing_assets = fields.Monetary(
        currency_field='currency_id', tracking=True,
        compute='_compute_closing', store=True, readonly=False,
        precompute=True, string="Closing Plan Assets", aggregator=None,
        help="Derived from the rollforward; a keyed figure must tie to the "
             "movement analysis within 0.01.")

    net_liability = fields.Monetary(
        compute='_compute_position', store=True,
        currency_field='currency_id', string="Net Liability/(Asset)",
        aggregator=None,
        help="Closing DBO minus closing plan assets, before the asset "
             "ceiling. Negative = surplus.")
    surplus = fields.Monetary(
        compute='_compute_position', store=True,
        currency_field='currency_id', aggregator=None,
        help="Excess of plan assets over the DBO (zero when in deficit).")
    ceiling_effect = fields.Monetary(
        compute='_compute_position', store=True,
        currency_field='currency_id', string="Ceiling Effect (Closing)",
        aggregator=None,
        help="Cumulative surplus not recognisable: max(surplus - ceiling, "
             "0) when the ceiling applies. Disclosed separately.")
    ceiling_effect_delta = fields.Monetary(
        compute='_compute_position', store=True,
        currency_field='currency_id', string="Ceiling Effect Change",
        help="Period change in the ceiling effect; routed to OCI "
             "remeasurement (IAS 19.57(d)).")
    recognised_asset = fields.Monetary(
        compute='_compute_position', store=True,
        currency_field='currency_id', aggregator=None,
        help="min(surplus, asset ceiling) when in surplus (IAS 19.64).")
    recognised_net_position = fields.Monetary(
        compute='_compute_position', store=True,
        currency_field='currency_id', string="Recognised Net Position",
        aggregator=None,
        help="Net liability (positive) or recognised asset after the "
             "ceiling (negative).")
    oci_remeasurement = fields.Monetary(
        compute='_compute_position', store=True,
        currency_field='currency_id', string="OCI Remeasurement (Loss+)",
        help="Actuarial (gain)/loss on the DBO minus the excess return on "
             "assets plus the ceiling effect change. LOSS POSITIVE = debit "
             "to OCI. Never recycled to profit or loss (IAS 19.122).")

    move_id = fields.Many2one(
        'account.move', string="Journal Entry", readonly=True, copy=False,
        check_company=True)
    move_ids = fields.One2many('account.move', 'eh_benefit_valuation_id')
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

    supersedes_id = fields.Many2one(
        'eh.benefit.valuation', string="Corrects Valuation", readonly=True,
        copy=False, index=True, ondelete='restrict',
        help="Immutable link to the reversed valuation this revision "
             "corrects. Original valuation and journal evidence remain "
             "preserved.")
    superseded_by_ids = fields.One2many(
        'eh.benefit.valuation', 'supersedes_id', string="Corrections",
        readonly=True)

    notes = fields.Text()

    _unique_revision = models.Constraint(
        'UNIQUE (supersedes_id)',
        'A reversed valuation can have only one direct correction.')
    _check_opening = models.Constraint(
        'CHECK (opening_dbo >= 0 AND opening_assets >= 0 '
        'AND opening_ceiling_effect >= 0)',
        'Opening figures cannot be negative.')
    _check_nonneg_inputs = models.Constraint(
        'CHECK (current_service_cost >= 0 AND benefits_paid >= 0 '
        'AND contributions_employer >= 0 AND settlement_payment >= 0 '
        'AND settlement_dbo_released >= 0 AND asset_ceiling >= 0)',
        'Service cost, benefits, contributions, settlement amounts and the '
        'asset ceiling cannot be negative (actuarial gains/losses and past '
        'service cost carry their own signs).')
    _check_rate = models.Constraint(
        'CHECK (discount_rate >= 0)', 'The discount rate cannot be negative.')

    # Everything that feeds the posted figures freezes on post.
    _FROZEN_FIELDS = (
        'plan_id', 'period_end', 'opening_dbo', 'opening_assets',
        'opening_ceiling_effect', 'current_service_cost',
        'past_service_cost', 'benefits_paid', 'contributions_employer',
        'discount_rate', 'actuarial_gain_loss_dbo',
        'return_on_assets_excess', 'settlement_dbo_released',
        'settlement_payment', 'apply_asset_ceiling', 'asset_ceiling',
        'closing_dbo', 'closing_assets',
        'company_id', 'currency_id', 'plan_funded',
        'settlement_gain_loss', 'interest_cost', 'interest_income',
        'net_interest', 'service_cost_total', 'pnl_total', 'net_liability',
        'surplus', 'ceiling_effect', 'ceiling_effect_delta',
        'recognised_asset', 'recognised_net_position', 'oci_remeasurement',
    )
    _FROZEN_STATES = ('posted', 'reversed')

    def init(self):
        """One live revision per plan/period; reversed history is retained."""
        self.env.cr.execute("""
            DO $$
            DECLARE constraint_name text;
            BEGIN
                FOR constraint_name IN
                    SELECT conname
                      FROM pg_constraint
                     WHERE conrelid = 'eh_benefit_valuation'::regclass
                       AND contype = 'u'
                       AND pg_get_constraintdef(oid)
                           LIKE 'UNIQUE (plan_id, period_end)%'
                LOOP
                    EXECUTE format(
                        'ALTER TABLE eh_benefit_valuation '
                        'DROP CONSTRAINT %I', constraint_name
                    );
                END LOOP;
            END $$
        """)
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
                eh_benefit_valuation_live_period_uniq
            ON eh_benefit_valuation (plan_id, period_end)
            WHERE state IN ('draft', 'posted')
        """)

    # ------------------------------------------------------------------
    # rollforward arithmetic (single source of truth for computes,
    # constraints and the posting legs)
    # ------------------------------------------------------------------

    def _round(self, amount):
        cur = self.currency_id or self.company_id.currency_id
        return cur.round(amount) if cur else round(amount, 2)

    def _prior_posted(self):
        """Latest posted valuation of the same plan before this period."""
        self.ensure_one()
        if not self.plan_id or not self.period_end:
            return self.browse()
        domain = [
            ('plan_id', '=', self.plan_id.id),
            ('state', '=', 'posted'),
            ('period_end', '<', self.period_end),
        ]
        # Only exclude self once it has a real database id: Odoo 17 passes
        # a NewId straight into SQL and crashes (19 filters it silently).
        if isinstance(self.id, int):
            domain.append(('id', '!=', self.id))
        return self.search(
            domain, order='period_end desc, id desc', limit=1)

    def _derived_figures(self):
        """Recompute the full rollforward from the INPUTS only (never from
        the stored closing figures), in the documented rounding order."""
        self.ensure_one()
        rate = (self.discount_rate or 0.0) / 100.0
        funded = self.plan_id.funded
        interest_cost = self._round(self.opening_dbo * rate)
        interest_income = self._round(self.opening_assets * rate)
        net_interest = self._round(interest_cost - interest_income)
        settlement_gain = self._round(
            self.settlement_dbo_released - self.settlement_payment)
        closing_dbo = self._round(
            self.opening_dbo + self.current_service_cost
            + self.past_service_cost + interest_cost - self.benefits_paid
            + self.actuarial_gain_loss_dbo - self.settlement_dbo_released)
        asset_outflow = (
            self.benefits_paid + self.settlement_payment if funded else 0.0)
        closing_assets = self._round(
            self.opening_assets + interest_income
            + self.contributions_employer + self.return_on_assets_excess
            - asset_outflow)
        return {
            'interest_cost': interest_cost,
            'interest_income': interest_income,
            'net_interest': net_interest,
            'settlement_gain': settlement_gain,
            'closing_dbo': closing_dbo,
            'closing_assets': closing_assets,
        }

    def _position_figures(self):
        """Ceiling test and OCI routing over the STORED closing figures
        (which the tie constraint keeps within a cent of the derivation)."""
        self.ensure_one()
        net_liability = self._round(self.closing_dbo - self.closing_assets)
        surplus = max(-net_liability, 0.0)
        if self.apply_asset_ceiling and surplus > 0.0:
            ceiling_effect = self._round(
                max(surplus - self.asset_ceiling, 0.0))
            recognised_asset = self._round(
                min(surplus, self.asset_ceiling))
        else:
            ceiling_effect = 0.0
            recognised_asset = surplus
        delta = self._round(ceiling_effect - self.opening_ceiling_effect)
        oci = self._round(
            self.actuarial_gain_loss_dbo - self.return_on_assets_excess
            + delta)
        recognised_net = (
            net_liability if net_liability >= 0.0
            else self._round(-recognised_asset))
        return {
            'net_liability': net_liability,
            'surplus': surplus,
            'ceiling_effect': ceiling_effect,
            'ceiling_effect_delta': delta,
            'recognised_asset': recognised_asset,
            'recognised_net_position': recognised_net,
            'oci_remeasurement': oci,
        }

    # ------------------------------------------------------------------
    # computes
    # ------------------------------------------------------------------

    @api.depends('plan_id', 'period_end')
    def _compute_opening(self):
        for v in self:
            prior = v._prior_posted()
            if prior:
                # Chained: the opening position IS the prior closing.
                v.opening_dbo = prior.closing_dbo
                v.opening_assets = prior.closing_assets
                v.opening_ceiling_effect = prior.ceiling_effect
            else:
                # First valuation: editable-compute idiom keeps the keyed
                # figures.
                v.opening_dbo = v.opening_dbo
                v.opening_assets = v.opening_assets
                v.opening_ceiling_effect = v.opening_ceiling_effect

    @api.depends('opening_dbo', 'opening_assets', 'discount_rate',
                 'current_service_cost', 'past_service_cost',
                 'settlement_dbo_released', 'settlement_payment')
    def _compute_flows(self):
        for v in self:
            f = v._derived_figures()
            v.interest_cost = f['interest_cost']
            v.interest_income = f['interest_income']
            v.net_interest = f['net_interest']
            v.settlement_gain_loss = f['settlement_gain']
            v.service_cost_total = v._round(
                v.current_service_cost + v.past_service_cost)
            v.pnl_total = v._round(
                v.current_service_cost + v.past_service_cost
                + f['net_interest'] - f['settlement_gain'])

    @api.depends('opening_dbo', 'opening_assets', 'discount_rate',
                 'current_service_cost', 'past_service_cost',
                 'benefits_paid', 'contributions_employer',
                 'actuarial_gain_loss_dbo', 'return_on_assets_excess',
                 'settlement_dbo_released', 'settlement_payment',
                 'plan_id.funded')
    def _compute_closing(self):
        for v in self:
            f = v._derived_figures()
            v.closing_dbo = f['closing_dbo']
            v.closing_assets = f['closing_assets']

    @api.depends('closing_dbo', 'closing_assets', 'apply_asset_ceiling',
                 'asset_ceiling', 'opening_ceiling_effect',
                 'actuarial_gain_loss_dbo', 'return_on_assets_excess')
    def _compute_position(self):
        for v in self:
            p = v._position_figures()
            v.net_liability = p['net_liability']
            v.surplus = p['surplus']
            v.ceiling_effect = p['ceiling_effect']
            v.ceiling_effect_delta = p['ceiling_effect_delta']
            v.recognised_asset = p['recognised_asset']
            v.recognised_net_position = p['recognised_net_position']
            v.oci_remeasurement = p['oci_remeasurement']

    def _compute_move_count(self):
        for v in self:
            v.move_count = len(v.move_ids)

    # ------------------------------------------------------------------
    # constraints (the audit-proof requirement)
    # ------------------------------------------------------------------

    @api.constrains('opening_dbo', 'opening_assets', 'discount_rate',
                    'current_service_cost', 'past_service_cost',
                    'benefits_paid', 'contributions_employer',
                    'actuarial_gain_loss_dbo', 'return_on_assets_excess',
                    'settlement_dbo_released', 'settlement_payment',
                    'closing_dbo', 'closing_assets')
    def _check_rollforward_tie(self):
        """Closing figures must tie to the movement analysis within 0.01.

        The closing fields are editable so the actuary's reported figures
        can be keyed, but a mis-keyed closing (or a raw write that skips
        the compute) is refused here: the disclosure schedules read the
        stored figures, so they must reconcile opening -> closing exactly.
        """
        for v in self:
            f = v._derived_figures()
            if abs(v.closing_dbo - f['closing_dbo']) > TIE_TOLERANCE:
                raise ValidationError(_(
                    "%(name)s: the closing DBO (%(keyed).2f) does not tie "
                    "to the obligation rollforward (opening + service "
                    "costs + interest cost - benefits paid + actuarial "
                    "loss - settlements = %(derived).2f). Correct the "
                    "closing figure or the movement inputs.",
                    name=v.display_name, keyed=v.closing_dbo,
                    derived=f['closing_dbo']))
            if abs(v.closing_assets - f['closing_assets']) > TIE_TOLERANCE:
                raise ValidationError(_(
                    "%(name)s: the closing plan assets (%(keyed).2f) do "
                    "not tie to the asset rollforward (opening + interest "
                    "income + contributions + excess return - benefits and "
                    "settlements paid from assets = %(derived).2f). "
                    "Correct the closing figure or the movement inputs.",
                    name=v.display_name, keyed=v.closing_assets,
                    derived=f['closing_assets']))
            if f['closing_dbo'] < -0.005:
                raise ValidationError(_(
                    "%(name)s: the rollforward drives the closing DBO "
                    "negative (%(derived).2f); an obligation cannot be "
                    "negative. Check benefits paid and settlements.",
                    name=v.display_name, derived=f['closing_dbo']))
            if f['closing_assets'] < -0.005:
                raise ValidationError(_(
                    "%(name)s: the rollforward drives the closing plan "
                    "assets negative (%(derived).2f); a fund cannot pay "
                    "out more than it holds.",
                    name=v.display_name, derived=f['closing_assets']))

    @api.constrains('opening_dbo', 'opening_assets',
                    'opening_ceiling_effect', 'plan_id', 'period_end')
    def _check_opening_chain(self):
        for v in self:
            prior = v._prior_posted()
            if not prior:
                continue
            pairs = (
                (v.opening_dbo, prior.closing_dbo, _("opening DBO")),
                (v.opening_assets, prior.closing_assets,
                 _("opening plan assets")),
                (v.opening_ceiling_effect, prior.ceiling_effect,
                 _("opening ceiling effect")),
            )
            for keyed, chained, label in pairs:
                if abs(keyed - chained) > TIE_TOLERANCE:
                    raise ValidationError(_(
                        "%(name)s: the %(label)s (%(keyed).2f) must equal "
                        "the prior posted valuation's closing figure "
                        "(%(chained).2f, %(prior)s). The rollforward "
                        "chain is what makes the disclosures audit-proof.",
                        name=v.display_name, label=label, keyed=keyed,
                        chained=chained, prior=prior.display_name))

    @api.constrains('plan_id', 'opening_assets', 'contributions_employer',
                    'return_on_assets_excess', 'apply_asset_ceiling')
    def _check_unfunded_inputs(self):
        for v in self:
            if v.plan_id.funded:
                continue
            if (v.opening_assets or v.contributions_employer
                    or v.return_on_assets_excess):
                raise ValidationError(_(
                    "%s belongs to an unfunded plan: it holds no plan "
                    "assets, so opening assets, employer contributions and "
                    "excess return must be zero. The employer pays "
                    "benefits directly.", v.display_name))
            if v.apply_asset_ceiling:
                raise ValidationError(_(
                    "%s belongs to an unfunded plan, which can never be in "
                    "surplus; the asset ceiling (IAS 19.64) does not "
                    "apply.", v.display_name))

    # ------------------------------------------------------------------
    # frozen-after-post guards
    # ------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        if any('supersedes_id' in vals for vals in vals_list):
            raise AccessError(_(
                "Valuation correction provenance is server-owned."
            ))
        self._eh_authorize_plan_targets([
            vals.get('plan_id')
            or self.env.context.get('default_plan_id')
            for vals in vals_list
        ])
        for vals in vals_list:
            plan_id = vals.get('plan_id') \
                or self.env.context.get('default_plan_id')
            period_end = vals.get('period_end') \
                or self.env.context.get('default_period_end')
            if plan_id and period_end:
                live = self.search([
                    ('plan_id', '=', plan_id),
                    ('period_end', '=', period_end),
                    ('state', 'in', ('draft', 'posted')),
                ], limit=1)
                if live:
                    raise ValidationError(_(
                        "Plan %(plan)s already has a live valuation for "
                        "%(period)s. Reverse it before creating a correction.",
                        plan=live.plan_id.display_name,
                        period=period_end,
                    ))
                reversed_prior = self.search([
                    ('plan_id', '=', plan_id),
                    ('period_end', '=', period_end),
                    ('state', '=', 'reversed'),
                    ('superseded_by_ids', '=', False),
                ], order='id desc', limit=1)
                if reversed_prior:
                    reversed_prior._eh_assert_provenance_ready()
                    vals['supersedes_id'] = reversed_prior.id
            if vals.get('name', '/') == '/':
                plan = self.env['eh.benefit.plan'].browse(plan_id).exists()
                sequence_date = fields.Date.to_date(period_end)
                if plan and sequence_date:
                    vals['name'] = next_company_reference(
                        self.env, 'eh.benefit.valuation', plan.company_id,
                        sequence_date,
                    ) or '/'
        return super().create(vals_list)

    def _eh_authorize_plan_targets(self, plan_ids):
        plans = self.env['eh.benefit.plan'].browse(
            sorted({plan_id for plan_id in plan_ids if plan_id}))
        if plans and not self.env.su:
            plans._eh_check_access('write')
        if plans:
            plans.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM eh_benefit_plan WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(plans.ids),),
            )
            plans.invalidate_recordset()
        if plans.filtered(lambda plan: plan.state == 'closed'):
            raise UserError(_(
                "A valuation cannot be attached to a closed benefit plan."
            ))
        return plans

    def _eh_lock_integrity(self, target_plan_ids=()):
        """Lock parents then children before any freeze/reparent decision."""
        plan_ids = self.mapped('plan_id').ids + list(target_plan_ids)
        self._eh_authorize_plan_targets(plan_ids)
        if not self.env.su:
            self._eh_check_access('write')
        ids = sorted(self.ids)
        if ids:
            self.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM eh_benefit_valuation WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(ids),),
            )
            self.invalidate_recordset()
            self._check_company()
        return self

    def write(self, vals):
        if 'supersedes_id' in vals or 'superseded_by_ids' in vals:
            raise AccessError(_(
                "Valuation correction provenance is immutable."
            ))
        self._eh_lock_integrity(
            [vals.get('plan_id')] if 'plan_id' in vals else [])
        # Posted-figure INPUTS are frozen for everyone (restate via reversal):
        # a data-integrity guard, not su-gated. STATE transitions are enforced
        # by the inherited eh.workflow.guard, which blocks a non-superuser
        # direct write; the sanctioned actions run under sudo. (A trusted
        # superuser re-keying state is an accepted footgun; the sealed move and
        # these frozen inputs still protect the figures.)
        frozen = [f for f in self._FROZEN_FIELDS if f in vals]
        if frozen:
            locked = self.filtered(
                lambda v: v.state in self._FROZEN_STATES)
            if locked:
                raise UserError(_(
                    "Valuation inputs (%(fields)s) are frozen once "
                    "posted (%(names)s). Reverse the valuation to "
                    "restate the period.",
                    fields=', '.join(frozen),
                    names=', '.join(locked.mapped('display_name'))))
        if ('provenance_remediation_reference' in vals
                and self.filtered(lambda v: not v.provenance_quarantined)):
            raise UserError(_(
                "A provenance remediation reference can only be entered "
                "while legacy evidence is quarantined."))
        return super().write(vals)

    def unlink(self):
        self._eh_lock_integrity()
        posted = self.filtered(lambda v: v.state in self._FROZEN_STATES)
        if posted:
            raise UserError(_(
                "A posted valuation cannot be deleted; its journal entry "
                "would be orphaned (%s). Reverse it instead.",
                ', '.join(posted.mapped('display_name'))))
        return super().unlink()

    # ------------------------------------------------------------------
    # posting
    # ------------------------------------------------------------------

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can post benefit "
                "valuations."))

    def _eh_lock_transition(self):
        self.ensure_one()
        if not self.env.su:
            self._eh_check_access('write')
            self.plan_id._eh_check_access('read')
        self.plan_id.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_benefit_plan WHERE id = %s FOR UPDATE",
            (self.plan_id.id,),
        )
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_benefit_valuation WHERE id = %s FOR UPDATE",
            (self.id,),
        )
        self.plan_id.invalidate_recordset()
        self.invalidate_recordset()
        self._check_company()
        return self

    def _validate_accounts(self):
        self.ensure_one()
        plan = self.plan_id
        cur = self.currency_id
        self._eh_assert_provenance_ready()
        plan._check_accounting_configuration()
        missing = []
        if not plan.journal_id:
            missing.append(_("journal"))
        for fname, label in (
                ('service_cost_account_id', _("service cost account")),
                ('net_interest_account_id', _("net interest account")),
                ('oci_account_id', _("remeasurement OCI account")),
                ('dbo_account_id', _("DBO liability account"))):
            if not plan[fname]:
                missing.append(label)
        if plan.funded and not plan.plan_asset_account_id:
            missing.append(_("plan asset account"))
        if plan.funded and not cur.is_zero(self.contributions_employer):
            contribution_account = (
                plan.contribution_account_id
                if plan.contributions_posted_elsewhere
                else plan.contribution_cash_account_id
            )
            if not contribution_account:
                missing.append(
                    _("contribution clearing account")
                    if plan.contributions_posted_elsewhere
                    else _("contribution bank account")
                )
        if (not plan.funded
                and not cur.is_zero(
                    self.benefits_paid + self.settlement_payment)
                and not plan.benefit_payment_account_id):
            missing.append(_("benefit payment account"))
        if missing:
            raise UserError(_(
                "Configure the %(missing)s on plan %(plan)s first.",
                missing=', '.join(missing), plan=plan.display_name))

    def _eh_assert_provenance_ready(self):
        self.ensure_one()
        if self.provenance_quarantined:
            raise UserError(_(
                "Legacy benefit-valuation provenance on %(name)s could not "
                "be proved. Accounting actions are blocked pending "
                "controlled remediation. %(note)s",
                name=self.display_name,
                note=self.provenance_quarantine_note or '',
            ))

    def _je_legs(self):
        """Build the single valuation entry (see the class docstring for
        the identity that guarantees balance). Zero legs are skipped."""
        self.ensure_one()
        plan = self.plan_id
        cur = self.currency_id
        legs = []

        def leg(account, amount, label):
            # amount > 0 = debit, < 0 = credit
            if cur.is_zero(amount):
                return
            legs.append((
                account,
                amount if amount > 0 else 0.0,
                -amount if amount < 0 else 0.0,
                label,
            ))

        # P&L: service cost (current + past, IAS 19.103 immediate).
        leg(plan.service_cost_account_id, self.service_cost_total,
            _("Service cost (current + past) %s", self.name))
        # P&L: settlement gain/(loss) inside service cost (IAS 19.8): a
        # gain credits, a loss debits.
        leg(plan.service_cost_account_id, -self.settlement_gain_loss,
            _("Settlement gain/loss %s", self.name))
        # P&L: net interest on the opening net position (IAS 19.123).
        leg(plan.net_interest_account_id, self.net_interest,
            _("Net interest on net defined benefit position %s", self.name))
        # OCI: remeasurements, loss positive = debit; never recycled
        # (IAS 19.122).
        leg(plan.oci_account_id, self.oci_remeasurement,
            _("Remeasurement to OCI (non-recycling) %s", self.name))
        # Balance sheet: DBO movement (increase = credit).
        leg(plan.dbo_account_id,
            -self._round(self.closing_dbo - self.opening_dbo),
            _("DBO movement %s", self.name))
        if plan.funded:
            # Balance sheet: gross plan asset movement (increase = debit).
            leg(plan.plan_asset_account_id,
                self._round(self.closing_assets - self.opening_assets),
                _("Plan assets movement %s", self.name))
            # Asset ceiling allowance: a growing effect credits the plan
            # asset account so the recognised position never exceeds the
            # ceiling; a release debits it back.
            leg(plan.plan_asset_account_id, -self.ceiling_effect_delta,
                _("Asset ceiling allowance (IAS 19.64) %s", self.name))
            # Contributions: credit clearing when another system posted the
            # cash leg; otherwise credit this module's explicit bank account.
            contribution_account = (
                plan.contribution_account_id
                if plan.contributions_posted_elsewhere
                else plan.contribution_cash_account_id
            )
            leg(contribution_account, -self.contributions_employer,
                _("Employer contributions %s", self.name))
        else:
            # Unfunded: the employer pays benefits and settlements.
            leg(plan.benefit_payment_account_id,
                -self._round(self.benefits_paid + self.settlement_payment),
                _("Benefits and settlements paid by employer %s",
                  self.name))
        return legs

    def action_post(self):
        self.ensure_one()
        self._check_manager()
        self._eh_lock_transition()
        if self.supersedes_id:
            self.supersedes_id._eh_assert_provenance_ready()
        self._compute_flows()
        self._compute_position()
        self.flush_recordset([
            'settlement_gain_loss', 'interest_cost', 'interest_income',
            'net_interest', 'service_cost_total', 'pnl_total',
            'net_liability', 'surplus', 'ceiling_effect',
            'ceiling_effect_delta', 'recognised_asset',
            'recognised_net_position', 'oci_remeasurement',
        ])
        if self.state != 'draft':
            raise UserError(_("Only a draft valuation can be posted."))
        if self.plan_id.state != 'active':
            raise UserError(_(
                "Activate plan %s before posting valuations.",
                self.plan_id.display_name))
        out_of_order = self.search_count([
            ('plan_id', '=', self.plan_id.id), ('state', '=', 'posted'),
            ('period_end', '>=', self.period_end)])
        if out_of_order:
            raise UserError(_(
                "A later valuation of %s is already posted; periods post "
                "in chronological order so the rollforward chain holds.",
                self.plan_id.display_name))
        # Re-validate at the posting gate: the api.constrains only fire on
        # writes to THIS record, so a draft created before the prior period
        # posted could carry a stale opening position (or a tie broken by a
        # later plan-flag change). Nothing unreconciled may reach the ledger.
        self._check_opening_chain()
        self._check_rollforward_tie()
        self._check_unfunded_inputs()
        self._validate_accounts()
        assert_accounting_date_open(
            self.env, self.company_id, self.plan_id.journal_id,
            self.period_end, _("valuation posting"),
        )
        legs = self._je_legs()
        if not legs:
            raise UserError(_(
                "Nothing to post on %s: every movement in the period is "
                "nil.", self.display_name))
        total = sum(l[1] - l[2] for l in legs)
        if abs(total) > 0.005:
            # Defensive: the identity in the class docstring makes this
            # unreachable; if it ever fires the inputs breached an
            # invariant and the entry must not post.
            raise UserError(_(
                "Internal tie failure on %(name)s: the valuation entry is "
                "out of balance by %(amount).2f. No entry was posted.",
                name=self.display_name, amount=total))
        move = self.env['account.move'].with_context(**{
            BENEFIT_MOVE_CONTEXT: BENEFIT_MOVE_CAPABILITY,
        })._eh_create_sealed({
            'move_type': 'entry',
            'company_id': self.company_id.id,
            'date': self.period_end,
            'journal_id': self.plan_id.journal_id.id,
            'ref': _("%(plan)s %(name)s", plan=self.plan_id.name,
                     name=self.name),
            'eh_benefit_valuation_id': self.id,
            'eh_benefit_provenance_version': BENEFIT_PROVENANCE_VERSION,
            'eh_sealed': True,
            'line_ids': [(0, 0, {
                'name': label, 'account_id': account.id,
                'debit': debit, 'credit': credit,
            }) for account, debit, credit, label in legs],
        })
        move.action_post()
        self.sudo().write({
            'state': 'posted', 'move_id': move.id})
        return True

    def action_reverse(self):
        """State-machine reversal: posts the mirroring entry and marks the
        valuation reversed. The standard reversal wizard path is blocked by
        the seal on the generated move (eh_account_base), so the audit
        trail always runs through here."""
        self.ensure_one()
        self._check_manager()
        self._eh_lock_transition()
        self._eh_assert_provenance_ready()
        # A legacy plan can carry gross historical accounts.  Reversal must
        # preserve and neutralise that sealed evidence even though NEW posts
        # now require a single net-position account under IAS 19.63.
        self.plan_id._check_account_classifications()
        if self.state != 'posted':
            raise UserError(_("Only a posted valuation can be reversed."))
        later = self.search_count([
            ('plan_id', '=', self.plan_id.id), ('state', '=', 'posted'),
            ('period_end', '>', self.period_end)])
        if later:
            raise UserError(_(
                "Reverse the later posted valuations of %s first: the "
                "opening chain runs newest to oldest.",
                self.plan_id.display_name))
        self._eh_validate_posting_move()
        reversal_date = self._eh_reversal_date()
        assert_accounting_date_open(
            self.env, self.company_id, self.plan_id.journal_id,
            reversal_date, _("valuation reversal"),
        )
        reversal = self.env['account.move'].with_context(**{
            BENEFIT_MOVE_CONTEXT: BENEFIT_MOVE_CAPABILITY,
        })._eh_create_sealed({
            'move_type': 'entry',
            'company_id': self.company_id.id,
            'date': reversal_date,
            'journal_id': self.plan_id.journal_id.id,
            'ref': _("Reversal of %s", self.name),
            'eh_benefit_valuation_id': self.id,
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
                "Odoo shifted the valuation reversal from %(requested)s to "
                "%(actual)s. Nothing was reversed. Choose an open exact "
                "accounting date.",
                requested=reversal_date, actual=reversal.date))
        self._eh_validate_reversal(reversal)
        self._eh_write_controlled_reversal_evidence({
            'state': 'reversed', 'reversal_date': reversal_date,
        })
        return True

    def _eh_write_controlled_reversal_evidence(self, vals):
        """Commit only server-proved reversal evidence on a closed plan.

        Ordinary ``write`` deliberately refuses any valuation mutation whose
        parent is closed.  A reversal has already locked both rows, validated
        the exact primary/counter-entry topology, and must be allowed to
        finish without reopening the plan.  Calling ``super`` bypasses only
        this model's closed-parent guard; the narrow allow-list prevents the
        helper becoming a general frozen-input escape hatch.
        """
        allowed = {
            'state', 'reversal_date', 'provenance_quarantined',
            'provenance_quarantine_note', 'provenance_remediated_by_id',
            'provenance_remediated_at',
        }
        if vals.get('state') != 'reversed' or set(vals) - allowed:
            raise AccessError(_("Invalid controlled reversal evidence write."))
        return super(EhBenefitValuation, self.sudo()).write(vals)

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
        """Prove exact posted ledger shape without claiming legacy origin.

        Legacy provenance cannot be recreated. Manager may only neutralise
        exact linked evidence; source figures are never unsealed or rewritten.
        """
        self.ensure_one()
        move = self.move_id
        expected = sorted(
            (account.id, self._round(debit), self._round(credit))
            for account, debit, credit, _label in self._je_legs()
        )
        if (
            not move
            or move.state != 'posted'
            or move.eh_benefit_valuation_id != self
            or move.eh_benefit_dc_accrual_id
            or move.reversed_entry_id
            or move.company_id != self.company_id
            or move.journal_id != self.plan_id.journal_id
            or move.date != self.period_end
            or self._eh_move_signature(move) != expected
        ):
            raise UserError(_(
                "Legacy valuation evidence does not match the frozen source "
                "and cannot use controlled remediation."))
        return move

    def action_remediate_provenance(self):
        """Neutralise quarantined evidence; never bless or delete history."""
        self.ensure_one()
        self._check_manager()
        self._eh_lock_transition()
        if not self.provenance_quarantined:
            raise UserError(_("This valuation is not quarantined."))
        reference = (self.provenance_remediation_reference or '').strip()
        if len(reference) < 8:
            raise UserError(_(
                "Enter a manager/auditor remediation reference of at least "
                "eight characters."))
        primary = self._eh_validate_legacy_primary_for_remediation()
        evidence = self.move_ids
        if self.state == 'posted':
            later = self.search_count([
                ('plan_id', '=', self.plan_id.id),
                ('state', '=', 'posted'),
                ('period_end', '>', self.period_end),
            ])
            if later:
                raise UserError(_(
                    "Reverse or remediate later posted valuations first."))
            reversal_date = self._eh_reversal_date()
            assert_accounting_date_open(
                self.env, self.company_id, self.plan_id.journal_id,
                reversal_date, _("valuation remediation reversal"),
            )
            values = {
                'move_type': 'entry',
                'company_id': self.company_id.id,
                'date': reversal_date,
                'journal_id': self.plan_id.journal_id.id,
                'ref': _("Audited remediation of %s", self.name),
                'eh_benefit_valuation_id': self.id,
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
                or reversal.journal_id != self.plan_id.journal_id
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
        self._eh_write_controlled_reversal_evidence({
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
        return sorted(
            (
                line.account_id.id,
                self._round(line.debit),
                self._round(line.credit),
            )
            for line in move.line_ids
            if not self.currency_id.is_zero(line.debit - line.credit)
        )

    def _eh_validate_posting_move(self):
        self.ensure_one()
        self._eh_assert_provenance_ready()
        self.plan_id._check_accounting_configuration()
        move = self.move_id
        if not self.env.su:
            move._eh_check_access('read')
        expected = sorted(
            (account.id, self._round(debit), self._round(credit))
            for account, debit, credit, _label in self._je_legs()
        )
        if (
            not move
            or move.state != 'posted'
            or not move.eh_sealed
            or move.eh_benefit_valuation_id != self
            or move.eh_benefit_dc_accrual_id
            or move.eh_benefit_provenance_version
               != BENEFIT_PROVENANCE_VERSION
            or move.reversed_entry_id
            or move.company_id != self.company_id
            or move.journal_id != self.plan_id.journal_id
            or move.date != self.period_end
            or self._eh_move_signature(move) != expected
        ):
            raise UserError(_(
                "The linked benefit valuation entry failed sealed "
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
            or reversal.eh_benefit_valuation_id != self
            or reversal.eh_benefit_dc_accrual_id
            or reversal.eh_benefit_provenance_version
               != BENEFIT_PROVENANCE_VERSION
            or reversal.reversed_entry_id != self.move_id
            or reversal.company_id != self.company_id
            or reversal.journal_id != self.plan_id.journal_id
            or reversal.date < self.move_id.date
            or (self.reversal_date and reversal.date != self.reversal_date)
            or self._eh_move_signature(reversal) != reverse_signature
        ):
            raise UserError(_(
                "The benefit valuation reversal failed sealed provenance "
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
            'domain': [('eh_benefit_valuation_id', '=', self.id)],
        }
