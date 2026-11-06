# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.provision: one IAS 37 provision or contingency.

A provision is recognised at the present value of the best estimate. The
discount unwinds each period as a finance cost that raises the carrying
amount towards the undiscounted estimate. Utilisation settles the obligation
against the provision. Contingent liabilities are disclosure-only and are
never posted.

Depth mechanics (IFRS 10/10 program, Phase 4):

* Onerous contracts (IAS 37.66-69, incl. the 2020 cost-of-fulfilment
  amendment): the provision is the lower of the net cost of fulfilling the
  contract (directly related fulfilment costs less the economic benefits
  expected under it, floored at zero) and the penalty of exiting. The best
  estimate derives from those inputs and is read-only for the onerous type
  unless a manual override with a documented reason is set.
* Restructuring (IAS 37.72, 37.80-81): recognition is gated on a detailed
  formal plan plus a valid expectation raised (announcement date and
  narrative), and the direct cost components (termination benefits,
  contract termination, other directly attributable) must sum exactly to
  the best estimate. Excluded costs (retraining, marketing, new systems)
  are registered but never enter the sum.
* Discount-rate governance: the pre-tax rate carries a basis attestation
  and a source note, both chatter-tracked; the rate and its basis are
  frozen once the provision posts and move only through Remeasure.
* Contingent assets (IAS 37.33-35): disclosure-only by default; when the
  inflow becomes virtually certain the related asset is recognised as an
  ASSET against income (never as a provision credit).
* Reimbursements (IAS 37.53): recognised as a separate asset, capped at
  the provision carrying amount, never netted against the provision
  liability. The credit is booked to the provision expense account, which
  implements the permitted NET presentation in profit or loss while the
  statement of financial position stays gross.
"""

from datetime import timedelta

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

# Direct restructuring cost kinds that enter the provision (IAS 37.80);
# every other kind is registered for completeness but excluded (IAS 37.81).
RESTRUCTURING_INCLUDED_KINDS = (
    'termination', 'contract_termination', 'other_direct')
RESTRUCTURING_COST_KINDS = (
    ('termination', "Termination benefits (direct)"),
    ('contract_termination', "Contract termination (direct)"),
    ('other_direct', "Other directly attributable (direct)"),
    ('retraining', "Retraining / relocating staff (excluded)"),
    ('marketing', "Marketing (excluded)"),
    ('new_systems', "Investment in new systems (excluded)"),
)

_ONEROUS_REMEASURE_STAGE_FLAGS = {
    'remeasure_cost_fulfil': 'remeasure_cost_fulfil_staged',
    'remeasure_benefit_expected': 'remeasure_benefit_expected_staged',
    'remeasure_penalty_exit': 'remeasure_penalty_exit_staged',
}

_RATE_REMEASURE_STAGE_FLAGS = {
    'remeasure_discount_rate': 'remeasure_discount_rate_staged',
}

_PROVISION_EVENT_TYPES = (
    ('recognition', "Recognition / addition"),
    ('asset_recognition', "Contingent asset recognition"),
    ('reimbursement', "Reimbursement recognition"),
    ('unwind', "Discount unwind"),
    ('remeasure_increase', "Remeasurement increase"),
    ('remeasure_decrease', "Remeasurement decrease / release"),
    ('utilisation', "Utilisation / settlement"),
    ('reversal', "Reversal"),
    ('asset_reversal', "Contingent asset reversal"),
)


# Unforgeable in-process capability used only while the provision engine
# creates its sealed journal entries.  RPC clients can copy context keys, but
# cannot reproduce this object identity.
_PROVISION_MOVE_LINK_CAPABILITY = object()


_PROVISION_ACCOUNT_TYPES = {
    'provision_account_id': frozenset({
        'liability_current', 'liability_non_current',
    }),
    'expense_account_id': frozenset({'expense', 'expense_direct_cost'}),
    'finance_cost_account_id': frozenset({'expense'}),
    'settlement_account_id': frozenset({
        'asset_cash', 'liability_payable', 'asset_receivable', 'asset_current',
    }),
    'settlement_excess_account_id': frozenset({
        'expense', 'expense_direct_cost',
    }),
    'asset_account_id': frozenset({
        'asset_receivable', 'asset_current', 'asset_non_current',
    }),
    'income_account_id': frozenset({'income', 'income_other'}),
    'reimbursement_account_id': frozenset({
        'asset_receivable', 'asset_current', 'asset_non_current',
    }),
}


class EhProvision(models.Model):
    _name = 'eh.provision'
    _description = "Provision / contingency (IAS 37)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'expected_settlement_date, id desc'
    _rec_name = 'name'

    # Workflow-critical fields. Two things may only move through the record's
    # own actions (recognise / recognise-asset / utilise / reverse / cancel /
    # unwind / remeasure / recognise-reimbursement), each of which posts the
    # required journal entry before writing the figure under sudo:
    #  * state - so a draft can never be relabelled 'recognised' straight past
    #    the posting; and
    #  * the posted subledger figures carrying_amount, utilised_amount and
    #    reimbursement_recognised - the IAS 37.84 carrying amount and the
    #    cumulative utilised / reimbursement-recognised anchors that Reverse
    #    and the deferred-tax temp-diff feed trust. A direct RPC
    #    write({'carrying_amount': ...}) by a low-privilege user (who holds
    #    perm_write on the model) would silently restate the booked figure and
    #    drive a later Reverse off a fabricated amount.
    # The shared eh.workflow.guard refuses every such write unless it
    # originates from an action (provenance proven by env.su, not a forgeable
    # context flag); the actions elevate through self._eh_workflow_action().
    _eh_guarded_fields = (
        'state', 'carrying_amount', 'utilised_amount',
        'reimbursement_recognised', 'recognition_date',
        'last_unwind_date', 'unwound_periods',
        'unwind_anchor_date',
        'currency_id', 'present_value',
        'eh_integrity_review_required', 'eh_integrity_review_reason',
        'eh_integrity_released_by_id', 'eh_integrity_released_on',
    )

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('recognised', "Recognised"),
         ('recognised_asset', "Asset Recognised"),
         ('settled', "Settled"), ('reversed', "Reversed"),
         ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    eh_integrity_review_required = fields.Boolean(
        readonly=True, copy=False, index=True,
        help="Legacy journal provenance did not pass the upgrade integrity "
             "checks. Posting and deferred-tax consumption remain blocked "
             "until the evidence is corrected and reviewed.")
    eh_integrity_review_reason = fields.Text(readonly=True, copy=False)
    eh_integrity_review_release_reason = fields.Text(
        copy=False, tracking=True,
        help="Manager review evidence required before a quarantined legacy "
             "provision can be released. The engine independently rechecks "
             "every linked journal entry before clearing quarantine.")
    eh_integrity_released_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False, string="Integrity Released By")
    eh_integrity_released_on = fields.Datetime(
        readonly=True, copy=False, string="Integrity Released On")

    classification = fields.Selection(
        [('provision', "Provision"),
         ('contingent_liability', "Contingent liability (disclosure)"),
         ('contingent_asset', "Contingent asset (disclosure)")],
        default='provision', required=True, tracking=True,
        help="Only provisions are recognised; contingent items are disclosed "
             "and never posted (IAS 37.27-28, 86).")
    provision_type = fields.Selection(
        [('legal', "Legal"), ('constructive', "Constructive"),
         ('onerous', "Onerous contract"), ('restructuring', "Restructuring"),
         ('warranty', "Warranty"), ('other', "Other")],
        default='other', required=True)

    # ---- onerous contract measurement (IAS 37.66-69) ----
    unavoidable_cost_fulfil = fields.Monetary(
        currency_field='currency_id', tracking=True,
        string="Cost of Fulfilling",
        help="Costs that relate directly to fulfilling the contract: the "
             "incremental costs plus an allocation of other costs that "
             "relate directly to it (IAS 37.68A, 2020 amendment).")
    contract_benefit_expected = fields.Monetary(
        currency_field='currency_id', tracking=True,
        string="Benefits Expected",
        help="Economic benefits expected to be received under the contract; "
             "they reduce the net cost of fulfilling (IAS 37.68).")
    penalty_exit = fields.Monetary(
        currency_field='currency_id', tracking=True,
        string="Penalty of Exiting",
        help="Compensation or penalty arising from failure to fulfil the "
             "contract. Zero means there is no exit option, so the net cost "
             "of fulfilling is not capped (IAS 37.68).")
    onerous_override = fields.Boolean(
        string="Manual Estimate Override", tracking=True,
        help="Tick to key the best estimate manually instead of deriving it "
             "from the onerous-contract inputs. Requires a documented "
             "reason.")
    onerous_override_reason = fields.Char(
        string="Override Reason", tracking=True,
        help="Why the derived onerous measure is overridden; kept for the "
             "audit trail.")

    # ---- restructuring recognition gate (IAS 37.72, 37.80-81) ----
    restructuring_plan = fields.Boolean(
        string="Detailed Formal Plan", tracking=True,
        help="A detailed formal plan identifying the business, locations, "
             "employees, expenditures and timing exists (IAS 37.72(a)).")
    restructuring_expectation = fields.Boolean(
        string="Valid Expectation Raised", tracking=True,
        help="Implementation has started or the main features have been "
             "announced to those affected (IAS 37.72(b)).")
    restructuring_announcement_date = fields.Date(
        string="Announcement Date", tracking=True,
        help="Date the plan was announced or implementation began.")
    restructuring_announcement = fields.Text(
        string="Announcement / Expectation Raised",
        help="How the valid expectation was raised: what was announced, to "
             "whom, and when implementation began.")
    restructuring_line_ids = fields.One2many(
        'eh.provision.restructuring.line', 'provision_id',
        string="Restructuring Cost Components")
    restructuring_component_total = fields.Monetary(
        compute='_compute_restructuring_totals',
        currency_field='currency_id', string="Direct Components Total",
        help="Sum of the direct cost components; must equal the best "
             "estimate before a restructuring provision can be recognised "
             "(IAS 37.80).")
    restructuring_excluded_total = fields.Monetary(
        compute='_compute_restructuring_totals',
        currency_field='currency_id', string="Excluded Costs Total",
        help="Costs registered for completeness but excluded from the "
             "provision: retraining, marketing, new systems (IAS 37.81).")

    best_estimate = fields.Monetary(
        currency_field='currency_id', required=True, tracking=True,
        compute='_compute_best_estimate', store=True, readonly=False,
        precompute=True,
        help="Undiscounted best estimate of the expenditure required to "
             "settle the obligation (IAS 37.36). For an onerous contract it "
             "derives from the fulfilment/penalty inputs (lower of the net "
             "cost of fulfilling and the penalty of exiting, IAS 37.68) and "
             "is read-only unless the manual override is set.")
    discount_rate = fields.Float(
        digits=(6, 3), default=0.0, tracking=True,
        help="Pre-tax discount rate, as a percentage. Zero when the time "
             "value of money is not material.")
    rate_basis = fields.Selection(
        [('risk_free_govt', "Risk-free government rate"),
         ('entity_specific_pretax', "Entity-specific pre-tax rate"),
         ('other', "Other")],
        default='entity_specific_pretax', required=True, tracking=True,
        string="Rate Basis",
        help="Attestation of what the discount rate is: IAS 37.47 requires "
             "a pre-tax rate reflecting current market assessments of the "
             "time value of money and risks specific to the liability.")
    rate_source = fields.Char(
        string="Rate Source", tracking=True,
        help="Where the rate came from (curve, quote, internal memo "
             "reference); chatter-tracked for the audit trail.")
    periods_to_settlement = fields.Integer(
        default=0,
        help="Annual discount periods derived from the accounting date and "
             "expected settlement date. A final partial year is one period; "
             "the field is retained for legacy/API compatibility but the "
             "posting action aligns it to the calendar dates.")
    expected_settlement_date = fields.Date(tracking=True)
    action_accounting_date = fields.Date(
        string="Accounting / Effective Date", tracking=True, copy=False,
        help="Explicit date used by the next provision posting action. No "
             "posting action derives its date from the server clock. Review "
             "this value before Recognise, Unwind, Remeasure, Utilise, "
             "Recognise Reimbursement, or Reverse. Integrations may instead "
             "pass an explicit date to the action method.")
    present_value = fields.Monetary(
        compute='_compute_present_value', store=True,
        currency_field='currency_id',
        help="Best estimate discounted to present value at the discount "
             "rate over the periods to settlement.")

    recognition_date = fields.Date(
        readonly=True, copy=False,
        help="Date the provision was recognised; the anchor for the discount "
             "unwind schedule.")
    last_unwind_date = fields.Date(
        readonly=True, copy=False,
        help="Date up to which the discount has been unwound. The next unwind "
             "accretes only the periods elapsed since this date.")
    unwind_anchor_date = fields.Date(
        readonly=True, copy=False,
        help="Date of the latest recognition or remeasurement from which the "
             "current-rate discount unwind schedule runs.")
    unwound_periods = fields.Integer(
        readonly=True, copy=False, default=0,
        help="Number of whole periods already unwound, capped at the periods "
             "to settlement.")

    carrying_amount = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Amount currently recognised as a liability.")
    utilised_amount = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id')
    utilise_amount = fields.Monetary(
        currency_field='currency_id',
        help="Amount to settle against the provision on the next Utilise "
             "action.")
    remeasure_estimate = fields.Monetary(
        currency_field='currency_id',
        help="Revised undiscounted best estimate of the obligation still "
             "outstanding. Enter it and use Remeasure to book the change in "
             "estimate to profit or loss (IAS 37.59). Ignored for an "
             "onerous contract without manual override: there the revision "
             "is staged through the revised fulfilment/penalty inputs.")
    remeasure_cost_fulfil = fields.Monetary(
        currency_field='currency_id', string="Revised Cost of Fulfilling",
        help="Stage the full revised set of onerous inputs, then Remeasure "
             "re-derives the estimate from them (IAS 37.59, 37.68).")
    remeasure_benefit_expected = fields.Monetary(
        currency_field='currency_id', string="Revised Benefits Expected")
    remeasure_penalty_exit = fields.Monetary(
        currency_field='currency_id', string="Revised Penalty of Exiting",
        help="Zero means no exit option (the net cost of fulfilling is not "
             "capped), consistent with the recognition inputs.")
    # Monetary zero is both a valid revised input and Odoo's value for an
    # untouched field.  Server-maintained provenance bits distinguish those
    # cases so a partial staged set can never silently replace omitted inputs
    # with zero during remeasurement.
    remeasure_cost_fulfil_staged = fields.Boolean(
        readonly=True, copy=False)
    remeasure_benefit_expected_staged = fields.Boolean(
        readonly=True, copy=False)
    remeasure_penalty_exit_staged = fields.Boolean(
        readonly=True, copy=False)

    # IAS 37.47 requires the rate used at each reporting-date remeasurement
    # to reflect CURRENT market assessments. The separate staging fields make
    # that decision explicit instead of silently reusing the recognition rate.
    remeasure_discount_rate = fields.Float(
        digits=(6, 3), string="Current Discount Rate (%)",
        help="Current pre-tax market rate to adopt on the next remeasurement "
             "(IAS 37.47). Enter it explicitly, including zero.")
    remeasure_discount_rate_staged = fields.Boolean(readonly=True, copy=False)
    remeasure_rate_basis = fields.Selection(
        [('risk_free_govt', "Risk-free government rate"),
         ('entity_specific_pretax', "Entity-specific pre-tax rate"),
         ('other', "Other")],
        string="Current Rate Basis")
    remeasure_rate_source = fields.Char(
        string="Current Rate Source",
        help="Curve, quote, or approval memo supporting the current rate.")
    remeasure_expected_settlement_date = fields.Date(
        string="Revised Settlement Date",
        help="Current expected settlement date to adopt on remeasurement. "
             "Leave blank to retain the existing expected date.")
    remeasure_restructuring_line_ids = fields.One2many(
        'eh.provision.restructuring.remeasure.line', 'provision_id',
        string="Revised Restructuring Components",
        help="Full revised component set considered at remeasurement. Direct "
             "costs are re-gated under IAS 37.72/80-81 and atomically replace "
             "the recognised set only after the journal posts.")

    # ---- contingent asset recognition (IAS 37.33-35) ----
    virtually_certain = fields.Boolean(
        string="Inflow Virtually Certain", tracking=True,
        help="When the realisation of income is virtually certain the "
             "related asset is not a contingent asset any more and is "
             "recognised as an asset against income (IAS 37.33). Until "
             "then the item stays disclosure-only.")

    # ---- reimbursement asset (IAS 37.53) ----
    reimbursement_partner_id = fields.Many2one(
        'res.partner', string="Reimbursement Counterparty", tracking=True,
        help="Insurer, indemnifier or supplier warranty expected to "
             "reimburse part or all of the expenditure.")
    reimbursement_amount = fields.Monetary(
        currency_field='currency_id', string="Reimbursement to Recognise",
        help="Expected reimbursement that is virtually certain to be "
             "received. Recognised as a SEPARATE asset, never netted "
             "against the provision; capped at the provision carrying "
             "amount (IAS 37.53).")
    reimbursement_recognised = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        string="Reimbursement Recognised", tracking=True,
        help="Current carrying amount of the reimbursement asset recognised "
             "against this provision. It is re-capped when the obligation "
             "falls and derecognised when the provision reverses. The P&L "
             "credit goes to the provision expense account (permitted net "
             "presentation, IAS 37.54); the balance sheet keeps asset and "
             "liability gross.")

    # ---- accounts ----
    provision_account_id = fields.Many2one(
        'account.account', string="Provision Liability Account", tracking=True,
        domain="[('account_type', 'in', "
               "['liability_current', 'liability_non_current'])]")
    expense_account_id = fields.Many2one(
        'account.account', string="Expense Account", tracking=True,
        domain="[('account_type', 'in', ['expense', 'expense_direct_cost'])]")
    finance_cost_account_id = fields.Many2one(
        'account.account', string="Finance Cost Account", tracking=True,
        domain="[('account_type', '=', 'expense')]")
    settlement_account_id = fields.Many2one(
        'account.account', string="Settlement Account", tracking=True,
        domain="[('account_type', 'in', "
               "['asset_cash', 'liability_payable', 'asset_receivable', "
               "'asset_current'])]")
    settlement_partner_id = fields.Many2one(
        'res.partner', string="Settlement Counterparty", tracking=True,
        help="Supplier, claimant, or other counterparty carried on payable or "
             "receivable settlement lines.")
    settlement_excess_account_id = fields.Many2one(
        'account.account', string="Settlement Excess Expense", tracking=True,
        domain="[('account_type', 'in', ['expense', 'expense_direct_cost'])]",
        help="Debited for actual settlement expenditure above the provision "
             "carrying amount. Required only when settlement exceeds carrying.")
    asset_account_id = fields.Many2one(
        'account.account', string="Asset / Receivable Account", tracking=True,
        domain="[('account_type', 'in', "
               "['asset_receivable', 'asset_current', 'asset_non_current'])]",
        help="Debited when a virtually certain contingent asset is "
             "recognised (IAS 37.33).")
    income_account_id = fields.Many2one(
        'account.account', string="Income Account", tracking=True,
        domain="[('account_type', 'in', ['income', 'income_other'])]",
        help="Credited when a virtually certain contingent asset is "
             "recognised.")
    reimbursement_account_id = fields.Many2one(
        'account.account', string="Reimbursement Asset Account",
        tracking=True,
        domain="[('account_type', 'in', "
               "['asset_receivable', 'asset_current', 'asset_non_current'])]",
        help="Debited when a reimbursement asset is recognised as a "
             "separate asset (IAS 37.53).")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        domain="[('type', '=', 'general'), ('company_id', '=', company_id)]")

    move_ids = fields.One2many('account.move', 'eh_provision_id')
    move_count = fields.Integer(compute='_compute_move_count')

    notes = fields.Text()
    disclosure_nature = fields.Text(
        string="Nature of Obligation / Contingency", tracking=True,
        help="Brief nature of the obligation or contingent item for the IAS "
             "37.85/86 disclosure note.")
    disclosure_uncertainties = fields.Text(
        string="Timing / Amount Uncertainties", tracking=True,
        help="Expected timing, major estimation uncertainties, and material "
             "assumptions used for IAS 37.85(b)/86 disclosure.")
    disclosure_rare_exemption = fields.Boolean(
        string="Rare Seriously Prejudicial Exemption", tracking=True,
        help="IAS 37.92 exemption from specific information in extremely "
             "rare cases where disclosure would seriously prejudice the "
             "entity's position. A reason remains mandatory.")
    disclosure_exemption_reason = fields.Text(
        string="Disclosure Exemption Reason", tracking=True)

    _check_estimate = models.Constraint(
        'CHECK (best_estimate >= 0)', 'Best estimate cannot be negative.')
    _check_rate = models.Constraint(
        'CHECK (discount_rate >= 0)', 'Discount rate cannot be negative.')
    _check_remeasure_rate = models.Constraint(
        'CHECK (remeasure_discount_rate >= 0)',
        'The current remeasurement discount rate cannot be negative.')
    _check_periods = models.Constraint(
        'CHECK (periods_to_settlement >= 0)',
        'Periods to settlement cannot be negative.')
    _check_onerous_inputs = models.Constraint(
        'CHECK (unavoidable_cost_fulfil >= 0 AND penalty_exit >= 0 '
        'AND contract_benefit_expected >= 0)',
        'Onerous contract inputs cannot be negative.')

    @api.constrains('disclosure_rare_exemption',
                    'disclosure_exemption_reason')
    def _check_disclosure_exemption_reason(self):
        for provision in self:
            if (
                provision.disclosure_rare_exemption
                and not (provision.disclosure_exemption_reason or '').strip()
            ):
                raise ValidationError(_(
                    "Document why disclosure would be seriously prejudicial "
                    "before applying the rare IAS 37.92 exemption."))

    # ---- onerous measurement (IAS 37.66-69) ----

    def _onerous_inputs_set(self):
        """Any onerous input keyed. All-zero inputs mean the engine is not
        in use (legacy onerous records keep their manual estimate)."""
        self.ensure_one()
        return bool(self.unavoidable_cost_fulfil or self.penalty_exit
                    or self.contract_benefit_expected)

    def _onerous_measure(self, fulfil=None, benefit=None, penalty=None):
        """Lower of the net cost of fulfilling and the penalty of exiting.

        net cost of fulfilling = max(fulfil costs - benefits expected, 0)
        measure = min(net cost, penalty) when an exit penalty exists,
        otherwise the net cost alone (no exit option, IAS 37.68).
        """
        self.ensure_one()
        fulfil = self.unavoidable_cost_fulfil if fulfil is None else fulfil
        benefit = (self.contract_benefit_expected if benefit is None
                   else benefit)
        penalty = self.penalty_exit if penalty is None else penalty
        net_fulfil = max(fulfil - benefit, 0.0)
        measure = min(net_fulfil, penalty) if penalty else net_fulfil
        return self.currency_id.round(measure) if self.currency_id \
            else measure

    @api.depends('provision_type', 'onerous_override',
                 'unavoidable_cost_fulfil', 'contract_benefit_expected',
                 'penalty_exit')
    def _compute_best_estimate(self):
        for p in self:
            if (p.provision_type == 'onerous' and not p.onerous_override
                    and p._onerous_inputs_set()):
                p.best_estimate = p._onerous_measure()
            else:
                # Editable-compute idiom: every other type keeps the keyed
                # estimate.
                p.best_estimate = p.best_estimate

    @api.constrains('best_estimate', 'provision_type', 'onerous_override',
                    'unavoidable_cost_fulfil', 'contract_benefit_expected',
                    'penalty_exit')
    def _check_onerous_best_estimate(self):
        # Guardrail, not honour-system: a hand-keyed best estimate on an
        # onerous contract without override cannot diverge from the derived
        # measure (a create() passing best_estimate bypasses the compute).
        for p in self:
            if (p.provision_type == 'onerous' and not p.onerous_override
                    and p._onerous_inputs_set()
                    and p.currency_id.compare_amounts(
                        p.best_estimate, p._onerous_measure()) != 0):
                raise ValidationError(_(
                    "The best estimate of an onerous contract derives from "
                    "its inputs: lower of the net cost of fulfilling and "
                    "the penalty of exiting (IAS 37.68). Set the manual "
                    "override (with a reason) to key it directly."))

    @api.constrains('onerous_override', 'onerous_override_reason')
    def _check_onerous_override_reason(self):
        for p in self:
            if p.onerous_override and not p.onerous_override_reason:
                raise ValidationError(_(
                    "Document why the derived onerous measure is being "
                    "overridden (audit trail)."))

    # ---- restructuring totals (IAS 37.80-81) ----

    @api.depends('restructuring_line_ids.amount',
                 'restructuring_line_ids.cost_kind')
    def _compute_restructuring_totals(self):
        for p in self:
            included = excluded = 0.0
            for line in p.restructuring_line_ids:
                if line.cost_kind in RESTRUCTURING_INCLUDED_KINDS:
                    included += line.amount
                else:
                    excluded += line.amount
            if p.currency_id:
                included = p.currency_id.round(included)
                excluded = p.currency_id.round(excluded)
            p.restructuring_component_total = included
            p.restructuring_excluded_total = excluded

    def _check_restructuring_gate(self, component_lines=None, estimate=None):
        """IAS 37.72 recognition gate for a restructuring provision."""
        self.ensure_one()
        if not self.restructuring_plan or not self.restructuring_expectation:
            raise UserError(_(
                "A restructuring provision is recognised only with a "
                "detailed formal plan and a valid expectation in those "
                "affected that it will be carried out (IAS 37.72). Confirm "
                "both checklist items on %s.", self.display_name))
        if not self.restructuring_announcement_date \
                or not self.restructuring_announcement:
            raise UserError(_(
                "Record the announcement date and how the valid "
                "expectation was raised (announcement of the main features "
                "or start of implementation, IAS 37.72) on %s.",
                self.display_name))
        component_lines = (
            self.restructuring_line_ids
            if component_lines is None else component_lines
        )
        included = component_lines.filtered(
            lambda l: l.cost_kind in RESTRUCTURING_INCLUDED_KINDS)
        if not included:
            raise UserError(_(
                "Break %s into its direct cost components (termination "
                "benefits, contract termination, other directly "
                "attributable costs) before recognition (IAS 37.80).",
                self.display_name))
        total = self.currency_id.round(sum(included.mapped('amount')))
        estimate = self.best_estimate if estimate is None else estimate
        if self.currency_id.compare_amounts(total, estimate) != 0:
            raise UserError(_(
                "The direct restructuring components (%(total).2f) must sum "
                "to the best estimate (%(estimate).2f). Excluded costs such "
                "as retraining, marketing or investment in new systems "
                "never enter the provision (IAS 37.81).",
                total=total, estimate=estimate))

    @api.model
    def _calendar_periods_between(self, start, end):
        """Whole annual discount periods plus one final partial year.

        This gives ``periods_to_settlement`` a stable calendar meaning. It is
        no longer an operator-selected number of arbitrary equal fractions of
        the date span: each boundary is an anniversary, with a final stub on
        the expected settlement date when necessary.
        """
        start = fields.Date.to_date(start)
        end = fields.Date.to_date(end)
        if not start or not end or end <= start:
            return 0
        delta = relativedelta(end, start)
        return delta.years + int(bool(delta.months or delta.days))

    def _align_settlement_schedule(self, accounting_date):
        """Align a draft provision's horizon to its explicit calendar dates."""
        self.ensure_one()
        accounting_date = fields.Date.to_date(accounting_date)
        end = self.expected_settlement_date
        periods = self.periods_to_settlement or 0
        if end:
            if end <= accounting_date:
                raise UserError(_(
                    "Expected settlement date %(settlement)s must follow "
                    "the recognition date %(recognition)s for %(provision)s.",
                    settlement=end, recognition=accounting_date,
                    provision=self.display_name))
            derived = self._calendar_periods_between(accounting_date, end)
            if periods != derived:
                self.with_context(eh_provision_remeasure=True).write({
                    'periods_to_settlement': derived,
                })
            return derived, end
        if periods > 0:
            # Legacy/API callers that supplied an annual count but no date are
            # made explicit at the posting boundary, preserving their measure
            # while eliminating a date-free schedule going forward.
            end = accounting_date + relativedelta(years=periods)
            self.with_context(eh_provision_remeasure=True).write({
                'expected_settlement_date': end,
            })
            return periods, end
        if self.discount_rate:
            raise UserError(_(
                "Set an expected settlement date before recognising a "
                "discounted provision; the discount horizon is calendar "
                "derived."))
        return 0, False

    @api.depends('best_estimate', 'discount_rate', 'periods_to_settlement')
    def _compute_present_value(self):
        for p in self:
            rate = (p.discount_rate or 0.0) / 100.0
            n = p.periods_to_settlement or 0
            pv = p.best_estimate / ((1.0 + rate) ** n) if rate and n else \
                p.best_estimate
            p.present_value = p.currency_id.round(pv) if p.currency_id else pv

    def _compute_move_count(self):
        for p in self:
            p.move_count = len(p.move_ids)

    def eh_deferred_tax_temp_diffs(self, reporting_date):
        """IAS 12 producer hook for the eh_account_deferred_tax seam.

        A recognised provision is a DEDUCTIBLE temporary difference: tax relief
        comes when the obligation is paid, not when it is provided, so the
        accounting carrying amount exceeds the tax base of nil. Carrying value
        is reconstructed from posted provision-account lines through the
        requested reporting date, so unwind, utilisation, remeasurement,
        reversal, and historical runs all use their actual as-of GL balance.
        Positive balances emit a liability with tax base 0; the deferred-tax
        run turns that deductible difference into a DTA.
        """
        quarantined = self.filtered('eh_integrity_review_required')
        if quarantined:
            raise UserError(_(
                "Deferred-tax extraction is blocked for quarantined "
                "provision %(provision)s: %(reason)s",
                provision=quarantined[0].display_name,
                reason=(quarantined[0].eh_integrity_review_reason
                        or _("legacy journal provenance requires review")),
            ))
        report_date = fields.Date.to_date(
            reporting_date or fields.Date.context_today(self),
        )
        provisions = self.filtered(
            lambda provision: (
                provision.classification == 'provision'
                and provision.provision_account_id
            )
        )
        provisions.flush_recordset(['provision_account_id'])
        self.env['account.move'].flush_model([
            'state', 'date', 'eh_provision_id',
        ])
        self.env['account.move.line'].flush_model([
            'move_id', 'account_id', 'debit', 'credit',
        ])
        balances = {}
        reimbursement_balances = {}
        if provisions:
            self.env.cr.execute(
                """
                SELECT move.eh_provision_id,
                       COALESCE(SUM(line.credit - line.debit), 0.0)
                  FROM account_move_line line
                  JOIN account_move move ON move.id = line.move_id
                  JOIN eh_provision provision
                    ON provision.id = move.eh_provision_id
                 WHERE move.eh_provision_id IN %s
                   AND move.state = 'posted'
                   AND move.date <= %s
                   AND line.account_id = provision.provision_account_id
                 GROUP BY move.eh_provision_id
                """,
                [tuple(provisions.ids), report_date],
            )
            balances = dict(self.env.cr.fetchall())
            self.env.cr.execute(
                """
                SELECT move.eh_provision_id,
                       COALESCE(SUM(line.debit - line.credit), 0.0)
                  FROM account_move_line line
                  JOIN account_move move ON move.id = line.move_id
                  JOIN eh_provision provision
                    ON provision.id = move.eh_provision_id
                 WHERE move.eh_provision_id IN %s
                   AND move.state = 'posted'
                   AND move.date <= %s
                   AND provision.reimbursement_account_id IS NOT NULL
                   AND line.account_id = provision.reimbursement_account_id
                 GROUP BY move.eh_provision_id
                """,
                [tuple(provisions.ids), report_date],
            )
            reimbursement_balances = dict(self.env.cr.fetchall())
        out = []
        for p in provisions:
            carrying = p.currency_id.round(balances.get(p.id, 0.0))
            if p.currency_id.compare_amounts(carrying, 0.0) <= 0:
                continue
            out.append({
                'name': _("Provision: %s", p.display_name),
                'source_key': 'provision:%s' % p.id,
                'category': 'provision',
                'nature': 'liability',
                'carrying_amount': carrying,
                'tax_base': 0.0,
                'through_oci': False,
            })
            reimbursement = p.currency_id.round(
                reimbursement_balances.get(p.id, 0.0))
            if p.currency_id.compare_amounts(reimbursement, 0.0) > 0:
                # A separately recognised reimbursement asset can have a tax
                # base of nil when its future recovery is taxable. Expose the
                # asset as a distinct producer item so the deferred-tax engine
                # can classify the taxable temporary difference rather than
                # silently netting it against the provision liability.
                out.append({
                    'name': _("Provision reimbursement: %s", p.display_name),
                    'source_key': 'provision_reimbursement:%s' % p.id,
                    'category': 'provision_reimbursement',
                    'nature': 'asset',
                    'carrying_amount': reimbursement,
                    'tax_base': 0.0,
                    'through_oci': False,
                })
        return out

    # Measurement inputs frozen once the provision is posted. Re-measuring a
    # recognised provision would silently move present_value away from the
    # posted carrying amount (IAS 37: a recognised provision is remeasured
    # only through the unwind/utilise/reversal postings, never by editing the
    # inputs in place).
    _FROZEN_FIELDS = (
        'name', 'company_id', 'journal_id', 'provision_account_id',
        'expense_account_id', 'finance_cost_account_id',
        'settlement_account_id', 'asset_account_id', 'income_account_id',
        'reimbursement_account_id',
        'best_estimate', 'discount_rate', 'periods_to_settlement',
        'expected_settlement_date', 'classification', 'provision_type',
        # onerous inputs feed the computed best estimate; a raw edit after
        # posting would silently move it through the compute (which runs
        # below the write() guard), so the inputs freeze with it.
        'unavoidable_cost_fulfil', 'contract_benefit_expected',
        'penalty_exit', 'onerous_override', 'onerous_override_reason',
        # discount-rate attestation and the gates that justified posting.
        'rate_basis', 'rate_source', 'restructuring_plan',
        'restructuring_expectation',
        'restructuring_announcement_date', 'restructuring_announcement',
        'virtually_certain',
    )

    _REFERENCE_FIELDS = frozenset({
        'company_id', 'journal_id', 'provision_account_id',
        'expense_account_id', 'finance_cost_account_id',
        'settlement_account_id', 'settlement_excess_account_id',
        'asset_account_id', 'income_account_id',
        'reimbursement_account_id',
    })

    # States in which the measurement is posted and therefore frozen.
    _FROZEN_STATES = (
        'recognised', 'recognised_asset', 'settled', 'reversed', 'cancelled',
    )

    @api.model
    def _next_company_reference(self, company_id):
        """Return a company-isolated provision number.

        ``ir.sequence.next_by_code`` falls back to a global sequence, which
        made one company's numbering consume another's. A transaction-scoped
        advisory lock makes lazy creation race-safe, and each company then has
        an independent counter under the same functional code.
        """
        company = self.env['res.company'].browse(company_id).exists()
        if not company:
            return '/'
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s), %s)",
            ['eh.provision.company.sequence', company.id],
        )
        Sequence = self.env['ir.sequence'].sudo().with_context(
            active_test=False)
        sequence = Sequence.search([
            ('code', '=', 'eh.provision'),
            ('company_id', '=', company.id),
        ], limit=1)
        if not sequence:
            template = Sequence.search([
                ('code', '=', 'eh.provision'),
                ('company_id', '=', False),
            ], limit=1)
            sequence = Sequence.create({
                'name': _("Provision Reference — %s", company.display_name),
                'code': 'eh.provision',
                'prefix': template.prefix or 'PROV/%(year)s/',
                'padding': template.padding or 4,
                'implementation': template.implementation or 'standard',
                'company_id': company.id,
            })
        return sequence.with_company(company).next_by_id() or '/'

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            for stage_field, marker_field in {
                    **_ONEROUS_REMEASURE_STAGE_FLAGS,
                    **_RATE_REMEASURE_STAGE_FLAGS,
            }.items():
                # Never trust a caller-supplied marker; derive it only from
                # whether that input was explicitly present, including zero.
                vals[marker_field] = stage_field in vals
            self._eh_assert_company_allowed(
                vals.get('company_id') or self.env.company.id)
            if vals.get('name', '/') == '/':
                vals['name'] = self._next_company_reference(
                    vals.get('company_id') or self.env.company.id)
        provisions = super().create(vals_list)
        provisions._eh_validate_references()
        return provisions

    def write(self, vals):
        vals = dict(vals or {})
        adopting_remeasure = bool(
            self.env.su
            and self.env.context.get('eh_provision_remeasure')
        )
        if not adopting_remeasure:
            # Readonly is a UI attribute, not an RPC security boundary.
            # Discard forged provenance and derive each marker from an actual
            # write of its corresponding staged input.
            for stage_field, marker_field in {
                    **_ONEROUS_REMEASURE_STAGE_FLAGS,
                    **_RATE_REMEASURE_STAGE_FLAGS,
            }.items():
                vals.pop(marker_field, None)
                if stage_field in vals:
                    vals[marker_field] = True
        if vals and not self.env.su:
            self._eh_check_access('write')
        if (
            'eh_integrity_review_release_reason' in vals
            and not self.env.su
            and not self.env.user.has_group(
                'eh_account_base.group_eh_manager')
        ):
            raise AccessError(_(
                "Only an EH Accounting Manager can document an integrity "
                "quarantine review."))
        # Notes, chatter tracking, and other non-financial staging inputs do
        # not need a database row lock or repeated company/account scans. The
        # lock remains mandatory for anchors, lifecycle state, frozen inputs,
        # and accounting references.
        financial_fields = (
            set(self._FROZEN_FIELDS)
            | set(self._eh_guarded_fields)
            | set(self._REFERENCE_FIELDS)
            | {'reimbursement_partner_id'}
        )
        needs_lock = bool(set(vals) & financial_fields)
        if needs_lock:
            self._eh_lock_for_transition()
        if set(vals) & self._REFERENCE_FIELDS:
            # One validation after the lock is the authoritative TOCTOU-safe
            # check; the old pre-lock duplicate added no protection.
            self._eh_validate_references(vals)
        if 'reimbursement_partner_id' in vals:
            partner_id = vals.get('reimbursement_partner_id') or False
            posted_reimbursements = self.filtered(
                lambda provision: (
                    not provision.currency_id.is_zero(
                        provision.reimbursement_recognised
                    )
                    and provision.reimbursement_partner_id.id != partner_id
                )
            )
            if posted_reimbursements:
                raise UserError(_(
                    "The reimbursement counterparty is frozen while a "
                    "reimbursement asset remains recognised. Derecognise "
                    "that asset through the provision lifecycle before "
                    "changing its subledger identity."
                ))
        frozen = [f for f in self._FROZEN_FIELDS if f in vals]
        # action_remeasure books a change in estimate through the ledger and
        # then updates best_estimate under this flag; a raw edit of the inputs
        # in place stays blocked.
        if frozen and not (
                self.env.su
                and self.env.context.get('eh_provision_remeasure')):
            posted = self.filtered(
                lambda p: p.state in self._FROZEN_STATES)
            if posted:
                raise UserError(_(
                    "Measurement inputs (%(fields)s) are frozen on a posted "
                    "provision. Use Remeasure to book a change in estimate "
                    "through profit or loss, or reverse it (IAS 37.59).",
                    fields=', '.join(frozen)))
        # The state of a recognised provision is a control point: a raw ORM
        # write of {'state': 'reversed'/'settled'/'cancelled'} would relabel it
        # without the utilise / reverse / cancel journal entry the action
        # methods post, leaving the liability standing while the record reads
        # settled. Force the transition through the sanctioned actions, which
        # carry the flag after posting the GL movement.
        if 'state' in vals and not (
                self.env.su
                and self.env.context.get('eh_provision_state_change')):
            crossing = self.filtered(
                lambda p: p.state in self._FROZEN_STATES
                and p.state != vals['state'])
            if crossing:
                raise UserError(_(
                    "Change a recognised provision through its actions "
                    "(Utilise, Reverse or Cancel), which post the required "
                    "journal entry. Its state cannot be re-keyed directly."))
        return super().write(vals)

    def unlink(self):
        # A provision that has posted moves (recognised, then settled or
        # reversed) must not be deleted: the master carries the posting-move
        # link and deleting it would orphan a posted GL entry. Cancel it
        # instead. Draft and cancelled provisions have no posted move and
        # remain deletable.
        if not self.env.su:
            self._eh_check_access('unlink')
        self._eh_validate_references()
        self._eh_lock_for_transition()
        self._eh_validate_references()
        posted = self.filtered(lambda p: p.move_ids)
        if posted:
            raise UserError(_(
                "A recognised provision cannot be deleted; its journal "
                "entries would be orphaned. Reverse or utilise it and then "
                "cancel it instead."))
        return super().unlink()

    @api.constrains('classification', 'state', 'virtually_certain')
    def _check_contingent_not_recognised(self):
        for p in self:
            if p.classification != 'provision' \
                    and p.state in ('recognised', 'settled'):
                raise ValidationError(_(
                    "%s is a contingent item and cannot be recognised; it is "
                    "disclosure-only (IAS 37.27-28).", p.display_name))
            if p.state == 'recognised_asset' and (
                    p.classification != 'contingent_asset'
                    or not p.virtually_certain):
                raise ValidationError(_(
                    "%s: the Asset Recognised state is reserved for a "
                    "contingent asset whose inflow is virtually certain "
                    "(IAS 37.33).", p.display_name))

    # ---- actions ----

    def action_recognise(self, accounting_date=None):
        self = self._eh_prepare_action(['provision', 'expense'])
        accounting_date = self._resolve_action_accounting_date(
            accounting_date, _("recognition"))
        if self.classification != 'provision':
            raise UserError(_(
                "Contingent items are disclosed, not recognised."))
        if self.state != 'draft':
            raise UserError(_("Only draft provisions can be recognised."))
        if self.provision_type == 'onerous' and not self.onerous_override \
                and not self._onerous_inputs_set():
            raise UserError(_(
                "Measure the onerous contract first: enter the cost of "
                "fulfilling, the benefits expected and any exit penalty "
                "(IAS 37.66-68), or set the manual override with a "
                "documented reason."))
        if self.provision_type == 'restructuring':
            self._check_restructuring_gate()
        self._validate_accounts(['provision', 'expense'])
        self._align_settlement_schedule(accounting_date)
        amount = self.present_value
        if self.currency_id.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_("Nothing to recognise: present value is nil."))
        self._post_move([
            (self.expense_account_id, amount, 0.0,
             _("Provision expense %s", self.name)),
            (self.provision_account_id, 0.0, amount,
             _("Provision recognised %s", self.name)),
        ], accounting_date, 'recognition')
        self.write({
            'state': 'recognised', 'carrying_amount': amount,
            'recognition_date': accounting_date,
            'unwind_anchor_date': accounting_date,
            'last_unwind_date': accounting_date,
            'unwound_periods': 0,
        })
        return True

    def action_recognise_asset(self, accounting_date=None):
        """IAS 37.33-35: when the realisation of income is virtually
        certain, the related asset is not a contingent asset any more; it
        is recognised as an ASSET against income. The credit never touches
        a provision liability account. Without the flag the item stays
        disclosure-only and posting is refused."""
        self = self._eh_prepare_action(['asset', 'income'])
        accounting_date = self._resolve_action_accounting_date(
            accounting_date, _("contingent-asset recognition"))
        if self.classification != 'contingent_asset':
            raise UserError(_(
                "Only a contingent asset can be recognised as an asset "
                "(IAS 37.33)."))
        if self.state != 'draft':
            raise UserError(_(
                "Only a draft contingent asset can be recognised."))
        if not self.virtually_certain:
            raise UserError(_(
                "A contingent asset is disclosure-only until the inflow is "
                "virtually certain (IAS 37.31-33). Attest 'Inflow Virtually "
                "Certain' on %s before recognising the asset.",
                self.display_name))
        self._validate_accounts(['asset', 'income'])
        self._align_settlement_schedule(accounting_date)
        amount = self.present_value
        if self.currency_id.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_("Nothing to recognise: present value is nil."))
        self._post_move([
            (self.asset_account_id, amount, 0.0,
             _("Asset recognised (inflow virtually certain) %s", self.name)),
            (self.income_account_id, 0.0, amount,
             _("Income on asset recognition %s", self.name)),
        ], accounting_date, 'asset_recognition')
        self.write({
            'state': 'recognised_asset', 'carrying_amount': amount,
            'recognition_date': accounting_date,
        })
        return True

    def action_recognise_reimbursement(self, accounting_date=None):
        """IAS 37.53: a virtually certain reimbursement is recognised as a
        SEPARATE asset, capped at the provision carrying amount, and never
        netted against the provision liability. The credit goes to the
        provision expense account: that is the permitted NET presentation
        in profit or loss (IAS 37.54), while the statement of financial
        position keeps the asset and the liability gross."""
        # Elevate first: this action writes the guarded reimbursement_recognised
        # anchor, which the shared eh.workflow.guard only accepts under sudo.
        self = self._eh_prepare_action(['reimbursement', 'expense'])
        accounting_date = self._resolve_action_accounting_date(
            accounting_date, _("reimbursement recognition"))
        if self.classification != 'provision' or self.state != 'recognised':
            raise UserError(_(
                "A reimbursement asset is recognised against a recognised "
                "provision (IAS 37.53)."))
        self._validate_accounts(['reimbursement', 'expense'])
        if (
            self.reimbursement_account_id.account_type == 'asset_receivable'
            and not self.reimbursement_partner_id
        ):
            raise UserError(_(
                "Set the reimbursement counterparty before posting to a "
                "receivable account."
            ))
        currency = self.currency_id
        amount = currency.round(self.reimbursement_amount)
        if currency.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_(
                "Enter a positive reimbursement amount to recognise."))
        if currency.compare_amounts(
                self.reimbursement_recognised + amount,
                self.carrying_amount) > 0:
            raise UserError(_(
                "The reimbursement recognised (%(total).2f) cannot exceed "
                "the provision carrying amount of %(cap).2f (IAS 37.53).",
                total=self.reimbursement_recognised + amount,
                cap=self.carrying_amount))
        self._post_move([
            (self.reimbursement_account_id, amount, 0.0,
             _("Reimbursement asset %s", self.name),
             self.reimbursement_partner_id),
            (self.expense_account_id, 0.0, amount,
             _("Reimbursement (net P&L presentation) %s", self.name)),
        ], accounting_date, 'reimbursement')
        self.reimbursement_recognised += amount
        self.reimbursement_amount = 0.0
        return True

    def _reimbursement_release_legs(self, revised_provision_carrying):
        """Return the excess reimbursement and its gross derecognition legs.

        IAS 37.53 caps the separately presented reimbursement asset at the
        related provision.  When a remeasurement, utilisation, or reversal
        lowers that cap, release the excess in the same sealed journal entry
        as the liability movement so no committed state can contain an
        over-cap asset.
        """
        self.ensure_one()
        currency = self.currency_id
        cap = max(currency.round(revised_provision_carrying), 0.0)
        release = currency.round(
            max(self.reimbursement_recognised - cap, 0.0)
        )
        if currency.compare_amounts(release, 0.0) <= 0:
            return 0.0, []
        return release, [
            (self.expense_account_id, release, 0.0,
             _("Reimbursement asset released %s", self.name)),
            (self.reimbursement_account_id, 0.0, release,
             _("Reimbursement derecognised %s", self.name),
             self.reimbursement_partner_id),
        ]

    def _elapsed_unwind_periods(self, effective_date):
        """New whole periods that have fallen due and are not yet unwound.

        The unwind schedule runs from the latest measurement anchor to
        ``expected_settlement_date`` in annual calendar periods plus a final
        partial-year stub.
        The number of whole periods due by the explicit effective date is
        counted from the
        recognition date (drift-free), and the periods already recognised
        (``unwound_periods``) are subtracted, so a repeat click inside the
        same period returns zero and accretes nothing.

        Returns ``None`` only when no valid schedule can be derived.  A
        missing settlement date on a legacy record is still derivable through
        annual recognition anniversaries; there is deliberately no per-click
        fallback because that could book a whole period immediately.
        """
        self.ensure_one()
        boundaries = self._unwind_boundaries()
        if boundaries is None:
            return None
        effective_date = fields.Date.to_date(effective_date)
        # Whole periods due = number of boundaries that the effective date
        # has reached or passed. Boundaries exclude recognition itself.
        due = sum(1 for b in boundaries if b <= effective_date)
        remaining = (self.periods_to_settlement or 0) - self.unwound_periods
        return max(0, min(due - self.unwound_periods, remaining))

    def _unwind_boundaries(self):
        """Dates on which each unwind period falls due, or ``None``.

        Periods are annual recognition/remeasurement anniversaries plus a
        final stub ending on ``expected_settlement_date``. They are never
        arbitrary equal fractions selected by an operator.
        """
        self.ensure_one()
        n = self.periods_to_settlement or 0
        start = self.unwind_anchor_date or self.recognition_date
        end = self.expected_settlement_date
        if n <= 0 or not start:
            return None
        if not end:
            return [
                start + relativedelta(years=k)
                for k in range(1, n + 1)
            ]
        if end <= start:
            return None
        boundaries = []
        for period in range(1, n + 1):
            anniversary = start + relativedelta(years=period)
            boundary = min(anniversary, end)
            if not boundaries or boundary > boundaries[-1]:
                boundaries.append(boundary)
        if not boundaries or boundaries[-1] != end:
            boundaries.append(end)
        return boundaries

    def action_unwind(self, effective_date=None):
        # Elevate first: this action writes the guarded carrying_amount anchor,
        # which the shared eh.workflow.guard only accepts under sudo.
        self = self._eh_prepare_action(['provision', 'finance_cost'])
        effective_date = self._resolve_action_accounting_date(
            effective_date, _("discount unwind"))
        if self.state != 'recognised':
            raise UserError(_(
                "Unwinding applies to a recognised provision."))
        self._validate_accounts(['provision', 'finance_cost'])
        currency = self.currency_id
        rate = (self.discount_rate or 0.0) / 100.0
        if not rate:
            raise UserError(_(
                "No discount to unwind (the discount rate is nil)."))

        periods = self._elapsed_unwind_periods(effective_date)
        if periods is not None:
            # Time-based path: accrete one compounded step per whole period
            # that has fallen due since the last unwind. A repeat click in the
            # same period yields zero elapsed periods and is refused, so the
            # same period can never be unwound twice.
            if periods <= 0:
                raise UserError(_(
                    "No new period has fallen due since the last unwind of "
                    "%s. The discount unwinds as time passes; it cannot be "
                    "unwound again for a period already recognised.",
                    self.display_name))
            boundaries = self._unwind_boundaries()
            posting_dates = boundaries[
                self.unwound_periods:self.unwound_periods + periods]
        else:
            raise UserError(_(
                "Discount unwind for %(provision)s has no valid elapsed-time "
                "schedule. Set a positive period count and a settlement date "
                "after recognition; legacy schedules without that date use "
                "annual recognition anniversaries.",
                provision=self.display_name))

        # One sealed move per covered boundary. This preserves period-level
        # evidence during catch-up instead of collapsing several due periods
        # into one entry dated when the operator happened to click Unwind.
        carrying = self.carrying_amount
        postings = []
        total_periods = self.periods_to_settlement or periods
        for offset, posting_date in enumerate(posting_dates, start=1):
            period_index = self.unwound_periods + offset
            cap = currency.round(self.best_estimate - carrying)
            interest = min(currency.round(carrying * rate), cap)
            if currency.compare_amounts(interest, 0.0) <= 0:
                break
            label = _(
                "Unwinding of discount %(name)s — period "
                "%(period)s/%(total)s due %(date)s",
                name=self.name, period=period_index, total=total_periods,
                date=fields.Date.to_string(posting_date))
            postings.append((posting_date, interest, label))
            carrying = currency.round(carrying + interest)

        if not postings:
            raise UserError(_(
                "The provision already sits at the undiscounted estimate."))
        # Preflight every date before creating the first catch-up move. One
        # locked boundary therefore aborts the whole action without partial
        # posting, even when a caller catches the resulting UserError.
        for posting_date, _interest, _label in postings:
            self._require_exact_posting_date(posting_date)
        for posting_date, interest, label in postings:
            self._post_move([
                (self.finance_cost_account_id, interest, 0.0, label),
                (self.provision_account_id, 0.0, interest,
                 _("Provision accretion %(name)s due %(date)s",
                   name=self.name,
                   date=fields.Date.to_string(posting_date))),
            ], posting_date, 'unwind', date_prechecked=True)
        self.carrying_amount = carrying
        self.unwound_periods += len(postings)
        self.last_unwind_date = postings[-1][0]
        return True

    def action_remeasure(self, accounting_date=None):
        """IAS 37.59: at each reporting date a provision is reviewed and
        adjusted to the current best estimate. The change in the carrying
        amount is recognised in profit or loss (an increase against the
        original expense account, a decrease as a writeback), and the stored
        best estimate is updated so subsequent unwinding accretes toward the
        revised figure.
        """
        # Elevate first: this action writes the guarded carrying_amount anchor
        # (and best_estimate under the remeasure flag), which the shared
        # eh.workflow.guard only accepts under sudo.
        self.ensure_one()
        needed = ['provision', 'expense']
        if self.reimbursement_recognised:
            needed.append('reimbursement')
        self = self._eh_prepare_action(needed)
        accounting_date = self._resolve_action_accounting_date(
            accounting_date, _("remeasurement"))
        if self.state != 'recognised':
            raise UserError(_(
                "Only a recognised provision can be remeasured."))
        self._validate_accounts(needed)
        currency = self.currency_id
        onerous_engine = (self.provision_type == 'onerous'
                          and not self.onerous_override)
        restructuring_engine = self.provision_type == 'restructuring'
        if onerous_engine:
            # The onerous measure re-derives from the revised inputs staged
            # in the remeasure_* fields (stage the FULL revised set); the
            # free-keyed estimate is ignored so the measurement stays
            # mechanical (IAS 37.59, 37.68).
            missing_inputs = [
                self._fields[field_name].string
                for field_name in _ONEROUS_REMEASURE_STAGE_FLAGS
                if not self[
                    _ONEROUS_REMEASURE_STAGE_FLAGS[field_name]
                ]
            ]
            if missing_inputs:
                raise UserError(_(
                    "Stage the full revised onerous input set before "
                    "remeasurement. Explicitly enter zero where applicable. "
                    "Missing: %(inputs)s.",
                    inputs=', '.join(missing_inputs)))
            revised = self._onerous_measure(
                fulfil=self.remeasure_cost_fulfil,
                benefit=self.remeasure_benefit_expected,
                penalty=self.remeasure_penalty_exit)
        elif restructuring_engine:
            staged_lines = self.remeasure_restructuring_line_ids
            if not staged_lines:
                raise UserError(_(
                    "Stage the full revised restructuring component set "
                    "before remeasurement (IAS 37.72, 37.80-81)."))
            revised = currency.round(sum(
                staged_lines.filtered(
                    lambda line: line.cost_kind
                    in RESTRUCTURING_INCLUDED_KINDS
                ).mapped('amount')
            ))
            # Re-run both the continuing IAS 37.72 obligation gate and the
            # direct-cost scope/total gate against the staged current set.
            self._check_restructuring_gate(staged_lines, revised)
        else:
            revised = currency.round(self.remeasure_estimate)
        if currency.compare_amounts(revised, 0.0) < 0:
            raise UserError(_("The revised estimate cannot be negative."))

        settlement_date = (
            self.remeasure_expected_settlement_date
            or self.expected_settlement_date
        )
        if settlement_date and settlement_date < accounting_date:
            raise UserError(_(
                "The revised settlement date cannot precede the "
                "remeasurement date."))
        remaining = self._calendar_periods_between(
            accounting_date, settlement_date)
        if (
            self.discount_rate
            and remaining
            and not self.remeasure_discount_rate_staged
        ):
            raise UserError(_(
                "Enter the current discount rate explicitly before "
                "remeasuring a discounted provision (IAS 37.47). The frozen "
                "recognition rate is not reused automatically."))
        if self.remeasure_discount_rate_staged:
            revised_rate = self.remeasure_discount_rate
            if not self.remeasure_rate_basis or not self.remeasure_rate_source:
                raise UserError(_(
                    "Document the current rate basis and source before "
                    "remeasurement (IAS 37.47)."))
        else:
            revised_rate = 0.0
        if revised_rate and not settlement_date:
            raise UserError(_(
                "Set a revised or existing settlement date before applying a "
                "current discount rate."))
        rate = revised_rate / 100.0
        target = revised / ((1.0 + rate) ** remaining) \
            if rate and remaining else revised
        target = currency.round(target)
        delta = currency.round(target - self.carrying_amount)
        release, reimbursement_legs = self._reimbursement_release_legs(
            target,
        )
        if currency.is_zero(delta) and not release:
            raise UserError(_(
                "The revised estimate leaves the carrying amount unchanged."))
        if currency.is_zero(delta):
            legs = []
            event_type = 'remeasure_decrease'
        elif currency.compare_amounts(delta, 0.0) > 0:
            # Increase in the obligation: Dr expense / Cr provision.
            legs = [
                (self.expense_account_id, delta, 0.0,
                 _("Provision remeasurement (increase) %s", self.name)),
                (self.provision_account_id, 0.0, delta,
                 _("Provision remeasured up %s", self.name)),
            ]
            event_type = 'remeasure_increase'
        else:
            # Decrease in the obligation: writeback to the expense account.
            amount = -delta
            legs = [
                (self.provision_account_id, amount, 0.0,
                 _("Provision remeasured down %s", self.name)),
                (self.expense_account_id, 0.0, amount,
                 _("Provision remeasurement (decrease) %s", self.name)),
            ]
            event_type = 'remeasure_decrease'
        self._post_move(
            legs + reimbursement_legs, accounting_date, event_type)
        self.carrying_amount = target
        if release:
            self.reimbursement_recognised -= release
        vals = {
            'best_estimate': revised,
            'remeasure_estimate': 0.0,
            'discount_rate': revised_rate,
            'periods_to_settlement': remaining,
            'expected_settlement_date': settlement_date or False,
            'unwind_anchor_date': accounting_date,
            'last_unwind_date': accounting_date,
            'unwound_periods': 0,
            'remeasure_discount_rate': 0.0,
            'remeasure_discount_rate_staged': False,
            'remeasure_expected_settlement_date': False,
        }
        if self.remeasure_discount_rate_staged:
            vals.update({
                'rate_basis': self.remeasure_rate_basis,
                'rate_source': self.remeasure_rate_source,
            })
        vals.update({
            'remeasure_rate_basis': False,
            'remeasure_rate_source': False,
        })
        if onerous_engine:
            # Adopt the staged inputs as the new measurement inputs so the
            # register and the posted figure stay in lock-step, then clear
            # the staging fields.
            vals.update({
                'unavoidable_cost_fulfil': self.remeasure_cost_fulfil,
                'contract_benefit_expected': self.remeasure_benefit_expected,
                'penalty_exit': self.remeasure_penalty_exit,
                'remeasure_cost_fulfil': 0.0,
                'remeasure_benefit_expected': 0.0,
                'remeasure_penalty_exit': 0.0,
                'remeasure_cost_fulfil_staged': False,
                'remeasure_benefit_expected_staged': False,
                'remeasure_penalty_exit_staged': False,
            })
        if restructuring_engine:
            adopted_lines = [
                (0, 0, {
                    'name': line.name,
                    'cost_kind': line.cost_kind,
                    'amount': line.amount,
                })
                for line in self.remeasure_restructuring_line_ids
            ]
            vals.update({
                'restructuring_line_ids': [(5, 0, 0)] + adopted_lines,
                'remeasure_restructuring_line_ids': [(5, 0, 0)],
            })
        self.with_context(eh_provision_remeasure=True).write(vals)
        if currency.is_zero(target):
            self.with_context(
                eh_provision_state_change=True).state = 'reversed'
        return True

    def action_utilise(self, accounting_date=None):
        self.ensure_one()
        needed = ['provision', 'settlement']
        if self.reimbursement_recognised:
            needed.extend(['reimbursement', 'expense'])
        self = self._eh_prepare_action(needed)
        accounting_date = self._resolve_action_accounting_date(
            accounting_date, _("utilisation"))
        if self.state != 'recognised':
            raise UserError(_(
                "Only a recognised provision can be utilised."))
        currency = self.currency_id
        amount = currency.round(self.utilise_amount)
        if currency.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_("Enter a positive amount to utilise."))
        covered = min(amount, currency.round(self.carrying_amount))
        excess = currency.round(amount - covered)
        if currency.compare_amounts(excess, 0.0) > 0:
            needed.append('settlement_excess')
        self._validate_accounts(needed)
        if (
            self.settlement_account_id.account_type
            in ('liability_payable', 'asset_receivable')
            and not self.settlement_partner_id
        ):
            raise UserError(_(
                "Set the settlement counterparty before posting to a payable "
                "or receivable account."))
        revised_carrying = currency.round(self.carrying_amount - covered)
        release, reimbursement_legs = self._reimbursement_release_legs(
            revised_carrying,
        )
        legs = [
            (self.provision_account_id, covered, 0.0,
             _("Provision utilised %s", self.name)),
        ]
        if currency.compare_amounts(excess, 0.0) > 0:
            legs.append((
                self.settlement_excess_account_id, excess, 0.0,
                _("Settlement above provision carrying amount %s", self.name),
            ))
        legs.append((
            self.settlement_account_id, 0.0, amount,
            _("Settlement %s", self.name), self.settlement_partner_id,
        ))
        self._post_move(
            legs + reimbursement_legs, accounting_date, 'utilisation')
        self.carrying_amount = revised_carrying
        if release:
            self.reimbursement_recognised -= release
        self.utilised_amount += amount
        self.utilise_amount = 0.0
        if currency.is_zero(self.carrying_amount):
            self.with_context(eh_provision_state_change=True).state = 'settled'
        return True

    def action_reverse(self, accounting_date=None):
        self.ensure_one()
        needed = (['asset', 'income'] if self.state == 'recognised_asset'
                  else ['provision', 'expense'])
        if self.state == 'recognised' and self.reimbursement_recognised:
            needed.append('reimbursement')
        self = self._eh_prepare_action(needed)
        accounting_date = self._resolve_action_accounting_date(
            accounting_date, _("reversal"))
        if self.state == 'recognised_asset':
            # IAS 37.35: contingent assets are assessed continually; if the
            # inflow stops being virtually certain the recognised asset is
            # derecognised against the income that carried it.
            self._validate_accounts(['asset', 'income'])
            currency = self.currency_id
            amount = currency.round(self.carrying_amount)
            if currency.compare_amounts(amount, 0.0) <= 0:
                raise UserError(_(
                    "Nothing to reverse: the asset carrying amount is nil."))
            self._post_move([
                (self.income_account_id, amount, 0.0,
                 _("Asset recognition reversed %s", self.name)),
                (self.asset_account_id, 0.0, amount,
                 _("Asset derecognised %s", self.name)),
            ], accounting_date, 'asset_reversal')
            self.carrying_amount -= amount
            self.with_context(
                eh_provision_state_change=True).state = 'reversed'
            return True
        if self.state != 'recognised':
            raise UserError(_(
                "Only a recognised provision can be reversed."))
        self._validate_accounts(needed)
        currency = self.currency_id
        amount = currency.round(self.carrying_amount)
        if currency.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_(
                "Nothing to reverse: the provision carrying amount is nil."))
        # A provision no longer required is credited back to profit or loss
        # against the original expense account (IAS 37.59). Dr provision
        # liability / Cr expense for the unused carrying amount.
        release, reimbursement_legs = self._reimbursement_release_legs(0.0)
        self._post_move([
            (self.provision_account_id, amount, 0.0,
             _("Provision reversed %s", self.name)),
            (self.expense_account_id, 0.0, amount,
             _("Provision writeback %s", self.name)),
        ] + reimbursement_legs, accounting_date, 'reversal')
        self.carrying_amount -= amount
        if release:
            self.reimbursement_recognised -= release
        self.with_context(eh_provision_state_change=True).state = 'reversed'
        return True

    def action_cancel(self):
        for original in self:
            p = original._eh_prepare_action([])
            if p.state in ('recognised', 'recognised_asset') \
                    and not p.currency_id.is_zero(
                    p.carrying_amount):
                raise UserError(_(
                    "Reverse or utilise the carrying amount before "
                    "cancelling %s.", p.display_name))
            p.with_context(eh_provision_state_change=True).state = 'cancelled'

    def _eh_integrity_failures(self):
        """Return deterministic reasons the current journal graph is unsafe."""
        self.ensure_one()
        failures = []
        moves = self.move_ids
        if self.state == 'draft' and moves:
            failures.append(_("a draft provision has linked journal entries"))
        if (
            self.state in ('recognised', 'recognised_asset', 'settled',
                           'reversed')
            and not moves.filtered(lambda move: move.state == 'posted')
        ):
            failures.append(_("the posted lifecycle has no posted entry"))
        allowed_accounts = {
            account.id
            for account in (
                self.provision_account_id,
                self.expense_account_id,
                self.finance_cost_account_id,
                self.settlement_account_id,
                self.settlement_excess_account_id,
                self.asset_account_id,
                self.income_account_id,
                self.reimbursement_account_id,
            )
            if account
        }
        legacy_pairs = {
            (self.expense_account_id.id, self.provision_account_id.id),
            (self.finance_cost_account_id.id,
             self.provision_account_id.id),
            (self.provision_account_id.id, self.expense_account_id.id),
            (self.provision_account_id.id, self.settlement_account_id.id),
            (self.asset_account_id.id, self.income_account_id.id),
            (self.income_account_id.id, self.asset_account_id.id),
            (self.reimbursement_account_id.id,
             self.expense_account_id.id),
            (self.expense_account_id.id,
             self.reimbursement_account_id.id),
        }
        for move in moves:
            prefix = _("entry %s", move.display_name)
            if move.company_id != self.company_id:
                failures.append(_("%s is cross-company", prefix))
            if move.journal_id != self.journal_id:
                failures.append(_("%s uses another journal", prefix))
            if move.state != 'posted' or not move.eh_sealed \
                    or move.move_type != 'entry':
                failures.append(_("%s is not a posted sealed entry", prefix))
            if (move.ref or '') != (self.name or ''):
                failures.append(_("%s reference does not match", prefix))
            lines = move.line_ids.filtered(lambda line: line.debit or line.credit)
            debit = self.currency_id.round(sum(lines.mapped('debit')))
            credit = self.currency_id.round(sum(lines.mapped('credit')))
            if not lines or self.currency_id.compare_amounts(debit, credit):
                failures.append(_("%s is not balanced", prefix))
            if any(line.account_id.id not in allowed_accounts for line in lines):
                failures.append(_("%s contains an unapproved account", prefix))
            if not move.eh_provision_event:
                # Pre-event legacy entries are releasable only when their
                # exact two-line shape is one the engine historically emitted.
                debit_lines = lines.filtered(lambda line: line.debit > 0)
                credit_lines = lines.filtered(lambda line: line.credit > 0)
                pair = (
                    debit_lines.account_id.id if len(debit_lines) == 1 else 0,
                    credit_lines.account_id.id if len(credit_lines) == 1 else 0,
                )
                if len(lines) != 2 or pair not in legacy_pairs:
                    failures.append(_(
                        "%s has no verifiable engine movement shape", prefix))
        return failures

    def action_release_integrity_quarantine(self):
        """Manager-reviewed release after an independent current-byte check."""
        self.ensure_one()
        if not self.env.su:
            self._eh_check_access('write')
        self._check_manager()
        releasing_user = self.env.user
        self._eh_lock_for_transition()
        self._eh_validate_references()
        if not self.eh_integrity_review_required:
            raise UserError(_("This provision is not quarantined."))
        if not (self.eh_integrity_review_release_reason or '').strip():
            raise UserError(_(
                "Document the integrity review evidence before release."))
        failures = self._eh_integrity_failures()
        if failures:
            raise UserError(_(
                "Integrity quarantine cannot be released: %s",
                '; '.join(failures)))
        release_reason = self.eh_integrity_review_release_reason.strip()
        provision = self._eh_workflow_action()
        provision.write({
            'eh_integrity_review_required': False,
            'eh_integrity_review_reason': False,
            'eh_integrity_released_by_id': releasing_user.id,
            'eh_integrity_released_on': fields.Datetime.now(),
        })
        provision.message_post(body=_(
            "Integrity quarantine released by %(user)s after journal-graph "
            "revalidation. Review evidence: %(reason)s",
            user=releasing_user.display_name, reason=release_reason))
        return True

    def action_view_moves(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('eh_provision_id', '=', self.id)],
        }

    # ---- helpers ----

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can run provision lifecycle "
                "actions."))

    @api.model
    def _eh_assert_company_allowed(self, company_id):
        if (
            not self.env.su
            and company_id
            and int(company_id) not in self.env.companies.ids
        ):
            raise AccessError(_(
                "A provision may only use a company enabled in the caller's "
                "current company context."
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

    def _eh_validate_references(self, vals=None, needed=None):
        """Validate caller access and exact accounting-company ownership."""
        vals = vals or {}
        account_fields = (
            'provision_account_id', 'expense_account_id',
            'finance_cost_account_id', 'settlement_account_id',
            'settlement_excess_account_id',
            'asset_account_id', 'income_account_id',
            'reimbursement_account_id',
        )
        for provision in self:
            company_id = vals.get('company_id', provision.company_id.id)
            self._eh_assert_company_allowed(company_id)
            company = self.env['res.company'].browse(company_id)
            if not self.env.su:
                company._eh_check_access('read')
            journal_id = vals.get('journal_id', provision.journal_id.id)
            journal = self.env['account.journal'].browse(journal_id)
            if journal:
                if not self.env.su:
                    journal._eh_check_access('read')
                if journal.company_id != company:
                    raise ValidationError(_(
                        "The provision journal must belong to the provision "
                        "company."
                    ))
                if journal.type != 'general':
                    raise ValidationError(_(
                        "Provision postings require a general journal."
                    ))
            for field_name in account_fields:
                account_id = vals.get(field_name, provision[field_name].id)
                account = self.env['account.account'].browse(account_id)
                if not account:
                    continue
                if not self.env.su:
                    account._eh_check_access('read')
                if not self._eh_record_matches_company(account, company):
                    raise ValidationError(_(
                        "Provision posting accounts must be available to the "
                        "provision company."
                    ))
                allowed_types = _PROVISION_ACCOUNT_TYPES[field_name]
                if account.account_type not in allowed_types:
                    raise ValidationError(_(
                        "%(field)s has incompatible account type "
                        "%(account_type)s.",
                        field=provision._fields[field_name].string,
                        account_type=account.account_type,
                    ))
        return self

    def _eh_lock_for_transition(self):
        """Serialise every financial/lifecycle transition on one provision."""
        ids = tuple(sorted(set(self.ids)))
        if not ids:
            return self
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_provision WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            (ids,),
        )
        self.invalidate_recordset()
        return self

    def _eh_prepare_action(self, needed):
        """Check caller scope, lock, refresh, recheck, then elevate writes."""
        self.ensure_one()
        if not self.env.su:
            self._eh_check_access('write')
        self._check_manager()
        self._eh_validate_references(needed=needed)
        self._eh_lock_for_transition()
        self._eh_validate_references(needed=needed)
        if self.eh_integrity_review_required:
            raise UserError(_(
                "Provision %(provision)s is quarantined because its legacy "
                "journal provenance failed integrity review: %(reason)s",
                provision=self.display_name,
                reason=(self.eh_integrity_review_reason
                        or _("manual review required")),
            ))
        return self._eh_workflow_action()

    def _validate_accounts(self, needed):
        self.ensure_one()
        field_map = {
            'provision': ('provision_account_id', _("provision liability account")),
            'expense': ('expense_account_id', _("expense account")),
            'finance_cost': ('finance_cost_account_id', _("finance cost account")),
            'settlement': ('settlement_account_id', _("settlement account")),
            'settlement_excess': (
                'settlement_excess_account_id',
                _("settlement excess expense account"),
            ),
            'asset': ('asset_account_id', _("asset / receivable account")),
            'income': ('income_account_id', _("income account")),
            'reimbursement': ('reimbursement_account_id',
                              _("reimbursement asset account")),
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
                "Configure the %s on provision %s first.",
                ', '.join(missing), self.display_name))

    def _resolve_action_accounting_date(self, supplied_date, action_label):
        """Return explicit date selected for one posting action.

        ``context_today`` is deliberately not a fallback. Provision entries
        are accounting evidence, so a missing date must stop before journal
        creation instead of inheriting whichever date the worker process sees.
        """
        self.ensure_one()
        raw_date = supplied_date or self.action_accounting_date
        if not raw_date:
            raise UserError(_(
                "Set an explicit Accounting / Effective Date before "
                "%(action)s for %(provision)s.",
                action=action_label, provision=self.display_name))
        try:
            accounting_date = fields.Date.to_date(raw_date)
        except (TypeError, ValueError):
            raise UserError(_(
                "%(value)s is not a valid accounting date for %(action)s.",
                value=raw_date, action=action_label))
        if not accounting_date:
            raise UserError(_(
                "Set a valid Accounting / Effective Date before %(action)s.",
                action=action_label))
        if self.recognition_date and accounting_date < self.recognition_date:
            raise UserError(_(
                "%(action)s date %(date)s cannot precede recognition date "
                "%(recognition)s for %(provision)s.",
                action=action_label, date=accounting_date,
                recognition=self.recognition_date,
                provision=self.display_name))
        return accounting_date

    def _require_exact_posting_date(self, accounting_date):
        """Fail before Odoo can move a locked-date entry to an open period."""
        self.ensure_one()
        accounting_date = fields.Date.to_date(accounting_date)
        today = fields.Date.context_today(self)
        if accounting_date > today:
            raise UserError(_(
                "Provision %(provision)s cannot post on future accounting "
                "date %(date)s. Odoo would defer that move while the "
                "provision anchor changed immediately. Post the action on "
                "or after its explicit effective date instead.",
                provision=self.display_name, date=accounting_date))
        company = self.company_id
        checker = getattr(company, '_get_violated_lock_dates', None)
        if checker:
            try:
                lock_dates = checker(accounting_date, False, self.journal_id)
            except TypeError as journal_signature_error:
                try:
                    lock_dates = checker(accounting_date, False)
                except TypeError:
                    raise journal_signature_error
        else:
            # Older supported releases expose only the effective user fiscal
            # lock. Preserve importability while still failing closed there.
            fiscal_checker = getattr(
                company, '_get_user_fiscal_lock_date', None)
            if fiscal_checker:
                try:
                    fiscal_lock = fiscal_checker(self.journal_id)
                except TypeError:
                    fiscal_lock = fiscal_checker()
            else:
                candidates = [
                    company[field_name]
                    for field_name in (
                        'fiscalyear_lock_date', 'hard_lock_date')
                    if field_name in company._fields and company[field_name]
                ]
                fiscal_lock = max(candidates) if candidates else False
            lock_dates = (
                [(fiscal_lock, 'fiscalyear_lock_date')]
                if fiscal_lock and accounting_date <= fiscal_lock
                else []
            )
        if not lock_dates:
            return True
        formatter = getattr(company, '_format_lock_dates', None)
        lock_description = (
            formatter(lock_dates)
            if formatter
            else ', '.join(
                str(lock_date) for lock_date, _field in lock_dates)
        )
        raise UserError(_(
            "Provision %(provision)s requires exact accounting date "
            "%(date)s, but that date is sealed by %(locks)s. Odoo would "
            "otherwise move the entry to an open period. Obtain an "
            "authorised lock-date exception or choose an explicit open-period "
            "correction date.",
            provision=self.display_name, date=accounting_date,
            locks=lock_description))

    def _post_move(
            self, legs, accounting_date, event_type, date_prechecked=False):
        self.ensure_one()
        if event_type not in dict(_PROVISION_EVENT_TYPES):
            raise ValueError("Unknown provision event type: %s" % event_type)
        accounting_date = fields.Date.to_date(accounting_date)
        if not date_prechecked:
            self._require_exact_posting_date(accounting_date)
        lines = []
        for leg in legs:
            if len(leg) not in (4, 5):
                raise ValueError("Provision posting legs need 4 or 5 values")
            account, debit, credit, label = leg[:4]
            partner = leg[4] if len(leg) == 5 else False
            line_vals = {
                'name': label, 'account_id': account.id,
                'debit': debit, 'credit': credit,
            }
            if partner:
                line_vals['partner_id'] = partner.id
            lines.append((0, 0, line_vals))
        move = self.env['account.move'].with_context(
            eh_provision_move_link_capability=(
                _PROVISION_MOVE_LINK_CAPABILITY
            )
        )._eh_create_sealed({
            'move_type': 'entry',
            'company_id': self.company_id.id,
            'date': accounting_date,
            'journal_id': self.journal_id.id,
            'ref': self.name,
            'eh_provision_id': self.id,
            'eh_provision_event': event_type,
            'eh_sealed': True,
            'line_ids': lines,
        })
        move.action_post()
        if move.state != 'posted' or move.date != accounting_date \
                or move.eh_provision_id != self:
            raise UserError(_(
                "Provision posting provenance failed for %(provision)s: "
                "requested %(requested)s, journal entry %(move)s is "
                "%(state)s on %(actual)s. No provision anchor was updated.",
                provision=self.display_name, requested=accounting_date,
                move=move.display_name, state=move.state,
                actual=move.date))
        return move


class EhProvisionRestructuringLine(models.Model):
    _name = 'eh.provision.restructuring.line'
    _description = "Restructuring cost component (IAS 37.80-81)"
    _order = 'id'

    provision_id = fields.Many2one(
        'eh.provision', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='provision_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='provision_id.currency_id', store=True, readonly=True)
    name = fields.Char(required=True, string="Description")
    cost_kind = fields.Selection(
        RESTRUCTURING_COST_KINDS,
        required=True, default='termination', string="Cost Kind",
        help="Direct kinds enter the provision and must sum to the best "
             "estimate (IAS 37.80). Excluded kinds relate to the future "
             "conduct of the business and never enter the sum (IAS 37.81); "
             "they are registered here for completeness only.")
    in_scope = fields.Boolean(
        compute='_compute_in_scope', string="In Provision",
        help="Whether this component enters the recognised provision.")
    amount = fields.Monetary(
        currency_field='currency_id', required=True)

    _check_amount = models.Constraint(
        'CHECK (amount >= 0)',
        'A restructuring cost component cannot be negative.')

    @api.depends('cost_kind')
    def _compute_in_scope(self):
        for line in self:
            line.in_scope = line.cost_kind in RESTRUCTURING_INCLUDED_KINDS

    # The parent's measurement freezes when it posts; its component lines
    # feed that measurement, so they freeze with it at create, write and
    # unlink (a raw line edit would silently desync the recognised sum).
    def _check_parent_open(self, provisions=None):
        if (self.env.su
                and self.env.context.get('eh_provision_remeasure')):
            return
        provisions = (provisions if provisions is not None
                      else self.mapped('provision_id'))
        provisions = provisions.exists()
        if not self.env.su:
            provisions._eh_check_access('write')
        provisions._eh_validate_references()
        provisions._eh_lock_for_transition()
        provisions._eh_validate_references()
        frozen = provisions.filtered(
            lambda p: p.state in p._FROZEN_STATES)
        if frozen:
            raise UserError(_(
                "The restructuring components of a posted provision are "
                "frozen (%s). Use Remeasure to book a change in estimate.",
                ', '.join(frozen.mapped('display_name'))))

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and any(
                {'company_id', 'currency_id'} & set(vals)
                for vals in vals_list):
            raise AccessError(_(
                "Restructuring line company/currency derive from its "
                "provision and cannot be set directly."
            ))
        # Guard BEFORE the insert: a component added to a posted provision
        # must never reach the table.
        self._check_parent_open(self.env['eh.provision'].browse(
            [v['provision_id'] for v in vals_list if v.get('provision_id')]))
        return super().create(vals_list)

    def write(self, vals):
        if vals and not self.env.su:
            self._eh_check_access('write')
        if not self.env.su and {'company_id', 'currency_id'} & set(vals):
            raise AccessError(_(
                "Restructuring line company/currency derive from its "
                "provision and cannot be changed directly."
            ))
        provisions = self.mapped('provision_id')
        if vals.get('provision_id'):
            provisions |= self.env['eh.provision'].browse(
                vals['provision_id'],
            )
        self._check_parent_open(provisions)
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        self._check_parent_open()
        return super().unlink()


class EhProvisionRestructuringRemeasureLine(models.Model):
    _name = 'eh.provision.restructuring.remeasure.line'
    _description = "Staged restructuring remeasurement component"
    _order = 'id'

    provision_id = fields.Many2one(
        'eh.provision', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='provision_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='provision_id.currency_id', store=True, readonly=True)
    name = fields.Char(required=True, string="Description")
    cost_kind = fields.Selection(
        RESTRUCTURING_COST_KINDS,
        required=True, default='termination', string="Cost Kind")
    in_scope = fields.Boolean(compute='_compute_in_scope', string="In Provision")
    amount = fields.Monetary(currency_field='currency_id', required=True)

    _check_amount = models.Constraint(
        'CHECK (amount >= 0)',
        'A revised restructuring component cannot be negative.')

    @api.depends('cost_kind')
    def _compute_in_scope(self):
        for line in self:
            line.in_scope = line.cost_kind in RESTRUCTURING_INCLUDED_KINDS

    @api.constrains('provision_id')
    def _check_remeasurement_parent(self):
        for line in self:
            if line.provision_id.provision_type != 'restructuring' \
                    or line.provision_id.state != 'recognised':
                raise ValidationError(_(
                    "Revised restructuring components may only be staged on "
                    "a recognised restructuring provision."))

    @api.model_create_multi
    def create(self, vals_list):
        provisions = self.env['eh.provision'].browse([
            vals['provision_id'] for vals in vals_list
            if vals.get('provision_id')
        ])
        if not self.env.su:
            provisions._eh_check_access('write')
            provisions._check_manager()
        provisions._eh_validate_references()
        return super().create(vals_list)

    def write(self, vals):
        if vals and not self.env.su:
            self.mapped('provision_id')._eh_check_access('write')
            self.mapped('provision_id')._check_manager()
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            self.mapped('provision_id')._eh_check_access('write')
            self.mapped('provision_id')._check_manager()
        return super().unlink()


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_provision_id = fields.Many2one(
        'eh.provision', string="Provision", readonly=True, index=True,
        ondelete='restrict', copy=False)
    eh_provision_event = fields.Selection(
        _PROVISION_EVENT_TYPES, string="Provision Movement", readonly=True,
        index=True, copy=False,
        help="Engine-owned movement classification used by the IAS 37 "
             "roll-forward disclosure.")

    @api.model_create_multi
    def create(self, vals_list):
        if any(
            vals.get('eh_provision_id') or vals.get('eh_provision_event')
            for vals in vals_list
        ) and not (
            self.env.context.get('eh_provision_move_link_capability')
            is _PROVISION_MOVE_LINK_CAPABILITY
        ):
            raise AccessError(_(
                "Provision journal-entry provenance is engine-owned."
            ))
        return super().create(vals_list)

    def write(self, vals):
        if {'eh_provision_id', 'eh_provision_event'} & set(vals) and not (
            self.env.context.get('eh_provision_move_link_capability')
            is _PROVISION_MOVE_LINK_CAPABILITY
        ):
            raise AccessError(_(
                "Provision journal-entry provenance is immutable."
            ))
        return super().write(vals)
