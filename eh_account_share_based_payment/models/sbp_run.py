# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.sbp.period.run: the IFRS 2 period accounting engine.

One run per plan per period end date. The run computes the cumulative
measure at its period end (equity: grant-date FV x expected-to-vest x
vested fraction plus modification incremental FV; cash: current FV x
expected-to-vest x vested fraction) and posts the DELTA against what the
plan has already recognised:

* equity-settled: Dr expense / Cr SBP equity reserve (reversed legs for a
  true-down or a non-market failure reversal, IFRS 2.20/2.23 contrast);
* cash-settled:   Dr expense / Cr liability (reversed for a fair-value
  fall, IFRS 2.30-33).

Runs post in chronological order and are frozen once posted; the
generated entry is sealed. Estimate changes (forfeiture updates, failed
conditions, final vesting outcomes) flow through the NEXT run's delta:
that is the IFRS 2.20 true-up through the current period.
"""

import math

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .sbp_plan import (
    EH_SBP_ENGINE_CAPABILITY,
    EH_SBP_ENGINE_CONTEXT_KEY,
    EH_SBP_PROVENANCE_VERSION,
    _assert_finite_numeric_vals,
    _assert_finite_record_fields,
    _has_engine_capability,
    _lock_sbp_plans,
    _reject_related_outputs,
)


class EhSbpPeriodRun(models.Model):
    _name = 'eh.sbp.period.run'
    _description = "Share-based payment period run (IFRS 2)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'period_end desc, id desc'
    _rec_name = 'name'
    _check_company_auto = True

    # State moves only through Compute/Post (Post books the sealed journal
    # entry); the mixin blocks any direct RPC write to it by a low-privilege
    # user, closing the "write({'state': 'posted'}) skips the entry" bypass.
    _eh_guarded_fields = ('state',)
    _SERVER_OWNED_FIELDS = frozenset({
        'company_id', 'currency_id', 'plan_settlement',
        'plan_graded_vesting', 'vested_fraction',
        'cumulative_target', 'prior_recognised', 'period_charge', 'move_id',
        'is_zero_charge',
    })
    _NUMERIC_FIELDS = frozenset({
        'current_fair_value', 'vested_fraction', 'cumulative_target',
        'prior_recognised', 'period_charge',
    })

    name = fields.Char(required=True, copy=False, default='/')
    state = fields.Selection(
        [('draft', "Draft"), ('posted', "Posted")],
        default='draft', required=True, tracking=True, index=True)
    plan_id = fields.Many2one(
        'eh.sbp.plan', required=True, ondelete='restrict', index=True,
        check_company=True)
    company_id = fields.Many2one(
        related='plan_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='plan_id.currency_id', store=True, readonly=True)
    plan_settlement = fields.Selection(
        related='plan_id.settlement', string="Settlement")
    plan_graded_vesting = fields.Boolean(
        related='plan_id.graded_vesting', string="Graded Vesting")
    period_end = fields.Date(required=True, tracking=True)
    current_fair_value = fields.Float(
        digits=(16, 4), string="Current FV / Instrument", tracking=True,
        help="Fair value per instrument at the period end. Required for "
             "cash-settled plans: the liability remeasures to it every "
             "period (IFRS 2.30). Ignored for equity-settled plans, which "
             "stay at grant-date fair value (IFRS 2.19).")
    tranche_value_ids = fields.One2many(
        'eh.sbp.period.run.tranche.value', 'run_id',
        string="Current Fair Values by Tranche",
        help="Cash-settled graded awards require one period-end fair value "
             "for every vesting tranche.")

    vested_fraction = fields.Float(
        digits=(16, 6), readonly=True, copy=False,
        help="Straight-line service fraction at the period end (whole "
             "months over total vesting months). Informational for graded "
             "plans, where each tranche applies its own fraction.")
    cumulative_target = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Cumulative expense (equity) or liability (cash) the plan "
             "should carry at this period end.")
    prior_recognised = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id')
    period_charge = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        tracking=True,
        help="Cumulative target minus the amount already recognised; "
             "negative for a true-down or reversal.")
    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False,
        string="Journal Entry", check_company=True)
    is_zero_charge = fields.Boolean(
        readonly=True, copy=False, index=True,
        help="The posted subledger checkpoint had no currency-rounded "
             "journal delta; it intentionally has no journal entry.")
    notes = fields.Char()

    _unique_plan_period = models.Constraint(
        'UNIQUE (plan_id, period_end)',
        'Only one period run per plan and period end date.')

    @api.model_create_multi
    def create(self, vals_list):
        internal = _has_engine_capability(self)
        clean_vals_list = []
        for incoming in vals_list:
            vals = dict(incoming or {})
            blocked = self._SERVER_OWNED_FIELDS.intersection(vals)
            if blocked and not internal:
                raise AccessError(_(
                    "Period-run measurement outputs are server-owned: "
                    "%(fields)s.",
                    fields=', '.join(sorted(blocked)),
                ))
            _assert_finite_numeric_vals(
                vals, self._NUMERIC_FIELDS, _("SBP period run"),
            )
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.sbp.period.run') or '/'
            clean_vals_list.append(vals)
        plans = _lock_sbp_plans(
            self,
            self.env['eh.sbp.plan'].browse(
                [v['plan_id'] for v in clean_vals_list if v.get('plan_id')],
            ),
        )
        plans._eh_assert_not_quarantined_for_edit()
        runs = super().create(clean_vals_list)
        runs._check_company()
        return runs

    # Posted runs are the ledger trail of the cumulative formula: freeze
    # every measurement input once posted.
    _FROZEN_FIELDS = ('plan_id', 'period_end', 'current_fair_value')

    def write(self, vals):
        blocked = self._SERVER_OWNED_FIELDS.intersection(vals)
        if blocked and not _has_engine_capability(self):
            raise AccessError(_(
                "Period-run measurement outputs are server-owned: "
                "%(fields)s.",
                fields=', '.join(sorted(blocked)),
            ))
        _assert_finite_numeric_vals(
            vals, self._NUMERIC_FIELDS, _("SBP period run"),
        )
        if not _has_engine_capability(self):
            target = self.env['eh.sbp.plan'].browse(
                vals.get('plan_id'),
            ) if vals.get('plan_id') else self.env['eh.sbp.plan']
            self._eh_lock_edit_scope(extra_plans=target)
            self.mapped('plan_id')._eh_assert_not_quarantined_for_edit()
        frozen = [f for f in self._FROZEN_FIELDS if f in vals]
        if frozen:
            posted = self.filtered(lambda r: r.state == 'posted')
            if posted:
                raise UserError(_(
                    "A posted period run is frozen (%(fields)s on "
                    "%(runs)s).",
                    fields=', '.join(frozen),
                    runs=', '.join(posted.mapped('display_name'))))
        # STATE transitions are enforced by the inherited eh.workflow.guard
        # (blocks a non-superuser direct write); the sanctioned Compute/Post
        # actions run under sudo. Provenance is env.su, not a context key.
        result = super().write(vals)
        if 'plan_id' in vals:
            self._check_company()
        return result

    @api.constrains(
        'current_fair_value', 'vested_fraction', 'cumulative_target',
        'prior_recognised', 'period_charge',
    )
    def _check_finite_run_numbers(self):
        for run in self:
            values = (
                run.current_fair_value, run.vested_fraction,
                run.cumulative_target, run.prior_recognised,
                run.period_charge,
            )
            if any(not math.isfinite(float(value or 0.0))
                   for value in values):
                raise ValidationError(_(
                    "Every period-run accounting number must be finite."
                ))

    def unlink(self):
        plans = self._eh_lock_edit_scope()
        plans._eh_assert_not_quarantined_for_edit()
        posted = self.filtered(lambda r: r.state == 'posted')
        if posted:
            raise UserError(_(
                "A posted period run cannot be deleted; its journal entry "
                "would be orphaned."))
        return super().unlink()

    # ------------------------------------------------------------------
    # engine
    # ------------------------------------------------------------------
    def _validate_inputs(self):
        self.ensure_one()
        plan = self.plan_id
        plan._eh_assert_provenance_ready()
        plan._eh_assert_finite_measurement_graph()
        _assert_finite_record_fields(
            self, self._NUMERIC_FIELDS, _("SBP period run"),
        )
        if self.company_id != plan.company_id \
                or self.currency_id != plan.currency_id:
            raise UserError(_(
                "The run company/currency anchors do not match its plan."
            ))
        if plan.state != 'active':
            raise UserError(_(
                "Activate plan %s before running the period engine.",
                plan.display_name))
        if self.period_end <= plan.grant_date:
            raise UserError(_(
                "The period end must fall after the grant date."))
        last = max(plan.run_ids.filtered(
            lambda r: r.state == 'posted' and r.id != self.id).mapped(
                'period_end'), default=False)
        if last and self.period_end <= last:
            raise UserError(_(
                "Runs post in chronological order: the last posted run of "
                "%(plan)s covers %(last)s.",
                plan=plan.display_name, last=last))
        if plan.settlement == 'cash':
            if plan.graded_vesting:
                values_by_tranche = {
                    line.tranche_id.id: line.current_fair_value
                    for line in self.tranche_value_ids
                }
                if set(values_by_tranche) != set(plan.tranche_ids.ids):
                    raise UserError(_(
                        "A cash-settled graded run needs exactly one current "
                        "fair value for every plan tranche (IFRS 2.30)."
                    ))
                if self.current_fair_value:
                    raise UserError(_(
                        "A graded cash-settled run uses tranche fair values, "
                        "not the single current fair value field."
                    ))
            elif self.current_fair_value <= 0.0:
                raise UserError(_(
                    "A cash-settled run needs the current fair value per "
                    "instrument (IFRS 2.30)."))
            elif self.tranche_value_ids:
                raise UserError(_(
                    "A non-graded cash-settled run cannot carry tranche fair "
                    "values."
                ))
        elif self.tranche_value_ids:
            raise UserError(_(
                "Equity-settled runs do not accept current tranche fair values."
            ))

    def _measure(self):
        """(cumulative_target, prior, charge) at this run's period end."""
        self.ensure_one()
        plan = self.plan_id
        if plan.settlement == 'cash':
            tranche_values = {
                line.tranche_id.id: line.current_fair_value
                for line in self.tranche_value_ids
            }
            target = plan._cash_liability_at(
                self.period_end,
                self.current_fair_value,
                tranche_values=tranche_values,
            )
        else:
            target = plan._equity_cumulative_at(self.period_end)
        prior = plan.recognised_cumulative
        charge = plan.currency_id.round(target - prior)
        return target, prior, charge

    def action_compute(self):
        for run in self:
            run._eh_check_access('write')
            plan = run.plan_id
            plan._eh_check_access('write')
            plan._check_manager()
            plan = run._eh_lock_post_scope()
            plan._eh_recompute_vesting_anchor()
            if run.state != 'draft':
                raise UserError(_("Only a draft run can be recomputed."))
            run._validate_inputs()
            target, prior, charge = run._measure()
            run.sudo().with_context(**{
                EH_SBP_ENGINE_CONTEXT_KEY: EH_SBP_ENGINE_CAPABILITY,
            }).write({
                'vested_fraction': run.plan_id._service_fraction(
                    run.period_end),
                'cumulative_target': target,
                'prior_recognised': prior,
                'period_charge': charge,
                'is_zero_charge': run.currency_id.is_zero(charge),
            })
        return True

    def _eh_lock_post_scope(self):
        """Lock this run, then its plan, before recomputing/posting.

        Different runs for one plan can be submitted concurrently.  The plan
        lock serializes their shared ``recognised_cumulative`` anchor; the run
        lock makes double-click/retry posting idempotent.
        """
        self.ensure_one()
        return self._eh_lock_edit_scope()

    def _eh_lock_edit_scope(self, extra_plans=None):
        """Lock all selected runs first, then their plans, in id order."""
        if not self:
            return self.env['eh.sbp.plan']
        self._eh_check_access('write')
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_sbp_period_run WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            (tuple(self.ids),),
        )
        self.invalidate_recordset()
        plans = self.mapped('plan_id')
        if extra_plans:
            plans |= extra_plans
        plans = _lock_sbp_plans(self, plans)
        self.invalidate_recordset()
        return plans[:1] if len(plans) == 1 else plans

    def action_post(self):
        self.ensure_one()
        self._eh_check_access('write')
        plan = self.plan_id
        plan._eh_check_access('write')
        plan._check_manager()
        plan = self._eh_lock_post_scope()
        with self.env.cr.savepoint():
            return self._eh_action_post_locked(plan)

    def _eh_action_post_locked(self, plan):
        """Post GL evidence and all run/plan anchors as one transition."""
        self.ensure_one()
        plan._eh_recompute_vesting_anchor()
        if self.state != 'draft':
            raise UserError(_("This period run has already posted."))
        self._validate_inputs()
        # Recompute at post time so a stale preview can never post: the
        # estimates may have moved since Compute was clicked.
        target, prior, charge = self._measure()
        currency = plan.currency_id
        if currency.is_zero(charge):
            self.sudo().with_context(**{
                EH_SBP_ENGINE_CONTEXT_KEY: EH_SBP_ENGINE_CAPABILITY,
            }).write({
                'vested_fraction': plan._service_fraction(self.period_end),
                'cumulative_target': target,
                'prior_recognised': prior,
                'period_charge': charge,
                'move_id': False,
                'is_zero_charge': True,
            })
            plan._eh_engine_write({'recognised_cumulative': target})
            self.sudo().with_context(**{
                EH_SBP_ENGINE_CONTEXT_KEY: EH_SBP_ENGINE_CAPABILITY,
            }).write({'state': 'posted'})
            return True
        if plan.settlement == 'equity':
            plan._validate_accounts(['expense', 'equity'])
            counter = plan.equity_account_id
            up_label = _("SBP expense %s", self.name)
            down_label = _("SBP true-up reversal %s", self.name)
        else:
            plan._validate_accounts(['expense', 'liability'])
            counter = plan.liability_account_id
            up_label = _("SAR expense %s", self.name)
            down_label = _("SAR remeasurement gain %s", self.name)
        if currency.compare_amounts(charge, 0.0) > 0:
            move = plan._post_move([
                (plan.expense_account_id, charge, 0.0, up_label),
                (counter, 0.0, charge, up_label),
            ], date=self.period_end, ref=self.name)
        else:
            move = plan._post_move([
                (counter, -charge, 0.0, down_label),
                (plan.expense_account_id, 0.0, -charge, down_label),
            ], date=self.period_end, ref=self.name)
        if (
            move.company_id != plan.company_id
            or move.eh_sbp_plan_id != plan
            or move.eh_sbp_provenance_version
               != EH_SBP_PROVENANCE_VERSION
            or move.reversed_entry_id
            or move.state != 'posted'
            or ('eh_sealed' in move._fields and not move.eh_sealed)
        ):
            raise UserError(_(
                "The generated journal entry failed its sealed SBP "
                "provenance validation."
            ))
        self.sudo().with_context(**{
            EH_SBP_ENGINE_CONTEXT_KEY: EH_SBP_ENGINE_CAPABILITY,
        }).write({
            'vested_fraction': plan._service_fraction(self.period_end),
            'cumulative_target': target,
            'prior_recognised': prior,
            'period_charge': charge,
            'move_id': move.id,
            'is_zero_charge': False,
        })
        # recognised_cumulative is a guarded anchor on the plan (see
        # eh.sbp.plan._eh_guarded_fields); the sanctioned post writes it as su
        # so a real (non-superuser) manager's post is not refused by the guard.
        plan._eh_engine_write({'recognised_cumulative': target})
        self.sudo().with_context(**{
            EH_SBP_ENGINE_CONTEXT_KEY: EH_SBP_ENGINE_CAPABILITY,
        }).write({'state': 'posted'})
        return True


class EhSbpPeriodRunTrancheValue(models.Model):
    _name = 'eh.sbp.period.run.tranche.value'
    _description = "Cash-settled period fair value by vesting tranche"
    _order = 'tranche_id, id'
    _check_company_auto = True
    _NUMERIC_FIELDS = frozenset({'current_fair_value'})

    run_id = fields.Many2one(
        'eh.sbp.period.run', required=True, ondelete='cascade', index=True,
        check_company=True)
    plan_id = fields.Many2one(
        related='run_id.plan_id', store=True, index=True)
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='run_id.currency_id', store=True, readonly=True)
    tranche_id = fields.Many2one(
        'eh.sbp.plan.tranche', required=True, ondelete='restrict',
        check_company=True)
    current_fair_value = fields.Float(
        digits=(16, 4), required=True,
        string="Current FV / Instrument")

    _unique_run_tranche = models.Constraint(
        'UNIQUE (run_id, tranche_id)',
        'Enter one period-end fair value per tranche.')
    _check_positive_fair_value = models.Constraint(
        'CHECK (current_fair_value > 0)',
        'A tranche current fair value must be positive.')

    @api.constrains('current_fair_value', 'tranche_id', 'run_id')
    def _check_value_scope(self):
        for line in self:
            if not math.isfinite(line.current_fair_value):
                raise ValidationError(_(
                    "Tranche current fair value must be finite."
                ))
            if line.tranche_id.plan_id != line.plan_id:
                raise ValidationError(_(
                    "A period fair-value line must use a tranche from its "
                    "own plan."
                ))
            if line.plan_id.settlement != 'cash' \
                    or not line.plan_id.graded_vesting:
                raise ValidationError(_(
                    "Period tranche fair values belong only to a graded "
                    "cash-settled plan."
                ))

    @api.model
    def _eh_assert_draft_runs(self, runs):
        posted = runs.filtered(lambda run: run.state != 'draft')
        if posted:
            raise UserError(_(
                "Posted period-run tranche fair values are immutable: %s.",
                ', '.join(posted.mapped('display_name')),
            ))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            _reject_related_outputs(
                vals, {'plan_id', 'company_id', 'currency_id'},
                _("SBP period tranche value"),
            )
            _assert_finite_numeric_vals(
                vals, self._NUMERIC_FIELDS,
                _("SBP period tranche value"),
            )
        runs = self.env['eh.sbp.period.run'].browse([
            vals['run_id'] for vals in vals_list if vals.get('run_id')
        ])
        runs._eh_lock_edit_scope()
        self._eh_assert_draft_runs(runs)
        records = super().create(vals_list)
        records._check_company()
        return records

    def write(self, vals):
        _reject_related_outputs(
            vals, {'plan_id', 'company_id', 'currency_id'},
            _("SBP period tranche value"),
        )
        _assert_finite_numeric_vals(
            vals, self._NUMERIC_FIELDS,
            _("SBP period tranche value"),
        )
        runs = self.mapped('run_id')
        if vals.get('run_id'):
            runs |= self.env['eh.sbp.period.run'].browse(vals['run_id'])
        runs._eh_lock_edit_scope()
        self._eh_assert_draft_runs(runs)
        result = super().write(vals)
        self._check_company()
        return result

    def unlink(self):
        runs = self.mapped('run_id')
        runs._eh_lock_edit_scope()
        self._eh_assert_draft_runs(runs)
        return super().unlink()
