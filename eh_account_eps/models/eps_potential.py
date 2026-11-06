# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.eps.potential: a class of dilutive potential ordinary shares.

Each instrument would, on conversion or exercise, add potential ordinary
shares to the denominator and (for convertibles) add back an after-tax
earnings amount to the numerator. Its incremental EPS (earnings adjustment
per potential share) sequences the dilution test: instruments are added most-
dilutive first, and only while they continue to reduce EPS (IAS 33.44).
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class EhEpsPotential(models.Model):
    _name = 'eh.eps.potential'
    _inherit = 'eh.eps.child.mixin'
    _description = "EPS potential ordinary shares"
    _order = 'run_id, incremental_eps, id'

    run_id = fields.Many2one(
        'eh.eps.run', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='run_id.currency_id', store=True, readonly=True)

    name = fields.Char(required=True)
    instrument_type = fields.Selection(
        [('options', "Options / warrants"),
         ('convertible_bond', "Convertible bond"),
         ('convertible_pref', "Convertible preference shares"),
         ('share_based_payment', "Share-based payment award"),
         ('contingently_issuable', "Contingently issuable shares"),
         ('share_settled_contract', "Share-settled contract"),
         ('cash_or_share_contract', "Contract settled in cash or shares"),
         ('written_put_forward', "Written put / forward purchase"),
         ('subsidiary_instrument', "Subsidiary or associate instrument")],
        default='options', required=True)

    effective_from = fields.Date(
        string="Potential shares outstanding from",
        help="First date this instrument was outstanding. The diluted "
             "denominator is day-weighted inside the EPS reporting period "
             "(IAS 33.36-38). Blank means the period start.")
    effective_to = fields.Date(
        string="Potential shares outstanding to",
        help="Last date this instrument was outstanding. Blank means the "
             "period end.")
    contingency_satisfied = fields.Boolean(
        default=True,
        help="For contingently issuable shares, confirms that period-end "
             "conditions are satisfied. Unsatisfied instruments add no "
             "shares (IAS 33.52-57).")

    potential_shares = fields.Float(
        digits=(16, 2), required=True,
        help="Gross ordinary shares that would be issued on conversion, "
             "exercise, or settlement. The engine performs any treasury-"
             "stock buy-back netting exactly once.")
    share_count_date = fields.Date(
        string="Share-count Basis Date",
        help="Date whose capital structure the potential-share count uses. "
             "Required when the run has dated bonus/split/consolidation "
             "events; only later events restate this count.")
    earnings_adjustment = fields.Monetary(
        currency_field='currency_id',
        help="After-tax amount added back to earnings on conversion, e.g. "
             "interest saved on a convertible bond or preference dividends "
             "avoided. Zero for options.")

    exercise_price = fields.Monetary(
        currency_field='currency_id',
        help="Option/warrant exercise price. With the average market price, "
             "enables the treasury-stock method (IAS 33.45-46): assumed "
             "proceeds are treated as buying back shares at the average "
             "market price, so only the net increment dilutes.")
    manual_average_market_price = fields.Monetary(
        currency_field='currency_id',
        help="Fallback average market price used only while no observations "
             "fall inside the EPS period. Stored separately so deleting the "
             "last in-period observation cannot retain its stale mean.")
    average_market_price = fields.Monetary(
        currency_field='currency_id',
        compute='_compute_average_market_price',
        inverse='_inverse_average_market_price', store=True, readonly=False,
        help="Average market price of the ordinary share over the period. "
             "Used with the exercise price for the treasury-stock method "
             "(IAS 33.45-46). With dated price observations recorded, this "
             "resolves to the arithmetic mean of the observations inside "
             "the run period; without observations it stays a manual "
             "scalar.")
    observation_ids = fields.One2many(
        'eh.eps.price.observation', 'potential_id', copy=True,
        string="Price observations")
    has_observations = fields.Boolean(
        compute='_compute_has_observations',
        help="True when dated price observations resolve the average "
             "market price, making the scalar a computed result rather "
             "than an input.")

    net_incremental_shares = fields.Float(
        compute='_compute_net_incremental', store=True, digits=(16, 2),
        help="Shares actually added to the diluted denominator. For options "
             "under the treasury-stock method this is potential shares net of "
             "the assumed buy-back; out-of-the-money options add zero. For "
             "all other cases it equals the gross potential shares.")
    weighted_incremental_shares = fields.Float(
        compute='_compute_weighted_incremental', store=True, digits=(16, 2),
        help="Net incremental shares day-weighted for the part of the EPS "
             "period that the instrument was outstanding (IAS 33.36-38).")

    incremental_eps = fields.Float(
        compute='_compute_incremental', store=True, digits=(16, 6),
        help="Earnings adjustment per potential share; the ordering key for "
             "the dilution test (lower is more dilutive).")
    is_dilutive = fields.Boolean(
        readonly=True, copy=False,
        help="Set by the run when the instrument is included as dilutive.")

    _check_potential = models.Constraint(
        'CHECK (potential_shares >= 0)',
        'Potential shares cannot be negative.')

    _check_effective_dates = models.Constraint(
        'CHECK (effective_from IS NULL OR effective_to IS NULL '
        'OR effective_from <= effective_to)',
        'Potential-share start date must not be after its end date.')

    @api.constrains('share_count_date', 'run_id')
    def _check_share_count_date(self):
        for potential in self:
            if (potential.share_count_date
                    and potential.run_id.period_end
                    and potential.share_count_date
                    > potential.run_id.period_end):
                raise UserError(_(
                    "Potential-share basis date cannot be after the EPS "
                    "reporting period."))

    _SERVER_FIELDS = (
        'company_id', 'currency_id', 'net_incremental_shares',
        'weighted_incremental_shares', 'incremental_eps', 'is_dilutive',
    )

    @api.model_create_multi
    def create(self, vals_list):
        # Appending a potential-share line to an already-computed run would
        # silently move the diluted denominator/numerator. Block create on a
        # frozen run unless it is the run's own compute writing the line.
        if not (self.env.su and (
                self.env.context.get('eh_eps_compute')
                or self.env.context.get('eh_eps_internal'))):
            blocked = set().union(*(set(vals) for vals in vals_list or [{}])) \
                & set(self._SERVER_FIELDS)
            if blocked:
                raise AccessError(_(
                    "Potential-share company and output fields are owned by "
                    "the EPS engine: %s.", ', '.join(sorted(blocked))))
        for vals in vals_list:
            if 'average_market_price' in vals:
                if vals.get('observation_ids'):
                    raise UserError(_(
                        "Do not provide a manual average while dated price "
                        "observations are supplied."
                    ))
                vals.setdefault(
                    'manual_average_market_price',
                    vals.pop('average_market_price'),
                )
        self._eh_prepare_runs([
            vals.get('run_id') for vals in vals_list if vals.get('run_id')])
        if not (self.env.su and self.env.context.get('eh_eps_compute')):
            Run = self.env['eh.eps.run']
            for vals in vals_list:
                run = Run.browse(vals['run_id']) if vals.get('run_id') else None
                if run and run.state == 'computed':
                    raise UserError(_(
                        "Potential-share lines cannot be added to run %s once "
                        "it is computed. Set the run back to draft first "
                        "(IAS 33).", run.display_name))
        return super().create(vals_list)

    def write(self, vals):
        # The run's own dilution test flips is_dilutive on these lines; that
        # internal write carries the eh_eps_compute context and is allowed.
        internal = self.env.su and (
            self.env.context.get('eh_eps_compute')
            or self.env.context.get('eh_eps_internal'))
        if set(vals) & set(self._SERVER_FIELDS) and not internal:
            raise AccessError(_(
                "Potential-share company and output fields are owned by the "
                "EPS engine."))
        if 'average_market_price' in vals and not internal:
            if self.filtered('has_observations'):
                raise AccessError(_(
                    "Average market price is engine-owned while in-period "
                    "observations exist."))
            vals = dict(vals)
            vals['manual_average_market_price'] = vals.pop(
                'average_market_price',
            )
        run_ids = self.mapped('run_id').ids
        if vals.get('run_id'):
            run_ids.append(vals['run_id'])
        target_runs = self._eh_prepare_runs(run_ids)
        if not (self.env.su and self.env.context.get('eh_eps_compute')):
            self._eh_check_run_frozen(_("Potential-share lines"))
            if any(run.state != 'draft' for run in target_runs):
                raise UserError(_(
                    "Potential-share lines cannot be reparented to a computed "
                    "EPS run."))
        return super().write(vals)

    def unlink(self):
        self._eh_prepare_runs(self.mapped('run_id').ids)
        self._eh_check_run_frozen(_("Potential-share lines"))
        return super().unlink()

    @api.depends('observation_ids.date', 'run_id.period_start',
                 'run_id.period_end')
    def _compute_has_observations(self):
        for p in self:
            p.has_observations = bool(p._eh_in_period_observations())

    def _eh_in_period_observations(self):
        self.ensure_one()
        start = self.run_id.period_start
        end = self.run_id.period_end
        return self.observation_ids.filtered(
            lambda observation: observation.date
            and (not start or observation.date >= start)
            and (not end or observation.date <= end),
        )

    @api.depends('observation_ids.date', 'observation_ids.price',
                 'run_id.period_start', 'run_id.period_end',
                 'manual_average_market_price')
    def _compute_average_market_price(self):
        """Resolve the period-average market price (IAS 33.45).

        IAS 33 applies the treasury-stock method at the average market price
        DURING the period. With price observations recorded, the average is
        the arithmetic mean of the observations dated inside the run period
        (out-of-period observations are ignored); without any in-period
        observation the manually entered scalar is preserved, so existing
        runs behave exactly as before."""
        for p in self:
            obs = p._eh_in_period_observations()
            if obs:
                p.average_market_price = (
                    sum(obs.mapped('price')) / len(obs))
            else:
                p.average_market_price = p.manual_average_market_price

    def _inverse_average_market_price(self):
        for potential in self:
            if potential._eh_in_period_observations():
                raise UserError(_(
                    "In-period observations own the average market price."
                ))
            potential.manual_average_market_price = (
                potential.average_market_price
            )

    @api.depends('instrument_type', 'potential_shares',
                 'exercise_price', 'average_market_price',
                 'contingency_satisfied')
    def _compute_net_incremental(self):
        """Treasury-stock method for options (IAS 33.45-46).

        When an option's exercise price and the average market price are both
        set, assume the proceeds buy back shares at the average market price,
        so only the net increment dilutes:
            net = potential_shares * (1 - exercise_price / average_market_price)
        floored at 0 (out-of-the-money options add nothing). In every other
        case (non-options, or prices not supplied) net equals the gross
        potential shares, so existing runs are unchanged."""
        for p in self:
            if (p.instrument_type == 'contingently_issuable'
                    and not p.contingency_satisfied):
                p.net_incremental_shares = 0.0
            elif (p.instrument_type == 'options'
                    and p.exercise_price
                    and p.average_market_price):
                net = p.potential_shares * (
                    1.0 - p.exercise_price / p.average_market_price)
                p.net_incremental_shares = net if net > 0.0 else 0.0
            elif (p.instrument_type == 'written_put_forward'
                    and p.exercise_price
                    and p.average_market_price):
                # Reverse treasury-stock method (IAS 33.63): assume shares
                # are issued at average market price to fund the contractual
                # repurchase. Only the excess over shares repurchased dilutes.
                net = p.potential_shares * (
                    p.exercise_price / p.average_market_price - 1.0
                )
                p.net_incremental_shares = net if net > 0.0 else 0.0
            else:
                p.net_incremental_shares = p.potential_shares

    @api.depends('net_incremental_shares', 'effective_from', 'effective_to',
                 'run_id.period_start', 'run_id.period_end')
    def _compute_weighted_incremental(self):
        for potential in self:
            start = potential.run_id.period_start
            end = potential.run_id.period_end
            if not start or not end or end < start:
                potential.weighted_incremental_shares = 0.0
                continue
            outstanding_from = max(potential.effective_from or start, start)
            outstanding_to = min(potential.effective_to or end, end)
            if outstanding_to < outstanding_from:
                potential.weighted_incremental_shares = 0.0
                continue
            period_days = (end - start).days + 1
            outstanding_days = (outstanding_to - outstanding_from).days + 1
            potential.weighted_incremental_shares = (
                potential.net_incremental_shares
                * outstanding_days / period_days
            )

    @api.depends('earnings_adjustment', 'weighted_incremental_shares')
    def _compute_incremental(self):
        for p in self:
            p.incremental_eps = (
                p.earnings_adjustment / p.weighted_incremental_shares
                if p.weighted_incremental_shares else 0.0)
