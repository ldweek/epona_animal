# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Flexible-budget support models.

Two building blocks live here:

* eh.budget.activity -- the manual activity register. One row per
  measurement period on a budget (e.g. "Q1: 1,000 units budgeted,
  1,200 units actual"). Budget lines whose driver is the activity
  register resolve their activity pair by summing the register rows
  that overlap the line's own period.

* eh.budget.forecast.revision (+ line) -- period-stamped rolling
  reforecast snapshots. action_reforecast on the budget re-runs the
  seeding engines over trailing actuals plus re-projected remaining
  periods and stores the result here. The budget's own lines (the
  baseline) are never touched, so baseline-vs-latest-revision variance
  reporting stays honest.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


class EhBudgetActivity(models.Model):
    _name = 'eh.budget.activity'
    _description = "Budget activity register (flexible budget driver)"
    _order = 'period_from, id'

    budget_id = fields.Many2one(
        'eh.budget.budget',
        required=True,
        ondelete='cascade',
        index=True,
        check_company=True,
    )
    company_id = fields.Many2one(
        related='budget_id.company_id', store=True, readonly=True,
    )
    name = fields.Char(
        help="Optional label for the measurement period (e.g. 'Q1 units').",
    )
    period_from = fields.Date(required=True)
    period_to = fields.Date(required=True)
    budgeted_activity = fields.Float(
        required=True, default=0.0, digits=(16, 4),
        help=(
            "Planned activity level for this period, in the budget's "
            "activity unit (units sold, machine hours, headcount...)."
        ),
    )
    actual_activity = fields.Float(
        default=0.0, digits=(16, 4),
        help="Measured activity level for this period.",
    )

    _activity_period_range = models.Constraint(
        'check(period_from <= period_to)',
        'Activity period_from must be before or equal to period_to.',
    )
    _activity_non_negative = models.Constraint(
        'check(budgeted_activity >= 0 AND actual_activity >= 0)',
        'Activity levels must be zero or positive.',
    )

    def _eh_assert_non_overlapping_periods(self):
        """Prevent one activity measure from contributing twice.

        Every create/write path first serialises on the parent budget row via
        ``_eh_lock_scope``. The overlap search is therefore concurrency-safe,
        unlike an ordinary Python constraint racing in two transactions.
        """
        for activity in self:
            if not (
                activity.budget_id
                and activity.period_from
                and activity.period_to
            ):
                continue
            overlap = self.search([
                ('id', '!=', activity.id),
                ('budget_id', '=', activity.budget_id.id),
                ('period_from', '<=', activity.period_to),
                ('period_to', '>=', activity.period_from),
            ], order='period_from, id', limit=1)
            if overlap:
                raise ValidationError(_(
                    "Activity period %(period)s overlaps %(other)s on "
                    "budget %(budget)s. Use non-overlapping measurement "
                    "periods so flexible-budget activity is counted once.",
                    period=activity.display_name,
                    other=overlap.display_name,
                    budget=activity.budget_id.display_name,
                ))

    def _eh_lock_scope(self, extra_budget_ids=()):
        if self and not self.env.su:
            self._eh_check_access('write')
        budgets = self.mapped('budget_id') | self.env[
            'eh.budget.budget'
        ].browse([budget_id for budget_id in extra_budget_ids if budget_id])
        if not self.env.su:
            budgets._eh_check_access('write')
        budgets._eh_lock_for_update()
        ids = tuple(sorted(self.ids))
        if ids:
            self.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM eh_budget_activity WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (ids,),
            )
            self.invalidate_recordset()
        return budgets

    @api.model
    def _eh_assert_baseline_editable(self, budgets):
        if (
            not budgets._eh_basis_engine_allowed()
            and budgets.filtered(lambda budget: budget.state != 'draft')
        ):
            raise UserError(_(
                "Approved budget activity periods are immutable. Create a "
                "new budget version to revise planned activity."
            ))

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and any('company_id' in vals for vals in vals_list):
            raise AccessError(_(
                "Activity-row company is derived from its budget."
            ))
        budgets = self._eh_lock_scope(
            [vals.get('budget_id') for vals in vals_list]
        )
        self._eh_assert_baseline_editable(budgets)
        activities = super().create(vals_list)
        activities._eh_assert_non_overlapping_periods()
        return activities

    def write(self, vals):
        budgets = self._eh_lock_scope((vals.get('budget_id'),))
        baseline_fields = {
            'budget_id', 'period_from', 'period_to', 'budgeted_activity',
            'company_id',
        }
        if baseline_fields & vals.keys():
            self._eh_assert_baseline_editable(budgets)
        result = super().write(vals)
        if {'budget_id', 'period_from', 'period_to'} & vals.keys():
            self._eh_assert_non_overlapping_periods()
        return result

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        budgets = self._eh_lock_scope()
        self._eh_assert_baseline_editable(budgets)
        return super().unlink()


class EhBudgetForecastRevision(models.Model):
    _name = 'eh.budget.forecast.revision'
    _description = "Budget forecast revision (rolling reforecast snapshot)"
    _order = 'revision_date desc, id desc'
    _rec_name = 'name'

    budget_id = fields.Many2one(
        'eh.budget.budget',
        required=True,
        ondelete='restrict',
        index=True,
        check_company=True,
    )
    name = fields.Char(required=True)
    revision_date = fields.Date(
        required=True,
        default=fields.Date.context_today,
        help="Date the reforecast snapshot was taken.",
    )
    company_id = fields.Many2one(
        related='budget_id.company_id', store=True, readonly=True,
    )
    currency_id = fields.Many2one(
        related='budget_id.currency_id', store=False, readonly=True,
    )
    line_ids = fields.One2many(
        'eh.budget.forecast.revision.line', 'revision_id',
    )
    line_count = fields.Integer(compute='_compute_totals')
    total_amount = fields.Monetary(
        compute='_compute_totals',
        currency_field='currency_id',
        help="Sum of the snapshot line amounts (actuals + projections).",
    )
    notes = fields.Char()
    eh_integrity_review_required = fields.Boolean(
        readonly=True, copy=False, index=True,
        help=(
            "Legacy snapshot predates engine-owned evidence controls and "
            "is excluded from latest-revision reporting."
        ),
    )
    eh_integrity_review_reason = fields.Text(readonly=True, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env['eh.budget.budget']._eh_basis_engine_allowed():
            raise AccessError(_(
                "Forecast revisions are generated only by the controlled "
                "Reforecast action."
            ))
        return super().create(vals_list)

    def write(self, vals):
        raise AccessError(_(
            "Forecast revision snapshots are immutable. Generate a new "
            "snapshot instead."
        ))

    def unlink(self):
        raise AccessError(_(
            "Forecast revision snapshots are retained as audit evidence."
        ))

    @api.depends('line_ids.amount')
    def _compute_totals(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)
            rec.total_amount = sum(rec.line_ids.mapped('amount'))


class EhBudgetForecastRevisionLine(models.Model):
    _name = 'eh.budget.forecast.revision.line'
    _description = "Budget forecast revision line"
    _order = 'period_from, account_id, analytic_account_id, id'

    revision_id = fields.Many2one(
        'eh.budget.forecast.revision',
        required=True,
        ondelete='cascade',
        index=True,
        check_company=True,
    )
    company_id = fields.Many2one(
        related='revision_id.company_id', store=True, readonly=True,
    )
    account_id = fields.Many2one(
        'account.account',
        required=True,
        ondelete='restrict',
        index=True,
        check_company=True,
    )
    analytic_account_id = fields.Many2one(
        'account.analytic.account',
        string="Analytic Account",
        ondelete='restrict',
        index=True,
        check_company=True,
        help=(
            "Optional analytic scope inherited from the baseline budget "
            "line. Empty means the snapshot carries the full account balance."
        ),
    )
    period_from = fields.Date(required=True)
    period_to = fields.Date(required=True)
    amount = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Snapshot amount for this account and period: the posted "
            "actual for elapsed periods, the re-projected figure for "
            "remaining periods. Always a positive magnitude, matching "
            "the budgeted_amount convention on budget lines."
        ),
    )
    source = fields.Char(
        help=(
            "Where the amount came from: 'actual' for elapsed periods, "
            "otherwise the projection algorithm used (holt_winters, "
            "linear_trend, mean)."
        ),
    )
    currency_id = fields.Many2one(
        related='revision_id.currency_id', store=False, readonly=True,
    )

    _revision_period_range = models.Constraint(
        'check(period_from <= period_to)',
        'Revision line period_from must be before or equal to period_to.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env['eh.budget.budget']._eh_basis_engine_allowed():
            raise AccessError(_(
                "Forecast revision lines are generated only by the "
                "controlled Reforecast action."
            ))
        return super().create(vals_list)

    def write(self, vals):
        raise AccessError(_("Forecast revision lines are immutable."))

    def unlink(self):
        raise AccessError(_(
            "Forecast revision lines are retained as audit evidence."
        ))
