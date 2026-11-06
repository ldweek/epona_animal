# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.sbp.plan: one IFRS 2 share-based payment plan and its grants.

Measurement mechanics implemented here (IFRS 2 as published):

* Equity-settled, service / non-market conditions (IFRS 2.19-20): the
  cumulative expense at any date is grant-date fair value x instruments
  expected to vest x vested fraction. The number expected to vest is the
  granted count net of the current forfeiture estimate and is trued up
  each period through the current period charge (change in estimate). A
  non-market performance condition that fails reverses the cumulative
  expense in full (IFRS 2.23 by contrast: only MARKET conditions stick).
* Market conditions (IFRS 2.21): the market condition is reflected in the
  grant-date fair value, so the expense is never trued up for the market
  outcome. Failure of the market condition with service completed leaves
  the cumulative expense standing; only service forfeitures adjust the
  count.
* Graded vesting (IFRS 2.IG11): each tranche is expensed over its OWN
  vesting period off its own grant-date fair value. Tranche instruments =
  expected-to-vest x tranche portion.
* Cash-settled (IFRS 2.30-33): the liability is remeasured to CURRENT
  fair value x vested fraction each period; the period charge is the
  liability movement. Settlement trues the liability to the settlement
  amount through expense, then pays it.
* Modifications (IFRS 2.27): only beneficial modifications add
  measurement: the incremental fair value granted is expensed over the
  remaining vesting period (immediately when modified after vesting). A
  modification that REDUCES fair value is ignored for measurement, which
  is why a negative incremental fair value is refused outright: the
  original grant-date expense continues unchanged.
* Cancellation (IFRS 2.28(a)): cancelling an equity plan accelerates the
  remaining unrecognised expense immediately.

Vested-fraction convention: calendar months elapsed from the grant date,
including the proportional current month, divided by the exact service
period. Exact anniversaries remain exact whole-month fractions. Cumulative
amounts are rounded once to company currency; the period charge is the
difference of rounded cumulatives.

Grantees are res.partner records: employees are partners in Odoo and this
keeps the module free of a hard hr dependency. Deployments with hr
installed simply pick the employee's partner (employee_id.work_contact_id
or address_home_id depending on series).
"""

import hashlib
import json
import math

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import float_compare


EH_SBP_ENGINE_CAPABILITY = object()
EH_SBP_ENGINE_CONTEXT_KEY = 'eh_sbp_engine_capability'
EH_SBP_PROVENANCE_VERSION = 1
EH_SBP_VALUATION_PROVENANCE_VERSION = 1

_SBP_ACCOUNT_TYPES = {
    'expense_account_id': frozenset({
        'expense', 'expense_direct_cost', 'expense_other',
    }),
    'equity_account_id': frozenset({'equity'}),
    'liability_account_id': frozenset({
        'liability_current', 'liability_non_current',
    }),
    'settlement_account_id': frozenset({
        'asset_cash', 'liability_payable', 'asset_current',
    }),
}


def _has_engine_capability(recordset):
    return (
        recordset.env.context.get(EH_SBP_ENGINE_CONTEXT_KEY)
        is EH_SBP_ENGINE_CAPABILITY
    )


def _lock_sbp_companies(
        recordset, companies, access_mode='read', require_allowed=True):
    """Authorize and lock company currency anchors deterministically.

    Company currency is part of every SBP monetary measurement.  Lock it
    before plan and valuation rows so a currency change cannot race an award
    activation, a valuation computation, or child-term creation.
    """
    companies = companies.exists()
    if not companies:
        return companies
    if not recordset.env.su:
        companies._eh_check_access(access_mode)
        foreign = companies.filtered(
            lambda company: company.id not in recordset.env.companies.ids
        ) if require_allowed else recordset.env['res.company']
        if require_allowed and foreign:
            raise AccessError(_(
                "The SBP company must be one of your allowed companies."
            ))
    companies.flush_recordset()
    recordset.env.cr.execute(
        "SELECT id FROM res_company WHERE id IN %s ORDER BY id FOR UPDATE",
        (tuple(sorted(companies.ids)),),
    )
    companies.invalidate_recordset(['currency_id'])
    return companies


def _lock_sbp_plans(recordset, plans, lock_companies=True):
    """Check actor access/company, then lock parent rows deterministically."""
    plans = plans.exists()
    if not plans:
        return plans
    if not recordset.env.su:
        plans._eh_check_access('write')
        foreign = plans.filtered(
            lambda plan: plan.company_id.id not in recordset.env.companies.ids
        )
        if foreign:
            raise AccessError(_(
                "The target SBP plan belongs to a company outside your "
                "allowed companies."
            ))
    if lock_companies:
        _lock_sbp_companies(recordset, plans.mapped('company_id'))
    plans.flush_recordset()
    recordset.env.cr.execute(
        "SELECT id FROM eh_sbp_plan WHERE id IN %s ORDER BY id FOR UPDATE",
        (tuple(plans.ids),),
    )
    plans.invalidate_recordset()
    return plans


def _reject_related_outputs(vals, names, label):
    blocked = set(vals).intersection(names)
    if blocked:
        raise AccessError(_(
            "%(label)s stored related outputs are server-owned: %(fields)s.",
            label=label,
            fields=', '.join(sorted(blocked)),
        ))


def _assert_finite_numeric_vals(vals, field_names, label):
    """Reject NaN/Infinity before Odoo's Float precision conversion.

    PostgreSQL comparisons and SQL CHECK constraints do not reject NaN, and
    Odoo may try to round a ``digits=`` Float before ``@api.constrains`` runs.
    All RPC/create/write accounting numbers therefore pass this common gate
    before reaching the ORM converter. ``False``/``None`` retain Odoo's usual
    numeric-clear semantics; required/range constraints validate them later.
    """
    invalid = []
    for field_name in set(vals).intersection(field_names):
        value = vals[field_name]
        if value is False or value is None:
            continue
        try:
            finite = math.isfinite(float(value))
        except (TypeError, ValueError, OverflowError):
            finite = False
        if not finite:
            invalid.append(field_name)
    if invalid:
        raise ValidationError(_(
            "%(label)s accounting numbers must be finite: %(fields)s.",
            label=label,
            fields=', '.join(sorted(invalid)),
        ))


def _assert_finite_record_fields(records, field_names, label):
    """Fail closed when legacy/direct-SQL non-finite values reach an action."""
    invalid = []
    for record in records:
        for field_name in field_names:
            value = record[field_name]
            try:
                finite = math.isfinite(float(value or 0.0))
            except (TypeError, ValueError, OverflowError):
                finite = False
            if not finite:
                invalid.append("%s.%s" % (record.display_name, field_name))
    if invalid:
        raise UserError(_(
            "%(label)s contains non-finite accounting evidence: %(fields)s. "
            "Correct or quarantine the source before continuing.",
            label=label,
            fields=', '.join(invalid),
        ))


def _fractional_months_between(start, end):
    """Calendar months including the current month's elapsed fraction.

    Exact grant-date anniversaries remain exact whole months, while a period
    end between anniversaries accrues the corresponding part of that calendar
    month.  This avoids the old staircase expense without introducing leap-day
    drift into one-, two- or three-year service periods.
    """
    if not start or not end or end <= start:
        return 0.0
    delta = relativedelta(end, start)
    whole = delta.years * 12 + delta.months
    anchor = start + relativedelta(months=whole)
    if anchor >= end:
        return float(whole)
    next_anchor = start + relativedelta(months=whole + 1)
    calendar_days = (next_anchor - anchor).days
    if calendar_days <= 0:
        return float(whole)
    return whole + (end - anchor).days / calendar_days


def _service_period_fraction(start, end, on_date):
    """Straight-line calendar-month fraction within an exact date range."""
    if not start or not end or end <= start:
        return 1.0
    if not on_date or on_date <= start:
        return 0.0
    if on_date >= end:
        return 1.0
    total = _fractional_months_between(start, end)
    if total <= 0.0:
        return 1.0
    return min(
        max(_fractional_months_between(start, on_date) / total, 0.0),
        1.0,
    )


class EhSbpPlan(models.Model):
    _name = 'eh.sbp.plan'
    _description = "Share-based payment plan (IFRS 2)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'grant_date desc, id desc'
    _rec_name = 'name'
    _check_company_auto = True

    # State moves only through Activate/Settle/Cancel (each posts a journal
    # entry), and recognised_cumulative is the cumulative-posted IFRS 2
    # expense / cash-settled liability anchor that the period-run engine trues
    # up against at post time. The mixin blocks any direct RPC write to either
    # field by a low-privilege user, closing both the
    # "write({'state': 'active'}) skips the action" bypass AND a silent
    # restatement of recognised_cumulative (which would double-charge the next
    # run's delta or suppress a legitimate true-up, and immediately corrupt the
    # IFRS 2.45 disclosure figure). The sanctioned actions and the run engine
    # write recognised_cumulative under su (self.sudo()).
    _eh_guarded_fields = ('state', 'recognised_cumulative')
    _eh_create_guarded_fields = ('state',)
    _SERVER_OWNED_FIELDS = frozenset({
        'currency_id', 'measurement_currency_id', 'vesting_end_date',
        'recognised_cumulative',
        'legacy_provenance_quarantined',
        'legacy_provenance_quarantine_note',
        'legacy_provenance_reviewed_at',
        'legacy_provenance_reviewed_by_id',
        'legacy_provenance_review_snapshot',
        'legacy_provenance_review_digest',
        'legacy_provenance_review_move_count',
        'legacy_provenance_review_unverified_move_count',
    })
    _LEGACY_REVIEW_INPUT_FIELDS = frozenset({
        'legacy_provenance_review_currency_id',
        'legacy_provenance_review_anchor',
        'legacy_provenance_review_anchor_confirmed',
        'legacy_provenance_review_note',
    })
    _NUMERIC_FIELDS = frozenset({
        'vesting_years', 'vesting_months', 'recognised_cumulative',
        'settlement_amount', 'cancellation_payment_amount',
        'cancellation_repurchase_fair_value',
        'legacy_provenance_review_anchor',
    })

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('active', "Active"),
         ('settled', "Settled"), ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    measurement_currency_id = fields.Many2one(
        'res.currency', readonly=True, copy=False, index=True,
        string="Approved Measurement Currency",
        help="Immutable currency identity stamped when the plan is activated. "
             "It prevents a later company-currency change from silently "
             "re-denominating frozen award terms and accounting evidence.")

    settlement = fields.Selection(
        [('equity', "Equity-settled"), ('cash', "Cash-settled")],
        default='equity', required=True, tracking=True,
        help="Equity-settled awards are measured once at grant-date fair "
             "value (IFRS 2.19); cash-settled awards are remeasured to "
             "current fair value every period until settled (IFRS 2.30).")
    condition_kind = fields.Selection(
        [('service', "Service condition"),
         ('non_market', "Non-market performance condition"),
         ('market', "Market condition")],
        default='service', required=True, tracking=True,
        help="Service and non-market conditions true up through the number "
             "of instruments expected to vest (IFRS 2.19-20); a failed "
             "non-market condition reverses the expense in full. A market "
             "condition is reflected in the grant-date fair value and its "
             "outcome is never trued up (IFRS 2.21): the expense stands "
             "once service is rendered.")
    instrument_desc = fields.Char(
        string="Instrument",
        help="Description of the instrument granted (e.g. options over "
             "ordinary shares, share appreciation rights).")

    grant_date = fields.Date(required=True, tracking=True,
                             default=fields.Date.context_today)
    vesting_years = fields.Integer(
        default=3, tracking=True,
        help="Vesting period in whole years (cliff plans). Ignored when "
             "graded vesting is on: each tranche carries its own end date.")
    vesting_months = fields.Integer(
        default=0, tracking=True,
        help="Additional vesting months on top of the vesting years.")
    graded_vesting = fields.Boolean(
        tracking=True,
        help="Each tranche vests on its own date and is expensed over its "
             "own vesting period off its own fair value (IFRS 2.IG11).")
    tranche_ids = fields.One2many(
        'eh.sbp.plan.tranche', 'plan_id', string="Vesting Tranches")
    vesting_end_date = fields.Date(
        compute='_compute_vesting_end_date', store=True,
        help="Final vesting date: grant date plus the vesting period, or "
             "the latest tranche end for graded plans.")

    grant_ids = fields.One2many('eh.sbp.grant', 'plan_id', string="Grants")
    modification_ids = fields.One2many(
        'eh.sbp.modification', 'plan_id', string="Modifications")
    run_ids = fields.One2many(
        'eh.sbp.period.run', 'plan_id', string="Period Runs")
    run_count = fields.Integer(compute='_compute_counts')

    recognised_cumulative = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        tracking=True,
        help="Cumulative expense recognised to date. For a cash-settled "
             "plan this is the liability carrying amount.")
    legacy_provenance_quarantined = fields.Boolean(
        readonly=True, copy=False,
        help="A legacy child/company/output anchor could not be proved during "
             "upgrade. Accounting actions remain blocked until controlled "
             "remediation.")
    legacy_provenance_quarantine_note = fields.Text(
        readonly=True, copy=False)
    legacy_provenance_review_currency_id = fields.Many2one(
        'res.currency', copy=False, string="Reviewed Measurement Currency",
        help="Currency proved from original award and valuation records. It "
             "must match current company currency before future accounting "
             "can resume; historical amounts are never converted here.")
    legacy_provenance_review_anchor = fields.Monetary(
        copy=False, currency_field='currency_id',
        string="Reviewed Cumulative Anchor",
        help="Manager-entered cumulative expense/liability independently "
             "agreed to historical runs and general ledger evidence.")
    legacy_provenance_review_anchor_confirmed = fields.Boolean(
        copy=False, string="Cumulative Anchor Agreed",
        help="Explicit confirmation that reviewed cumulative anchor was "
             "agreed to source and general-ledger evidence.")
    legacy_provenance_review_note = fields.Text(
        copy=False, string="Legacy Review Attestation",
        help="Manager explanation of source documents, valuation currency, "
             "period runs and general-ledger anchor reviewed.")
    legacy_provenance_reviewed_at = fields.Datetime(
        readonly=True, copy=False)
    legacy_provenance_reviewed_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False)
    legacy_provenance_review_snapshot = fields.Text(
        readonly=True, copy=False,
        help="Canonical JSON snapshot of quarantined source, result and "
             "ledger evidence at controlled release.")
    legacy_provenance_review_digest = fields.Char(
        readonly=True, copy=False, index=True,
        help="SHA-256 digest of the immutable legacy review snapshot.")
    legacy_provenance_review_move_count = fields.Integer(
        readonly=True, copy=False)
    legacy_provenance_review_unverified_move_count = fields.Integer(
        readonly=True, copy=False)
    settlement_amount = fields.Monetary(
        currency_field='currency_id',
        help="Cash paid to settle a cash-settled plan. Settle trues the "
             "liability to this amount through expense, then pays it "
             "(IFRS 2.30: the liability is remeasured to fair value at "
             "settlement).")
    cancellation_date = fields.Date(
        copy=False, tracking=True,
        help="Effective date used for cancellation acceleration and any "
             "cancellation payment. If left blank, the action stamps the "
             "current accounting date before posting.")
    settlement_date = fields.Date(
        copy=False, tracking=True,
        help="Effective date used for final vesting or cash settlement. If "
             "left blank, the action stamps the current accounting date "
             "before posting.")
    cancellation_payment_amount = fields.Monetary(
        currency_field='currency_id', copy=False, tracking=True,
        string="Cancellation Payment",
        help="Cash paid on cancellation. For an equity-settled award, the "
             "portion up to the repurchased instruments' fair value is a "
             "deduction from equity; any excess is an expense (IFRS "
             "2.28(b)).")
    cancellation_repurchase_fair_value = fields.Monetary(
        currency_field='currency_id', copy=False, tracking=True,
        string="Repurchased Award Fair Value",
        help="Fair value, at the cancellation date, of equity instruments "
             "repurchased by the cancellation payment (IFRS 2.28(b)).")

    # ---- accounts ----
    expense_account_id = fields.Many2one(
        'account.account', string="Expense Account", tracking=True,
        check_company=True,
        domain="[('account_type', 'in', "
               "['expense', 'expense_direct_cost', 'expense_other'])]")
    equity_account_id = fields.Many2one(
        'account.account', string="Equity Reserve Account", tracking=True,
        check_company=True,
        domain="[('account_type', '=', 'equity')]",
        help="Share-based payment reserve credited as equity-settled "
             "expense accrues (IFRS 2.7).")
    liability_account_id = fields.Many2one(
        'account.account', string="Liability Account", tracking=True,
        check_company=True,
        domain="[('account_type', 'in', "
               "['liability_current', 'liability_non_current'])]",
        help="Liability remeasured to current fair value each period for "
             "cash-settled awards (IFRS 2.30).")
    settlement_account_id = fields.Many2one(
        'account.account', string="Settlement Account", tracking=True,
        check_company=True,
        domain="[('account_type', 'in', "
               "['asset_cash', 'liability_payable', 'asset_current'])]",
        help="Credited when a cash-settled plan pays out.")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        check_company=True,
        domain="[('type', '=', 'general'), ('company_id', '=', company_id)]")

    move_ids = fields.One2many('account.move', 'eh_sbp_plan_id')
    move_count = fields.Integer(compute='_compute_counts')

    # ---- disclosure feed (IFRS 2.45) ----
    disclosure_date = fields.Date(
        default=fields.Date.context_today, tracking=True,
        help="Reporting date used to calculate remaining contractual life "
             "for the IFRS 2.45 option disclosure.")
    granted_total = fields.Integer(
        compute='_compute_disclosure', string="Granted")
    forfeited_total = fields.Integer(
        compute='_compute_disclosure', string="Forfeited")
    exercised_total = fields.Integer(
        compute='_compute_disclosure', string="Exercised")
    expired_total = fields.Integer(
        compute='_compute_disclosure', string="Expired")
    outstanding_total = fields.Integer(
        compute='_compute_disclosure', string="Outstanding")
    waep_outstanding = fields.Float(
        compute='_compute_disclosure', digits=(16, 4),
        string="WAEP (Outstanding)",
        help="Weighted average exercise price of the outstanding "
             "instruments that carry an exercise price (IFRS 2.45(b)).")
    exercisable_total = fields.Integer(
        compute='_compute_disclosure', string="Exercisable")
    waep_granted = fields.Float(
        compute='_compute_disclosure', digits=(16, 4),
        string="WAEP (Granted)")
    waep_forfeited = fields.Float(
        compute='_compute_disclosure', digits=(16, 4),
        string="WAEP (Forfeited)")
    waep_exercised = fields.Float(
        compute='_compute_disclosure', digits=(16, 4),
        string="WAEP (Exercised)")
    waep_expired = fields.Float(
        compute='_compute_disclosure', digits=(16, 4),
        string="WAEP (Expired)")
    waep_exercisable = fields.Float(
        compute='_compute_disclosure', digits=(16, 4),
        string="WAEP (Exercisable)")
    exercise_price_min = fields.Float(
        compute='_compute_disclosure', digits=(16, 4),
        string="Exercise Price (Low)")
    exercise_price_max = fields.Float(
        compute='_compute_disclosure', digits=(16, 4),
        string="Exercise Price (High)")
    wa_remaining_contractual_life_years = fields.Float(
        compute='_compute_disclosure', digits=(16, 4),
        string="WA Remaining Contractual Life (Years)")
    wa_share_price_at_exercise = fields.Float(
        compute='_compute_disclosure', digits=(16, 4),
        string="WA Share Price at Exercise")

    notes = fields.Text()

    _check_vesting = models.Constraint(
        'CHECK (vesting_years >= 0 AND vesting_months >= 0)',
        'The vesting period cannot be negative.')
    _check_cancellation_amounts = models.Constraint(
        'CHECK (cancellation_payment_amount >= 0 AND '
        'cancellation_repurchase_fair_value >= 0)',
        'Cancellation payment and repurchase fair value cannot be negative.')

    @api.constrains(
        'journal_id', 'expense_account_id', 'equity_account_id',
        'liability_account_id', 'settlement_account_id',
    )
    def _check_accounting_configuration(self):
        """Enforce every IFRS 2 posting classification in the ORM."""
        for plan in self:
            if plan.journal_id and plan.journal_id.type != 'general':
                raise ValidationError(_(
                    "Share-based-payment entries must use a general journal."
                ))
            for field_name, allowed_types in _SBP_ACCOUNT_TYPES.items():
                account = plan[field_name]
                if account and account.account_type not in allowed_types:
                    raise ValidationError(_(
                        "%(account)s has classification %(actual)s, which is "
                        "not permitted for %(field)s.",
                        account=account.display_name,
                        actual=account.account_type,
                        field=plan._fields[field_name].string,
                    ))

    @api.constrains(
        'vesting_years', 'vesting_months', 'recognised_cumulative',
        'settlement_amount', 'cancellation_payment_amount',
        'cancellation_repurchase_fair_value',
    )
    def _check_finite_plan_numbers(self):
        for plan in self:
            values = (
                plan.vesting_years, plan.vesting_months,
                plan.recognised_cumulative, plan.settlement_amount,
                plan.cancellation_payment_amount,
                plan.cancellation_repurchase_fair_value,
            )
            if any(not math.isfinite(float(value or 0.0))
                   for value in values):
                raise ValidationError(_(
                    "Every SBP plan accounting number must be finite."
                ))

    def _eh_assert_provenance_ready(self):
        self.ensure_one()
        if self.legacy_provenance_quarantined:
            raise UserError(_(
                "Legacy SBP child or posting provenance on %(plan)s could "
                "not be proved. Accounting actions are blocked pending "
                "controlled remediation. %(note)s",
                plan=self.display_name,
                note=self.legacy_provenance_quarantine_note or '',
            ))
        if self.state != 'draft' and (
                not self.measurement_currency_id
                or self.measurement_currency_id != self.company_id.currency_id
                or self.currency_id != self.measurement_currency_id):
            raise UserError(_(
                "The approved measurement currency of %(plan)s is missing or "
                "does not match its current company currency. Historical "
                "amounts cannot be re-denominated; review the plan before "
                "continuing.",
                plan=self.display_name,
            ))

    def _eh_assert_not_quarantined_for_edit(self):
        quarantined = self.filtered('legacy_provenance_quarantined')
        if quarantined:
            raise UserError(_(
                "Legacy SBP evidence on %(plans)s is frozen pending "
                "controlled manager review. Only review attestation fields "
                "may change before release.",
                plans=', '.join(quarantined.mapped('display_name')),
            ))
        return True

    # ------------------------------------------------------------------
    # computes
    # ------------------------------------------------------------------
    @api.depends('grant_date', 'vesting_years', 'vesting_months',
                 'graded_vesting', 'tranche_ids.vesting_end_date')
    def _compute_vesting_end_date(self):
        for plan in self:
            if plan.graded_vesting:
                ends = plan.tranche_ids.mapped('vesting_end_date')
                plan.vesting_end_date = max(ends) if ends else False
            elif plan.grant_date:
                plan.vesting_end_date = plan.grant_date + relativedelta(
                    years=plan.vesting_years or 0,
                    months=plan.vesting_months or 0)
            else:
                plan.vesting_end_date = False

    def _compute_counts(self):
        for plan in self:
            plan.move_count = len(plan.move_ids)
            plan.run_count = len(plan.run_ids)

    @api.depends('grant_ids.instruments_granted',
                 'grant_ids.forfeited_qty', 'grant_ids.exercised_qty',
                 'grant_ids.expired_qty', 'grant_ids.outstanding_qty',
                 'grant_ids.exercisable_qty', 'grant_ids.exercise_price',
                 'grant_ids.exercise_date_share_price',
                 'grant_ids.contractual_expiry_date', 'disclosure_date')
    def _compute_disclosure(self):
        for plan in self:
            grants = plan.grant_ids
            plan.granted_total = sum(grants.mapped('instruments_granted'))
            plan.forfeited_total = sum(grants.mapped('forfeited_qty'))
            plan.exercised_total = sum(grants.mapped('exercised_qty'))
            plan.expired_total = sum(grants.mapped('expired_qty'))
            plan.outstanding_total = sum(grants.mapped('outstanding_qty'))
            plan.exercisable_total = sum(grants.mapped('exercisable_qty'))

            def weighted_price(quantity_field):
                rows = grants.filtered(
                    lambda grant: grant.exercise_price > 0
                    and grant[quantity_field] > 0,
                )
                weight = sum(rows.mapped(quantity_field))
                return (
                    sum(grant.exercise_price * grant[quantity_field]
                        for grant in rows) / weight
                    if weight else 0.0
                )

            plan.waep_granted = weighted_price('instruments_granted')
            plan.waep_forfeited = weighted_price('forfeited_qty')
            plan.waep_exercised = weighted_price('exercised_qty')
            plan.waep_expired = weighted_price('expired_qty')
            plan.waep_outstanding = weighted_price('outstanding_qty')
            plan.waep_exercisable = weighted_price('exercisable_qty')

            priced_outstanding = grants.filtered(
                lambda grant: grant.exercise_price > 0
                and grant.outstanding_qty > 0,
            )
            prices = priced_outstanding.mapped('exercise_price')
            plan.exercise_price_min = min(prices) if prices else 0.0
            plan.exercise_price_max = max(prices) if prices else 0.0

            exercised = grants.filtered(
                lambda grant: grant.exercised_qty > 0
                and grant.exercise_date_share_price > 0,
            )
            exercised_weight = sum(exercised.mapped('exercised_qty'))
            plan.wa_share_price_at_exercise = (
                sum(grant.exercise_date_share_price * grant.exercised_qty
                    for grant in exercised) / exercised_weight
                if exercised_weight else 0.0
            )

            disclosure_date = fields.Date.to_date(plan.disclosure_date)
            lives = []
            if disclosure_date:
                for grant in grants.filtered(
                        lambda row: row.outstanding_qty > 0
                        and row.contractual_expiry_date):
                    remaining_days = max(
                        (grant.contractual_expiry_date - disclosure_date).days,
                        0,
                    )
                    lives.append((
                        grant.outstanding_qty,
                        remaining_days / 365.25,
                    ))
            life_weight = sum(weight for weight, _years in lives)
            plan.wa_remaining_contractual_life_years = (
                sum(weight * years for weight, years in lives) / life_weight
                if life_weight else 0.0
            )

    # ------------------------------------------------------------------
    # measurement engine
    # ------------------------------------------------------------------
    def _total_vesting_months(self):
        self.ensure_one()
        return (self.vesting_years or 0) * 12 + (self.vesting_months or 0)

    def _service_fraction(self, on_date):
        """Straight-line vested fraction for a cliff (non-graded) plan.

        Calendar months elapsed since grant over the exact service period,
        including a proportional current month and capped at one.
        """
        self.ensure_one()
        return _service_period_fraction(
            self.grant_date, self.vesting_end_date, on_date,
        )

    def _expected_to_vest(self):
        """Instruments currently expected to vest across all grants."""
        self.ensure_one()
        return sum(g._expected_to_vest() for g in self.grant_ids)

    def _modification_amount(self, on_date, full=False):
        """Cumulative incremental-FV expense of modifications dated on or
        before ``on_date`` (IFRS 2.27): incremental fair value x instruments
        expected to vest x elapsed fraction of the REMAINING vesting period.
        Graded awards allocate the incremental fair value to each tranche and
        use that tranche's own remaining service end. A modification after a
        tranche vested expenses that tranche immediately (fraction 1)."""
        self.ensure_one()
        expected = self._expected_to_vest()
        total = 0.0
        for mod in self.modification_ids.filtered(
                lambda row: row.modification_kind != 'equity_to_cash'):
            if not mod.date or mod.date > on_date:
                continue
            if full:
                total += expected * mod.incremental_fv
            elif self.graded_vesting:
                for tranche in self.tranche_ids:
                    total += (
                        expected
                        * (tranche.portion_pct / 100.0)
                        * mod.incremental_fv
                        * _service_period_fraction(
                            mod.date, tranche.vesting_end_date, on_date,
                        )
                    )
            else:
                total += (
                    expected * mod.incremental_fv
                    * _service_period_fraction(
                        mod.date, self.vesting_end_date, on_date,
                    )
                )
        return total

    def _equity_cumulative_at(self, on_date, full=False):
        """Cumulative equity-settled expense at ``on_date`` (IFRS 2.19-20).

        base   = sum over grants of expected-to-vest x grant-date FV x
                 vested fraction; graded plans split the expected count by
                 tranche portion and expense each tranche over its own
                 period off its own FV (IFRS 2.IG11).
        mods   = incremental FV spread over the remaining vesting period.
        ``full=True`` measures at fraction 1 everywhere (cancellation
        acceleration, IFRS 2.28(a)).

        Rounded ONCE to company currency at the end; period charges are
        differences of rounded cumulatives.
        """
        self.ensure_one()
        base = 0.0
        for grant in self.grant_ids:
            n = grant._expected_to_vest()
            if not n:
                continue
            if self.graded_vesting:
                for tranche in self.tranche_ids:
                    frac = 1.0 if full else tranche._vested_fraction(on_date)
                    base += (n * (tranche.portion_pct / 100.0)
                             * tranche.fair_value * frac)
            else:
                frac = 1.0 if full else self._service_fraction(on_date)
                base += n * grant.grant_date_fair_value * frac
        base += self._modification_amount(on_date, full=full)
        return self.currency_id.round(base)

    def _cash_liability_at(
            self, on_date, fair_value=0.0, tranche_values=None):
        """Cash-settled liability at ``on_date`` (IFRS 2.30-33): instruments
        expected to vest x CURRENT fair value x vested fraction. Graded
        plans apply each tranche's own vested fraction to its portion."""
        self.ensure_one()
        tranche_values = tranche_values or {}
        total = 0.0
        for grant in self.grant_ids:
            n = grant._expected_to_vest()
            if not n:
                continue
            if self.graded_vesting:
                for tranche in self.tranche_ids:
                    tranche_fair_value = tranche_values.get(tranche.id)
                    if tranche_fair_value is None:
                        raise UserError(_(
                            "Cash-settled graded plans require a current "
                            "fair value for every vesting tranche."
                        ))
                    total += (n * (tranche.portion_pct / 100.0)
                              * tranche_fair_value
                              * tranche._vested_fraction(on_date))
            else:
                total += n * fair_value * self._service_fraction(on_date)
        return self.currency_id.round(total)

    # ------------------------------------------------------------------
    # freeze / state guards
    # ------------------------------------------------------------------
    # Activation fixes the award measurement basis.  Waiting until the first
    # run posts would leave a gap in which an ordinary writer could substitute
    # the terms or posting accounts after manager approval but before the
    # manager posts the run. Estimates and outcomes stay updatable on grants;
    # those are explicit IFRS 2 true-up inputs, not changes to the award terms.
    _FROZEN_FIELDS = (
        'settlement', 'condition_kind', 'instrument_desc', 'grant_date',
        'vesting_years', 'vesting_months', 'graded_vesting',
        'expense_account_id', 'equity_account_id', 'liability_account_id',
        'settlement_account_id', 'journal_id', 'company_id', 'grant_ids',
        'tranche_ids',
    )
    _PLAIN_WRITE_FIELDS = frozenset({'notes', 'disclosure_date'})
    _TERMINAL_SOURCE_FIELDS = frozenset({
        'settlement_amount', 'settlement_date', 'cancellation_date',
        'cancellation_payment_amount',
        'cancellation_repurchase_fair_value',
    })

    @api.model
    def _eh_validate_company_value(self, company_id):
        company = self.env['res.company'].browse(company_id).exists()
        if not company:
            raise UserError(_("The selected company no longer exists."))
        if not self.env.su:
            company._eh_check_access('read')
            if company.id not in self.env.companies.ids:
                raise AccessError(_(
                    "A share-based-payment plan must belong to one of your "
                    "allowed companies."
                ))
        return company

    def _has_posted_run(self):
        self.ensure_one()
        return bool(self.run_ids.filtered(lambda r: r.state == 'posted'))

    @api.model_create_multi
    def create(self, vals_list):
        internal = _has_engine_capability(self)
        clean_vals_list = []
        for incoming in vals_list:
            vals = dict(incoming or {})
            blocked = self._SERVER_OWNED_FIELDS.intersection(vals)
            if blocked and not internal:
                raise AccessError(_(
                    "Plan measurement outputs are server-owned: %(fields)s.",
                    fields=', '.join(sorted(blocked)),
                ))
            review_inputs = self._LEGACY_REVIEW_INPUT_FIELDS.intersection(
                vals,
            )
            if review_inputs and not internal:
                raise AccessError(_(
                    "Legacy review inputs belong only to an upgrade-"
                    "quarantined plan: %(fields)s.",
                    fields=', '.join(sorted(review_inputs)),
                ))
            _assert_finite_numeric_vals(
                vals, self._NUMERIC_FIELDS, _("SBP plan"),
            )
            self._eh_validate_company_value(
                vals.get('company_id') or self.env.company.id,
            )
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.sbp.plan') or '/'
            clean_vals_list.append(vals)
        _lock_sbp_companies(
            self,
            self.env['res.company'].browse([
                vals.get('company_id') or self.env.company.id
                for vals in clean_vals_list
            ]),
        )
        plans = super().create(clean_vals_list)
        plans._check_company()
        return plans

    def write(self, vals):
        internal = _has_engine_capability(self)
        blocked = self._SERVER_OWNED_FIELDS.intersection(vals)
        if blocked and not internal:
            raise AccessError(_(
                "Plan measurement outputs are server-owned: %(fields)s.",
                fields=', '.join(sorted(blocked)),
            ))
        _assert_finite_numeric_vals(
            vals, self._NUMERIC_FIELDS, _("SBP plan"),
        )
        if not internal:
            _lock_sbp_plans(
                self,
                self,
                lock_companies=bool(
                    set(vals).difference(self._PLAIN_WRITE_FIELDS)
                ),
            )
            review_inputs = set(vals).intersection(
                self._LEGACY_REVIEW_INPUT_FIELDS,
            )
            quarantined = self.filtered('legacy_provenance_quarantined')
            if quarantined:
                if quarantined != self or set(vals) - review_inputs:
                    self._eh_assert_not_quarantined_for_edit()
                if not review_inputs:
                    self._eh_assert_not_quarantined_for_edit()
                if self.filtered('legacy_provenance_reviewed_at'):
                    raise AccessError(_(
                        "Legacy SBP review evidence is immutable after "
                        "controlled release."
                    ))
                self._check_manager()
                return super().write(vals)
            if review_inputs:
                raise AccessError(_(
                    "Legacy review inputs are writable only while a plan is "
                    "quarantined."
                ))
        if vals.get('company_id') and self.filtered(
            lambda plan: plan.company_id.id != vals['company_id']
        ):
            raise AccessError(_(
                "An SBP plan's company is immutable; create a plan in the "
                "other company instead."
            ))
        frozen = [f for f in self._FROZEN_FIELDS if f in vals]
        if frozen and not internal:
            locked = self.filtered(lambda p: p.state != 'draft')
            if locked:
                raise UserError(_(
                    "Measurement inputs (%(fields)s) are frozen when "
                    "%(plans)s is activated. Estimates true up through the "
                    "next period run; terms change only through a "
                    "Modification (IFRS 2.27).",
                    fields=', '.join(frozen),
                    plans=', '.join(locked.mapped('display_name'))))
        terminal_source = self._TERMINAL_SOURCE_FIELDS.intersection(vals)
        if terminal_source:
            terminal = self.filtered(
                lambda p: p.state in ('settled', 'cancelled')
            )
            if terminal:
                raise UserError(_(
                    "The terminal settlement/cancellation basis of "
                    "%(plans)s is immutable (%(fields)s).",
                    plans=', '.join(terminal.mapped('display_name')),
                    fields=', '.join(sorted(terminal_source)),
                ))
        # STATE transitions are enforced by the inherited eh.workflow.guard,
        # which blocks a non-superuser direct write; the sanctioned actions
        # (Activate, Settle, Cancel) run under sudo. No separate su-side arm is
        # needed - provenance is env.su, not a forgeable context key.
        result = super().write(vals)
        if set(vals).intersection({
            'company_id', 'expense_account_id', 'equity_account_id',
            'liability_account_id', 'settlement_account_id', 'journal_id',
        }):
            self._check_company()
        return result

    def unlink(self):
        _lock_sbp_plans(self, self)
        self._eh_assert_not_quarantined_for_edit()
        blocked = self.filtered(
            lambda p: p.move_ids or p.state in ('active', 'settled')
            or any(r.state == 'posted' for r in p.run_ids))
        if blocked:
            raise UserError(_(
                "A plan with posted entries or an active/settled plan "
                "cannot be deleted; cancel it instead."))
        return super().unlink()

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def _eh_lock_transition(self):
        """Authorize and serialize plan lifecycle/accounting actions."""
        self.ensure_one()
        return _lock_sbp_plans(self, self)

    def _eh_engine_write(self, vals):
        """Stamp server-owned accounting outputs after actor checks."""
        return self.sudo().with_context(**{
            EH_SBP_ENGINE_CONTEXT_KEY: EH_SBP_ENGINE_CAPABILITY,
        }).write(vals)

    def _eh_lock_and_validate_legacy_moves(self):
        """Freeze review scope and prove every linked GL row is immutable."""
        self.ensure_one()
        moves = (self.move_ids | self.run_ids.mapped('move_id')).sudo()
        if moves:
            moves.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM account_move WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(sorted(moves.ids)),),
            )
            moves.invalidate_recordset([
                'state', 'company_id', 'eh_sbp_plan_id', 'eh_sealed',
                'eh_legacy_unverified_seal',
                'eh_sbp_provenance_version',
            ])
        invalid = moves.filtered(lambda move: (
            move.state != 'posted'
            or move.company_id != self.company_id
            or move.eh_sbp_plan_id != self
            or not (
                move.eh_legacy_unverified_seal
                or (
                    move.eh_sealed
                    and move.eh_sbp_provenance_version
                    == EH_SBP_PROVENANCE_VERSION
                )
            )
        ))
        if invalid:
            raise UserError(_(
                "Legacy review found mutable, foreign or unproved journal "
                "entries on %(plan)s: %(moves)s. Quarantine cannot be "
                "released until upgrade-owned legacy seals freeze them.",
                plan=self.display_name,
                moves=', '.join(invalid.mapped('display_name')),
            ))
        return moves

    def _eh_validate_legacy_run_anchor(self, moves):
        """Reconcile stored run outputs and active anchor to linked GL."""
        self.ensure_one()
        currency = self.currency_id
        posted = self.run_ids.filtered(lambda run: run.state == 'posted')
        orphaned = self.run_ids.filtered(
            lambda run: (
                run.state == 'posted' and not run.move_id and not (
                    run.is_zero_charge
                    and currency.is_zero(run.period_charge)
                    and currency.compare_amounts(
                        run.prior_recognised,
                        run.cumulative_target,
                    ) == 0
                )
            ) or (
                run.state != 'posted' and bool(run.move_id)
            ),
        )
        if orphaned:
            raise UserError(_(
                "Legacy period-run state and journal links disagree on %s.",
                self.display_name,
            ))
        for run in posted:
            if run.move_id and run.move_id not in moves:
                raise UserError(_(
                    "Posted legacy run %s has no frozen plan-linked entry.",
                    run.display_name,
                ))
            expected = currency.round(
                run.prior_recognised + run.period_charge,
            )
            if currency.compare_amounts(
                    expected, run.cumulative_target) != 0:
                raise UserError(_(
                    "Legacy run %(run)s does not reconcile: prior plus "
                    "charge differs from cumulative target.",
                    run=run.display_name,
                ))
        ordered = posted.sorted(key=lambda run: (run.period_end, run.id))
        for previous, current in zip(ordered, ordered[1:]):
            if currency.compare_amounts(
                    previous.cumulative_target,
                    current.prior_recognised) != 0:
                raise UserError(_(
                    "Legacy run chain breaks between %(previous)s and "
                    "%(current)s.",
                    previous=previous.display_name,
                    current=current.display_name,
                ))
        if self.state == 'active':
            if ordered and currency.compare_amounts(
                    ordered[-1].cumulative_target,
                    self.recognised_cumulative) != 0:
                raise UserError(_(
                    "Last posted legacy run does not equal current "
                    "cumulative anchor on %s.",
                    self.display_name,
                ))
            counter = (
                self.equity_account_id
                if self.settlement == 'equity'
                else self.liability_account_id
            )
            if not counter and not currency.is_zero(
                    self.recognised_cumulative):
                raise UserError(_(
                    "Legacy cumulative anchor has no configured reserve or "
                    "liability account."
                ))
            ledger_anchor = sum(
                line.credit - line.debit
                for line in moves.mapped('line_ids')
                if line.account_id == counter
            ) if counter else 0.0
            if currency.compare_amounts(
                    ledger_anchor, self.recognised_cumulative) != 0:
                raise UserError(_(
                    "Legacy cumulative anchor %(anchor)s does not agree to "
                    "linked general-ledger balance %(ledger)s on %(account)s.",
                    anchor=self.recognised_cumulative,
                    ledger=ledger_anchor,
                    account=counter.display_name if counter else '-',
                ))
        return True

    def _eh_legacy_review_snapshot(self, moves, reviewed_at):
        """Return canonical pre-release source/result/ledger evidence."""
        self.ensure_one()

        def date_value(value):
            return str(value) if value else None

        grants = []
        for grant in self.grant_ids.sorted('id'):
            valuation = grant.valuation_id
            grants.append({
                'id': grant.id,
                'partner_id': grant.partner_id.id,
                'instruments_granted': grant.instruments_granted,
                'grant_date_fair_value': grant.grant_date_fair_value,
                'exercise_price': grant.exercise_price,
                'expected_forfeiture_pct': grant.expected_forfeiture_pct,
                'forfeited': grant.forfeited,
                'condition_failed': grant.condition_failed,
                'vesting_finalised': grant.vesting_finalised,
                'actual_vested_qty': grant.actual_vested_qty,
                'exercised_qty': grant.exercised_qty,
                'expired_qty': grant.expired_qty,
                'exercisable_qty': grant.exercisable_qty,
                'exercise_date_share_price':
                    grant.exercise_date_share_price,
                'contractual_expiry_date':
                    date_value(grant.contractual_expiry_date),
                'valuation': ({
                    'id': valuation.id,
                    'company_id': valuation.company_id.id,
                    'pricing_model': valuation.pricing_model,
                    'spot': valuation.spot,
                    'strike': valuation.strike,
                    'volatility_pct': valuation.volatility_pct,
                    'rate_pct': valuation.rate_pct,
                    'term_years': valuation.term_years,
                    'dividend_yield_pct': valuation.dividend_yield_pct,
                    'steps': valuation.steps,
                    'result_value': valuation.result_value,
                    'result_provenance_version':
                        valuation.result_provenance_version,
                    'result_currency_id': valuation.result_currency_id.id,
                    'legacy_result_value': valuation.legacy_result_value,
                    'legacy_result_provenance_version':
                        valuation.legacy_result_provenance_version,
                    'legacy_result_currency_id':
                        valuation.legacy_result_currency_id.id,
                    'legacy_result_unverified':
                        valuation.legacy_result_unverified,
                } if valuation else None),
            })
        payload = {
            'schema': 'eh.sbp.legacy-review.v1',
            'review': {
                'reviewed_at': date_value(reviewed_at),
                'reviewed_by_id': self.env.user.id,
                'currency_id': self.legacy_provenance_review_currency_id.id,
                'anchor': self.legacy_provenance_review_anchor,
                'anchor_confirmed':
                    self.legacy_provenance_review_anchor_confirmed,
                'attestation': ' '.join(
                    (self.legacy_provenance_review_note or '').split(),
                ),
            },
            'plan': {
                'id': self.id,
                'name': self.name,
                'state': self.state,
                'company_id': self.company_id.id,
                'currency_id': self.currency_id.id,
                'measurement_currency_id': self.measurement_currency_id.id,
                'settlement': self.settlement,
                'condition_kind': self.condition_kind,
                'grant_date': date_value(self.grant_date),
                'vesting_years': self.vesting_years,
                'vesting_months': self.vesting_months,
                'graded_vesting': self.graded_vesting,
                'vesting_end_date': date_value(self.vesting_end_date),
                'recognised_cumulative': self.recognised_cumulative,
                'settlement_amount': self.settlement_amount,
                'settlement_date': date_value(self.settlement_date),
                'cancellation_date': date_value(self.cancellation_date),
                'cancellation_payment_amount':
                    self.cancellation_payment_amount,
                'cancellation_repurchase_fair_value':
                    self.cancellation_repurchase_fair_value,
                'disclosure_date': date_value(self.disclosure_date),
                'expense_account_id': self.expense_account_id.id,
                'equity_account_id': self.equity_account_id.id,
                'liability_account_id': self.liability_account_id.id,
                'settlement_account_id': self.settlement_account_id.id,
                'journal_id': self.journal_id.id,
                'quarantine_note':
                    self.legacy_provenance_quarantine_note or '',
            },
            'grants': grants,
            'tranches': [{
                'id': tranche.id,
                'name': tranche.name,
                'portion_pct': tranche.portion_pct,
                'vesting_end_date': date_value(tranche.vesting_end_date),
                'fair_value': tranche.fair_value,
            } for tranche in self.tranche_ids.sorted('id')],
            'modifications': [{
                'id': modification.id,
                'name': modification.name,
                'date': date_value(modification.date),
                'modification_kind': modification.modification_kind,
                'replacement_award_reference':
                    modification.replacement_award_reference,
                'incremental_fv': modification.incremental_fv,
                'cash_fair_value': modification.cash_fair_value,
                'applied': modification.applied,
                'transfer_move_id': modification.transfer_move_id.id,
                'tranche_values': [{
                    'tranche_id': line.tranche_id.id,
                    'current_fair_value': line.current_fair_value,
                } for line in modification.tranche_value_ids.sorted(
                    'tranche_id',
                )],
            } for modification in self.modification_ids.sorted('id')],
            'runs': [{
                'id': run.id,
                'name': run.name,
                'state': run.state,
                'period_end': date_value(run.period_end),
                'current_fair_value': run.current_fair_value,
                'vested_fraction': run.vested_fraction,
                'cumulative_target': run.cumulative_target,
                'prior_recognised': run.prior_recognised,
                'period_charge': run.period_charge,
                'move_id': run.move_id.id,
                'is_zero_charge': run.is_zero_charge,
                'tranche_values': [{
                    'tranche_id': line.tranche_id.id,
                    'current_fair_value': line.current_fair_value,
                } for line in run.tranche_value_ids.sorted('tranche_id')],
            } for run in self.run_ids.sorted('id')],
            'moves': [{
                'id': move.id,
                'name': move.name,
                'date': date_value(move.date),
                'state': move.state,
                'journal_id': move.journal_id.id,
                'currency_id': move.currency_id.id,
                'eh_sealed': move.eh_sealed,
                'eh_legacy_unverified_seal':
                    move.eh_legacy_unverified_seal,
                'eh_sbp_provenance_version':
                    move.eh_sbp_provenance_version,
                'lines': [{
                    'id': line.id,
                    'account_id': line.account_id.id,
                    'partner_id': line.partner_id.id,
                    'debit': line.debit,
                    'credit': line.credit,
                    'balance': line.balance,
                    'currency_id': line.currency_id.id,
                    'amount_currency': line.amount_currency,
                } for line in move.line_ids.sorted('id')],
            } for move in moves.sorted('id')],
        }
        return json.dumps(
            payload, sort_keys=True, separators=(',', ':'), allow_nan=False,
        )

    def action_review_legacy_provenance(self):
        """Release future accounting without blessing legacy evidence."""
        self.ensure_one()
        self._eh_check_access('write')
        self._check_manager()
        self._eh_lock_transition()
        if not self.legacy_provenance_quarantined:
            raise UserError(_(
                "Plan %s does not require legacy provenance review.",
                self.display_name,
            ))
        note = ' '.join(
            (self.legacy_provenance_review_note or '').split(),
        )
        if len(note) < 20:
            raise UserError(_(
                "Record at least 20 characters identifying source, "
                "valuation and ledger evidence reviewed."
            ))
        if not self.legacy_provenance_review_currency_id:
            raise UserError(_(
                "Select measurement currency proved by original evidence."
            ))
        if self.legacy_provenance_review_currency_id \
                != self.company_id.currency_id:
            raise UserError(_(
                "Reviewed measurement currency must match current company "
                "currency. Historical amounts cannot be silently converted "
                "during quarantine release."
            ))
        if not self.legacy_provenance_review_anchor_confirmed:
            raise UserError(_(
                "Confirm cumulative anchor was independently agreed."
            ))
        if self.currency_id.compare_amounts(
                self.legacy_provenance_review_anchor,
                self.recognised_cumulative) != 0:
            raise UserError(_(
                "Reviewed cumulative anchor %(reviewed)s differs from stored "
                "anchor %(stored)s.",
                reviewed=self.legacy_provenance_review_anchor,
                stored=self.recognised_cumulative,
            ))
        self._eh_assert_finite_measurement_graph()
        moves = self._eh_lock_and_validate_legacy_moves()
        valuations = self.grant_ids.mapped('valuation_id')
        _assert_finite_record_fields(
            valuations,
            self.env['eh.sbp.valuation']._NUMERIC_FIELDS
            | {'legacy_result_value'},
            _("Legacy SBP valuation evidence"),
        )
        _assert_finite_record_fields(
            moves.mapped('line_ids'),
            {'debit', 'credit', 'balance', 'amount_currency'},
            _("Legacy SBP journal evidence"),
        )
        self._eh_validate_legacy_run_anchor(moves)
        reviewed_at = fields.Datetime.now()
        snapshot = self._eh_legacy_review_snapshot(moves, reviewed_at)
        digest = hashlib.sha256(snapshot.encode('utf-8')).hexdigest()
        unverified = moves.filtered('eh_legacy_unverified_seal')
        release_note = _(
            "Controlled legacy review completed %(date)s by %(user)s; "
            "snapshot SHA-256 %(digest)s. Historical unverified evidence "
            "remains unverified and frozen.",
            date=reviewed_at,
            user=self.env.user.display_name,
            digest=digest,
        )
        quarantine_note = '%s\n%s' % (
            self.legacy_provenance_quarantine_note or '', release_note,
        )
        self._eh_engine_write({
            'measurement_currency_id':
                self.legacy_provenance_review_currency_id.id,
            'legacy_provenance_quarantined': False,
            'legacy_provenance_quarantine_note': quarantine_note.strip(),
            'legacy_provenance_reviewed_at': reviewed_at,
            'legacy_provenance_reviewed_by_id': self.env.user.id,
            'legacy_provenance_review_snapshot': snapshot,
            'legacy_provenance_review_digest': digest,
            'legacy_provenance_review_move_count': len(moves),
            'legacy_provenance_review_unverified_move_count': len(unverified),
        })
        self.message_post(body=_(
            "Legacy SBP provenance review completed by %(user)s. Snapshot "
            "digest: %(digest)s. %(moves)s historical move(s) preserved; "
            "%(unverified)s remain permanently unverified. Attestation: "
            "%(note)s",
            user=self.env.user.display_name,
            digest=digest,
            moves=len(moves),
            unverified=len(unverified),
            note=note,
        ))
        return True

    def _eh_recompute_vesting_anchor(self):
        """Refresh the stored vesting date while the plan row is locked."""
        guarded = self.with_context(**{
            EH_SBP_ENGINE_CONTEXT_KEY: EH_SBP_ENGINE_CAPABILITY,
        })
        guarded._compute_vesting_end_date()
        guarded.flush_recordset(['vesting_end_date'])
        guarded.invalidate_recordset(['vesting_end_date'])
        return guarded.vesting_end_date

    def _eh_assert_linked_valuation_evidence(self):
        """Prove every linked option value still supports its grant amount.

        A blank valuation link is the explicit manual-fair-value path.  Once a
        valuation is linked, however, activation treats it as evidence and
        therefore requires the current server result and the copied grant
        amount to agree at company-currency precision.
        """
        self.ensure_one()
        rounding = self.currency_id.rounding
        if not math.isfinite(rounding) or rounding <= 0:
            raise UserError(_(
                "Plan company currency %(currency)s has no finite positive "
                "rounding. Linked valuation evidence cannot be approved.",
                currency=self.currency_id.display_name,
            ))
        for grant in self.grant_ids.filtered('valuation_id'):
            valuation = grant.valuation_id
            _assert_finite_record_fields(
                valuation,
                self.env['eh.sbp.valuation']._NUMERIC_FIELDS,
                _("Linked SBP valuation"),
            )
            if valuation.company_id != self.company_id \
                    or grant.company_id != self.company_id:
                raise AccessError(_(
                    "Grant %(grant)s may only rely on valuation %(valuation)s "
                    "when both belong to plan company %(company)s.",
                    grant=grant.display_name,
                    valuation=valuation.display_name,
                    company=self.company_id.display_name,
                ))
            if valuation.result_currency_id != self.company_id.currency_id \
                    or grant.currency_id != self.company_id.currency_id:
                raise UserError(_(
                    "Valuation %(valuation)s linked to grant %(grant)s was "
                    "not computed in the plan company's current currency. "
                    "Recompute it and use the valuation again before "
                    "activation.",
                    valuation=valuation.display_name,
                    grant=grant.display_name,
                ))
            if valuation.result_provenance_version \
                    != EH_SBP_VALUATION_PROVENANCE_VERSION:
                raise UserError(_(
                    "Valuation %(valuation)s linked to grant %(grant)s has no "
                    "current server provenance. Recompute it and use the "
                    "valuation on the grant again before activation.",
                    valuation=valuation.display_name,
                    grant=grant.display_name,
                ))
            if not math.isfinite(valuation.result_value) \
                    or not math.isfinite(grant.grant_date_fair_value):
                raise UserError(_(
                    "Grant %(grant)s and valuation %(valuation)s must carry "
                    "finite numeric fair values before activation.",
                    grant=grant.display_name,
                    valuation=valuation.display_name,
                ))
            if self.currency_id.compare_amounts(
                    valuation.result_value, 0.0) <= 0:
                raise UserError(_(
                    "Valuation %(valuation)s linked to grant %(grant)s has no "
                    "positive current result. Recompute it, or clear the "
                    "valuation link and document a manual fair value.",
                    valuation=valuation.display_name,
                    grant=grant.display_name,
                ))
            if self.currency_id.compare_amounts(
                    grant.grant_date_fair_value,
                    valuation.result_value,
            ) != 0:
                raise UserError(_(
                    "Grant %(grant)s carries fair value %(grant_value)s but "
                    "linked valuation %(valuation)s currently supports "
                    "%(valuation_value)s. Use the valuation again, or clear "
                    "the link and document the manual fair value, before "
                    "activation.",
                    grant=grant.display_name,
                    grant_value=grant.grant_date_fair_value,
                    valuation=valuation.display_name,
                    valuation_value=valuation.result_value,
                ))

    def _eh_assert_finite_measurement_graph(self):
        """Validate every numeric source that can feed this plan's GL.

        Normal CRUD is screened before field conversion.  This second gate is
        deliberately action-time and runs after the plan lock, so legacy data
        or direct SQL cannot smuggle NaN/Infinity into activation, a period
        measure, cancellation acceleration, or settlement.
        """
        for plan in self:
            _assert_finite_record_fields(
                plan, self._NUMERIC_FIELDS, _("SBP plan"),
            )
            _assert_finite_record_fields(
                plan.grant_ids,
                self.env['eh.sbp.grant']._NUMERIC_FIELDS,
                _("SBP grants"),
            )
            _assert_finite_record_fields(
                plan.tranche_ids,
                self.env['eh.sbp.plan.tranche']._NUMERIC_FIELDS,
                _("SBP tranches"),
            )
            _assert_finite_record_fields(
                plan.modification_ids,
                self.env['eh.sbp.modification']._NUMERIC_FIELDS,
                _("SBP modifications"),
            )
            _assert_finite_record_fields(
                plan.modification_ids.mapped('tranche_value_ids'),
                self.env[
                    'eh.sbp.modification.tranche.value'
                ]._NUMERIC_FIELDS,
                _("SBP classification tranche fair values"),
            )
            _assert_finite_record_fields(
                plan.run_ids,
                self.env['eh.sbp.period.run']._NUMERIC_FIELDS,
                _("SBP period runs"),
            )
            _assert_finite_record_fields(
                plan.run_ids.mapped('tranche_value_ids'),
                self.env['eh.sbp.period.run.tranche.value']._NUMERIC_FIELDS,
                _("SBP period-run tranche fair values"),
            )
            _assert_finite_record_fields(
                plan.currency_id, {'rounding'}, _("Plan currency"),
            )
            if plan.currency_id.rounding <= 0:
                raise UserError(_(
                    "Plan currency %s must have positive rounding.",
                    plan.currency_id.display_name,
                ))

    def _eh_post_equity_cumulative_delta(self, target, on_date, label):
        """True the posted equity reserve to ``target`` in either direction.

        Terminal actions must close the measurement gap themselves: once a
        plan is cancelled or settled, period runs can no longer repair an
        omitted expense or an over-recognised estimate.
        """
        self.ensure_one()
        self._validate_accounts(['expense', 'equity'])
        target = self.currency_id.round(target)
        delta = self.currency_id.round(
            target - self.recognised_cumulative,
        )
        if self.currency_id.compare_amounts(delta, 0.0) > 0:
            self._post_move([
                (self.expense_account_id, delta, 0.0,
                 _("%(label)s expense %(plan)s",
                   label=label, plan=self.name)),
                (self.equity_account_id, 0.0, delta,
                 _("%(label)s reserve %(plan)s",
                   label=label, plan=self.name)),
            ], date=on_date)
        elif self.currency_id.compare_amounts(delta, 0.0) < 0:
            amount = -delta
            self._post_move([
                (self.equity_account_id, amount, 0.0,
                 _("%(label)s reserve true-down %(plan)s",
                   label=label, plan=self.name)),
                (self.expense_account_id, 0.0, amount,
                 _("%(label)s expense reversal %(plan)s",
                   label=label, plan=self.name)),
            ], date=on_date)
        self._eh_engine_write({'recognised_cumulative': target})
        return delta

    def _eh_effective_action_date(self, field_name, label):
        """Return and durably stamp the explicit terminal accounting date."""
        self.ensure_one()
        effective_date = fields.Date.to_date(
            self[field_name] or fields.Date.context_today(self),
        )
        today = fields.Date.context_today(self)
        if effective_date < self.grant_date:
            raise UserError(_(
                "%(label)s date %(date)s cannot predate grant date %(grant)s.",
                label=label,
                date=effective_date,
                grant=self.grant_date,
            ))
        if effective_date > today:
            raise UserError(_(
                "%(label)s date %(date)s cannot be in the future.",
                label=label,
                date=effective_date,
            ))
        last_run_date = max(
            self.run_ids.filtered(
                lambda run: run.state == 'posted',
            ).mapped('period_end'),
            default=False,
        )
        if last_run_date and effective_date < last_run_date:
            raise UserError(_(
                "%(label)s date %(date)s cannot precede the last posted "
                "period run (%(last)s).",
                label=label,
                date=effective_date,
                last=last_run_date,
            ))
        if not self[field_name]:
            self._eh_engine_write({field_name: effective_date})
        return effective_date

    def _eh_post_cancellation_payment(self, on_date):
        """Post IFRS 2.28(b) equity repurchase and excess expense."""
        self.ensure_one()
        currency = self.currency_id
        payment = currency.round(self.cancellation_payment_amount)
        repurchase_fair_value = currency.round(
            self.cancellation_repurchase_fair_value,
        )
        if currency.is_zero(payment):
            return self.env['account.move']
        if self.settlement != 'equity':
            raise UserError(_(
                "Cancellation-payment repurchase accounting applies only "
                "to an equity-settled award; settle a cash liability instead."
            ))
        self._validate_accounts(['expense', 'equity', 'settlement'])
        equity_component = min(payment, repurchase_fair_value)
        excess_expense = currency.round(payment - equity_component)
        legs = []
        if not currency.is_zero(equity_component):
            legs.append((
                self.equity_account_id,
                equity_component,
                0.0,
                _("Cancellation equity repurchase %s", self.name),
            ))
        if not currency.is_zero(excess_expense):
            legs.append((
                self.expense_account_id,
                excess_expense,
                0.0,
                _("Cancellation payment excess %s", self.name),
            ))
        legs.append((
            self.settlement_account_id,
            0.0,
            payment,
            _("Cancellation payment %s", self.name),
        ))
        return self._post_move(legs, date=on_date)

    def action_activate(self):
        self.ensure_one()
        self._eh_check_access('write')
        self._check_manager()
        self._eh_lock_transition()
        if self.state != 'draft':
            raise UserError(_("Only a draft plan can be activated."))
        if not self.grant_ids:
            raise UserError(_(
                "Add at least one grant before activating %s.",
                self.display_name))
        # Lock referenced valuation evidence after the plan row, matching the
        # valuation-edit path's global plan -> valuation order.  Activation
        # cannot race a recompute or deletion of the result being approved.
        self.grant_ids.mapped('valuation_id')._eh_lock_valuations()
        self._eh_assert_finite_measurement_graph()
        self._eh_assert_linked_valuation_evidence()
        self._eh_assert_provenance_ready()
        self._eh_recompute_vesting_anchor()
        if self.settlement == 'equity':
            self._validate_accounts(['expense', 'equity'])
        else:
            self._validate_accounts(['expense', 'liability', 'settlement'])
        if self.graded_vesting:
            if not self.tranche_ids:
                raise UserError(_(
                    "A graded-vesting plan needs its tranche lines "
                    "(portion, vesting end date and fair value)."))
            total = sum(self.tranche_ids.mapped('portion_pct'))
            if float_compare(total, 100.0, precision_digits=2) != 0:
                raise UserError(_(
                    "Tranche portions must sum to 100%% "
                    "(currently %(total).4f%%).", total=total))
            for tranche in self.tranche_ids:
                if not tranche.vesting_end_date \
                        or tranche.vesting_end_date <= self.grant_date:
                    raise UserError(_(
                        "Every tranche needs a vesting end date after the "
                        "grant date."))
                if self.settlement == 'equity' and tranche.fair_value <= 0:
                    raise UserError(_(
                        "Every tranche of an equity-settled graded plan "
                        "needs its own grant-date fair value "
                        "(IFRS 2.IG11)."))
        else:
            if self._total_vesting_months() <= 0:
                raise UserError(_(
                    "Set a vesting period (years/months) before "
                    "activating."))
            if self.settlement == 'equity' and any(
                    g.grant_date_fair_value <= 0 for g in self.grant_ids):
                raise UserError(_(
                    "Every grant of an equity-settled plan needs a "
                    "grant-date fair value (IFRS 2.19). Use the valuation "
                    "helper or key it manually."))
        self._eh_engine_write({
            'measurement_currency_id': self.company_id.currency_id.id,
            'state': 'active',
        })
        return True

    def action_cancel(self):
        """IFRS 2.28(a): a cancellation during the vesting period is
        accounted for as an acceleration of vesting: the amount that would
        otherwise have been recognised over the remainder of the vesting
        period is recognised immediately. The full measure keeps the
        CURRENT forfeiture-adjusted expected count and all modification
        incremental fair value granted to date."""
        self.ensure_one()
        self._eh_check_access('write')
        self._check_manager()
        self._eh_lock_transition()
        with self.env.cr.savepoint():
            return self._eh_action_cancel_locked()

    def _eh_action_cancel_locked(self):
        """Execute the complete cancellation under one action savepoint."""
        self.ensure_one()
        self._eh_assert_provenance_ready()
        self._eh_assert_finite_measurement_graph()
        self._eh_recompute_vesting_anchor()
        cancellation_date = self._eh_effective_action_date(
            'cancellation_date', _("Cancellation"),
        )
        replacement = self.modification_ids.filtered(
            lambda modification:
                modification.modification_kind == 'replacement',
        )
        if replacement:
            raise UserError(_(
                "Replacement award %(replacement)s is an IFRS 2 "
                "modification, not a cancellation. Keep the original plan "
                "active and recognise its incremental fair value through "
                "period runs.",
                replacement=', '.join(replacement.mapped('display_name')),
            ))
        if self.state == 'draft':
            if not self.currency_id.is_zero(
                    self.cancellation_payment_amount):
                raise UserError(_(
                    "A draft award cannot carry a cancellation payment; "
                    "activate approved terms before accounting for a repurchase."
                ))
            self._eh_engine_write({
                'measurement_currency_id': self.company_id.currency_id.id,
                'state': 'cancelled',
            })
            return True
        if self.state != 'active':
            raise UserError(_("Only a draft or active plan can be "
                              "cancelled."))
        if self.settlement == 'cash':
            if not self.currency_id.is_zero(
                    self.cancellation_payment_amount):
                raise UserError(_(
                    "Use cash settlement, not equity-repurchase cancellation "
                    "payment fields, for a cash-settled award."
                ))
            if not self.currency_id.is_zero(self.recognised_cumulative):
                raise UserError(_(
                    "Settle the cash-settled liability before cancelling "
                    "%s.", self.display_name))
            self._eh_engine_write({'state': 'cancelled'})
            return True
        full = self._equity_cumulative_at(cancellation_date, full=True)
        self._eh_post_equity_cumulative_delta(
            full, cancellation_date, _("Cancellation"),
        )
        self._eh_post_cancellation_payment(cancellation_date)
        self._eh_engine_write({'state': 'cancelled'})
        return True

    def action_settle(self):
        self.ensure_one()
        self._eh_check_access('write')
        self._check_manager()
        self._eh_lock_transition()
        with self.env.cr.savepoint():
            return self._eh_action_settle_locked()

    def _eh_action_settle_locked(self):
        """Execute every settlement move and anchor update atomically."""
        self.ensure_one()
        self._eh_assert_provenance_ready()
        self._eh_assert_finite_measurement_graph()
        self._eh_recompute_vesting_anchor()
        if self.state != 'active':
            raise UserError(_("Only an active plan can be settled."))
        currency = self.currency_id
        settlement_date = self._eh_effective_action_date(
            'settlement_date', _("Settlement"),
        )
        if self.settlement == 'equity':
            # Exercise/share-issue mechanics move amounts WITHIN equity and
            # are entity-specific policy; the reserve stays in equity
            # (IFRS 2.23 does not require a transfer). The plan closes once
            # vesting has run its course.
            if self.vesting_end_date \
                    and settlement_date < self.vesting_end_date:
                raise UserError(_(
                    "An equity-settled plan is settled after its vesting "
                    "end (%s). Cancel it to accelerate instead.",
                    self.vesting_end_date))
            target = self._equity_cumulative_at(settlement_date, full=True)
            self._eh_post_equity_cumulative_delta(
                target, settlement_date, _("Final vesting"),
            )
            self._eh_engine_write({'state': 'settled'})
            return True
        # Cash-settled: remeasure the liability to the settlement amount
        # through expense (IFRS 2.30), then pay it.
        self._validate_accounts(['expense', 'liability', 'settlement'])
        amount = currency.round(self.settlement_amount)
        if currency.compare_amounts(amount, 0.0) < 0:
            raise UserError(_("The settlement amount cannot be negative."))
        delta = currency.round(amount - self.recognised_cumulative)
        if not currency.is_zero(delta):
            if currency.compare_amounts(delta, 0.0) > 0:
                self._post_move([
                    (self.expense_account_id, delta, 0.0,
                     _("Settlement remeasurement %s", self.name)),
                    (self.liability_account_id, 0.0, delta,
                     _("SAR liability true-up %s", self.name)),
                ], date=settlement_date)
            else:
                self._post_move([
                    (self.liability_account_id, -delta, 0.0,
                     _("SAR liability true-down %s", self.name)),
                    (self.expense_account_id, 0.0, -delta,
                     _("Settlement remeasurement %s", self.name)),
                ], date=settlement_date)
        if currency.compare_amounts(amount, 0.0) > 0:
            self._post_move([
                (self.liability_account_id, amount, 0.0,
                 _("SAR liability settled %s", self.name)),
                (self.settlement_account_id, 0.0, amount,
                 _("Settlement paid %s", self.name)),
            ], date=settlement_date)
        # recognised_cumulative is guarded (see _eh_guarded_fields); the
        # sanctioned settle action writes it under su.  Keep settlement_amount
        # as immutable terminal source evidence: it is the exact basis of both
        # sealed settlement entries and must remain independently reproducible.
        self._eh_engine_write({'recognised_cumulative': 0.0})
        self._eh_engine_write({'state': 'settled'})
        return True

    def action_view_moves(self):
        self.ensure_one()
        self._eh_check_access('read')
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('eh_sbp_plan_id', '=', self.id)],
        }

    def action_view_runs(self):
        self.ensure_one()
        self._eh_check_access('read')
        return {
            'type': 'ir.actions.act_window',
            'name': _("Period Runs"),
            'res_model': 'eh.sbp.period.run',
            'view_mode': 'list,form',
            'domain': [('plan_id', '=', self.id)],
            'context': {'default_plan_id': self.id},
        }

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can post share-based "
                "payment entries."))

    def _validate_accounts(self, needed):
        self.ensure_one()
        self._eh_assert_provenance_ready()
        self._check_accounting_configuration()
        field_map = {
            'expense': ('expense_account_id', _("expense account")),
            'equity': ('equity_account_id', _("equity reserve account")),
            'liability': ('liability_account_id', _("liability account")),
            'settlement': ('settlement_account_id',
                           _("settlement account")),
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
                "Configure the %s on plan %s first.",
                ', '.join(missing), self.display_name))
        self._check_company()
        if self.journal_id.company_id != self.company_id:
            raise UserError(_(
                "The SBP journal must belong to the plan company."
            ))
        if not self.env.su:
            self.journal_id._eh_check_access('read')
        for key in needed:
            account = self[field_map[key][0]]
            if not self.env.su:
                account._eh_check_access('read')
            if 'company_ids' in account._fields:
                wrong_company = self.company_id not in account.company_ids
            else:
                wrong_company = (
                    account.company_id and account.company_id != self.company_id
                )
            if wrong_company:
                raise UserError(_(
                    "Every SBP account must be available to the plan company."
                ))

    @api.private
    def _post_move(self, legs, date=None, ref=None):
        self.ensure_one()
        self._eh_check_access('write')
        self._check_manager()
        self._eh_assert_provenance_ready()
        self._check_accounting_configuration()
        if not self.journal_id or any(not leg[0] for leg in legs):
            raise UserError(_(
                "Configure the journal and every account used by the SBP "
                "entry before posting."
            ))
        if self.journal_id.company_id != self.company_id:
            raise UserError(_("The SBP journal must match the plan company."))
        requested_date = fields.Date.to_date(
            date or fields.Date.context_today(self)
        )
        checker = getattr(self.company_id, '_get_violated_lock_dates', None)
        if checker:
            try:
                violated = checker(requested_date, False, self.journal_id)
            except TypeError as journal_signature_error:
                try:
                    violated = checker(requested_date, False)
                except TypeError:
                    raise journal_signature_error
        else:
            fiscal_checker = self.company_id._get_user_fiscal_lock_date
            try:
                fiscal_lock = fiscal_checker(self.journal_id)
            except TypeError as journal_signature_error:
                try:
                    fiscal_lock = fiscal_checker()
                except TypeError:
                    raise journal_signature_error
            violated = (
                [(fiscal_lock, 'fiscalyear_lock_date')]
                if fiscal_lock and requested_date <= fiscal_lock else []
            )
        if violated:
            raise UserError(_(
                "Share-based-payment accounting date %(date)s is locked. "
                "Use a new explicit open-period run; silent redating would "
                "break IFRS 2 subledger chronology.",
                date=requested_date,
            ))
        if not self.env.su:
            self.journal_id._eh_check_access('read')
        accounts = self.env['account.account']
        for leg in legs:
            accounts |= leg[0]
        for account in accounts:
            if not self.env.su:
                account._eh_check_access('read')
            if 'company_ids' in account._fields:
                wrong_company = self.company_id not in account.company_ids
            else:
                wrong_company = (
                    account.company_id and account.company_id != self.company_id
                )
            if wrong_company:
                raise UserError(_(
                    "Every SBP entry account must be available to the plan "
                    "company."
                ))
            configured_types = set()
            for field_name, allowed_types in _SBP_ACCOUNT_TYPES.items():
                if self[field_name] == account:
                    configured_types.update(allowed_types)
            if not configured_types or account.account_type not in configured_types:
                raise UserError(_(
                    "Every SBP journal leg must use one of the plan's "
                    "classified posting accounts."
                ))
        lines = []
        for account, debit, credit, label in legs:
            lines.append((0, 0, {
                'name': label, 'account_id': account.id,
                'debit': debit, 'credit': credit,
            }))
        with self.env.cr.savepoint():
            move = self.env['account.move'].with_context(**{
                EH_SBP_ENGINE_CONTEXT_KEY: EH_SBP_ENGINE_CAPABILITY,
            })._eh_create_sealed({
                'move_type': 'entry',
                'date': requested_date,
                'journal_id': self.journal_id.id,
                'ref': ref or self.name,
                'eh_sbp_plan_id': self.id,
                'eh_sbp_provenance_version': EH_SBP_PROVENANCE_VERSION,
                'eh_sealed': True,
                'line_ids': lines,
            })
            move.action_post()
            move.invalidate_recordset([
                'state', 'date', 'eh_sealed', 'eh_sbp_plan_id',
                'eh_sbp_provenance_version', 'reversed_entry_id',
            ])
        if (
            move.company_id != self.company_id
            or move.journal_id != self.journal_id
            or move.date != requested_date
            or move.eh_sbp_plan_id != self
            or move.eh_sbp_provenance_version
               != EH_SBP_PROVENANCE_VERSION
            or move.reversed_entry_id
            or move.state != 'posted'
            or not move.eh_sealed
        ):
            raise UserError(_(
                "The generated SBP journal entry failed durable engine "
                "date, seal, or provenance validation."
            ))
        return move


class EhSbpPlanTranche(models.Model):
    _name = 'eh.sbp.plan.tranche'
    _description = "Graded-vesting tranche (IFRS 2.IG11)"
    _order = 'vesting_end_date, id'
    _check_company_auto = True
    _NUMERIC_FIELDS = frozenset({'portion_pct', 'fair_value'})

    plan_id = fields.Many2one(
        'eh.sbp.plan', required=True, ondelete='cascade', index=True,
        check_company=True)
    company_id = fields.Many2one(
        related='plan_id.company_id', store=True, index=True)
    name = fields.Char(required=True, string="Description",
                       default="Tranche")
    portion_pct = fields.Float(
        digits=(16, 6), required=True, string="Portion %",
        help="Share of each grant's instruments vesting in this tranche. "
             "Portions must sum to 100.")
    vesting_end_date = fields.Date(
        required=True,
        help="This tranche's own vesting end; it is expensed straight-line "
             "from the grant date to this date (IFRS 2.IG11).")
    fair_value = fields.Float(
        digits=(16, 4), string="Fair Value / Instrument",
        help="Grant-date fair value per instrument for this tranche. "
             "Longer-vesting tranches usually carry a different fair value "
             "(IFRS 2.IG11). Used for equity-settled plans; cash-settled "
             "plans remeasure at the run's current fair value.")

    _check_portion = models.Constraint(
        'CHECK (portion_pct > 0)', 'A tranche portion must be positive.')
    _check_fv = models.Constraint(
        'CHECK (fair_value >= 0)',
        'A tranche fair value cannot be negative.')

    @api.constrains('portion_pct', 'fair_value')
    def _check_finite_tranche_values(self):
        for tranche in self:
            if not math.isfinite(tranche.portion_pct) \
                    or not math.isfinite(tranche.fair_value):
                raise ValidationError(_(
                    "Tranche portion and fair value must be finite numbers."
                ))

    def _vested_fraction(self, on_date):
        """Straight-line fraction through this tranche's exact service end."""
        self.ensure_one()
        return _service_period_fraction(
            self.plan_id.grant_date, self.vesting_end_date, on_date,
        )

    # Tranches are approved award terms and freeze at activation: guard create,
    # write AND unlink so no post-approval topology substitution is possible.
    def _check_plan_open(self, plans=None):
        plans = plans if plans is not None else self.mapped('plan_id')
        locked = plans.filtered(lambda p: p.state != 'draft')
        if locked:
            raise UserError(_(
                "The tranches of %s are frozen once the plan is activated.",
                ', '.join(locked.mapped('display_name'))))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            _reject_related_outputs(
                vals, {'company_id'}, _("SBP tranche"),
            )
            _assert_finite_numeric_vals(
                vals, self._NUMERIC_FIELDS, _("SBP tranche"),
            )
        plans = _lock_sbp_plans(
            self,
            self.env['eh.sbp.plan'].browse(
                [v['plan_id'] for v in vals_list if v.get('plan_id')],
            ),
        )
        self._check_plan_open(plans)
        records = super().create(vals_list)
        records._check_company()
        return records

    def write(self, vals):
        _reject_related_outputs(vals, {'company_id'}, _("SBP tranche"))
        _assert_finite_numeric_vals(
            vals, self._NUMERIC_FIELDS, _("SBP tranche"),
        )
        plans = self.mapped('plan_id')
        if vals.get('plan_id'):
            plans |= self.env['eh.sbp.plan'].browse(vals['plan_id'])
        plans = _lock_sbp_plans(self, plans)
        self._check_plan_open(plans)
        result = super().write(vals)
        self._check_company()
        return result

    def unlink(self):
        plans = _lock_sbp_plans(self, self.mapped('plan_id'))
        self._check_plan_open(plans)
        return super().unlink()


class EhSbpGrant(models.Model):
    _name = 'eh.sbp.grant'
    _description = "Share-based payment grant (IFRS 2)"
    _order = 'id'
    _rec_name = 'partner_id'
    _check_company_auto = True
    _NUMERIC_FIELDS = frozenset({
        'instruments_granted', 'grant_date_fair_value', 'exercise_price',
        'expected_forfeiture_pct', 'actual_vested_qty', 'exercised_qty',
        'expired_qty', 'exercisable_qty', 'exercise_date_share_price',
    })

    plan_id = fields.Many2one(
        'eh.sbp.plan', required=True, ondelete='cascade', index=True,
        check_company=True)
    company_id = fields.Many2one(
        related='plan_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='plan_id.currency_id', store=True, readonly=True)
    partner_id = fields.Many2one(
        'res.partner', required=True, string="Grantee", check_company=True,
        help="The grantee as a partner. Employees are partners in Odoo, so "
             "this avoids a hard hr dependency; with hr installed, pick "
             "the employee's related partner.")
    instruments_granted = fields.Integer(required=True)
    grant_date_fair_value = fields.Float(
        digits=(16, 4), string="Grant-date FV / Instrument",
        help="Fair value per instrument at grant date (IFRS 2.19). Market "
             "conditions are baked into this value (IFRS 2.21). Fill from "
             "a valuation record or key it manually. Ignored for graded "
             "plans (each tranche carries its own FV) and for cash-settled "
             "plans (remeasured at each run).")
    exercise_price = fields.Float(
        digits=(16, 4), string="Exercise Price",
        help="Strike per instrument, used for the WAEP disclosure "
             "(IFRS 2.45(b)). Zero for free shares / SARs.")
    valuation_id = fields.Many2one(
        'eh.sbp.valuation', string="Valuation", check_company=True,
        help="Option valuation whose result can be pulled into the "
             "grant-date fair value.")

    expected_forfeiture_pct = fields.Float(
        digits=(6, 3), string="Expected Forfeiture %",
        help="Current estimate of the portion NOT expected to vest for "
             "service/non-market reasons. Updatable while vesting runs; "
             "the change flows through the next period run (IFRS 2.20).")
    forfeited = fields.Boolean(
        help="The grantee left before vesting (service condition failed): "
             "the grant's cumulative expense reverses through the next "
             "run.")
    condition_failed = fields.Boolean(
        string="Performance Condition Failed",
        help="A failed NON-MARKET condition reverses the expense in full. "
             "A failed MARKET condition changes nothing: the expense "
             "stands once service is rendered (IFRS 2.21 vs 2.23 - the "
             "market outcome was priced into the grant-date fair value).")
    vesting_finalised = fields.Boolean(
        help="Vesting has completed and the actual outcome below replaces "
             "the estimate (final true-up). For market-condition plans the "
             "actual count is the instruments whose SERVICE condition was "
             "met, whatever the market outcome (IFRS 2.21).")
    actual_vested_qty = fields.Integer(
        string="Actual Vested",
        help="Instruments that actually vested (service rendered). Used "
             "once vesting is finalised.")
    exercised_qty = fields.Integer(
        string="Exercised",
        help="Instruments exercised / settled to date (disclosure "
             "rollforward, IFRS 2.45).")
    expired_qty = fields.Integer(
        string="Expired",
        help="Vested instruments that lapsed unexercised (disclosure "
             "rollforward, IFRS 2.45).")
    exercisable_qty = fields.Integer(
        string="Exercisable",
        help="Outstanding instruments exercisable at the plan disclosure "
             "date (IFRS 2.45(b)(vii)).")
    exercise_date_share_price = fields.Float(
        digits=(16, 4), string="WA Share Price at Exercise",
        help="Weighted average market price of the underlying shares on "
             "exercise dates for this grant during the reporting period "
             "(IFRS 2.45(c)).")
    contractual_expiry_date = fields.Date(
        string="Contractual Expiry",
        help="Contractual option expiry used with the plan disclosure date "
             "to calculate weighted average remaining life (IFRS 2.45(d)).")

    forfeited_qty = fields.Integer(
        compute='_compute_rollforward', string="Forfeited (actual)")
    outstanding_qty = fields.Integer(
        compute='_compute_rollforward', string="Outstanding")

    notes = fields.Char()

    _check_granted = models.Constraint(
        'CHECK (instruments_granted > 0)',
        'A grant must carry a positive number of instruments.')
    _check_fv = models.Constraint(
        'CHECK (grant_date_fair_value >= 0 AND exercise_price >= 0)',
        'Fair value and exercise price cannot be negative.')
    _check_quantities = models.Constraint(
        'CHECK (actual_vested_qty >= 0 AND exercised_qty >= 0 '
        'AND expired_qty >= 0 AND exercisable_qty >= 0)',
        'Grant quantities cannot be negative.')
    _check_exercise_share_price = models.Constraint(
        'CHECK (exercise_date_share_price >= 0)',
        'The exercise-date share price cannot be negative.')

    @api.constrains('expected_forfeiture_pct')
    def _check_forfeiture_range(self):
        for grant in self:
            if not 0.0 <= grant.expected_forfeiture_pct <= 100.0:
                raise ValidationError(_(
                    "The expected forfeiture must lie between 0 and 100%%."))

    @api.constrains(
        'instruments_granted', 'grant_date_fair_value', 'exercise_price',
        'expected_forfeiture_pct', 'actual_vested_qty', 'exercised_qty',
        'expired_qty', 'exercisable_qty', 'exercise_date_share_price',
    )
    def _check_finite_award_values(self):
        for grant in self:
            values = (
                grant.instruments_granted, grant.grant_date_fair_value,
                grant.exercise_price, grant.expected_forfeiture_pct,
                grant.actual_vested_qty, grant.exercised_qty,
                grant.expired_qty, grant.exercisable_qty,
                grant.exercise_date_share_price,
            )
            if any(not math.isfinite(float(value or 0.0))
                   for value in values):
                raise ValidationError(_(
                    "Every grant quantity, fair value, price and estimate "
                    "must be a finite number."
                ))

    @api.constrains('actual_vested_qty', 'instruments_granted',
                    'exercised_qty', 'expired_qty', 'exercisable_qty',
                    'forfeited', 'condition_failed', 'vesting_finalised',
                    'plan_id.condition_kind')
    def _check_quantity_caps(self):
        for grant in self:
            if grant.actual_vested_qty > grant.instruments_granted:
                raise ValidationError(_(
                    "Actual vested instruments cannot exceed the granted "
                    "count."))
            if grant.forfeited or (
                    grant.condition_failed
                    and grant.plan_id.condition_kind == 'non_market'):
                available = 0
            elif grant.vesting_finalised:
                available = grant.actual_vested_qty
            else:
                available = grant.instruments_granted
            if grant.exercised_qty + grant.expired_qty > available:
                raise ValidationError(_(
                    "Exercised plus expired instruments cannot exceed the "
                    "vested instruments available for exercise."))
            outstanding = max(
                available - grant.exercised_qty - grant.expired_qty, 0,
            )
            if grant.exercisable_qty > outstanding:
                raise ValidationError(_(
                    "Exercisable instruments cannot exceed vested "
                    "instruments still outstanding."
                ))

    def _expected_to_vest(self):
        """Instruments expected to vest for measurement (IFRS 2.19-21).

        * service forfeiture (grantee left): zero, always trued up;
        * failed non-market condition: zero (full reversal, the IFRS 2.23
          contrast case);
        * failed MARKET condition: NO adjustment (IFRS 2.21) - the flag is
          recorded for the register but the count ignores it;
        * finalised: the actual vested count (for market plans: the count
          that completed service);
        * otherwise: granted x (1 - current forfeiture estimate).
        """
        self.ensure_one()
        plan = self.plan_id
        if self.condition_failed and plan.condition_kind == 'non_market':
            return 0.0
        if self.vesting_finalised:
            return float(self.actual_vested_qty)
        if self.forfeited:
            return 0.0
        return self.instruments_granted * (
            1.0 - (self.expected_forfeiture_pct or 0.0) / 100.0)

    @api.depends('instruments_granted', 'forfeited', 'condition_failed',
                 'vesting_finalised', 'actual_vested_qty', 'exercised_qty',
                 'expired_qty', 'plan_id.condition_kind')
    def _compute_rollforward(self):
        for grant in self:
            if grant.condition_failed \
                    and grant.plan_id.condition_kind == 'non_market':
                forfeited = grant.instruments_granted
            elif grant.vesting_finalised:
                forfeited = max(
                    grant.instruments_granted - grant.actual_vested_qty, 0)
            elif grant.forfeited:
                forfeited = grant.instruments_granted
            else:
                forfeited = 0
            grant.forfeited_qty = forfeited
            grant.outstanding_qty = max(
                grant.instruments_granted - forfeited
                - grant.exercised_qty - grant.expired_qty, 0)

    def action_use_valuation(self):
        plans = _lock_sbp_plans(self, self.mapped('plan_id'))
        plans._check_manager()
        self._check_plan_open(plans)
        self.mapped('valuation_id')._eh_lock_valuations()
        for grant in self:
            if not grant.valuation_id:
                raise UserError(_(
                    "Link a valuation record first, then pull its result."))
            if not math.isfinite(grant.valuation_id.result_value):
                raise UserError(_(
                    "Valuation %s has a non-finite result and must be "
                    "recomputed from finite inputs.",
                    grant.valuation_id.display_name,
                ))
            if grant.currency_id.compare_amounts(
                    grant.valuation_id.result_value, 0.0) <= 0:
                raise UserError(_(
                    "Compute a positive valuation %s first.",
                    grant.valuation_id.display_name))
            if grant.valuation_id.result_provenance_version \
                    != EH_SBP_VALUATION_PROVENANCE_VERSION:
                raise UserError(_(
                    "Valuation %s carries an unproved legacy result. "
                    "Recompute it with the current server engine before "
                    "using it on a grant.",
                    grant.valuation_id.display_name,
                ))
            if not self.env.su:
                grant._eh_check_access('write')
                grant.valuation_id._eh_check_access('read')
            if grant.valuation_id.company_id != grant.company_id:
                raise AccessError(_(
                    "A grant may only use a valuation from its own company."
                ))
            if grant.valuation_id.result_currency_id != grant.company_id.currency_id \
                    or grant.currency_id != grant.company_id.currency_id:
                raise UserError(_(
                    "Valuation %s was computed in a different currency. "
                    "Recompute it after the company-currency change before "
                    "using its result.",
                    grant.valuation_id.display_name,
                ))
            grant.grant_date_fair_value = grant.valuation_id.result_value
        return True

    # Award terms and grant topology freeze at activation. Estimate/outcome
    # fields stay open because IFRS 2.20 trues them up through the next period.
    _POST_EDITABLE = (
        'expected_forfeiture_pct', 'forfeited', 'condition_failed',
        'vesting_finalised', 'actual_vested_qty', 'exercised_qty',
        'expired_qty', 'exercisable_qty', 'exercise_date_share_price',
        'contractual_expiry_date', 'notes',
    )
    _TERMINAL_EDITABLE = (
        'exercised_qty', 'expired_qty', 'exercisable_qty',
        'exercise_date_share_price', 'contractual_expiry_date', 'notes',
    )

    def _check_plan_open(self, plans=None):
        plans = plans if plans is not None else self.mapped('plan_id')
        locked = plans.filtered(lambda p: p.state != 'draft')
        if locked:
            raise UserError(_(
                "Grants of %s are frozen once the plan is activated: "
                "only the vesting estimates and outcomes stay updatable "
                "(IFRS 2.20). A new award is a new plan.",
                ', '.join(locked.mapped('display_name'))))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            _reject_related_outputs(
                vals, {'company_id', 'currency_id'}, _("SBP grant"),
            )
            _assert_finite_numeric_vals(
                vals, self._NUMERIC_FIELDS, _("SBP grant"),
            )
        plans = _lock_sbp_plans(
            self,
            self.env['eh.sbp.plan'].browse(
                [v['plan_id'] for v in vals_list if v.get('plan_id')],
            ),
        )
        self._check_plan_open(plans)
        records = super().create(vals_list)
        records._check_company()
        return records

    def write(self, vals):
        _reject_related_outputs(
            vals, {'company_id', 'currency_id'}, _("SBP grant"),
        )
        _assert_finite_numeric_vals(
            vals, self._NUMERIC_FIELDS, _("SBP grant"),
        )
        plans = self.mapped('plan_id')
        if vals.get('plan_id'):
            plans |= self.env['eh.sbp.plan'].browse(vals['plan_id'])
        plans = _lock_sbp_plans(self, plans)
        plans._eh_assert_not_quarantined_for_edit()
        if vals.get('forfeited'):
            completed = self.filtered(lambda grant: (
                grant.vesting_finalised
                or (
                    grant.plan_id.vesting_end_date
                    and fields.Date.context_today(grant)
                    >= grant.plan_id.vesting_end_date
                )
            ))
            if completed:
                raise UserError(_(
                    "A service forfeiture cannot reverse instruments after "
                    "vesting completed: %s. Record later lapse or exercise "
                    "through the IFRS 2.45 disclosure quantities instead.",
                    ', '.join(completed.mapped('display_name')),
                ))
        terminal = plans.filtered(
            lambda plan: plan.state in ('settled', 'cancelled')
        )
        terminal_blocked = [
            field for field in vals if field not in self._TERMINAL_EDITABLE
        ]
        if terminal and terminal_blocked:
            raise UserError(_(
                "Terminal grants on %(plans)s are immutable except for "
                "disclosure-only exercise/expiry quantities and notes "
                "(%(fields)s).",
                plans=', '.join(terminal.mapped('display_name')),
                fields=', '.join(sorted(terminal_blocked)),
            ))
        if any(f not in self._POST_EDITABLE for f in vals):
            self._check_plan_open(plans)
        result = super().write(vals)
        self._check_company()
        return result

    def unlink(self):
        plans = _lock_sbp_plans(self, self.mapped('plan_id'))
        plans._eh_assert_not_quarantined_for_edit()
        self._check_plan_open(plans)
        return super().unlink()


class EhSbpModification(models.Model):
    _name = 'eh.sbp.modification'
    _description = "Share-based payment modification (IFRS 2.27)"
    _order = 'date, id'
    _check_company_auto = True
    _NUMERIC_FIELDS = frozenset({'incremental_fv', 'cash_fair_value'})
    _SERVER_OWNED_FIELDS = frozenset({
        'applied', 'applied_at', 'applied_by_id', 'transfer_move_id',
    })

    def _eh_check_modification_manager(self):
        if not self.env.user.has_group(
                'eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager may change an activated "
                "award through an IFRS 2 modification."
            ))

    @api.model
    def _eh_check_modification_plans_open(self, plans):
        terminal = plans.filtered(
            lambda plan: plan.state in ('settled', 'cancelled')
        )
        if terminal:
            raise UserError(_(
                "Modifications of terminal SBP plans are immutable: %s.",
                ', '.join(terminal.mapped('display_name')),
            ))

    plan_id = fields.Many2one(
        'eh.sbp.plan', required=True, ondelete='cascade', index=True,
        check_company=True)
    company_id = fields.Many2one(
        related='plan_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='plan_id.currency_id', store=True, readonly=True)
    name = fields.Char(required=True, string="Description")
    date = fields.Date(required=True,
                       default=fields.Date.context_today)
    modification_kind = fields.Selection([
        ('beneficial', "Beneficial Modification"),
        ('replacement', "Replacement Award"),
        ('equity_to_cash', "Equity-to-Cash Classification Change"),
    ], default='beneficial', required=True, index=True)
    replacement_award_reference = fields.Char(
        help="Identifier of the replacement award. IFRS 2.28(c) treats a "
             "new award identified as replacement as a modification of the "
             "original award.")
    incremental_fv = fields.Float(
        digits=(16, 4), required=True,
        string="Incremental FV / Instrument",
        help="Fair value granted by the modification, per instrument: "
             "modified-award FV minus original-award FV, both at the "
             "modification date. Expensed over the remaining vesting "
             "period (IFRS 2.27). A modification that REDUCES fair value "
             "is ignored for measurement - the original grant-date expense "
             "continues as if nothing happened - so a negative amount is "
             "refused here rather than silently booked.")
    cash_fair_value = fields.Float(
        digits=(16, 4), string="Current Cash FV / Instrument",
        help="Current liability fair value per instrument at an equity-to-"
             "cash classification-change date. For a graded plan, enter one "
             "current fair value per tranche below instead (IFRS 2.B44A).")
    tranche_value_ids = fields.One2many(
        'eh.sbp.modification.tranche.value', 'modification_id',
        string="Current Fair Values by Tranche")
    applied = fields.Boolean(readonly=True, copy=False, index=True)
    applied_at = fields.Datetime(readonly=True, copy=False)
    applied_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False)
    transfer_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
        check_company=True,
        help="Sealed transfer from equity reserve to cash-settled liability "
             "at a classification change.")

    @api.constrains('incremental_fv')
    def _check_incremental_fv(self):
        for mod in self:
            if not math.isfinite(mod.incremental_fv) \
                    or mod.incremental_fv < 0.0:
                raise ValidationError(_(
                    "Incremental fair value must be finite and cannot be "
                    "negative: a "
                    "modification that reduces fair value is IGNORED for "
                    "measurement and the original expense continues "
                    "(IFRS 2.27). Record it at zero if you want it on "
                    "file."))

    @api.constrains(
        'modification_kind', 'incremental_fv', 'cash_fair_value',
        'replacement_award_reference',
    )
    def _check_modification_terms(self):
        for mod in self:
            if not math.isfinite(mod.cash_fair_value) \
                    or mod.cash_fair_value < 0.0:
                raise ValidationError(_(
                    "Classification-change fair value must be finite and "
                    "cannot be negative."
                ))
            if mod.modification_kind == 'replacement' \
                    and not (mod.replacement_award_reference or '').strip():
                raise ValidationError(_(
                    "A replacement modification must identify the replacement "
                    "award (IFRS 2.28(c))."
                ))
            if mod.modification_kind == 'equity_to_cash' \
                    and mod.incremental_fv:
                raise ValidationError(_(
                    "An equity-to-cash classification change is remeasured "
                    "through liability fair value, not incremental fair value."
                ))

    @api.constrains('date', 'plan_id')
    def _check_date(self):
        for mod in self:
            if mod.plan_id.grant_date and mod.date \
                    and mod.date < mod.plan_id.grant_date:
                raise ValidationError(_(
                    "A modification cannot predate the grant."))

    @api.constrains('plan_id', 'modification_kind', 'applied')
    def _check_plan_settlement(self):
        for mod in self:
            converted = (
                mod.modification_kind == 'equity_to_cash'
                and mod.applied
                and mod.plan_id.settlement == 'cash'
            )
            if mod.plan_id.settlement != 'equity' and not converted:
                raise ValidationError(_(
                    "Modifications apply to equity-settled plans; a "
                    "cash-settled award is remeasured to current fair "
                    "value every period anyway (IFRS 2.30)."))

    # A modification already consumed by a posted run (run period end on or
    # after its date) is part of the posted cumulative and freezes.
    def _check_not_consumed(self):
        for mod in self:
            consumed = mod.plan_id.run_ids.filtered(
                lambda run: run.state == 'posted' and (
                    run.period_end > mod.date
                    or (
                        run.period_end == mod.date
                        and (
                            not mod.plan_id.vesting_end_date
                            or mod.date >= mod.plan_id.vesting_end_date
                        )
                    )
                )
            )
            if consumed:
                raise UserError(_(
                    "Modification %s already feeds a posted period run and "
                    "is frozen.", mod.display_name))

    @api.model
    def _eh_check_candidate_not_consumed(self, plan, date):
        if plan.run_ids.filtered(
            lambda run: run.state == 'posted' and (
                run.period_end > date
                or (
                    run.period_end == date
                    and (
                        not plan.vesting_end_date
                        or date >= plan.vesting_end_date
                    )
                )
            )
        ):
            raise UserError(_(
                "A modification dated %(date)s would rewrite a posted "
                "period run of %(plan)s.",
                date=date,
                plan=plan.display_name,
            ))

    @api.model_create_multi
    def create(self, vals_list):
        self._eh_check_modification_manager()
        for vals in vals_list:
            protected = self._SERVER_OWNED_FIELDS.intersection(vals)
            if protected and not _has_engine_capability(self):
                raise AccessError(_(
                    "Modification application evidence is server-owned: %s.",
                    ', '.join(sorted(protected)),
                ))
            _reject_related_outputs(
                vals, {'company_id', 'currency_id'}, _("SBP modification"),
            )
            _assert_finite_numeric_vals(
                vals, self._NUMERIC_FIELDS, _("SBP modification"),
            )
        plans = _lock_sbp_plans(
            self,
            self.env['eh.sbp.plan'].browse(
                [v['plan_id'] for v in vals_list if v.get('plan_id')],
            ),
        )
        plans._eh_assert_not_quarantined_for_edit()
        self._eh_check_modification_plans_open(plans)
        for vals in vals_list:
            plan = plans.filtered(lambda p: p.id == vals.get('plan_id'))
            if plan:
                self._eh_check_candidate_not_consumed(
                    plan,
                    fields.Date.to_date(vals.get('date'))
                    or fields.Date.context_today(self),
                )
        records = super().create(vals_list)
        records._check_company()
        return records

    def write(self, vals):
        self._eh_check_modification_manager()
        internal = _has_engine_capability(self)
        protected = self._SERVER_OWNED_FIELDS.intersection(vals)
        if protected and not internal:
            raise AccessError(_(
                "Modification application evidence is server-owned: %s.",
                ', '.join(sorted(protected)),
            ))
        _reject_related_outputs(
            vals, {'company_id', 'currency_id'}, _("SBP modification"),
        )
        _assert_finite_numeric_vals(
            vals, self._NUMERIC_FIELDS, _("SBP modification"),
        )
        plans = self.mapped('plan_id')
        target = self.env['eh.sbp.plan']
        if vals.get('plan_id'):
            target = self.env['eh.sbp.plan'].browse(vals['plan_id'])
            plans |= target
        plans = _lock_sbp_plans(self, plans)
        plans._eh_assert_not_quarantined_for_edit()
        self._eh_check_modification_plans_open(plans)
        if self.filtered('applied') and not internal:
            raise UserError(_(
                "An applied classification change is immutable."
            ))
        self._check_not_consumed()
        for mod in self:
            plan = target or mod.plan_id
            self._eh_check_candidate_not_consumed(
                plan, fields.Date.to_date(vals.get('date')) or mod.date,
            )
        result = super().write(vals)
        self._check_company()
        return result

    def unlink(self):
        self._eh_check_modification_manager()
        plans = _lock_sbp_plans(self, self.mapped('plan_id'))
        plans._eh_assert_not_quarantined_for_edit()
        self._eh_check_modification_plans_open(plans)
        self._check_not_consumed()
        if self.filtered('applied'):
            raise UserError(_(
                "An applied classification change and its sealed transfer "
                "entry cannot be deleted."
            ))
        return super().unlink()

    def action_apply_classification_change(self):
        """Apply IFRS 2.B44A equity-to-cash reclassification exactly once."""
        self.ensure_one()
        self._eh_check_access('write')
        self._eh_check_modification_manager()
        plan = _lock_sbp_plans(self, self.plan_id)
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_sbp_modification WHERE id = %s FOR UPDATE",
            (self.id,),
        )
        self.invalidate_recordset()
        if self.modification_kind != 'equity_to_cash':
            raise UserError(_(
                "Only an equity-to-cash modification uses this application "
                "action."
            ))
        if self.applied:
            raise UserError(_("This classification change is already applied."))
        if plan.state != 'active' or plan.settlement != 'equity':
            raise UserError(_(
                "Apply an equity-to-cash change only to an active "
                "equity-settled plan."
            ))
        if self.date > fields.Date.context_today(self):
            raise UserError(_(
                "A classification change cannot be applied before its "
                "effective date."
            ))
        if plan.run_ids.filtered(
                lambda run: run.state == 'posted'
                and run.period_end >= self.date):
            raise UserError(_(
                "The classification-change date cannot rewrite a posted "
                "period run."
            ))
        plan._eh_assert_provenance_ready()
        plan._eh_assert_finite_measurement_graph()
        plan._validate_accounts([
            'expense', 'equity', 'liability', 'settlement',
        ])
        tranche_values = {
            line.tranche_id.id: line.current_fair_value
            for line in self.tranche_value_ids
        }
        if plan.graded_vesting:
            if set(tranche_values) != set(plan.tranche_ids.ids):
                raise UserError(_(
                    "A graded classification change needs exactly one "
                    "current fair value for every plan tranche."
                ))
        elif self.tranche_value_ids:
            raise UserError(_(
                "A cliff plan uses the modification's single current fair "
                "value, not tranche values."
            ))
        elif self.cash_fair_value <= 0.0:
            raise UserError(_(
                "Enter a positive current cash fair value before applying "
                "the classification change."
            ))
        target = plan._cash_liability_at(
            self.date,
            self.cash_fair_value,
            tranche_values=tranche_values,
        )
        prior = plan.currency_id.round(plan.recognised_cumulative)
        delta = plan.currency_id.round(target - prior)
        legs = []
        if plan.currency_id.compare_amounts(prior, 0.0) > 0:
            legs.append((
                plan.equity_account_id, prior, 0.0,
                _("Classification transfer from equity %s", plan.name),
            ))
        if plan.currency_id.compare_amounts(delta, 0.0) > 0:
            legs.append((
                plan.expense_account_id, delta, 0.0,
                _("Classification remeasurement expense %s", plan.name),
            ))
        elif plan.currency_id.compare_amounts(delta, 0.0) < 0:
            legs.append((
                plan.expense_account_id, 0.0, -delta,
                _("Classification remeasurement gain %s", plan.name),
            ))
        if plan.currency_id.compare_amounts(target, 0.0) > 0:
            legs.append((
                plan.liability_account_id, 0.0, target,
                _("Classification cash liability %s", plan.name),
            ))
        move = plan._post_move(legs, date=self.date) if legs \
            else self.env['account.move']
        plan._eh_engine_write({
            'settlement': 'cash',
            'recognised_cumulative': target,
        })
        self.sudo().with_context(**{
            EH_SBP_ENGINE_CONTEXT_KEY: EH_SBP_ENGINE_CAPABILITY,
        }).write({
            'applied': True,
            'applied_at': fields.Datetime.now(),
            'applied_by_id': self.env.user.id,
            'transfer_move_id': move.id,
        })
        return True


class EhSbpModificationTrancheValue(models.Model):
    _name = 'eh.sbp.modification.tranche.value'
    _description = "Classification-change fair value by vesting tranche"
    _order = 'tranche_id, id'
    _check_company_auto = True
    _NUMERIC_FIELDS = frozenset({'current_fair_value'})

    modification_id = fields.Many2one(
        'eh.sbp.modification', required=True, ondelete='cascade', index=True,
        check_company=True)
    plan_id = fields.Many2one(
        related='modification_id.plan_id', store=True, index=True)
    company_id = fields.Many2one(
        related='modification_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='modification_id.currency_id', store=True, readonly=True)
    tranche_id = fields.Many2one(
        'eh.sbp.plan.tranche', required=True, ondelete='restrict',
        check_company=True)
    current_fair_value = fields.Float(
        digits=(16, 4), required=True,
        string="Current FV / Instrument")

    _unique_modification_tranche = models.Constraint(
        'UNIQUE (modification_id, tranche_id)',
        'Enter one classification-change fair value per tranche.')
    _check_positive_fair_value = models.Constraint(
        'CHECK (current_fair_value > 0)',
        'A tranche current fair value must be positive.')

    @api.constrains('current_fair_value', 'tranche_id', 'modification_id')
    def _check_value_scope(self):
        for line in self:
            if not math.isfinite(line.current_fair_value):
                raise ValidationError(_(
                    "Tranche current fair value must be finite."
                ))
            if line.tranche_id.plan_id != line.plan_id:
                raise ValidationError(_(
                    "A classification fair-value line must use a tranche "
                    "from its own plan."
                ))
            if line.modification_id.modification_kind != 'equity_to_cash':
                raise ValidationError(_(
                    "Tranche current fair values belong only to an "
                    "equity-to-cash classification change."
                ))

    def _eh_assert_open(self):
        if self.mapped('modification_id').filtered('applied'):
            raise UserError(_(
                "Applied classification-change fair values are immutable."
            ))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            _reject_related_outputs(
                vals, {'plan_id', 'company_id', 'currency_id'},
                _("SBP classification tranche value"),
            )
            _assert_finite_numeric_vals(
                vals, self._NUMERIC_FIELDS,
                _("SBP classification tranche value"),
            )
        modifications = self.env['eh.sbp.modification'].browse([
            vals['modification_id'] for vals in vals_list
            if vals.get('modification_id')
        ])
        modifications._eh_check_modification_manager()
        plans = _lock_sbp_plans(self, modifications.mapped('plan_id'))
        plans._eh_assert_not_quarantined_for_edit()
        modifications._eh_check_modification_plans_open(plans)
        if modifications.filtered('applied'):
            raise UserError(_(
                "Applied classification-change fair values are immutable."
            ))
        records = super().create(vals_list)
        records._check_company()
        return records

    def write(self, vals):
        self.mapped('modification_id')._eh_check_modification_manager()
        _reject_related_outputs(
            vals, {'plan_id', 'company_id', 'currency_id'},
            _("SBP classification tranche value"),
        )
        _assert_finite_numeric_vals(
            vals, self._NUMERIC_FIELDS,
            _("SBP classification tranche value"),
        )
        modifications = self.mapped('modification_id')
        if vals.get('modification_id'):
            modifications |= self.env['eh.sbp.modification'].browse(
                vals['modification_id'],
            )
        plans = _lock_sbp_plans(self, modifications.mapped('plan_id'))
        plans._eh_assert_not_quarantined_for_edit()
        self._eh_assert_open()
        result = super().write(vals)
        self._check_company()
        return result

    def unlink(self):
        self.mapped('modification_id')._eh_check_modification_manager()
        _lock_sbp_plans(self, self.mapped('plan_id'))
        self._eh_assert_open()
        return super().unlink()


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_sbp_plan_id = fields.Many2one(
        'eh.sbp.plan', string="SBP Plan", readonly=True, index=True,
        ondelete='restrict', copy=False, check_company=True)
    eh_sbp_provenance_version = fields.Integer(
        string="SBP Provenance Version", readonly=True, copy=False,
        help="Engine-owned durable version stamp. Zero denotes untrusted "
             "legacy journal evidence.")

    @api.model
    def _eh_validate_sbp_provenance(self, vals):
        plan_id = vals.get('eh_sbp_plan_id')
        if not plan_id:
            if vals.get('eh_sbp_provenance_version'):
                raise UserError(_(
                    "An SBP provenance stamp requires an SBP source plan."
                ))
            return self.env['eh.sbp.plan']
        if not _has_engine_capability(self):
            raise AccessError(_(
                "The SBP journal-entry provenance is assigned only by the "
                "SBP posting engine."
            ))
        plan = self.env['eh.sbp.plan'].browse(plan_id).exists()
        if not plan:
            raise UserError(_("The SBP source plan no longer exists."))
        if vals.get('eh_sbp_provenance_version') \
                != EH_SBP_PROVENANCE_VERSION:
            raise AccessError(_(
                "An SBP journal entry must carry the current engine-owned "
                "provenance version."
            ))
        if vals.get('reversed_entry_id'):
            raise AccessError(_(
                "SBP posting routes create primary entries; they cannot "
                "masquerade as reversals."
            ))
        if not self.env.su:
            plan._eh_check_access('write')
        journal = self.env['account.journal'].browse(
            vals.get('journal_id')
        ).exists()
        company = self.env['res.company'].browse(
            vals.get('company_id')
        ).exists() or journal.company_id or self[:1].company_id
        if company and company != plan.company_id:
            raise AccessError(_(
                "An SBP entry and its source plan must belong to the same "
                "company."
            ))
        return plan

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            protected = {
                field_name
                for field_name in (
                    'eh_sbp_plan_id', 'eh_sbp_provenance_version',
                )
                if vals.get(field_name)
            }
            if protected and not _has_engine_capability(self):
                raise AccessError(_(
                    "SBP journal-entry provenance is engine-owned."
                ))
            if protected:
                self._eh_validate_sbp_provenance(vals)
        return super().create(vals_list)

    def write(self, vals):
        protected = {
            'eh_sbp_plan_id', 'eh_sbp_provenance_version',
        }.intersection(vals)
        if protected:
            if not _has_engine_capability(self):
                raise AccessError(_(
                    "The SBP journal-entry provenance is server-owned."
                ))
            changed = self.filtered(lambda move: (
                'eh_sbp_plan_id' in vals
                and move.eh_sbp_plan_id.id
                    != (vals.get('eh_sbp_plan_id') or False)
            ) or (
                'eh_sbp_provenance_version' in vals
                and move.eh_sbp_provenance_version
                    != vals.get('eh_sbp_provenance_version')
            ))
            if changed and changed.filtered(
                    lambda move: move.eh_sbp_plan_id
                    or move.eh_sbp_provenance_version):
                raise AccessError(_(
                    "SBP journal-entry provenance is immutable after "
                    "creation."
                ))
            for move in self:
                check_vals = dict(vals)
                check_vals.setdefault(
                    'eh_sbp_plan_id', move.eh_sbp_plan_id.id)
                check_vals.setdefault(
                    'eh_sbp_provenance_version',
                    move.eh_sbp_provenance_version,
                )
                check_vals.setdefault('company_id', move.company_id.id)
                check_vals.setdefault('journal_id', move.journal_id.id)
                move._eh_validate_sbp_provenance(check_vals)
        return super().write(vals)
