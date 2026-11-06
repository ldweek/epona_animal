# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.sbp.valuation: option-pricing helper for grant-date fair values.

Two pure-Python models, no scipy:

* Black-Scholes(-Merton) European call with a continuous dividend yield.
  The standard normal CDF uses the Abramowitz-Stegun 7.1.26 erf
  approximation, absolute error below 1.5e-7, which is orders of
  magnitude inside the 4dp storage precision of the result.
* Cox-Ross-Rubinstein binomial tree (European exercise, default 100
  steps) as the lattice alternative; it converges to Black-Scholes as the
  step count grows.

Degenerate inputs are priced deterministically: zero term returns the
intrinsic value, zero volatility returns the discounted forward
intrinsic. The result is copied onto a grant with the grant's "Use
Valuation" action; the inputs stay manual (IFRS 2 wants the entity's own
market inputs, the module automates only the arithmetic).
"""

import math

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .sbp_plan import (
    EH_SBP_ENGINE_CAPABILITY,
    EH_SBP_ENGINE_CONTEXT_KEY,
    EH_SBP_VALUATION_PROVENANCE_VERSION,
    _assert_finite_numeric_vals,
    _has_engine_capability,
    _lock_sbp_companies,
    _lock_sbp_plans,
)


def _norm_cdf(x):
    """Standard normal CDF via the Abramowitz-Stegun 7.1.26 erf
    approximation (|error| < 1.5e-7)."""
    sign = 1.0 if x >= 0.0 else -1.0
    z = abs(x) / math.sqrt(2.0)
    t = 1.0 / (1.0 + 0.3275911 * z)
    poly = (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t
             - 0.284496736) * t + 0.254829592) * t
    erf = 1.0 - poly * math.exp(-z * z)
    return 0.5 * (1.0 + sign * erf)


def _bs_call(spot, strike, vol, rate, term, dividend=0.0):
    """Black-Scholes-Merton European call value."""
    if term <= 0.0:
        return max(spot - strike, 0.0)
    # K=0 is a valid degenerate call: it delivers the underlying for no
    # exercise payment.  Handle it before log(S/K), both to avoid a raw
    # ZeroDivisionError and to retain the dividend discount on the share.
    if strike <= 0.0:
        return spot * math.exp(-dividend * term)
    if vol <= 0.0:
        return max(spot * math.exp(-dividend * term)
                   - strike * math.exp(-rate * term), 0.0)
    sq = vol * math.sqrt(term)
    d1 = (math.log(spot / strike)
          + (rate - dividend + vol * vol / 2.0) * term) / sq
    d2 = d1 - sq
    return (spot * math.exp(-dividend * term) * _norm_cdf(d1)
            - strike * math.exp(-rate * term) * _norm_cdf(d2))


def _crr_call(spot, strike, vol, rate, term, dividend=0.0, steps=100):
    """Cox-Ross-Rubinstein binomial European call value."""
    if term <= 0.0:
        return max(spot - strike, 0.0)
    if vol <= 0.0:
        return max(spot * math.exp(-dividend * term)
                   - strike * math.exp(-rate * term), 0.0)
    dt = term / steps
    up = math.exp(vol * math.sqrt(dt))
    down = 1.0 / up
    growth = math.exp((rate - dividend) * dt)
    prob = (growth - down) / (up - down)
    if not math.isfinite(prob) or not 0.0 <= prob <= 1.0:
        raise ValidationError(_(
            "The binomial inputs violate the no-arbitrage condition: the "
            "risk-neutral probability must lie between zero and one. "
            "Adjust the rate, dividend yield, volatility, term or steps."
        ))
    disc = math.exp(-rate * dt)
    values = [
        max(spot * (up ** j) * (down ** (steps - j)) - strike, 0.0)
        for j in range(steps + 1)
    ]
    for i in range(steps, 0, -1):
        values = [
            disc * (prob * values[j + 1] + (1.0 - prob) * values[j])
            for j in range(i)
        ]
    return values[0]


class EhSbpValuation(models.Model):
    _name = 'eh.sbp.valuation'
    _description = "Option valuation (Black-Scholes / binomial)"
    _order = 'id desc'
    _rec_name = 'name'
    _check_company_auto = True

    _INPUT_FIELDS = frozenset({
        'pricing_model', 'spot', 'strike', 'volatility_pct', 'rate_pct',
        'term_years', 'dividend_yield_pct', 'steps',
    })
    _NUMERIC_FIELDS = frozenset({
        'spot', 'strike', 'volatility_pct', 'rate_pct', 'term_years',
        'dividend_yield_pct', 'steps', 'result_value',
    })
    _RESULT_FIELDS = frozenset({
        'result_value', 'result_provenance_version', 'result_currency_id',
        'legacy_result_value', 'legacy_result_provenance_version',
        'legacy_result_currency_id', 'legacy_result_unverified',
        'legacy_result_quarantine_note',
    })

    name = fields.Char(required=True, copy=False, default='/')
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    pricing_model = fields.Selection(
        [('black_scholes', "Black-Scholes"),
         ('binomial', "Binomial (CRR)")],
        default='black_scholes', required=True, string="Model")
    spot = fields.Float(digits=(16, 4), required=True,
                        string="Spot Price")
    strike = fields.Float(digits=(16, 4), required=True,
                          string="Strike Price")
    volatility_pct = fields.Float(
        digits=(6, 3), string="Volatility %",
        help="Annualised volatility of the underlying, in percent.")
    rate_pct = fields.Float(
        digits=(6, 3), string="Risk-free Rate %",
        help="Continuously compounded annual risk-free rate, in percent.")
    term_years = fields.Float(
        digits=(6, 4), string="Term (Years)",
        help="Expected term to exercise in years. IFRS 2.B17: use the "
             "expected term, not the contractual life.")
    dividend_yield_pct = fields.Float(
        digits=(6, 3), string="Dividend Yield %")
    steps = fields.Integer(
        default=100,
        help="Binomial tree steps (CRR). 100 steps sit within a few cents "
             "of Black-Scholes for typical employee-option inputs.")
    result_value = fields.Float(
        digits=(16, 4), readonly=True, copy=False,
        string="Value / Instrument",
        help="Computed call value per instrument, pullable into a grant's "
             "grant-date fair value.")
    result_provenance_version = fields.Integer(
        readonly=True, copy=False, default=0,
        help="Server-owned valuation-result provenance. Zero identifies a "
             "legacy or invalidated result that must be recomputed before "
             "it can be consumed by a grant.")
    result_currency_id = fields.Many2one(
        'res.currency', readonly=True, copy=False, index=True,
        string="Result Currency",
        help="Server-owned currency identity in force when this valuation "
             "result was computed. A company-currency change invalidates the "
             "result and requires a recomputation.")
    legacy_result_value = fields.Float(
        digits=(16, 4), readonly=True, copy=False,
        string="Preserved Legacy Result",
        help="Exact pre-invalidation result preserved by upgrade. It is "
             "unverified historical evidence and can never be consumed by "
             "a new grant.")
    legacy_result_provenance_version = fields.Integer(
        readonly=True, copy=False,
        string="Preserved Legacy Provenance")
    legacy_result_currency_id = fields.Many2one(
        'res.currency', readonly=True, copy=False,
        string="Preserved Legacy Result Currency")
    legacy_result_unverified = fields.Boolean(
        readonly=True, copy=False, index=True,
        string="Legacy Result Unverified",
        help="Upgrade-preserved result lacks current durable measurement "
             "provenance. Linked approved plans keep it as frozen evidence; "
             "only unconsumed valuations may be recomputed.")
    legacy_result_quarantine_note = fields.Text(
        readonly=True, copy=False)
    notes = fields.Char()

    _check_positive = models.Constraint(
        'CHECK (spot >= 0 AND strike >= 0)',
        'Spot and strike prices cannot be negative.')

    @api.constrains('spot', 'strike', 'volatility_pct', 'rate_pct',
                    'term_years', 'dividend_yield_pct', 'steps')
    def _check_inputs(self):
        for val in self:
            numeric_inputs = (
                val.spot, val.strike, val.volatility_pct, val.rate_pct,
                val.term_years, val.dividend_yield_pct,
            )
            if any(not math.isfinite(value) for value in numeric_inputs):
                raise ValidationError(_(
                    "Every option-pricing input must be a finite number."
                ))
            if val.volatility_pct < 0 or val.dividend_yield_pct < 0 \
                    or val.term_years < 0:
                raise ValidationError(_(
                    "Volatility, dividend yield and term cannot be "
                    "negative."))
            if not 1 <= val.steps <= 2000:
                raise ValidationError(_(
                    "Binomial steps must lie between 1 and 2000."))

    @api.constrains('result_value')
    def _check_finite_result(self):
        for valuation in self:
            if not math.isfinite(valuation.result_value):
                raise ValidationError(_(
                    "The option-pricing result must be a finite number."
                ))

    @api.model_create_multi
    def create(self, vals_list):
        clean_vals_list = []
        for incoming in vals_list:
            vals = dict(incoming or {})
            protected = self._RESULT_FIELDS.intersection(vals)
            if protected and not _has_engine_capability(self):
                raise AccessError(_(
                    "The option-pricing result and provenance are computed "
                    "by the server."
                ))
            _assert_finite_numeric_vals(
                vals, self._NUMERIC_FIELDS, _("SBP valuation"),
            )
            company = self.env['res.company'].browse(
                vals.get('company_id') or self.env.company.id,
            ).exists()
            if not company or (
                not self.env.su and company.id not in self.env.companies.ids
            ):
                raise AccessError(_(
                    "The valuation company must be one of your allowed "
                    "companies."
                ))
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.sbp.valuation') or '/'
            clean_vals_list.append(vals)
        return super().create(clean_vals_list)

    def _eh_lock_valuations(self, access_mode='write'):
        if not self:
            return
        self._eh_check_access(access_mode)
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_sbp_valuation WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            (tuple(self.ids),),
        )
        self.invalidate_recordset()

    def _eh_lock_consumption_scope(self, access_mode='write'):
        """Lock linked plans then valuations and reject approved evidence.

        Grant create/write already locks its plan.  Using the same plan-first
        order here serializes activation, grant linking, input edits,
        recomputation, and deletion without a valuation/plan deadlock.
        """
        if not self:
            return self.env['eh.sbp.plan']
        self._eh_check_access(access_mode)
        if not self.env.su:
            foreign = self.filtered(
                lambda value: value.company_id.id not in self.env.companies.ids
            )
            if foreign:
                raise AccessError(_(
                    "A valuation outside your allowed companies cannot be "
                    "changed."
                ))
        _lock_sbp_companies(self, self.mapped('company_id'))
        self.flush_recordset()
        self.env['eh.sbp.grant'].flush_model(['valuation_id', 'plan_id'])
        self.env['eh.sbp.plan'].flush_model(['state'])
        self.env.cr.execute(
            "SELECT DISTINCT plan_id FROM eh_sbp_grant "
            "WHERE valuation_id IN %s ORDER BY plan_id",
            (tuple(self.ids),),
        )
        plans = _lock_sbp_plans(
            self,
            self.env['eh.sbp.plan'].browse(
                [row[0] for row in self.env.cr.fetchall()],
            ),
        )
        self._eh_lock_valuations(access_mode=access_mode)
        plans.invalidate_recordset(['state'])
        approved = plans.filtered(lambda plan: plan.state != 'draft')
        if approved:
            raise UserError(_(
                "Valuation evidence linked to an activated or terminal SBP "
                "plan is immutable: %s.",
                ', '.join(approved.mapped('display_name')),
            ))
        return plans

    def write(self, vals):
        if 'company_id' in vals:
            raise AccessError(_(
                "A valuation's company is immutable; create a valuation in "
                "the other company instead."
            ))
        protected = self._RESULT_FIELDS.intersection(vals)
        if protected and not _has_engine_capability(self):
            raise AccessError(_(
                "The option-pricing result and provenance are computed by "
                "the server."
            ))
        _assert_finite_numeric_vals(
            vals, self._NUMERIC_FIELDS, _("SBP valuation"),
        )
        changed_inputs = self._INPUT_FIELDS.intersection(vals)
        if changed_inputs:
            self._eh_lock_consumption_scope()
        result = super().write(vals)
        if changed_inputs and not _has_engine_capability(self):
            self.sudo().with_context(**{
                EH_SBP_ENGINE_CONTEXT_KEY: EH_SBP_ENGINE_CAPABILITY,
            }).write({
                'result_value': 0.0,
                'result_provenance_version': 0,
                'result_currency_id': False,
            })
        return result

    def action_compute(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an ERP Heritage Accounting Manager may finalise an "
                "option valuation result."
            ))
        self._eh_lock_consumption_scope()
        # Recheck under the valuation row lock.  Besides normal ORM writes,
        # this fails closed for any pre-control or direct-SQL non-finite input
        # that bypassed the field constraint.
        self._check_inputs()
        for val in self:
            if val.spot <= 0.0:
                raise UserError(_(
                    "Enter a positive spot price to value the option."))
            args = (val.spot, val.strike, val.volatility_pct / 100.0,
                    val.rate_pct / 100.0, val.term_years,
                    val.dividend_yield_pct / 100.0)
            if val.pricing_model == 'binomial':
                result = _crr_call(*args, steps=val.steps)
            else:
                result = _bs_call(*args)
            if not math.isfinite(result):
                raise UserError(_(
                    "The option-pricing inputs on %s produced a non-finite "
                    "result. Correct the inputs before recomputing.",
                    val.display_name,
                ))
            val.sudo().with_context(**{
                EH_SBP_ENGINE_CONTEXT_KEY: EH_SBP_ENGINE_CAPABILITY,
            }).write({
                'result_value': result,
                'result_provenance_version':
                    EH_SBP_VALUATION_PROVENANCE_VERSION,
                'result_currency_id': val.company_id.currency_id.id,
            })
        return True

    def unlink(self):
        self._eh_lock_consumption_scope(access_mode='unlink')
        return super().unlink()
