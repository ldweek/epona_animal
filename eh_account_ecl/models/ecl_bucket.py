# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.ecl.bucket: one ageing band in a provision matrix.

Each bucket covers a range of days past due and carries a loss rate. The
expected credit loss on the bucket is its gross carrying amount at that rate
(IFRS 9.5.5.15, simplified approach). Under the general approach the bucket
carries EAD / LGD / PD inputs and its stage is assigned by the run's stage
engine from days-past-due backstops, qualitative flags and cure probation;
forward-looking scenarios on the run probability-weight the result
(IFRS 9.5.5.17).
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_FROZEN = frozenset({'computed', 'posted', 'reversed'})

# Day-count basis for deriving discount periods from a maturity date.
_DAYS_PER_YEAR = 365.0


class EhEclBucket(models.Model):
    _name = 'eh.ecl.bucket'
    _description = "ECL provision-matrix bucket"
    _order = 'run_id, days_from, id'

    run_id = fields.Many2one(
        'eh.ecl.run', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='run_id.currency_id', store=True, readonly=True)

    name = fields.Char(required=True, help="Label, e.g. '1-30 days'.")
    days_from = fields.Integer(
        required=True, default=0,
        help="Inclusive lower bound of days past due. Not-yet-due is 0. "
             "The stage engine reads this band bound as the bucket's "
             "days-past-due signal for the 30/90 day backstops.")
    days_to = fields.Integer(
        help="Inclusive upper bound of days past due. Leave 0 for the "
             "open-ended oldest bucket.")
    segment_id = fields.Many2one(
        'eh.ecl.segment', ondelete='restrict',
        help="Portfolio segment this band belongs to. Populate from "
             "receivables splits the matrix per segment when segments "
             "exist; a band without a segment holds the unsegmented rest.")
    origin = fields.Selection(
        [('manual', "Manual"),
         ('auto', "Populated"),
         ('provider', "Populated (provider)")],
        default='manual', required=True,
        help="Populated buckets are created by populate-from-receivables "
             "and replaced on each repopulation; manual buckets are the "
             "hand-defined matrix and are never restructured by it. Provider "
             "buckets hold exposures ingested from an external loss-allowance "
             "provider (for example presented-cheque suspense holdings) that "
             "have left the open-receivables population.")
    loss_rate = fields.Float(
        digits=(6, 3), required=True, default=0.0,
        help="Expected loss rate for this bucket, as a percentage.")
    discount_rate = fields.Float(
        digits=(6, 3), default=0.0, string="Effective Interest Rate (%)",
        help="Effective interest rate determined at initial recognition, "
             "as a percentage. IFRS 9.5.5.17(b): expected credit losses are "
             "discounted to the reporting date at the EIR. Leave 0 for an "
             "undiscounted provision matrix.")
    periods_to_recovery = fields.Integer(
        default=0,
        help="Manual number of periods until the expected shortfall is "
             "realised. Used only when no maturity date is set; a maturity "
             "date derives the period from the reporting date instead.")
    maturity_date = fields.Date(
        help="Expected recovery / maturity date of the exposures in this "
             "bucket. When set, the discount period is derived as "
             "(maturity date - reporting date) in years on a 365-day basis, "
             "so the shortfall is discounted at the EIR over the actual "
             "remaining term (IFRS 9.5.5.17(b)).")
    periods_effective = fields.Float(
        compute='_compute_periods_effective', digits=(12, 4),
        help="Discount periods actually applied: derived from the maturity "
             "date when set, otherwise the manual periods to recovery.")
    stage = fields.Selection(
        [('1', "Stage 1"), ('2', "Stage 2"), ('3', "Stage 3")],
        default='1',
        help="IFRS 9 stage. Under the general approach the run's stage "
             "engine assigns it each compute from the DPD backstops, "
             "qualitative flags and cure probation; the simplified approach "
             "always measures lifetime ECL and uses the stage for "
             "disclosure only.")

    # ---- general (3-stage) model inputs ----
    exposure_at_default = fields.Monetary(
        currency_field='currency_id',
        help="Exposure at default (EAD) for the general 3-stage model: the "
             "expected gross carrying amount at the point of default. "
             "Populate from receivables fills it with the open residual "
             "under the general approach.")
    pd_12m = fields.Float(
        digits=(6, 3), default=0.0,
        help="12-month probability of default (percentage), used for "
             "Stage 1 exposures in the general model.")
    pd_lifetime = fields.Float(
        digits=(6, 3), default=0.0,
        help="Lifetime probability of default (percentage), used for "
             "Stage 2, Stage 3 and POCI exposures in the general model.")
    lgd = fields.Float(
        digits=(6, 3), default=0.0,
        help="Loss given default (percentage): the share of the exposure "
             "expected to be lost on default.")
    sicr = fields.Boolean(
        string="SICR",
        help="Qualitative override: significant increase in credit risk "
             "since initial recognition. The stage engine moves the "
             "exposure to Stage 2 (measure lifetime ECL).")
    credit_impaired = fields.Boolean(
        help="Qualitative override: exposure is credit-impaired. The stage "
             "engine moves it to Stage 3.")
    low_credit_risk = fields.Boolean(
        help="Low-credit-risk exemption (IFRS 9.5.5.10): the entity may "
             "assume no significant increase in credit risk, so the stage "
             "engine keeps the exposure in Stage 1 despite the 30+ DPD "
             "backstop. Does not override credit-impairment or the 90+ DPD "
             "backstop.")
    poci = fields.Boolean(
        string="POCI",
        help="Purchased or originated credit-impaired (IFRS 9.5.5.13): "
             "always measured at lifetime ECL from initial recognition, "
             "never reverts to 12-month ECL, excluded from the stage "
             "engine, and disclosed on its own reconciliation line.")
    stage_prior = fields.Selection(
        [('1', "Stage 1"), ('2', "Stage 2"), ('3', "Stage 3")],
        readonly=True,
        help="Stage the exposure carried in the prior run, snapshotted by "
             "the stage engine so recomputes stay deterministic.")
    cure_streak = fields.Integer(
        readonly=True,
        help="Consecutive runs (including this one) the exposure has sat "
             "below its staged risk level. It reverts (cures) only once "
             "the streak reaches the run's probation length.")
    stage_auto = fields.Selection(
        [('1', "Stage 1"), ('2', "Stage 2"), ('3', "Stage 3")],
        compute='_compute_stage_auto',
        help="Stage derived from the general-model risk flags: Stage 3 if "
             "credit-impaired, else Stage 2 if a significant increase in "
             "credit risk, else Stage 1.")

    gross_carrying = fields.Monetary(
        currency_field='currency_id',
        help="Gross carrying amount of receivables in this bucket. Entered "
             "directly or populated from open receivables.")
    ecl_undiscounted = fields.Monetary(
        compute='_compute_ecl', store=True, currency_field='currency_id',
        help="Expected credit loss before present-value discounting: gross "
             "carrying amount at the loss rate.")
    ecl = fields.Monetary(
        compute='_compute_ecl', store=True, currency_field='currency_id',
        help="Expected credit loss: gross carrying amount at the loss rate, "
             "discounted to present value at the EIR over the effective "
             "periods (IFRS 9.5.5.17).")
    ecl_general = fields.Monetary(
        compute='_compute_ecl_general', store=True,
        currency_field='currency_id',
        help="General 3-stage expected credit loss: EAD x LGD x PD, where "
             "PD is the 12-month PD for Stage 1 and the lifetime PD for "
             "Stage 2, Stage 3 and POCI, probability-weighted over the "
             "run's forward-looking scenarios when any exist, and "
             "discounted at the EIR.")
    ecl_effective = fields.Monetary(
        compute='_compute_ecl_effective', store=True,
        currency_field='currency_id',
        help="The ECL that flows into the run's closing allowance: the "
             "general-model figure when the run uses the general approach, "
             "otherwise the simplified provision-matrix figure.")

    _check_rate = models.Constraint(
        'CHECK (loss_rate >= 0 AND loss_rate <= 100)',
        'Loss rate must be between 0 and 100.')
    _check_days = models.Constraint(
        'CHECK (days_from >= 0)',
        'Days from cannot be negative.')
    _check_discount = models.Constraint(
        'CHECK (discount_rate >= 0 AND periods_to_recovery >= 0)',
        'Discount rate and periods to recovery cannot be negative.')

    def _effective_periods(self):
        """Discount periods in years: maturity-derived, else manual."""
        self.ensure_one()
        if self.maturity_date and self.run_id.reporting_date:
            days = (self.maturity_date - self.run_id.reporting_date).days
            return max(days, 0) / _DAYS_PER_YEAR
        return float(self.periods_to_recovery)

    @api.depends('maturity_date', 'periods_to_recovery',
                 'run_id.reporting_date')
    def _compute_periods_effective(self):
        for b in self:
            b.periods_effective = b._effective_periods()

    def _pv_factor(self):
        """Present-value factor shared by the simplified and general ECL."""
        self.ensure_one()
        periods = self._effective_periods()
        if self.discount_rate and periods:
            return 1.0 / ((1.0 + self.discount_rate / 100.0) ** periods)
        return 1.0

    @api.depends('gross_carrying', 'loss_rate', 'discount_rate',
                 'periods_to_recovery', 'maturity_date',
                 'run_id.reporting_date')
    def _compute_ecl(self):
        for b in self:
            undiscounted = b.gross_carrying * b.loss_rate / 100.0
            b.ecl_undiscounted = undiscounted
            ecl = undiscounted * b._pv_factor()
            currency = b.currency_id
            b.ecl = currency.round(ecl) if currency else round(ecl, 2)

    @api.depends('credit_impaired', 'sicr')
    def _compute_stage_auto(self):
        for b in self:
            if b.credit_impaired:
                b.stage_auto = '3'
            elif b.sicr:
                b.stage_auto = '2'
            else:
                b.stage_auto = '1'

    @api.depends('exposure_at_default', 'lgd', 'pd_12m', 'pd_lifetime',
                 'stage', 'poci', 'discount_rate', 'periods_to_recovery',
                 'maturity_date', 'run_id.reporting_date',
                 'run_id.scenario_ids.weight',
                 'run_id.scenario_ids.pd_factor',
                 'run_id.scenario_ids.lgd_factor',
                 'run_id.stage3_pd_policy')
    def _compute_ecl_general(self):
        for b in self:
            # POCI is measured at lifetime ECL from initial recognition
            # regardless of the stage label (IFRS 9.5.5.13).
            defaulted_stage3 = bool(
                not b.poci
                and b.stage == '3'
                and b.run_id.stage3_pd_policy == 'defaulted_100'
            )
            pd = (
                100.0
                if defaulted_stage3
                else b.pd_lifetime if (b.poci or b.stage != '1')
                else b.pd_12m
            )
            pv = b._pv_factor()
            scenarios = b.run_id.scenario_ids
            if scenarios:
                raw = 0.0
                for scenario in scenarios:
                    pd_eff = (
                        100.0
                        if defaulted_stage3
                        else min(pd * scenario.pd_factor, 100.0)
                    )
                    lgd_eff = min(b.lgd * scenario.lgd_factor, 100.0)
                    raw += (scenario.weight * b.exposure_at_default
                            * lgd_eff / 100.0 * pd_eff / 100.0)
                raw *= pv
            else:
                raw = (b.exposure_at_default * b.lgd / 100.0
                       * pd / 100.0 * pv)
            currency = b.currency_id
            b.ecl_general = currency.round(raw) if currency else round(raw, 2)

    @api.depends('ecl', 'ecl_general', 'run_id.measurement_approach')
    def _compute_ecl_effective(self):
        for b in self:
            if b.run_id.measurement_approach == 'general':
                b.ecl_effective = b.ecl_general
            else:
                b.ecl_effective = b.ecl

    def _matches(self, days_overdue):
        """True when days_overdue falls in this bucket's range."""
        self.ensure_one()
        if days_overdue < self.days_from:
            return False
        if self.days_to and days_overdue > self.days_to:
            return False
        return True

    # ---- stage engine helpers ----

    def _recon_stage(self):
        """Reconciliation category: POCI is its own IFRS 7.35H line."""
        self.ensure_one()
        return 'poci' if self.poci else (self.stage or '1')

    def _candidate_stage(self):
        """(stage, reason) the risk signals point to, ignoring probation.

        Order of precedence: the 90+ DPD backstop (reason 'backstop_90') and
        the credit-impaired override (reason 'credit_impaired', a Stage 3
        move driven by qualitative impairment rather than ageing) force
        Stage 3 (IFRS 9.B5.5.37); the low-credit-risk exemption then holds
        Stage 1 (IFRS 9.5.5.10); the 30+ DPD backstop (reason 'backstop_30',
        IFRS 9.5.5.11) or the qualitative SICR flag (reason 'sicr_flag')
        give Stage 2; otherwise Stage 1. The impaired override carries its
        own reason so the transfer trail distinguishes a Stage-3 impairment
        move from a Stage-2 SICR move.
        """
        self.ensure_one()
        run = self.run_id
        if self.days_from > run.backstop_dpd_stage3:
            return '3', 'backstop_90'
        if self.credit_impaired:
            return '3', 'credit_impaired'
        if self.low_credit_risk:
            return '1', 'cure'
        if self.days_from > run.backstop_dpd_stage2:
            return '2', 'backstop_30'
        if self.sicr:
            return '2', 'sicr_flag'
        return '1', 'cure'

    def _match_prior_bucket(self, prior_run):
        """This bucket's counterpart in the prior run, or empty.

        Matched within the same segment by label first (the stable identity
        as an exposure ages across bands), then by identical band bounds.
        """
        self.ensure_one()
        if not prior_run:
            return self.browse()
        same_segment = prior_run.bucket_ids.filtered(
            lambda b: b.segment_id == self.segment_id)
        hit = same_segment.filtered(lambda b: b.name == self.name)[:1]
        if not hit:
            hit = same_segment.filtered(
                lambda b: b.days_from == self.days_from
                and b.days_to == self.days_to)[:1]
        return hit

    def _check_parent_not_posted(self):
        """Raise when any of these buckets hangs off a posted/reversed run."""
        engine = self.env.su and self.env.context.get('eh_ecl_run_engine')
        if not engine and any(r.state in _FROZEN for r in self.run_id):
            raise UserError(_(
                "This ECL run is posted; its matrix can no longer change. "
                "Reverse it to reopen (EH Accounting Manager only)."))

    @api.model
    def _eh_validate_target(self, vals, current=None):
        run = self.env['eh.ecl.run'].browse(
            vals.get('run_id', current.run_id.id if current else False))
        segment = self.env['eh.ecl.segment'].browse(vals.get(
            'segment_id', current.segment_id.id if current else False))
        if run:
            run._eh_validate_references()
        if segment and not self.env.su:
            segment._eh_check_access('read')
        if segment and run and segment.company_id != run.company_id:
            raise UserError(_(
                "An ECL bucket segment must belong to its run company."
            ))
        return run

    def _eh_lock_parent_runs(self, extra_run_ids=()):
        runs = self.mapped('run_id') | self.env['eh.ecl.run'].browse(
            [run_id for run_id in extra_run_ids if run_id])
        runs._eh_validate_references()
        if not self.env.context.get('eh_ecl_population_prelocked'):
            runs._eh_lock_for_transition()
        runs._eh_validate_references()
        ids = tuple(sorted(self.ids))
        if ids:
            self.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM eh_ecl_bucket WHERE id IN %s "
                "ORDER BY id FOR UPDATE", (ids,))
            self.invalidate_recordset()
        return runs

    @api.model_create_multi
    def create(self, vals_list):
        # Adding a bucket to a posted run would recompute its closing allowance
        # and silently move the recognised movement, bypassing the freeze that
        # write()/unlink() enforce. Block it when the target run is posted or
        # reversed (IFRS 9.5.5.8); reverse the run to reopen the matrix.
        run_ids = {vals.get('run_id') for vals in vals_list if vals.get('run_id')}
        for vals in vals_list:
            self._eh_validate_target(vals)
        runs = self.env['eh.ecl.run'].browse(run_ids)
        if not self.env.context.get('eh_ecl_population_prelocked'):
            runs._eh_lock_for_transition()
        for vals in vals_list:
            self._eh_validate_target(vals)
        if run_ids:
            frozen = runs.filtered(
                lambda r: r.state in _FROZEN)
            engine = self.env.su and self.env.context.get('eh_ecl_run_engine')
            if frozen and not engine:
                raise UserError(_(
                    "Buckets cannot be added to a posted ECL run; its matrix "
                    "is frozen. Reverse the run to reopen it (EH Accounting "
                    "Manager only)."))
        return super().create(vals_list)

    def write(self, vals):
        for bucket in self:
            self._eh_validate_target(vals, current=bucket)
        target_run_ids = (
            (vals.get('run_id'),) if vals.get('run_id') else ())
        self._eh_lock_parent_runs(target_run_ids)
        for bucket in self:
            self._eh_validate_target(vals, current=bucket)
        locked = {'gross_carrying', 'loss_rate', 'days_from', 'days_to',
                  'discount_rate', 'periods_to_recovery', 'maturity_date',
                  'exposure_at_default', 'pd_12m', 'pd_lifetime', 'lgd',
                  'sicr', 'credit_impaired', 'low_credit_risk', 'poci',
                  'stage', 'stage_prior', 'cure_streak', 'segment_id',
                  'origin', 'run_id'}
        if locked.intersection(vals):
            self._check_parent_not_posted()
        # Moving a bucket INTO a posted / reversed run would recompute that
        # run's closing allowance. _check_parent_not_posted only inspects the
        # current (source) parent, so guard the target explicitly.
        if vals.get('run_id'):
            target = self.env['eh.ecl.run'].browse(vals['run_id'])
            engine = self.env.su and self.env.context.get('eh_ecl_run_engine')
            if target.state in _FROZEN and not engine:
                raise UserError(_(
                    "Buckets cannot be moved into a posted ECL run; its "
                    "matrix is frozen. Reverse the run to reopen it "
                    "(EH Accounting Manager only)."))
        return super().write(vals)

    def unlink(self):
        # Deleting a bucket from a posted run would drop it from the closing
        # allowance and move the recognised figure; block it while the parent
        # run is posted or reversed.
        for bucket in self:
            self._eh_validate_target({}, current=bucket)
        self._eh_lock_parent_runs()
        for bucket in self:
            self._eh_validate_target({}, current=bucket)
        self._check_parent_not_posted()
        return super().unlink()
