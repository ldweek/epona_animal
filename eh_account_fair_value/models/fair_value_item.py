# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.fair.value.item: an item measured at fair value under IFRS 13.

The item is classified in the fair-value hierarchy by the observability of
its inputs (Level 1/2/3). Remeasuring posts the change from the current
carrying amount to fair value, to profit or loss or OCI, and then rolls the
carrying amount forward to the new fair value.
"""

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


_FV_MOVE_LINK_INTERNAL = 'eh_fair_value_move_link_internal'
_FV_MOVE_LINK_CAPABILITY = object()
_FV_ITEM_INTERNAL = 'eh_fair_value_item_internal'
_FV_ITEM_CAPABILITY = object()
_FV_LEVEL_TRANSFER_INTERNAL = 'eh_fair_value_level_transfer_internal'
_FV_LEVEL_TRANSFER_CAPABILITY = object()


class EhFairValueItem(models.Model):
    _name = 'eh.fair.value.item'
    _description = "Fair value item (IFRS 13)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'measurement_date desc, id desc'
    _rec_name = 'name'
    _check_company_auto = True

    # Workflow-critical fields that may only change through this model's own
    # actions (action_remeasure / action_cancel / action_reset_to_draft /
    # action_derecognise / action_recycle), never a direct RPC write: a plain
    # user could otherwise write state='measured' to skip the remeasurement
    # posting, or flip recycled to bypass the OCI-reserve settlement.
    _eh_guarded_fields = (
        'state', 'recycled', 'currency_id', 'sppi_pass',
        'ifrs9_classification', 'remeasurement', 'derecognition_date',
        'prior_company_carrying', 'level',
    )
    _eh_create_guarded_fields = (
        'state', 'recycled', 'sppi_pass', 'ifrs9_classification',
        'remeasurement', 'derecognition_date',
    )

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('measured', "Measured"),
         ('derecognised', "Derecognised"), ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    company_currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True,
        string="Company Currency")
    currency_id = fields.Many2one(
        'res.currency', required=True,
        default=lambda self: self.env.company.currency_id,
        string="Measurement Currency", tracking=True,
        help="Currency in which the instrument's carrying amount and fair "
             "value are stated. Journal entries remain balanced in company "
             "currency and preserve this currency in amount_currency.")

    nature = fields.Selection(
        [('financial_asset', "Financial asset"),
         ('financial_liability', "Financial liability"),
         ('investment_property', "Investment property"),
         ('non_financial_asset', "Other non-financial asset")],
        default='financial_asset', required=True, tracking=True)

    # ---- IFRS 9 classification engine (IFRS 9.4.1) ----
    instrument_type = fields.Selection(
        [('debt', "Debt instrument"),
         ('equity', "Equity instrument"),
         ('derivative', "Derivative")],
        tracking=True,
        help="Contractual nature of the financial instrument, driving the "
             "IFRS 9 classification engine. Leave empty on non-financial "
             "items and on records created before the engine existed; those "
             "keep the manual routing behaviour.")
    sppi_fixed_dates = fields.Boolean(
        string="Cash Flows on Fixed Dates", tracking=True,
        help="IFRS 9.4.1.2(b): the contractual terms give rise on specified "
             "dates to cash flows.")
    sppi_interest_only = fields.Boolean(
        string="Solely Principal and Interest", tracking=True,
        help="IFRS 9.4.1.3: interest is consideration only for the time "
             "value of money and the credit risk on the principal amount "
             "outstanding.")
    sppi_no_leverage = fields.Boolean(
        string="No Leverage Features", tracking=True,
        help="IFRS 9.B4.1.9: no contractual leverage that amplifies the "
             "variability of the contractual cash flows.")
    sppi_no_contingent_returns = fields.Boolean(
        string="No Contingent Returns", tracking=True,
        help="No returns linked to equity prices, commodity prices or other "
             "variables unrelated to a basic lending arrangement.")
    sppi_pass = fields.Boolean(
        compute='_compute_sppi_pass', store=True, precompute=True,
        string="SPPI Test Passed",
        help="A debt instrument whose four questionnaire answers all hold "
             "passes the solely-payments-of-principal-and-interest test. "
             "Equity instruments and derivatives fail by nature.")
    business_model = fields.Selection(
        [('hold_to_collect', "Hold to collect"),
         ('hold_collect_sell', "Hold to collect and sell"),
         ('other', "Other / trading")],
        tracking=True,
        help="IFRS 9.4.1.1(a): the business model within which the "
             "instrument is held.")
    held_for_trading = fields.Boolean(
        tracking=True,
        help="Held for trading (IFRS 9 Appendix A). Blocks the FVOCI equity "
             "election and forces fair value through profit or loss.")
    floating_rate = fields.Boolean(
        string="Floating rate", tracking=True,
        help="The instrument bears a floating (variable) interest rate. "
             "Flagged items feed the computed IFRS 7.40 interest-rate "
             "sensitivity in the disclosures module at their fair value; "
             "the impact of a FVOCI-debt instrument routes to OCI, "
             "everything else to profit or loss. Purely informational "
             "here: it changes no measurement or posting.")
    fvoci_equity_election = fields.Boolean(
        string="FVOCI Equity Election", tracking=True,
        help="IFRS 9.5.7.5: irrevocable election at initial recognition to "
             "present fair-value changes of a non-trading equity instrument "
             "in OCI. Cannot be changed once a journal entry has been "
             "posted under the elected treatment.")
    fvtpl_designation = fields.Boolean(
        string="FVTPL Designation", tracking=True,
        help="IFRS 9.4.1.5 designation, at initial recognition, of an "
             "otherwise amortised-cost or FVOCI debt asset at fair value "
             "through profit or loss to eliminate or significantly reduce "
             "an accounting mismatch.")
    fvtpl_designation_basis = fields.Selection(
        [('accounting_mismatch', "Eliminates/reduces accounting mismatch")],
        tracking=True,
        help="The IFRS 9.4.1.5 eligibility basis for the designation.")
    fvtpl_designation_reason = fields.Text(
        tracking=True,
        help="Contemporaneous description of the accounting mismatch and "
             "why FVTPL eliminates or significantly reduces it.")
    ifrs9_classification = fields.Selection(
        [('amortised_cost', "Amortised cost"),
         ('fvoci_debt', "FVOCI - debt (recycles to P&L)"),
         ('fvoci_equity', "FVOCI - equity election (never recycles)"),
         ('fvtpl', "FVTPL - fair value through profit or loss")],
        compute='_compute_ifrs9_classification', store=True, precompute=True,
        tracking=True, string="IFRS 9 Classification (derived)",
        help="Derived by the IFRS 9.4.1 engine from the instrument type, "
             "the SPPI test and the business model; never set by hand. "
             "Empty when no instrument type is captured (legacy records and "
             "non-financial items).")

    level = fields.Selection(
        [('1', "Level 1 - quoted prices"),
         ('2', "Level 2 - observable inputs"),
         ('3', "Level 3 - unobservable inputs")],
        default='1', required=True, tracking=True,
        help="Fair-value hierarchy level by input observability "
             "(IFRS 13.72-90).")
    proposed_level = fields.Selection(
        [('1', "Level 1 - quoted prices"),
         ('2', "Level 2 - observable inputs"),
         ('3', "Level 3 - unobservable inputs")],
        string="Transfer To Level",
        help="New hierarchy level for the controlled IFRS 13 transfer "
             "action. Direct level changes are frozen after measurement.")
    level_transfer_date = fields.Date(
        string="Level Transfer Date",
        help="Date on which the change in input observability occurred.")
    level_transfer_reason = fields.Text(
        help="Reason for the hierarchy transfer required by IFRS 13.93(c).")
    level_transfer_ids = fields.One2many(
        'eh.fair.value.level.transfer', 'item_id',
        string="Hierarchy Transfer History", copy=False)
    valuation_technique = fields.Selection(
        [('market', "Market approach"), ('income', "Income approach"),
         ('cost', "Cost approach")],
        default='market', required=True)
    unobservable_inputs = fields.Text(
        help="Significant unobservable inputs and key assumptions, for "
             "Level 3 disclosure (IFRS 13.91-93).")

    measurement_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)
    prior_carrying = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Current carrying amount before this remeasurement.")
    fair_value = fields.Monetary(
        currency_field='currency_id', required=True, tracking=True,
        help="Fair value at the measurement date.")
    remeasurement = fields.Monetary(
        compute='_compute_remeasurement', store=True,
        currency_field='currency_id',
        help="Fair value less prior carrying amount.")
    prior_company_carrying = fields.Monetary(
        currency_field='company_currency_id', tracking=True,
        string="Prior Carrying (Company Currency)",
        help="Company-currency carrying amount immediately before this "
             "measurement. A foreign-currency position needs this historical "
             "book amount so IFRS 9.B5.7.2 can split exchange movement from "
             "the other FVOCI-debt fair-value movement.")
    company_fair_value = fields.Monetary(
        compute='_compute_company_measurement',
        currency_field='company_currency_id',
        string="Fair Value (Company Currency)")
    company_remeasurement = fields.Monetary(
        compute='_compute_company_measurement',
        currency_field='company_currency_id',
        string="Remeasurement (Company Currency)")
    fx_remeasurement = fields.Monetary(
        compute='_compute_company_measurement',
        currency_field='company_currency_id',
        string="FX Component (P&L)",
        help="For FVOCI debt in a foreign currency, the exchange component "
             "recognised in profit or loss under IFRS 9.B5.7.2.")
    non_fx_remeasurement = fields.Monetary(
        compute='_compute_company_measurement',
        currency_field='company_currency_id',
        string="Non-FX Fair Value Component")
    routing = fields.Selection(
        [('pl', "Profit or loss"), ('oci', "OCI")],
        compute='_compute_routing', store=True, readonly=False,
        precompute=True, required=True,
        help="Where the fair-value change is recognised. Derived from the "
             "IFRS 9 classification whenever the engine is active (FVTPL to "
             "profit or loss, FVOCI to OCI); manual only for non-financial "
             "items and legacy records without an instrument type.")
    fvoci_classification = fields.Selection(
        [('fvtpl', "FVTPL - fair value through profit or loss"),
         ('fvoci_debt', "FVOCI - debt (recycles to P&L)"),
         ('fvoci_equity', "FVOCI - equity election (never recycles)")],
        string="IFRS 9 Classification (legacy)", tracking=True,
        help="Manual classification label kept for records created before "
             "the IFRS 9.4.1 engine. Ignored as soon as an instrument type "
             "is set: the derived classification then governs and a "
             "conflicting legacy label is refused.")
    recycled = fields.Boolean(
        readonly=True, copy=False,
        help="The accumulated OCI reserve has been recycled or transferred "
             "on derecognition.")
    disposal_proceeds = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Consideration received (asset) or paid (liability) on "
             "derecognition.")
    disposal_date = fields.Date(
        string="Derecognition / Accounting Date", copy=False, tracking=True,
        help="Explicit accounting date for the disposal or settlement. "
             "Derecognise will not infer today's date, and will refuse a "
             "date that does not match the final fair-value measurement or "
             "that precedes posted measurement evidence.")
    derecognition_date = fields.Date(
        readonly=True, copy=False, tracking=True,
        help="Accounting date actually used by the sealed derecognition "
             "and reserve-settlement entries.")
    reversal_date = fields.Date(
        copy=False, tracking=True,
        help="Explicit open accounting date for reversing the latest "
             "remeasurement entry.")
    reversal_reason = fields.Text(
        copy=False, tracking=True,
        help="Audit reason for the controlled correction of the latest "
             "remeasurement.")

    # ---- accounts ----
    balance_sheet_account_id = fields.Many2one(
        'account.account', string="Balance Sheet Account", tracking=True,
        check_company=True,
        domain="[('internal_group', 'in', "
               "['asset', 'liability', 'equity'])]",
        help="Statement-of-financial-position account carrying the item. "
             "Must be an asset, liability or equity account, never a "
             "profit-or-loss account.")
    gain_loss_account_id = fields.Many2one(
        'account.account', string="Gain / Loss Account", tracking=True,
        check_company=True,
        domain="[('account_type', 'in', ['income_other', 'expense'])]")
    oci_account_id = fields.Many2one(
        'account.account', string="OCI / Equity Account", tracking=True,
        check_company=True,
        domain="[('account_type', '=', 'equity')]")
    settlement_account_id = fields.Many2one(
        'account.account', string="Settlement Account", tracking=True,
        check_company=True,
        domain="[('internal_group', '=', 'asset')]",
        help="Cash or clearing account taking the proceeds leg of the "
             "derecognition entry.")
    retained_earnings_account_id = fields.Many2one(
        'account.account', string="Retained Earnings Account", tracking=True,
        check_company=True,
        domain="[('account_type', 'in', ['equity', 'equity_unaffected'])]",
        help="Equity destination for the FVOCI equity-election reserve "
             "transfer on derecognition. The transfer stays within equity "
             "and never passes through profit or loss (IFRS 9.B5.7.1).")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        check_company=True,
        domain="[('type', '=', 'general'), ('company_id', '=', company_id)]")

    move_ids = fields.One2many('account.move', 'eh_fair_value_item_id')
    move_count = fields.Integer(compute='_compute_move_count')
    oci_reserve_balance = fields.Monetary(
        compute='_compute_oci_reserve_balance',
        currency_field='company_currency_id',
        help="Net movement posted to the OCI / equity account for this item "
             "(debit less credit), the reserve available to recycle on "
             "derecognition.")

    # ---- Level 3 reconciliation (IFRS 13.93(e)) ----
    rollforward_ids = fields.One2many(
        'eh.fair.value.rollforward', 'item_id',
        string="Level 3 Reconciliation")
    ties_to_fair_value = fields.Boolean(
        compute='_compute_ties_to_fair_value',
        help="The closing balance of the latest Level 3 reconciliation "
             "period equals the item's fair value (within rounding).")

    # ---- Level 3 sensitivity analysis (IFRS 13.93(h)) ----
    sensitivity_ids = fields.One2many(
        'eh.fair.value.sensitivity', 'item_id',
        string="Sensitivity Analysis")

    notes = fields.Text()

    @api.depends('fair_value', 'prior_carrying')
    def _compute_remeasurement(self):
        for item in self:
            item.remeasurement = item.fair_value - item.prior_carrying

    def _company_measurement_components(self):
        """Return company fair value, total movement, FX and non-FX parts."""
        self.ensure_one()
        company_currency = self.company_currency_id
        if self.currency_id == company_currency:
            company_fair = company_currency.round(self.fair_value)
            company_prior = company_currency.round(self.prior_carrying)
            return (
                company_fair,
                company_currency.round(company_fair - company_prior),
                0.0,
                company_currency.round(company_fair - company_prior),
            )
        date = self.measurement_date or fields.Date.context_today(self)
        company_fair = company_currency.round(self.currency_id._convert(
            self.fair_value, company_currency, self.company_id, date,
            round=False,
        ))
        prior_at_current_rate = company_currency.round(
            self.currency_id._convert(
                self.prior_carrying, company_currency, self.company_id, date,
                round=False,
            )
        )
        company_prior = company_currency.round(
            self.prior_company_carrying
        )
        fx_change = company_currency.round(
            prior_at_current_rate - company_prior
        )
        total = company_currency.round(company_fair - company_prior)
        # Make the disclosed split add exactly to the posting amount after
        # company-currency rounding.
        non_fx_change = company_currency.round(total - fx_change)
        return company_fair, total, fx_change, non_fx_change

    @api.depends(
        'currency_id', 'company_currency_id', 'company_id',
        'measurement_date', 'fair_value', 'prior_carrying',
        'prior_company_carrying', 'ifrs9_classification',
        'fvoci_classification',
    )
    def _compute_company_measurement(self):
        for item in self:
            company_fair, total, fx_change, non_fx_change = (
                item._company_measurement_components()
            )
            is_fvoci_debt = (
                item._effective_classification() == 'fvoci_debt'
                and item.currency_id != item.company_currency_id
            )
            item.company_fair_value = company_fair
            item.company_remeasurement = total
            item.fx_remeasurement = fx_change if is_fvoci_debt else 0.0
            item.non_fx_remeasurement = (
                non_fx_change if is_fvoci_debt else total
            )

    @api.depends('instrument_type', 'sppi_fixed_dates', 'sppi_interest_only',
                 'sppi_no_leverage', 'sppi_no_contingent_returns')
    def _compute_sppi_pass(self):
        # IFRS 9.4.1.2(b)/.4.1.3: only a plain debt instrument can have cash
        # flows that are solely payments of principal and interest. An equity
        # instrument or a derivative fails by nature, whatever the
        # questionnaire answers say.
        for item in self:
            item.sppi_pass = bool(
                item.instrument_type == 'debt'
                and item.sppi_fixed_dates
                and item.sppi_interest_only
                and item.sppi_no_leverage
                and item.sppi_no_contingent_returns)

    @api.depends('nature', 'instrument_type', 'sppi_pass', 'business_model',
                 'fvoci_equity_election', 'held_for_trading',
                 'fvtpl_designation')
    def _compute_ifrs9_classification(self):
        # IFRS 9.4.1.1-4.1.4 decision tree; FVTPL is the residual category.
        # No classification is derived without an instrument type, so records
        # created before the engine existed keep their manual behaviour.
        for item in self:
            if (item.nature not in ('financial_asset', 'financial_liability')
                    or not item.instrument_type):
                item.ifrs9_classification = False
            elif item.instrument_type == 'derivative':
                item.ifrs9_classification = 'fvtpl'
            elif item.instrument_type == 'equity':
                item.ifrs9_classification = (
                    'fvoci_equity'
                    if item.fvoci_equity_election
                    and not item.held_for_trading else 'fvtpl')
            elif item.nature == 'financial_liability':
                # IFRS 9.4.2.1: a financial liability sits at amortised cost
                # unless held for trading (own-credit FVTPL designation is
                # out of scope for this model).
                item.ifrs9_classification = (
                    'fvtpl' if item.held_for_trading else 'amortised_cost')
            elif item.fvtpl_designation:
                item.ifrs9_classification = 'fvtpl'
            elif (item.sppi_pass and not item.held_for_trading
                    and item.business_model == 'hold_to_collect'):
                item.ifrs9_classification = 'amortised_cost'
            elif (item.sppi_pass and not item.held_for_trading
                    and item.business_model == 'hold_collect_sell'):
                item.ifrs9_classification = 'fvoci_debt'
            else:
                item.ifrs9_classification = 'fvtpl'

    @api.depends('ifrs9_classification')
    def _compute_routing(self):
        for item in self:
            if item.ifrs9_classification in ('fvoci_debt', 'fvoci_equity'):
                item.routing = 'oci'
            elif item.ifrs9_classification:
                item.routing = 'pl'
            else:
                # Engine off: keep the manually captured routing.
                item.routing = item.routing or 'pl'

    def _compute_move_count(self):
        for item in self:
            item.move_count = len(item.move_ids)

    @api.depends('move_ids.line_ids.debit', 'move_ids.line_ids.credit',
                 'move_ids.state', 'oci_account_id')
    def _compute_oci_reserve_balance(self):
        for item in self:
            balance = 0.0
            if item.oci_account_id:
                for line in item.move_ids.line_ids:
                    if (line.parent_state == 'posted'
                            and line.account_id == item.oci_account_id):
                        balance += line.debit - line.credit
            item.oci_reserve_balance = balance

    @api.depends('rollforward_ids.closing_balance', 'rollforward_ids.period_end',
                 'company_fair_value', 'company_currency_id')
    def _compute_ties_to_fair_value(self):
        for item in self:
            latest = item.rollforward_ids.sorted(
                lambda r: (r.period_end or fields.Date.today(), r.id))[-1:]
            if latest:
                item.ties_to_fair_value = item.company_currency_id.is_zero(
                    latest.closing_balance - item.company_fair_value)
            else:
                item.ties_to_fair_value = False

    @api.constrains('balance_sheet_account_id')
    def _check_balance_sheet_account(self):
        # IFRS 9/13: the position leg of a fair-value remeasurement must land
        # on a statement-of-financial-position account (asset, liability or
        # equity), never on a profit-or-loss account. A P&L balance-sheet leg
        # would double-count the remeasurement and misstate the carrying
        # amount, so reject it outright rather than silently posting it.
        for item in self:
            account = item.balance_sheet_account_id
            if account and account.internal_group in ('income', 'expense'):
                raise UserError(_(
                    "The balance sheet account on %s must be an asset, "
                    "liability or equity account. %s is a profit-or-loss "
                    "account and cannot carry a fair-value remeasurement.",
                    item.display_name, account.display_name))

    @api.constrains('nature', 'instrument_type', 'fvoci_equity_election',
                    'held_for_trading', 'fvoci_classification',
                    'fvtpl_designation', 'fvtpl_designation_basis',
                    'fvtpl_designation_reason')
    def _check_ifrs9_combos(self):
        for item in self:
            if (item.instrument_type
                    and item.nature not in ('financial_asset',
                                            'financial_liability')):
                raise ValidationError(_(
                    "%s: an instrument type only applies to a financial "
                    "asset or financial liability; a non-financial item is "
                    "measured under its own standard, not IFRS 9.",
                    item.display_name))
            if (item.nature == 'financial_liability'
                    and item.instrument_type == 'equity'):
                raise ValidationError(_(
                    "%s: a holding of an equity instrument is a financial "
                    "asset; a financial liability cannot be classified as "
                    "an equity instrument (and can never take the FVOCI "
                    "equity election).", item.display_name))
            if item.fvoci_equity_election:
                if item.instrument_type != 'equity':
                    raise ValidationError(_(
                        "%s: the FVOCI election (IFRS 9.5.7.5) is available "
                        "only for an equity instrument, never for a debt "
                        "instrument or a derivative.", item.display_name))
                if item.held_for_trading:
                    raise ValidationError(_(
                        "%s: an equity instrument held for trading cannot "
                        "take the FVOCI election (IFRS 9.5.7.5).",
                        item.display_name))
            if (item.instrument_type == 'derivative'
                    and item.fvoci_classification
                    and item.fvoci_classification != 'fvtpl'):
                raise ValidationError(_(
                    "%s: a derivative is always at fair value through "
                    "profit or loss; it cannot carry an FVOCI label.",
                    item.display_name))
            if (item.fvoci_classification in ('fvoci_debt', 'fvoci_equity')
                    and item.nature != 'financial_asset'):
                raise ValidationError(_(
                    "%s: a legacy FVOCI label is available only for a "
                    "financial asset; liabilities and non-financial items "
                    "cannot be labelled FVOCI under IFRS 9.",
                    item.display_name))
            designation_metadata = (
                item.fvtpl_designation_basis
                or item.fvtpl_designation_reason
            )
            if designation_metadata and not item.fvtpl_designation:
                raise ValidationError(_(
                    "%s: FVTPL designation evidence cannot be retained when "
                    "the designation is not selected.", item.display_name))
            if item.fvtpl_designation:
                if (item.nature != 'financial_asset'
                        or item.instrument_type != 'debt'
                        or item.held_for_trading):
                    raise ValidationError(_(
                        "%s: IFRS 9.4.1.5 designation applies only to a debt "
                        "financial asset that is not already held for "
                        "trading.", item.display_name))
                if (item.fvtpl_designation_basis != 'accounting_mismatch'
                        or not (item.fvtpl_designation_reason or '').strip()):
                    raise ValidationError(_(
                        "%s: document the accounting-mismatch basis and a "
                        "contemporaneous reason for the IFRS 9.4.1.5 FVTPL "
                        "designation.", item.display_name))

    @api.constrains(
        'currency_id', 'company_id', 'prior_carrying',
        'prior_company_carrying',
    )
    def _check_foreign_carrying_basis(self):
        for item in self:
            if (
                item.currency_id != item.company_currency_id
                and not item.currency_id.is_zero(item.prior_carrying)
                and item.company_currency_id.is_zero(
                    item.prior_company_carrying
                )
            ):
                raise ValidationError(_(
                    "%s: enter the prior carrying amount in company currency "
                    "for this foreign-currency position so the exchange and "
                    "non-exchange movements can be separated.",
                    item.display_name,
                ))

    @api.constrains('routing', 'ifrs9_classification')
    def _check_routing_derived(self):
        # The recognition routing is a consequence of the IFRS 9
        # classification, not a choice: FVTPL to profit or loss, FVOCI to
        # OCI. The compute derives it; this constraint catches any raw write
        # that tries to detach the two.
        mapping = {'amortised_cost': 'pl', 'fvtpl': 'pl',
                   'fvoci_debt': 'oci', 'fvoci_equity': 'oci'}
        for item in self:
            expected = mapping.get(item.ifrs9_classification)
            if expected and item.routing != expected:
                raise ValidationError(_(
                    "%s: routing is derived from the IFRS 9 classification "
                    "(%s requires %s) and cannot be overridden.",
                    item.display_name, item.ifrs9_classification, expected))

    @api.constrains('ifrs9_classification', 'fvoci_classification')
    def _check_legacy_label_consistent(self):
        for item in self:
            if (item.ifrs9_classification and item.fvoci_classification
                    and item.fvoci_classification
                    != item.ifrs9_classification):
                raise ValidationError(_(
                    "%s: the derived IFRS 9 classification is %s; the "
                    "manual legacy label %s conflicts with it. Clear the "
                    "legacy label or fix the questionnaire.",
                    item.display_name, item.ifrs9_classification,
                    item.fvoci_classification))

    @api.constrains('retained_earnings_account_id')
    def _check_retained_earnings_account(self):
        # The equity-election transfer must stay within equity; a P&L
        # destination would smuggle the reserve into profit or loss.
        for item in self:
            account = item.retained_earnings_account_id
            if account and account.account_type not in (
                'equity', 'equity_unaffected',
            ):
                raise ValidationError(_(
                    "%s: the retained earnings account must be an equity "
                    "account; %s is a profit-or-loss account.",
                    item.display_name, account.display_name))

    # Fields locked once the item has been measured and posted, so a
    # recorded remeasurement cannot be retro-edited out from under its
    # journal entry. The classification inputs are included: flipping them
    # would silently re-derive the routing under a posted entry.
    _FROZEN_AFTER_MEASURED = (
        'fair_value', 'prior_carrying', 'prior_company_carrying', 'routing',
        'balance_sheet_account_id', 'gain_loss_account_id',
        'oci_account_id', 'nature', 'instrument_type', 'business_model',
        'sppi_fixed_dates', 'sppi_interest_only', 'sppi_no_leverage',
        'sppi_no_contingent_returns', 'held_for_trading',
        'fvoci_equity_election', 'fvtpl_designation',
        'fvtpl_designation_basis', 'fvtpl_designation_reason',
        'fvoci_classification', 'level',
        'sppi_pass', 'ifrs9_classification', 'remeasurement',
        'company_id', 'currency_id', 'measurement_date', 'journal_id',
    )
    # Reset opens only next-period measurement inputs. Instrument identity,
    # classification and ledger routing remain bound to posted history.
    _FROZEN_AFTER_HISTORY = (
        'name', 'nature', 'instrument_type', 'business_model',
        'sppi_fixed_dates', 'sppi_interest_only', 'sppi_no_leverage',
        'sppi_no_contingent_returns', 'held_for_trading',
        'fvoci_equity_election', 'fvtpl_designation',
        'fvtpl_designation_basis', 'fvtpl_designation_reason',
        'fvoci_classification', 'company_id', 'currency_id', 'routing',
        'balance_sheet_account_id', 'gain_loss_account_id',
        'oci_account_id', 'journal_id', 'prior_carrying',
        'prior_company_carrying', 'level',
    )
    # A derecognised item is a closed position: only narrative fields stay
    # writable (chatter plumbing is whitelisted by prefix in write()).
    _WRITABLE_AFTER_DERECOGNISED = ('notes', 'unobservable_inputs')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            company = self.env['res.company'].browse(
                vals.get('company_id')
            ) or self.env.company
            vals.setdefault('currency_id', company.currency_id.id)
            if (
                vals.get('currency_id') == company.currency_id.id
                and 'prior_company_carrying' not in vals
            ):
                vals['prior_company_carrying'] = vals.get(
                    'prior_carrying', 0.0,
                )
            if vals.get('instrument_type') and 'routing' in vals:
                raise AccessError(_(
                    "Routing is derived by the IFRS 9 classification engine "
                    "when an instrument type is set."
                ))
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.fair.value.item') or '/'
        return super().create(vals_list)

    def write(self, vals):
        self._eh_lock_transitions()
        internal_item_write = (
            self.env.context.get(_FV_ITEM_INTERNAL) is _FV_ITEM_CAPABILITY
        )
        if 'routing' in vals and not self.env.su:
            if vals.get('instrument_type') or any(
                item.instrument_type for item in self
            ):
                raise AccessError(_(
                    "Routing is derived by the IFRS 9 classification engine "
                    "and cannot be written directly."
                ))
        if 'fvoci_equity_election' in vals:
            # The FVOCI equity election is irrevocable at initial recognition
            # (IFRS 9.5.7.5): once any entry has been posted under the
            # elected treatment the flag can never flip, in any state.
            for item in self:
                if (bool(vals['fvoci_equity_election'])
                        != item.fvoci_equity_election
                        and any(m.state == 'posted' for m in item.move_ids)):
                    raise UserError(_(
                        "The FVOCI equity election on %s is irrevocable: a "
                        "journal entry has already been posted under the "
                        "elected treatment.", item.display_name))
        blocked = [
            f for f in vals
            if f not in self._WRITABLE_AFTER_DERECOGNISED
            and not f.startswith('message_') and not f.startswith('activity_')
        ]
        if blocked:
            for item in self:
                if item.state == 'derecognised':
                    raise UserError(_(
                        "Item %s is derecognised and closed; %s cannot be "
                        "changed.", item.display_name, ', '.join(blocked)))
        history_frozen = [
            field_name for field_name in self._FROZEN_AFTER_HISTORY
            if field_name in vals
        ]
        if internal_item_write and self.env.su:
            history_frozen = [
                field_name for field_name in history_frozen
                if field_name not in {
                    'prior_carrying', 'prior_company_carrying', 'level',
                }
            ]
        if (
            history_frozen == ['routing']
            and all(item.routing == vals.get('routing') for item in self)
        ):
            # Stored-compute refreshes assign routing on every remeasurement.
            # An idempotent assignment preserves the historical choice; only
            # an actual post-history routing change is forbidden below.
            history_frozen = []
        if history_frozen:
            for item in self.filtered('move_ids'):
                raise UserError(_(
                    "Item %s has posted history; instrument identity and "
                    "ledger routing (%s) cannot be changed. Reset opens only "
                    "the next fair-value measurement inputs.",
                    item.display_name, ', '.join(history_frozen),
                ))
        frozen = [f for f in self._FROZEN_AFTER_MEASURED if f in vals]
        idempotent_outputs = {
            field_name for field_name in (
                'sppi_pass', 'ifrs9_classification', 'remeasurement',
                'routing',
            )
            if field_name in vals and all(
                item[field_name] == vals[field_name] for item in self
            )
        }
        frozen = [
            field_name for field_name in frozen
            if field_name not in idempotent_outputs
        ]
        if internal_item_write and self.env.su:
            frozen = [
                field_name for field_name in frozen
                if field_name not in {
                    'prior_carrying', 'prior_company_carrying', 'level',
                }
            ]
        if frozen:
            for item in self:
                # action_remeasure rolls prior_carrying forward as it sets the
                # state to measured in the same write; allow that transition
                # but block edits once the item is already measured.
                if item.state == 'measured' and vals.get('state') != 'measured':
                    raise UserError(_(
                        "Item %s is measured and posted; %s cannot be "
                        "changed. Cancel and create a new measurement "
                        "instead.",
                        item.display_name, ', '.join(frozen)))
        return super().write(vals)

    def unlink(self):
        self._eh_lock_transitions()
        # An item that has posted a remeasurement carries the posting-move
        # link; deleting the master would orphan a posted GL entry. Block it
        # once any move exists (measured, or later reset-to-draft / cancelled
        # after a measurement). Items that never posted have no move and stay
        # deletable.
        posted = self.filtered(lambda i: i.move_ids)
        if posted:
            raise UserError(_(
                "A fair-value item with a posted remeasurement cannot be "
                "deleted; its journal entry would be orphaned. Cancel it "
                "instead."))
        return super().unlink()

    # ---- actions ----

    def _eh_lock_transitions(self):
        """Authorize and serialize item lifecycle/accounting actions."""
        self._eh_check_access('write')
        ids = sorted(self.ids)
        if not ids:
            return self
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_fair_value_item WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            (tuple(ids),),
        )
        self.invalidate_recordset()
        self._check_company()
        return self

    def _remeasurement_pair(
        self, counter_account, company_change, foreign_change, component,
    ):
        """Build one balanced company/transaction-currency movement pair."""
        self.ensure_one()
        company_change = self.company_currency_id.round(company_change)
        if self.company_currency_id.is_zero(company_change):
            return []
        is_liability = self.nature == 'financial_liability'
        balance_signed = -company_change if is_liability else company_change
        foreign_signed = -foreign_change if is_liability else foreign_change
        if is_liability:
            balance_label = _(
                "%s liability movement %s", component, self.name,
            )
            counter_label = _(
                "%s fair value %s %s",
                component,
                "loss" if company_change > 0 else "gain",
                self.name,
            )
        else:
            balance_label = _(
                "%s asset movement %s", component, self.name,
            )
            counter_label = _(
                "%s fair value %s %s",
                component,
                "gain" if company_change > 0 else "loss",
                self.name,
            )

        def _leg(account, signed_balance, label, amount_currency=0.0):
            values = {
                'account': account,
                'debit': max(signed_balance, 0.0),
                'credit': max(-signed_balance, 0.0),
                'label': label,
            }
            if (
                self.currency_id != self.company_currency_id
                and not self.currency_id.is_zero(amount_currency)
            ):
                values.update({
                    'currency_id': self.currency_id.id,
                    'amount_currency': amount_currency,
                })
            return values

        return [
            _leg(
                self.balance_sheet_account_id,
                balance_signed,
                balance_label,
                foreign_signed,
            ),
            _leg(
                counter_account,
                -balance_signed,
                counter_label,
                -foreign_signed,
            ),
        ]

    def action_remeasure(self):
        self.ensure_one()
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can post a fair-value "
                "remeasurement."))
        self._eh_lock_transitions()
        # Caller ACL, manager role, record rules and company integrity are now
        # proven.  Stored classification outputs are guarded server fields, so
        # recompute them only inside this sanctioned action boundary.
        self = self._eh_workflow_action()
        # Recompute every stored decision/output used by the posting gate
        # after the row lock.  A stale cache or earlier raw SQL change must
        # not determine classification, routing, or the amount posted.
        self._compute_sppi_pass()
        self._compute_ifrs9_classification()
        self._compute_routing()
        self._compute_remeasurement()
        self.flush_recordset([
            'sppi_pass', 'ifrs9_classification', 'routing', 'remeasurement',
        ])
        self._check_company()
        if self.state == 'cancelled':
            raise UserError(_("Cannot remeasure a cancelled item."))
        if self.state == 'derecognised':
            raise UserError(_(
                "Item %s is derecognised; a closed position cannot be "
                "remeasured.", self.display_name))
        if self._effective_classification() == 'amortised_cost':
            raise UserError(_(
                "%s is classified at amortised cost (SPPI passed, "
                "hold-to-collect business model); it is carried at "
                "amortised cost and is not remeasured to fair value "
                "(IFRS 9.4.1.2).", self.display_name))
        company_fair, total, fx_change, non_fx_change = (
            self._company_measurement_components()
        )
        classification = self._effective_classification()
        is_foreign = self.currency_id != self.company_currency_id
        foreign_change = self.currency_id.round(
            self.fair_value - self.prior_carrying
        )
        components = []
        if classification == 'fvoci_debt' and is_foreign:
            components.extend([
                (
                    self.gain_loss_account_id,
                    fx_change,
                    0.0,
                    _("FX component"),
                ),
                (
                    self.oci_account_id,
                    non_fx_change,
                    foreign_change,
                    _("Non-FX component"),
                ),
            ])
        else:
            counter = (
                self.oci_account_id if self.routing == 'oci'
                else self.gain_loss_account_id
            )
            components.append((
                counter,
                total,
                foreign_change if is_foreign else 0.0,
                _("Fair value"),
            ))
        non_zero_components = [
            component for component in components
            if not self.company_currency_id.is_zero(component[1])
        ]
        if not non_zero_components:
            if self.state == 'measured':
                raise UserError(_(
                    "Fair value equals the carrying amount; nothing to post."
                ))
            # A genuine zero-change observation is still a completed fair-
            # value measurement. It can therefore proceed to derecognition
            # without manufacturing a zero journal entry.
            self.with_context(**{
                _FV_ITEM_INTERNAL: _FV_ITEM_CAPABILITY,
            }).write({
                'state': 'measured',
                'prior_carrying': self.fair_value,
                'prior_company_carrying': company_fair,
            })
            return True
        self._validate_accounts()
        if any(not account for account, *_rest in non_zero_components):
            raise UserError(_(
                "Configure every account required by the fair-value and FX "
                "routing on item %s first.", self.display_name,
            ))
        lines = []
        for account, company_change, amount_currency, component in (
            non_zero_components
        ):
            lines.extend(self._remeasurement_pair(
                account,
                company_change,
                amount_currency,
                component,
            ))
        self._post_move(
            lines,
            role='remeasurement',
            prior_company_carrying=(
                self.prior_carrying
                if not is_foreign else self.prior_company_carrying
            ),
            posted_company_fair_value=company_fair,
        )
        self.with_context(**{
            _FV_ITEM_INTERNAL: _FV_ITEM_CAPABILITY,
        }).write({
            'state': 'measured',
            'prior_carrying': self.fair_value,
            'prior_company_carrying': company_fair,
        })
        return True

    def action_cancel(self):
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can cancel a fair-value "
                "item."))
        self._eh_lock_transitions()
        self = self._eh_workflow_action()
        for item in self:
            if item.state == 'cancelled':
                raise UserError(_(
                    "Item %s is already cancelled.", item.display_name))
            if item.state == 'derecognised':
                raise UserError(_(
                    "Item %s is derecognised; a closed position cannot be "
                    "cancelled.", item.display_name))
            # Cancellation is an administrative way to retire an unused
            # master record; it is not an accounting reversal.  Once the item
            # has generated any journal entry, hiding it behind ``cancelled``
            # would leave the posted carrying-value movement in the ledger
            # while downstream reports legitimately exclude cancelled items.
            # The item row is locked above, which is the same lock taken by
            # action_remeasure/action_derecognise, so this re-read also closes
            # the cancel-vs-post race: whichever transition wins is observed by
            # the loser before it changes state.
            if item.move_ids:
                raise UserError(_(
                    "Item %s has fair-value journal-entry history and cannot "
                    "be cancelled. Keep it active and use the controlled "
                    "remeasurement or derecognition workflow so the general "
                    "ledger remains visible and auditable.",
                    item.display_name))
            item.state = 'cancelled'
        return True

    def action_reset_to_draft(self):
        """Reopen a measured item so a new fair value can be recorded.

        The prior remeasurement stays posted; resetting only lifts the write
        freeze so the next fair value (and its incremental remeasurement) can
        be entered from the rolled-forward carrying amount.
        """
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can reopen a fair-value "
                "item."))
        self._eh_lock_transitions()
        self = self._eh_workflow_action()
        self.filtered(lambda i: i.state == 'measured').write({
            'state': 'draft',
            # A prepared disposal date must not silently survive a reopen and
            # later be reused after a new measurement cycle.
            'disposal_date': False,
        })
        return True

    def action_reverse_remeasurement(self):
        """Reverse the latest posted remeasurement and reopen correction."""
        self.ensure_one()
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can reverse a fair-value "
                "remeasurement."
            ))
        self._eh_lock_transitions()
        self = self._eh_workflow_action()
        if self.state != 'measured':
            raise UserError(_(
                "Only a measured item can reverse its latest remeasurement."
            ))
        reversal_reason = (self.reversal_reason or '').strip()
        if not reversal_reason:
            raise UserError(_(
                "Document the correction reason before reversing the latest "
                "remeasurement."
            ))
        reversal_date = fields.Date.to_date(self.reversal_date)
        if not reversal_date:
            raise UserError(_(
                "Enter an explicit reversal accounting date first."
            ))
        if reversal_date > fields.Date.context_today(self):
            raise UserError(_(
                "A fair-value reversal cannot use a future date."
            ))
        existing_reversed_ids = set(self.env['account.move'].sudo().search([
            ('reversed_entry_id', 'in', self.move_ids.ids),
        ]).mapped('reversed_entry_id').ids)
        candidates = self.move_ids.filtered(lambda move: (
            move.state == 'posted'
            and move.eh_fair_value_move_role in (False, 'remeasurement')
            and move.id not in existing_reversed_ids
        )).sorted(lambda move: (move.date, move.id), reverse=True)
        original = candidates[:1]
        if not original:
            raise UserError(_(
                "No unreversed remeasurement entry exists for %s.",
                self.display_name,
            ))
        if reversal_date < original.date:
            raise UserError(_(
                "The reversal date cannot precede the original measurement "
                "entry date %s.", original.date,
            ))
        if (
            original.eh_fair_value_item_id != self
            or original.company_id != self.company_id
            or original.move_type != 'entry'
            or not original.eh_sealed
            or original.eh_legacy_unverified_seal
        ):
            raise UserError(_(
                "The latest remeasurement failed its verified sealed "
                "provenance check and cannot be reversed."
            ))
        lock_probe = self.env['account.move'].new({
            'move_type': 'entry',
            'company_id': self.company_id.id,
            'journal_id': original.journal_id.id,
            'date': reversal_date,
        })
        if lock_probe._get_violated_lock_dates(reversal_date, False):
            raise UserError(_(
                "The requested reversal date is locked. Choose an explicit "
                "open accounting date; the engine will not let Odoo shift "
                "the correction silently."
            ))
        if original.eh_fair_value_snapshot_complete:
            restored_foreign = original.eh_fair_value_prior_carrying
            restored_company = (
                original.eh_fair_value_prior_company_carrying
            )
        else:
            position_balance = sum(
                line.debit - line.credit
                for line in original.line_ids
                if line.account_id == self.balance_sheet_account_id
            )
            company_change = (
                -position_balance
                if self.nature == 'financial_liability'
                else position_balance
            )
            current_company = (
                self.prior_company_carrying
                if self.currency_id != self.company_currency_id
                else self.prior_carrying
            )
            restored_company = self.company_currency_id.round(
                current_company - company_change
            )
            restored_foreign = restored_company
        reversal = original._eh_reverse_with_verified_capability([{
            'date': reversal_date,
            'journal_id': original.journal_id.id,
            'ref': _(
                "Fair-value correction %(item)s: %(reason)s",
                item=self.name,
                reason=reversal_reason,
            ),
        }], cancel=False)
        reversal.sudo().with_context(**{
            _FV_MOVE_LINK_INTERNAL: _FV_MOVE_LINK_CAPABILITY,
        })._eh_write_sealed_metadata({
            'eh_fair_value_item_id': self.id,
            'eh_fair_value_move_role': 'reversal',
        }, {
            'eh_fair_value_item_id', 'eh_fair_value_move_role',
        })
        reversal._eh_post_verified_reversal()
        reversal.invalidate_recordset(['state', 'date', 'eh_sealed'])
        if (
            reversal.state != 'posted'
            or reversal.date != reversal_date
            or not reversal.eh_sealed
        ):
            raise UserError(_(
                "The fair-value reversal did not post on the exact requested "
                "date; the correction was rolled back."
            ))
        self.with_context(**{
            _FV_ITEM_INTERNAL: _FV_ITEM_CAPABILITY,
        }).write({
            'state': 'draft',
            'prior_carrying': restored_foreign,
            'prior_company_carrying': restored_company,
            'reversal_date': False,
            'reversal_reason': False,
        })
        self.message_post(body=_(
            "Latest remeasurement reversed on %(date)s for controlled "
            "correction. Reason: %(reason)s",
            date=reversal_date,
            reason=reversal_reason,
        ))
        return True

    def action_transfer_level(self):
        """Record and apply an IFRS 13 hierarchy transfer."""
        self.ensure_one()
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can record a hierarchy "
                "transfer."
            ))
        self._eh_lock_transitions()
        self = self._eh_workflow_action()
        if self.state in ('derecognised', 'cancelled'):
            raise UserError(_(
                "A closed fair-value item cannot change hierarchy level."
            ))
        if not self.proposed_level or self.proposed_level == self.level:
            raise UserError(_(
                "Choose a hierarchy level different from the current level."
            ))
        transfer_date = fields.Date.to_date(self.level_transfer_date)
        transfer_reason = (self.level_transfer_reason or '').strip()
        if not transfer_date or not transfer_reason:
            raise UserError(_(
                "Enter the transfer date and the IFRS 13.93(c) reason."
            ))
        if transfer_date > fields.Date.context_today(self):
            raise UserError(_(
                "A hierarchy transfer cannot use a future date."
            ))
        if self.currency_id == self.company_currency_id:
            transfer_amount = self.company_currency_id.round(
                self.prior_carrying
            )
        else:
            transfer_amount = self.company_currency_id.round(
                self.prior_company_carrying
            )
        old_level = self.level
        self.env['eh.fair.value.level.transfer'].sudo().with_context(**{
            _FV_LEVEL_TRANSFER_INTERNAL: _FV_LEVEL_TRANSFER_CAPABILITY,
        }).create({
            'item_id': self.id,
            'transfer_date': transfer_date,
            'from_level': old_level,
            'to_level': self.proposed_level,
            'transfer_amount': transfer_amount,
            'reason': transfer_reason,
        })
        target_level = self.proposed_level
        self.with_context(**{
            _FV_ITEM_INTERNAL: _FV_ITEM_CAPABILITY,
        }).write({
            'level': target_level,
            'proposed_level': False,
            'level_transfer_date': False,
            'level_transfer_reason': False,
        })
        self.message_post(body=_(
            "Fair-value hierarchy transferred from Level %(source)s to "
            "Level %(target)s on %(date)s under the event-date policy. "
            "Reason: %(reason)s",
            source=old_level,
            target=target_level,
            date=transfer_date,
            reason=transfer_reason,
        ))
        return True

    def action_recycle(self):
        """Refuse standalone reserve settlement.

        Debt OCI recycling and equity-reserve transfer are derecognition
        consequences. ``action_derecognise`` posts disposal and reserve legs
        under one lock; settling reserve while instrument remains held would
        recognise disposal accounting too early.
        """
        self.ensure_one()
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can recycle a fair-value "
                "reserve."))
        self._eh_lock_transitions()
        raise UserError(_(
            "Reserve settlement is part of Derecognise and cannot be posted "
            "while %s remains recognised.", self.display_name,
        ))

    def action_derecognise(self, proceeds=None, accounting_date=None):
        """Derecognise the item and settle its OCI reserve atomically.

        IFRS 9.3.2 (assets) / 9.3.3 (liabilities): posts the disposal entry
        for the proceeds against the carrying amount, then in the same
        action settles the accumulated OCI reserve by classification:

        * FVOCI-debt: the reserve is reclassified to profit or loss as a
          reclassification adjustment (IFRS 9.5.7.10).
        * FVOCI-equity election: the reserve, including the final change up
          to the disposal price, is transferred within equity to retained
          earnings and never touches profit or loss (IFRS 9.B5.7.1).
        * FVTPL / amortised cost / unclassified: nothing to recycle.

        The item is frozen afterwards (state ``derecognised``).
        """
        self.ensure_one()
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can derecognise a fair-value "
                "item."))
        self._eh_lock_transitions()
        self = self._eh_workflow_action()
        self._check_company()
        if self.state == 'derecognised':
            raise UserError(_(
                "Item %s is already derecognised.", self.display_name))
        if self.state != 'measured':
            # The recycle amount is exact only when the carrying amount and
            # the OCI reserve are current, so a final remeasurement at the
            # disposal date must come first.
            raise UserError(_(
                "Remeasure %s to its fair value at the disposal date before "
                "derecognising it, so the carrying amount and the OCI "
                "reserve are current.", self.display_name))
        day = fields.Date.to_date(accounting_date or self.disposal_date)
        if not day:
            raise UserError(_(
                "Enter an explicit derecognition / accounting date for %s. "
                "The disposal entry must not be dated from the server "
                "clock.", self.display_name))
        posted_dates = self.move_ids.filtered(
            lambda move: move.state == 'posted' and move.date
        ).mapped('date')
        latest_evidence_date = max(
            posted_dates or [self.measurement_date]
        )
        if day < latest_evidence_date:
            raise UserError(_(
                "The derecognition date %(day)s for %(item)s is before its "
                "latest posted fair-value evidence on %(latest)s. Remeasure "
                "at the disposal date, or choose a date on or after that "
                "evidence.",
                day=fields.Date.to_string(day),
                item=self.display_name,
                latest=fields.Date.to_string(latest_evidence_date),
            ))
        if day != self.measurement_date:
            raise UserError(_(
                "The derecognition date %(day)s for %(item)s must match its "
                "final fair-value measurement date %(measurement)s. Reset, "
                "remeasure at the disposal date, then derecognise.",
                day=fields.Date.to_string(day),
                item=self.display_name,
                measurement=fields.Date.to_string(self.measurement_date),
            ))
        classification = self._effective_classification()
        non_financial_oci = (
            self.nature == 'non_financial_asset'
            and self.routing == 'oci'
            and not classification
        )
        if (
            self.routing == 'oci'
            and not classification
            and not non_financial_oci
        ):
            raise UserError(_(
                "Item %s accumulated an OCI reserve but has no IFRS 9 "
                "classification; set the instrument type (or the legacy "
                "label) so the reserve settlement is determined.",
                self.display_name))
        currency = self.currency_id
        company_currency = self.company_currency_id
        carrying_currency = currency.round(self.prior_carrying)
        proceeds_currency = currency.round(
            proceeds if proceeds is not None else self.disposal_proceeds
        )
        if proceeds_currency < 0:
            raise UserError(_(
                "Disposal proceeds on %s cannot be negative.",
                self.display_name))
        if currency == company_currency:
            carrying = company_currency.round(carrying_currency)
            proceeds_amt = company_currency.round(proceeds_currency)
        else:
            carrying = company_currency.round(
                self.prior_company_carrying
            )
            proceeds_amt = company_currency.round(currency._convert(
                proceeds_currency,
                company_currency,
                self.company_id,
                day,
                round=False,
            ))
        is_liability = self.nature == 'financial_liability'
        if is_liability:
            difference = company_currency.round(carrying - proceeds_amt)
        else:
            difference = company_currency.round(proceeds_amt - carrying)
        # For an FVOCI equity election the final change against carrying is
        # the last fair-value movement and belongs in OCI, never in profit or
        # loss; it is swept into the reserve transfer below. Every other
        # classification takes the difference to profit or loss
        # (IFRS 9.3.2.12).
        diff_to_oci = classification == 'fvoci_equity' and not is_liability
        diff_account = (self.oci_account_id if diff_to_oci
                        else self.gain_loss_account_id)
        missing = []
        if not self.journal_id:
            missing.append(_("journal"))
        if not self.balance_sheet_account_id:
            missing.append(_("balance sheet account"))
        if proceeds_amt and not self.settlement_account_id:
            missing.append(_("settlement account"))
        if difference and not diff_account:
            missing.append(_("gain / loss account"))
        if classification == 'fvoci_debt' and not self.gain_loss_account_id:
            missing.append(_("gain / loss account"))
        reserve_to_equity = classification == 'fvoci_equity' or (
            non_financial_oci
        )
        if reserve_to_equity:
            if not self.retained_earnings_account_id:
                missing.append(_("retained earnings account"))
            if not self.oci_account_id:
                missing.append(_("OCI / equity account"))
        if missing:
            raise UserError(_(
                "Configure the %s on item %s first.",
                ', '.join(dict.fromkeys(missing)), self.display_name))
        def _leg(account, debit, credit, label, amount_currency=0.0):
            values = {
                'account': account,
                'debit': debit,
                'credit': credit,
                'label': label,
            }
            if (
                currency != company_currency
                and not currency.is_zero(amount_currency)
            ):
                values.update({
                    'currency_id': currency.id,
                    'amount_currency': amount_currency,
                })
            return values

        legs = []
        if is_liability:
            if carrying:
                legs.append(_leg(
                    self.balance_sheet_account_id,
                    carrying,
                    0.0,
                    _("Derecognise carrying %s", self.name),
                    carrying_currency,
                ))
            if proceeds_amt:
                legs.append(_leg(
                    self.settlement_account_id,
                    0.0,
                    proceeds_amt,
                    _("Settlement %s", self.name),
                    -proceeds_currency,
                ))
            if difference > 0:
                legs.append(_leg(
                    diff_account,
                    0.0,
                    difference,
                    _("Gain on derecognition %s", self.name),
                    proceeds_currency - carrying_currency,
                ))
            elif difference < 0:
                legs.append(_leg(
                    diff_account,
                    -difference,
                    0.0,
                    _("Loss on derecognition %s", self.name),
                    proceeds_currency - carrying_currency,
                ))
        else:
            if proceeds_amt:
                legs.append(_leg(
                    self.settlement_account_id,
                    proceeds_amt,
                    0.0,
                    _("Disposal proceeds %s", self.name),
                    proceeds_currency,
                ))
            if carrying:
                legs.append(_leg(
                    self.balance_sheet_account_id,
                    0.0,
                    carrying,
                    _("Derecognise carrying %s", self.name),
                    -carrying_currency,
                ))
            if difference > 0:
                legs.append(_leg(
                    diff_account,
                    0.0,
                    difference,
                    _("Gain on derecognition %s", self.name),
                    carrying_currency - proceeds_currency,
                ))
            elif difference < 0:
                legs.append(_leg(
                    diff_account,
                    -difference,
                    0.0,
                    _("Loss on derecognition %s", self.name),
                    carrying_currency - proceeds_currency,
                ))
        if legs:
            self._post_move(legs, date=day, role='derecognition')
        reserve_settlement = (
            classification in ('fvoci_debt', 'fvoci_equity')
            or non_financial_oci
        )
        if reserve_settlement:
            # Re-read after the disposal posting so a final OCI leg (equity
            # election) is included and the recycle amount is exact.
            reserve = company_currency.round(self.oci_reserve_balance)
            if not company_currency.is_zero(reserve):
                amount = abs(reserve)
                if reserve_to_equity:
                    destination = self.retained_earnings_account_id
                    reclass_label = _(
                        "Transfer reserve to retained earnings %s", self.name)
                else:
                    destination = self.gain_loss_account_id
                    reclass_label = _("Recycle OCI to P&L %s", self.name)
                clear_label = _("Clear OCI reserve %s", self.name)
                if reserve < 0:  # net credit (accumulated gain) -> debit out
                    recycle_legs = [
                        _leg(self.oci_account_id, amount, 0.0, clear_label),
                        _leg(destination, 0.0, amount, reclass_label),
                    ]
                else:
                    recycle_legs = [
                        _leg(destination, amount, 0.0, reclass_label),
                        _leg(self.oci_account_id, 0.0, amount, clear_label),
                    ]
                self._post_move(
                    recycle_legs,
                    date=day,
                    role='reserve_settlement',
                )
        self.write({
            'state': 'derecognised',
            'disposal_date': day,
            'derecognition_date': day,
            'disposal_proceeds': proceeds_currency,
            'recycled': reserve_settlement,
        })
        return True

    def action_view_moves(self):
        self.ensure_one()
        self._eh_check_access('read')
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('eh_fair_value_item_id', '=', self.id)],
        }

    # ---- helpers ----

    def _effective_classification(self):
        # The engine-derived classification governs whenever the
        # questionnaire is in use; the manual legacy label only stands for
        # records without an instrument type.
        self.ensure_one()
        return self.ifrs9_classification or self.fvoci_classification

    def _validate_accounts(self):
        self.ensure_one()
        missing = []
        if not self.journal_id:
            missing.append(_("journal"))
        if not self.balance_sheet_account_id:
            missing.append(_("balance sheet account"))
        if self.routing == 'oci' and not self.oci_account_id:
            missing.append(_("OCI / equity account"))
        if self.routing == 'pl' and not self.gain_loss_account_id:
            missing.append(_("gain / loss account"))
        if (
            self._effective_classification() == 'fvoci_debt'
            and self.currency_id != self.company_currency_id
            and not self.gain_loss_account_id
        ):
            missing.append(_("FX gain / loss account"))
        if missing:
            raise UserError(_(
                "Configure the %s on item %s first.",
                ', '.join(missing), self.display_name))

    def _post_move(
        self,
        legs,
        date=None,
        role='remeasurement',
        prior_company_carrying=0.0,
        posted_company_fair_value=0.0,
    ):
        lines = []
        for leg in legs:
            values = {
                'name': leg['label'],
                'account_id': leg['account'].id,
                'debit': leg['debit'],
                'credit': leg['credit'],
            }
            if leg.get('currency_id'):
                values.update({
                    'currency_id': leg['currency_id'],
                    'amount_currency': leg.get('amount_currency', 0.0),
                })
            lines.append((0, 0, values))
        snapshot_complete = role == 'remeasurement'
        move = self.env['account.move'].sudo().with_context(
            **{_FV_MOVE_LINK_INTERNAL: _FV_MOVE_LINK_CAPABILITY},
        )._eh_create_sealed({
            'move_type': 'entry',
            'company_id': self.company_id.id,
            'date': date or self.measurement_date,
            'journal_id': self.journal_id.id,
            'ref': self.name,
            'eh_fair_value_item_id': self.id,
            'eh_fair_value_move_role': role,
            'eh_fair_value_snapshot_complete': snapshot_complete,
            'eh_fair_value_prior_carrying': (
                self.prior_carrying if snapshot_complete else 0.0
            ),
            'eh_fair_value_prior_company_carrying': (
                prior_company_carrying if snapshot_complete else 0.0
            ),
            'eh_fair_value_posted_fair_value': (
                self.fair_value if snapshot_complete else 0.0
            ),
            'eh_fair_value_posted_company_fair_value': (
                posted_company_fair_value if snapshot_complete else 0.0
            ),
            'eh_sealed': True,
            'line_ids': lines,
        })
        move.action_post()
        return move


class EhFairValueLevelTransfer(models.Model):
    _name = 'eh.fair.value.level.transfer'
    _description = "Fair value hierarchy transfer (IFRS 13.93(c), 95)"
    _order = 'transfer_date desc, id desc'
    _check_company_auto = True

    item_id = fields.Many2one(
        'eh.fair.value.item', required=True, ondelete='restrict',
        index=True, check_company=True)
    company_id = fields.Many2one(
        related='item_id.company_id', store=True, readonly=True, index=True)
    currency_id = fields.Many2one(
        related='item_id.company_currency_id', store=True, readonly=True)
    transfer_date = fields.Date(required=True, readonly=True, index=True)
    from_level = fields.Selection(
        [('1', "Level 1"), ('2', "Level 2"), ('3', "Level 3")],
        required=True, readonly=True)
    to_level = fields.Selection(
        [('1', "Level 1"), ('2', "Level 2"), ('3', "Level 3")],
        required=True, readonly=True)
    transfer_amount = fields.Monetary(
        currency_field='currency_id', required=True, readonly=True,
        help="Company-currency carrying amount disclosed as transferred into "
             "or out of Level 3 under IFRS 13.93(e)(iv).")
    recognition_policy = fields.Selection(
        [('event_date', "Recognise on the date observability changes")],
        default='event_date', required=True, readonly=True,
        help="Consistent IFRS 13.95 policy used for transfers between levels.")
    reason = fields.Text(required=True, readonly=True)

    _different_levels = models.Constraint(
        'CHECK (from_level <> to_level)',
        "A hierarchy transfer must move to a different level.")

    @api.model_create_multi
    def create(self, vals_list):
        if not (
            self.env.su
            and self.env.context.get(_FV_LEVEL_TRANSFER_INTERNAL)
            is _FV_LEVEL_TRANSFER_CAPABILITY
        ):
            raise AccessError(_(
                "Hierarchy-transfer evidence can be created only by the "
                "controlled item action."
            ))
        return super().create(vals_list)

    def write(self, vals):
        raise UserError(_(
            "Hierarchy-transfer evidence is immutable; record a later "
            "transfer rather than rewriting history."
        ))

    def unlink(self):
        raise UserError(_(
            "Hierarchy-transfer evidence is immutable and cannot be deleted."
        ))


class EhFairValueRollforward(models.Model):
    _name = 'eh.fair.value.rollforward'
    _description = "Level 3 fair value reconciliation (IFRS 13.93(e))"
    _inherit = ['eh.workflow.guard']
    _order = 'period_end desc, id desc'
    _check_company_auto = True

    # A reconciliation may only close (and reopen) through its own actions,
    # which enforce the tie-to-fair-value check; a direct write of
    # state='closed' would bypass that check and freeze an untied disclosure.
    _eh_guarded_fields = (
        'state', 'company_id', 'currency_id', 'closing_balance',
    )

    item_id = fields.Many2one(
        'eh.fair.value.item', string="Fair Value Item", required=True,
        ondelete='cascade', index=True, check_company=True)
    company_id = fields.Many2one(
        related='item_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='item_id.company_currency_id', store=True, readonly=True)
    state = fields.Selection(
        [('draft', "Draft"), ('closed', "Closed")],
        default='draft', required=True, index=True,
        help="Closing requires the computed closing balance to tie exactly "
             "to the item's fair value (IFRS 13.93(e)); a closed "
             "reconciliation is frozen.")
    move_ids = fields.One2many(
        'account.move', 'eh_fair_value_rollforward_id',
        string="Linked Journal Entries",
        help="The item's posted entries attributed to this period (Pull "
             "Ledger); they feed the gains columns from the ledger instead "
             "of a typed figure.")

    period_start = fields.Date(
        required=True, default=lambda self: self._default_period_bounds()[0],
        help="First day of the reconciliation period.")
    period_end = fields.Date(
        required=True, default=lambda self: self._default_period_bounds()[1],
        help="Reporting date the reconciliation rolls forward to.")

    # ---- roll-forward movements (IFRS 13.93(e)) ----
    opening_balance = fields.Monetary(
        currency_field='currency_id',
        help="Fair value at the beginning of the period.")
    gains_losses_in_pl = fields.Monetary(
        currency_field='currency_id', string="Gains / Losses in P&L",
        compute='_compute_ledger_gains', store=True, readonly=False,
        help="Total gains or losses for the period recognised in profit or "
             "loss (a loss is negative). Ledger-fed from the linked journal "
             "entries when any are attached; manual otherwise.")
    gains_losses_in_oci = fields.Monetary(
        currency_field='currency_id', string="Gains / Losses in OCI",
        compute='_compute_ledger_gains', store=True, readonly=False,
        help="Total gains or losses for the period recognised in other "
             "comprehensive income (a loss is negative). Ledger-fed from "
             "the linked journal entries when any are attached; manual "
             "otherwise.")
    purchases = fields.Monetary(currency_field='currency_id')
    issues = fields.Monetary(currency_field='currency_id')
    sales = fields.Monetary(currency_field='currency_id')
    settlements = fields.Monetary(currency_field='currency_id')
    transfers_into_level3 = fields.Monetary(
        currency_field='currency_id', string="Transfers into Level 3")
    transfers_out_of_level3 = fields.Monetary(
        currency_field='currency_id', string="Transfers out of Level 3")

    closing_balance = fields.Monetary(
        compute='_compute_closing_balance', store=True,
        currency_field='currency_id',
        help="Opening plus gains, purchases, issues and transfers in, less "
             "sales, settlements and transfers out (IFRS 13.93(e)).")

    notes = fields.Text()

    _period_order = models.Constraint(
        'CHECK (period_end >= period_start)',
        "The reconciliation period end cannot be before its start.")
    _unique_item_period = models.Constraint(
        'UNIQUE (item_id, period_start, period_end)',
        "A fair-value item can have only one reconciliation per period.")

    @api.model
    def _default_period_bounds(self):
        item_id = self.env.context.get('default_item_id')
        latest = self.search([
            ('item_id', '=', item_id),
        ], order='period_end desc, id desc', limit=1) if item_id else self
        if latest:
            period_start = latest.period_end + relativedelta(days=1)
            return (
                period_start,
                period_start + relativedelta(years=1, days=-1),
            )
        today = fields.Date.context_today(self)
        fiscal_year = self.env.company.compute_fiscalyear_dates(today)
        return fiscal_year['date_from'], fiscal_year['date_to']

    @api.depends('opening_balance', 'gains_losses_in_pl', 'gains_losses_in_oci',
                 'purchases', 'issues', 'sales', 'settlements',
                 'transfers_into_level3', 'transfers_out_of_level3')
    def _compute_closing_balance(self):
        for line in self:
            line.closing_balance = (
                line.opening_balance
                + line.gains_losses_in_pl
                + line.gains_losses_in_oci
                + line.purchases
                + line.issues
                - line.sales
                - line.settlements
                + line.transfers_into_level3
                - line.transfers_out_of_level3
            )

    @api.depends('move_ids.line_ids.debit', 'move_ids.line_ids.credit',
                 'move_ids.state', 'item_id.gain_loss_account_id',
                 'item_id.oci_account_id')
    def _compute_ledger_gains(self):
        # With journal entries linked, the gains columns are read from the
        # ledger, not typed: a credit on the item's P&L / OCI account is a
        # gain, a debit a loss. Without links the manually captured value
        # stands (legacy records and hand-built disclosures).
        for line in self:
            posted = line.move_ids.filtered(lambda move: (
                move.state == 'posted'
                and move.eh_fair_value_move_role == 'remeasurement'
            ))
            if not posted:
                line.gains_losses_in_pl = line.gains_losses_in_pl or 0.0
                line.gains_losses_in_oci = line.gains_losses_in_oci or 0.0
                continue
            pl = oci = 0.0
            for ml in posted.line_ids:
                if ml.account_id == line.item_id.gain_loss_account_id:
                    pl += ml.credit - ml.debit
                elif ml.account_id == line.item_id.oci_account_id:
                    oci += ml.credit - ml.debit
            line.gains_losses_in_pl = pl
            line.gains_losses_in_oci = oci

    # Movement figures locked once the reconciliation is closed, so a tied
    # disclosure cannot drift out from under its sign-off.
    _FROZEN_AFTER_CLOSED = (
        'opening_balance', 'gains_losses_in_pl', 'gains_losses_in_oci',
        'purchases', 'issues', 'sales', 'settlements',
        'transfers_into_level3', 'transfers_out_of_level3',
        'period_start', 'period_end', 'item_id',
    )

    @api.model_create_multi
    def create(self, vals_list):
        protected = {'company_id', 'currency_id', 'closing_balance'}
        if not self.env.su and any(
            protected.intersection(vals) for vals in vals_list
        ):
            raise AccessError(_(
                "Level 3 reconciliation company and computed outputs are "
                "server-owned."
            ))
        self._eh_authorize_item_targets(vals_list)
        return super().create(vals_list)

    def _eh_authorize_item_targets(self, vals_list, allow_closed=False):
        item_ids = {
            vals.get('item_id') for vals in vals_list if vals.get('item_id')
        }
        items = self.env['eh.fair.value.item'].browse(sorted(item_ids))
        if items:
            if not self.env.su:
                items._eh_check_access('write')
            items.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM eh_fair_value_item WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(items.ids),),
            )
            items.invalidate_recordset()
        closed_items = self.env['eh.fair.value.item']
        if not allow_closed:
            closed_items = items.filtered(
                lambda item: item.state in ('derecognised', 'cancelled')
            )
        if closed_items:
            raise UserError(_(
                "Level 3 reconciliation rows cannot be attached to a "
                "derecognised or cancelled fair-value item."
            ))
        return items

    def write(self, vals):
        current_items = self.mapped('item_id')
        target_id = vals.get('item_id')
        target_items = (
            self.env['eh.fair.value.item'].browse(target_id)
            if target_id else self.env['eh.fair.value.item']
        )
        engine_output = self.env.su and set(vals).issubset({
            'gains_losses_in_pl', 'gains_losses_in_oci', 'closing_balance',
        })
        self._eh_authorize_item_targets(
            [
                {'item_id': item.id}
                for item in (current_items | target_items)
            ],
            allow_closed=engine_output,
        )
        self._eh_lock_own_rows()
        if (
            {'gains_losses_in_pl', 'gains_losses_in_oci'}.intersection(vals)
            and not self.env.su
            and self.filtered('move_ids')
        ):
            raise AccessError(_(
                "Ledger-fed Level 3 gains are engine-owned while journal "
                "entries are linked."
            ))
        frozen = [f for f in self._FROZEN_AFTER_CLOSED if f in vals]
        if frozen:
            for line in self:
                if line.state == 'closed':
                    raise UserError(_(
                        "The Level 3 reconciliation %s is closed; %s cannot "
                        "be changed. Reopen it first.",
                        line.display_name, ', '.join(frozen)))
        return super().write(vals)

    def unlink(self):
        self._eh_authorize_item_targets([
            {'item_id': item.id} for item in self.mapped('item_id')
        ])
        self._eh_lock_own_rows()
        if any(line.state == 'closed' for line in self):
            raise UserError(_(
                "A closed Level 3 reconciliation cannot be deleted; reopen "
                "it first."))
        return super().unlink()

    # ---- actions ----

    def _eh_lock_own_rows(self):
        if not self.env.su:
            self._eh_check_access('write')
        ids = sorted(self.ids)
        if not ids:
            return self
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_fair_value_rollforward WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            (tuple(ids),),
        )
        self.invalidate_recordset()
        return self

    def _eh_lock_transitions(self, allow_closed=False):
        """Parent first, then rollforward: shared by edits and actions."""
        self._eh_authorize_item_targets(
            [{'item_id': item.id} for item in self.mapped('item_id')],
            allow_closed=allow_closed,
        )
        return self._eh_lock_own_rows()

    def action_pull_ledger(self):
        """Attribute the item's posted entries dated in the period to this
        reconciliation, so the gains columns are ledger-fed, not typed."""
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can attribute fair-value "
                "journal entries to a Level 3 reconciliation."
            ))
        self._eh_lock_transitions(allow_closed=True)
        for line in self:
            if line.state == 'closed':
                raise UserError(_(
                    "The Level 3 reconciliation %s is closed; reopen it "
                    "before re-pulling the ledger.", line.display_name))
            moves = line.item_id.move_ids.filtered(
                lambda m, line=line: m.state == 'posted' and m.date
                and m.eh_fair_value_move_role == 'remeasurement'
                and line.period_start <= m.date <= line.period_end)
            stale = line.move_ids - moves
            if not self.env.su:
                (stale | moves)._eh_check_access('write')
            if stale:
                stale.sudo().with_context(
                    **{_FV_MOVE_LINK_INTERNAL: _FV_MOVE_LINK_CAPABILITY},
                )._eh_write_sealed_metadata(
                    {'eh_fair_value_rollforward_id': False},
                    {'eh_fair_value_rollforward_id'},
                )
            if moves:
                moves.sudo().with_context(
                    **{_FV_MOVE_LINK_INTERNAL: _FV_MOVE_LINK_CAPABILITY},
                )._eh_write_sealed_metadata(
                    {'eh_fair_value_rollforward_id': line.id},
                    {'eh_fair_value_rollforward_id'},
                )
        return True

    def action_close(self):
        """Close the reconciliation; refused unless it ties to fair value.

        IFRS 13.93(e) requires the roll-forward to reconcile opening to
        closing fair value; a reconciliation whose closing balance does not
        equal the item's fair value is not a reconciliation, so closing it
        is blocked rather than warned about.
        """
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can close a Level 3 "
                "reconciliation."))
        self._eh_lock_transitions()
        # closing_balance and ledger-fed gains are server-owned stored
        # outputs.  Elevate only after actor write access, manager role and
        # parent/company locks have all succeeded.
        self = self._eh_workflow_action()
        self._compute_ledger_gains()
        self._compute_closing_balance()
        self.flush_recordset([
            'gains_losses_in_pl', 'gains_losses_in_oci', 'closing_balance',
        ])
        for line in self:
            if line.state == 'closed':
                raise UserError(_(
                    "The Level 3 reconciliation %s is already closed.",
                    line.display_name))
            delta = line.closing_balance - line.item_id.company_fair_value
            if not line.currency_id.is_zero(delta):
                raise UserError(_(
                    "Cannot close the Level 3 reconciliation for %(item)s: "
                    "the closing balance %(closing)s does not tie to the "
                    "item's fair value %(fv)s (difference %(delta)s). Fix "
                    "the movements or pull the ledger before closing.",
                    item=line.item_id.display_name,
                    closing=line.closing_balance,
                    fv=line.item_id.company_fair_value,
                    delta=delta))
            line.state = 'closed'
        return True

    def action_reopen(self):
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can reopen a Level 3 "
                "reconciliation."))
        self._eh_lock_transitions()
        self = self._eh_workflow_action()
        self.filtered(lambda line: line.state == 'closed').write(
            {'state': 'draft'})
        return True


class EhFairValueSensitivity(models.Model):
    _name = 'eh.fair.value.sensitivity'
    _description = "Level 3 sensitivity analysis (IFRS 13.93(h))"
    _order = 'item_id, input_name, shock_pct, id'
    _rec_name = 'input_name'
    _check_company_auto = True

    _SERVER_OWNED_FIELDS = frozenset({
        'company_id', 'currency_id', 'value_delta',
    })

    item_id = fields.Many2one(
        'eh.fair.value.item', string="Fair Value Item", required=True,
        ondelete='cascade', index=True, check_company=True)
    company_id = fields.Many2one(
        related='item_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='item_id.currency_id', store=True, readonly=True)

    input_name = fields.Char(
        required=True, string="Unobservable Input",
        help="Significant unobservable input being shocked, e.g. discount "
             "rate, terminal growth rate, capitalisation rate.")
    shock_pct = fields.Float(
        string="Shock (%)", required=True,
        help="Relative shock applied to the unobservable input, in percent; "
             "negative for a downward shock.")
    sensitivity_factor = fields.Float(
        default=1.0, required=True,
        help="Elasticity of the fair value to the shocked input: the "
             "fraction of the relative input shock that flows through to "
             "the measurement. 1.0 is a one-for-one linear response.")
    value_delta = fields.Monetary(
        compute='_compute_value_delta', store=True,
        currency_field='currency_id', string="Value Delta",
        help="fair value x shock% x factor: the change in the measurement "
             "if the input moved by the shock, the quantitative sensitivity "
             "IFRS 13.93(h) asks for.")
    notes = fields.Char()

    _shock_not_zero = models.Constraint(
        'CHECK (shock_pct <> 0)',
        "A sensitivity line needs a non-zero shock.")

    @api.depends('item_id.fair_value', 'shock_pct', 'sensitivity_factor')
    def _compute_value_delta(self):
        for line in self:
            delta = (line.item_id.fair_value * line.shock_pct / 100.0
                     * line.sensitivity_factor)
            currency = line.currency_id or line.item_id.currency_id
            line.value_delta = currency.round(delta) if currency else delta

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and any(
            self._SERVER_OWNED_FIELDS.intersection(vals)
            for vals in vals_list
        ):
            raise AccessError(_(
                "Sensitivity company, currency, and computed value delta "
                "are server-owned."
            ))
        self._eh_authorize_item_targets(vals_list)
        return super().create(vals_list)

    def _eh_authorize_item_targets(self, vals_list):
        item_ids = {
            vals.get('item_id') for vals in vals_list if vals.get('item_id')
        }
        items = self.env['eh.fair.value.item'].browse(sorted(item_ids))
        if items:
            if not self.env.su:
                items._eh_check_access('write')
            items.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM eh_fair_value_item WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(items.ids),),
            )
            items.invalidate_recordset()
        if items.filtered(
            lambda item: item.state in ('derecognised', 'cancelled')
        ):
            raise UserError(_(
                "Sensitivity rows cannot be attached to a derecognised or "
                "cancelled fair-value item."
            ))
        return items

    def write(self, vals):
        if not self.env.su and self._SERVER_OWNED_FIELDS.intersection(vals):
            raise AccessError(_(
                "Sensitivity company, currency, and computed value delta "
                "are server-owned."
            ))
        items = self.mapped('item_id')
        target_id = vals.get('item_id')
        if target_id:
            items |= self.env['eh.fair.value.item'].browse(target_id)
        self._eh_authorize_item_targets([
            {'item_id': item.id} for item in items
        ])
        return super().write(vals)

    def unlink(self):
        self._eh_authorize_item_targets([
            {'item_id': item.id} for item in self.mapped('item_id')
        ])
        return super().unlink()


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_fair_value_item_id = fields.Many2one(
        'eh.fair.value.item', string="Fair Value Item", readonly=True,
        index=True, ondelete='restrict', copy=False, check_company=True)
    eh_fair_value_rollforward_id = fields.Many2one(
        'eh.fair.value.rollforward', string="Level 3 Reconciliation",
        readonly=True, index=True, ondelete='set null', copy=False,
        check_company=True,
        help="Reconciliation period this entry is attributed to, so the "
             "roll-forward gains columns are ledger-fed.")
    eh_fair_value_move_role = fields.Selection(
        [('remeasurement', "Remeasurement"),
         ('derecognition', "Derecognition"),
         ('reserve_settlement', "Reserve Settlement"),
         ('reversal', "Remeasurement Reversal")],
        readonly=True, index=True, copy=False,
        help="Server-owned accounting role. Only remeasurement entries feed "
             "the IFRS 13 Level 3 period-gain columns.")
    eh_fair_value_snapshot_complete = fields.Boolean(
        readonly=True, copy=False,
        help="The remeasurement move carries the before/after carrying "
             "snapshot needed for a controlled latest-entry reversal.")
    eh_fair_value_prior_carrying = fields.Float(
        readonly=True, copy=False)
    eh_fair_value_prior_company_carrying = fields.Float(
        readonly=True, copy=False)
    eh_fair_value_posted_fair_value = fields.Float(
        readonly=True, copy=False)
    eh_fair_value_posted_company_fair_value = fields.Float(
        readonly=True, copy=False)

    _EH_FAIR_VALUE_PROTECTED_FIELDS = frozenset({
        'eh_fair_value_item_id', 'eh_fair_value_rollforward_id',
        'eh_fair_value_move_role', 'eh_fair_value_snapshot_complete',
        'eh_fair_value_prior_carrying',
        'eh_fair_value_prior_company_carrying',
        'eh_fair_value_posted_fair_value',
        'eh_fair_value_posted_company_fair_value',
    })

    @api.model_create_multi
    def create(self, vals_list):
        protected = self._EH_FAIR_VALUE_PROTECTED_FIELDS
        if any(
            any(vals.get(field_name) for field_name in protected)
            for vals in vals_list
        ) and not (
                self.env.context.get(_FV_MOVE_LINK_INTERNAL)
                is _FV_MOVE_LINK_CAPABILITY):
            raise AccessError(_(
                "Fair-value journal-entry provenance is server-owned."
            ))
        return super().create(vals_list)

    def write(self, vals):
        protected = self._EH_FAIR_VALUE_PROTECTED_FIELDS
        if protected.intersection(vals) and not (
                self.env.context.get(_FV_MOVE_LINK_INTERNAL)
                is _FV_MOVE_LINK_CAPABILITY):
            raise AccessError(_(
                "Fair-value journal-entry provenance is server-owned."
            ))
        return super().write(vals)
