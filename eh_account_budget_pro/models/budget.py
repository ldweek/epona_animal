# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Budget Pro models: budget header + budget lines.

A budget is a named period (date_from to date_to) carrying a list of
lines. Each line pins one account to a sub period within the budget and
sets a budgeted_amount. The actual_amount is computed from posted
journal items, in batch per budget, so a budget with hundreds of lines
runs one SQL query rather than one per line.

Versioning: action_create_version copies a budget to a new draft version
with a parent_id link. Stable root codes and v1 / v2 / v3 ordinals keep the
parent chain readable without accumulating code suffixes.

Lifecycle: draft -> confirmed -> closed. Actuals are computed at all
states so users can monitor a draft vs reality before confirming.
"""

import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from math import isfinite

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import SQL
from odoo.tools.misc import format_amount

from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery


_CODE_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]*$')

_BUDGET_BASIS_CAPABILITY = object()
_BUDGET_BASIS_CONTEXT_KEY = 'eh_budget_basis_capability'
_BUDGET_VERSION_CAPABILITY = object()
_BUDGET_VERSION_CONTEXT_KEY = 'eh_budget_version_capability'

# Credit-natured account types: actual balances on these post negative in
# Odoo convention while budget figures are entered as positive magnitudes,
# so variance-style computes flip the actual sign before comparing. Kept
# as one module constant so every compute normalises identically.
_INCOME_LIKE = (
    'income', 'income_other', 'liability_payable',
    'liability_credit_card', 'liability_current',
    'liability_non_current', 'equity', 'equity_unaffected',
)


def _eh_analytic_percentage(raw_value):
    """Accept JSON numbers only; return one finite, contributing percentage."""
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        return None
    try:
        percentage = float(raw_value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not isfinite(percentage) or percentage == 0.0:
        return None
    return percentage


def _eh_finite_float(raw_value, default=0.0):
    """Coerce trusted numeric engine output without propagating NaN/Inf."""
    if isinstance(raw_value, bool):
        return default
    try:
        value = float(raw_value)
    except (TypeError, ValueError, OverflowError):
        return default
    return value if isfinite(value) else default


def _eh_where_analytic_keys(query, analytic_ids):
    """Push a whitespace-tolerant composite analytic-key union to SQL."""
    keys = [str(int(analytic_id)) for analytic_id in analytic_ids or ()]
    if not keys:
        return query
    return query.where_raw(SQL(
        "EXISTS ("
        "SELECT 1 FROM jsonb_object_keys(CASE "
        "WHEN jsonb_typeof(aml.analytic_distribution) = 'object' "
        "THEN aml.analytic_distribution ELSE '{}'::jsonb END"
        ") AS analytic_key(key) "
        "WHERE regexp_split_to_array("
        "btrim(analytic_key.key), '\\s*,\\s*') && %s::text[]"
        ")",
        keys,
    ))


class EhBudget(models.Model):
    _name = 'eh.budget.budget'
    _description = "Budget"
    _order = 'date_from desc, name'
    _inherit = [
        'mail.thread', 'mail.activity.mixin', 'eh.cron.batch.mixin',
        'eh.workflow.guard',
    ]
    _rec_name = 'name'

    # State advances only through the lifecycle actions below (each runs as
    # su via _eh_workflow_action); a direct non-superuser write to state is
    # refused by eh.workflow.guard, closing the RPC-bypass of action_confirm
    # and its checks.
    _eh_guarded_fields = (
        'state', 'eh_commitment_integrity_review_required',
        'eh_commitment_integrity_review_reason', 'revised_budget_id',
        'parent_id', 'version_label', 'version_root_code',
        'version_sequence',
        'eh_activity_integrity_review_required',
        'eh_activity_integrity_review_reason',
        'eh_version_integrity_review_required',
        'eh_version_integrity_review_reason',
        'eh_basis_integrity_review_required',
        'eh_basis_integrity_review_reason',
    )
    _eh_create_guarded_fields = (
        'state', 'eh_commitment_integrity_review_required',
        'eh_commitment_integrity_review_reason', 'revised_budget_id',
        'parent_id', 'version_label',
        'eh_activity_integrity_review_required',
        'eh_activity_integrity_review_reason',
        'eh_version_integrity_review_required',
        'eh_version_integrity_review_reason',
        'eh_basis_integrity_review_required',
        'eh_basis_integrity_review_reason',
    )

    _FROZEN_BASIS_FIELDS = frozenset({
        'name', 'code', 'company_id', 'date_from', 'date_to', 'parent_id',
        'version_label', 'version_root_code', 'version_sequence',
        'is_rolling', 'overrun_policy', 'budget_type',
        'activity_uom', 'line_ids',
    })
    _MANAGER_POLICY_FIELDS = frozenset({
        'overrun_policy',
    })

    name = fields.Char(required=True, tracking=True)
    code = fields.Char(required=True, copy=False)

    company_id = fields.Many2one(
        'res.company',
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id',
        store=True,
        readonly=True,
    )
    eh_report_cache_version = fields.Integer(
        default=0,
        readonly=True,
        copy=False,
        help=(
            "Server-owned monotonic version for budget-aware report cache "
            "freshness."
        ),
    )

    date_from = fields.Date(required=True, tracking=True)
    date_to = fields.Date(required=True, tracking=True)

    parent_id = fields.Many2one(
        'eh.budget.budget',
        ondelete='restrict',
        check_company=True,
        help="Previous version of this budget. Forms a versioning chain.",
    )
    version_label = fields.Char(default="v1", required=True, readonly=True)
    version_root_code = fields.Char(
        required=True,
        readonly=True,
        copy=False,
        index=True,
        help=(
            "Stable code of the first budget in this version chain. "
            "Successor codes are derived from this root instead of appending "
            "suffixes to the previous successor."
        ),
    )
    version_sequence = fields.Integer(
        required=True,
        readonly=True,
        copy=False,
        default=1,
        help="Server-owned ordinal in the version chain: 1, 2, 3, and so on.",
    )
    is_rolling = fields.Boolean(
        default=False,
        help="When enabled, a future cron can extend this budget rolling forward.",
    )
    overrun_policy = fields.Selection(
        [
            ('off', "Off (no commitment tracking)"),
            ('warn', "Warn (commit + warn at confirm)"),
            ('block', "Block (commit + block at confirm)"),
        ],
        default='off', required=True, tracking=True,
        help=(
            "Encumbrance behaviour on this budget. off: ignore "
            "purchase orders entirely (the historical default). "
            "warn: create a commitment when a PO is confirmed and "
            "post a warning to the budget chatter when the line "
            "would go negative. block: prevent the PO confirm when "
            "available is insufficient. Per-budget rather than "
            "per-line so a single policy decision covers an entire "
            "budget version."
        ),
    )
    eh_commitment_integrity_review_required = fields.Boolean(
        readonly=True, copy=False, index=True,
        help=(
            "Legacy source-backed commitment provenance is unverified. "
            "Purchase-order commitment creation is blocked; create a "
            "reviewed replacement budget version instead of trusting old "
            "client-writable source anchors."
        ),
    )
    eh_commitment_integrity_review_reason = fields.Text(
        readonly=True, copy=False,
    )
    eh_activity_integrity_review_required = fields.Boolean(
        readonly=True, copy=False, index=True,
        help=(
            "Legacy activity baseline rows predate approved-parent freeze "
            "controls. Flexible-budget calculations ignore them until a "
            "reviewed replacement budget is approved."
        ),
    )
    eh_activity_integrity_review_reason = fields.Text(
        readonly=True, copy=False,
    )
    eh_version_integrity_review_required = fields.Boolean(
        readonly=True, copy=False, index=True,
        help=(
            "Legacy version links predate engine-owned lineage controls. "
            "They remain visible but cannot be extended as trusted lineage."
        ),
    )
    eh_version_integrity_review_reason = fields.Text(
        readonly=True, copy=False,
    )
    eh_basis_integrity_review_required = fields.Boolean(
        readonly=True, copy=False, index=True,
        help=(
            "Legacy approved header and line baselines predate immutable "
            "budget-basis controls. They remain visible as untrusted audit "
            "evidence but cannot drive commitments, revisions, or forecasts."
        ),
    )
    eh_basis_integrity_review_reason = fields.Text(
        readonly=True, copy=False,
    )

    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('confirmed', "Confirmed"),
            ('revised', "Revised"),
            ('closed', "Closed"),
        ],
        default='draft',
        required=True,
        tracking=True,
        index=True,
    )
    budget_type = fields.Selection(
        [
            ('expense', "Expense"),
            ('revenue', "Revenue"),
            ('both', "Revenue & Expense"),
        ],
        default='both', required=True, tracking=True,
        help=(
            "Scope of the budget. Drives which account types the split "
            "wizard offers and documents the budget's intent; 'both' "
            "places no restriction."
        ),
    )
    revised_budget_id = fields.Many2one(
        'eh.budget.budget', readonly=True, copy=False, ondelete='restrict',
        check_company=True,
        string="Superseded by",
        help="The revision that superseded this budget, if any.",
    )

    line_ids = fields.One2many(
        'eh.budget.line',
        'budget_id',
        copy=True,
    )
    line_count = fields.Integer(compute='_compute_line_count')

    total_budgeted = fields.Monetary(
        compute='_compute_totals',
        currency_field='currency_id',
    )
    total_actual = fields.Monetary(
        compute='_compute_totals',
        currency_field='currency_id',
    )
    total_variance = fields.Monetary(
        compute='_compute_totals',
        currency_field='currency_id',
    )
    total_variance_pct = fields.Float(
        compute='_compute_totals',
        digits=(8, 2),
    )
    total_theoretical = fields.Monetary(
        compute='_compute_totals',
        currency_field='currency_id',
        help="Sum of line theoretical (time-prorated) amounts to date.",
    )

    # ---- flexible budget ----
    activity_uom = fields.Char(
        string="Activity Unit",
        help=(
            "Label for the activity measure driving flexible-budget "
            "lines (units sold, machine hours, seats...). Documentation "
            "only; the maths uses the activity register values."
        ),
    )
    activity_period_ids = fields.One2many(
        'eh.budget.activity', 'budget_id',
        copy=True,
        help=(
            "Manual activity register: one row per measurement period "
            "with the budgeted and actual activity level. Lines whose "
            "driver is the activity register sum the rows overlapping "
            "their own period."
        ),
    )
    total_flexed = fields.Monetary(
        compute='_compute_flex_totals',
        currency_field='currency_id',
        help="Sum of line flexed amounts (budget restated at actual activity).",
    )
    total_flexed_variance = fields.Monetary(
        compute='_compute_flex_totals',
        currency_field='currency_id',
        help="Sum of line flexed variances (actual minus flexed budget).",
    )
    total_volume_variance = fields.Monetary(
        compute='_compute_flex_totals',
        currency_field='currency_id',
        help="Sum of line volume variances (flexed minus static budget).",
    )

    # ---- rolling reforecast ----
    forecast_revision_ids = fields.One2many(
        'eh.budget.forecast.revision', 'budget_id',
        copy=False,
        help=(
            "Period-stamped reforecast snapshots. The baseline lines are "
            "never overwritten; each reforecast stores a new revision."
        ),
    )
    active_revision_id = fields.Many2one(
        'eh.budget.forecast.revision',
        compute='_compute_active_revision',
        string="Latest Reforecast",
        help="Most recent forecast revision (by revision date, then id).",
    )
    revision_count = fields.Integer(compute='_compute_active_revision')

    notes = fields.Html()

    _unique_code_company = models.Constraint(
        'unique(code, company_id)',
        'Budget code must be unique per company.',
    )
    _unique_version_sequence = models.Constraint(
        'unique(company_id, version_root_code, version_sequence)',
        'Budget version sequence must be unique within its company and chain.',
    )
    _positive_version_sequence = models.Constraint(
        'check(version_sequence >= 1)',
        'Budget version sequence must be positive.',
    )
    _date_range = models.Constraint(
        'check(date_from <= date_to)',
        'date_from must be before or equal to date_to.',
    )

    @api.constrains('code')
    def _check_code_format(self):
        for rec in self:
            if not _CODE_RE.match(rec.code or ''):
                raise ValidationError(_(
                    "Budget code must match [a-zA-Z][a-zA-Z0-9_]* (got %r).",
                ) % rec.code)

    @api.depends('line_ids')
    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    @api.depends(
        'line_ids.budgeted_amount',
        'line_ids.normalised_actual_amount',
        'line_ids.variance_amount',
        'line_ids.theoretical_amount',
    )
    def _compute_totals(self):
        monetary_handler = self.env['eh.account.dynamic.report.handler']
        for rec in self:
            currency = rec.currency_id

            def _round_monetary(value):
                return monetary_handler._eh_round_monetary(
                    value, currency=currency,
                )

            # Sum the same currency-rounded figures rendered on each line,
            # so header and PDF totals reconcile with the visible detail.
            budgeted = _round_monetary(sum(
                _round_monetary(line.budgeted_amount or 0.0)
                for line in rec.line_ids
            ))
            actual = _round_monetary(sum(
                line.normalised_actual_amount or 0.0
                for line in rec.line_ids
            ))
            variance = _round_monetary(sum(
                line.variance_amount or 0.0
                for line in rec.line_ids
            ))
            rec.total_budgeted = budgeted
            rec.total_actual = actual
            rec.total_variance = variance
            rec.total_variance_pct = self._safe_variance_pct(budgeted, actual)
            rec.total_theoretical = _round_monetary(sum(
                _round_monetary(line.theoretical_amount or 0.0)
                for line in rec.line_ids
            ))

    @staticmethod
    def _safe_variance_pct(budgeted, actual):
        """Variance percentage that does not silently swallow overruns.

        With a zero budget, the standard formula would divide by zero so
        the previous code returned 0%, which read as 'no variance' even
        when actual was non zero. Now we return 100.0 to signal a full
        overrun against the zero baseline; views can pair this with the
        is_zero_budget flag to render an explicit 'n/a' badge instead of
        a misleading number.
        """
        if budgeted:
            return (actual - budgeted) / budgeted * 100.0
        if actual:
            return 100.0
        return 0.0

    @api.depends('line_ids.flexed_amount', 'line_ids.flexed_variance',
                 'line_ids.volume_variance')
    def _compute_flex_totals(self):
        for rec in self:
            rec.total_flexed = sum(rec.line_ids.mapped('flexed_amount'))
            rec.total_flexed_variance = sum(
                rec.line_ids.mapped('flexed_variance'))
            rec.total_volume_variance = sum(
                rec.line_ids.mapped('volume_variance'))

    @api.depends(
        'forecast_revision_ids.revision_date',
        'forecast_revision_ids.eh_integrity_review_required',
    )
    def _compute_active_revision(self):
        for rec in self:
            revisions = rec.forecast_revision_ids.filtered(
                lambda revision: not revision.eh_integrity_review_required
            ).sorted(
                key=lambda r: (
                    r.revision_date or fields.Date.today(), r.id or 0,
                ),
                reverse=True,
            )
            rec.active_revision_id = revisions[:1]
            rec.revision_count = len(revisions)

    # ---- lifecycle ----

    @api.model
    def _eh_require_budget_manager(self):
        if (
            not self.env.su
            and not self.env.user.has_group(
                'eh_account_base.group_eh_manager'
            )
        ):
            raise AccessError(_(
                "Only an EH Accounting Manager may approve, close, "
                "version, or change the enforcement policy of a budget."
            ))

    @api.model
    def _eh_validate_company_scope(self, company_id):
        company_id = int(company_id or self.env.company.id)
        if not self.env.su and company_id not in self.env.companies.ids:
            raise AccessError(_(
                "A budget may only use a company enabled in the caller's "
                "current company context."
            ))
        return company_id

    def _eh_assert_single_analytic_plan(self):
        """Keep grand totals additive across the budget's analytic axis."""
        for budget in self:
            plans = budget.line_ids.mapped('analytic_account_id.plan_id')
            if len(plans) > 1:
                raise ValidationError(_(
                    "Budget %(budget)s contains analytic accounts from more "
                    "than one analytic plan. Cross-plan amounts describe "
                    "parallel dimensions and cannot be added into one total. "
                    "Create one budget per analytic plan.",
                    budget=budget.display_name,
                ))
        return True

    def _eh_assert_non_overlapping_actual_scopes(self):
        """Keep header/pivot actuals additive across approved lines.

        Two lines for the same account and analytic scope cannot cover the
        same day.  An account-wide line also cannot overlap any analytic line
        for that account: the former already contains the latter's amount and
        summing both would double count actuals.  Different analytic accounts
        in the one validated plan remain independent dimensions and may share
        a period.
        """
        for budget in self:
            intervals_by_account = defaultdict(
                lambda: defaultdict(list),
            )
            for line in budget.line_ids:
                if not (line.account_id and line.period_from and line.period_to):
                    continue
                scope = line.analytic_account_id.id or False
                intervals_by_account[line.account_id.id][scope].append((
                    line.period_from, line.period_to,
                ))

            for account_id, intervals_by_scope in intervals_by_account.items():
                for scope, intervals in intervals_by_scope.items():
                    ordered = sorted(intervals)
                    for previous, current in zip(ordered, ordered[1:]):
                        if current[0] <= previous[1]:
                            raise ValidationError(_(
                                "Budget %(budget)s contains overlapping "
                                "periods for account %(account)s and the "
                                "same analytic scope. Split or consolidate "
                                "those lines before approval.",
                                budget=budget.display_name,
                                account=self.env['account.account'].browse(
                                    account_id,
                                ).display_name,
                            ))

                global_intervals = sorted(
                    intervals_by_scope.get(False, []),
                )
                if not global_intervals:
                    continue
                for scope, analytic_intervals in intervals_by_scope.items():
                    if not scope:
                        continue
                    left = right = 0
                    analytic_intervals = sorted(analytic_intervals)
                    while (
                        left < len(global_intervals)
                        and right < len(analytic_intervals)
                    ):
                        global_start, global_end = global_intervals[left]
                        analytic_start, analytic_end = analytic_intervals[right]
                        if global_end < analytic_start:
                            left += 1
                        elif analytic_end < global_start:
                            right += 1
                        else:
                            raise ValidationError(_(
                                "Budget %(budget)s contains an account-wide "
                                "line overlapping an analytic line for "
                                "account %(account)s. That would double "
                                "count actuals in totals.",
                                budget=budget.display_name,
                                account=self.env['account.account'].browse(
                                    account_id,
                                ).display_name,
                            ))
        return True

    def _eh_lock_for_update(self):
        if not self.env.su:
            self._eh_check_access('write')
        ids = tuple(sorted(self.ids))
        if ids:
            self.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM eh_budget_budget WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (ids,),
            )
            self.invalidate_recordset()
        return self

    def _eh_basis_engine_allowed(self):
        return bool(
            self.env.su
            and self.env.context.get(_BUDGET_BASIS_CONTEXT_KEY)
            is _BUDGET_BASIS_CAPABILITY
        )

    @api.private
    def _eh_report_cache_snapshot(self):
        """Return one constant-time, transaction-stable cache version.

        Every output-relevant line mutation first locks this parent FOR
        UPDATE.  Taking a SHARE lock here therefore prevents either cached or
        newly computed output from crossing a concurrent version change.  The
        lock is one indexed row read; budget size does not affect cache hits.
        """
        self.ensure_one()
        self._eh_check_access('read')
        self.env.cr.execute(
            "SELECT COALESCE(eh_report_cache_version, 0) "
            "FROM eh_budget_budget "
            "WHERE id = %s FOR SHARE",
            (self.id,),
        )
        row = self.env.cr.fetchone()
        return int(row[0]) if row else None

    @api.private
    def _eh_bump_report_cache_version(self):
        """Atomically advance selected-budget cache identity.

        Callers already hold each budget row FOR UPDATE.  Direct SQL keeps a
        multi-record mutation to one bump per affected budget and prevents
        concurrent increments from collapsing into one value.
        """
        ids = tuple(sorted(set(int(budget_id) for budget_id in self.ids)))
        if not ids:
            return
        self.env.cr.execute(
            "UPDATE eh_budget_budget SET eh_report_cache_version = "
            "COALESCE(eh_report_cache_version, 0) + 1 WHERE id IN %s",
            (ids,),
        )
        self.browse(ids).invalidate_recordset(['eh_report_cache_version'])

    def _eh_prepare_manager_transition(self, related=None):
        self._eh_check_access('write')
        self._eh_require_budget_manager()
        related = related or self.env[self._name]
        records_to_lock = self | related
        records_to_lock._eh_check_access('write')
        records_to_lock._eh_lock_for_update()
        self._eh_check_access('write')
        self._eh_require_budget_manager()
        return self._eh_workflow_action()

    @api.model_create_multi
    def create(self, vals_list):
        version_engine = bool(
            self.env.su
            and self.env.context.get(_BUDGET_VERSION_CONTEXT_KEY)
            is _BUDGET_VERSION_CAPABILITY
        )
        protected_version_fields = {
            'version_root_code', 'version_sequence',
        }
        protected_version_defaults = {
            'default_%s' % field_name
            for field_name in protected_version_fields
        }
        if not version_engine and (
            any(
                protected_version_fields.intersection(vals)
                for vals in vals_list
            )
            or protected_version_defaults.intersection(self.env.context)
        ):
            raise AccessError(_(
                "Budget version root and sequence are assigned only by the "
                "versioning engine."
            ))
        if (
            any('eh_report_cache_version' in vals for vals in vals_list)
            or 'default_eh_report_cache_version' in self.env.context
        ):
            raise AccessError(_(
                "The budget report-cache version is server-owned and "
                "cannot be supplied when creating a budget."
            ))
        default_policy = self.default_get(
            ['overrun_policy'],
        ).get('overrun_policy', 'off')
        for vals in vals_list:
            self._eh_validate_company_scope(vals.get('company_id'))
            if not version_engine:
                vals['version_root_code'] = vals.get('code')
                vals['version_sequence'] = 1
                vals['version_label'] = 'v1'
            policy = vals.get('overrun_policy', default_policy)
            if policy in ('warn', 'block'):
                self._eh_require_budget_manager()
        return super().create(vals_list)

    def write(self, vals):
        version_fields = {
            'version_label', 'version_root_code', 'version_sequence',
        }.intersection(vals)
        version_engine = bool(
            self.env.su
            and self.env.context.get(_BUDGET_VERSION_CONTEXT_KEY)
            is _BUDGET_VERSION_CAPABILITY
        )
        if version_fields and not version_engine:
            raise AccessError(_(
                "Budget version identity is maintained only by the "
                "versioning engine: %(fields)s.",
                fields=', '.join(sorted(version_fields)),
            ))
        if 'eh_report_cache_version' in vals:
            raise AccessError(_(
                "The budget report-cache version is server-owned and "
                "cannot be edited directly."
            ))
        self._eh_check_access('write')
        if 'code' in vals and self.filtered(
            lambda budget: (
                budget.state != 'draft'
                or budget.parent_id
                or budget.revised_budget_id
                or budget.version_sequence != 1
            )
        ):
            raise UserError(_(
                "A version-chain code is immutable. Rename only an "
                "independent first-version draft."
            ))
        if self._MANAGER_POLICY_FIELDS & vals.keys():
            self._eh_require_budget_manager()
        if 'company_id' in vals:
            self._eh_validate_company_scope(vals['company_id'])
        self._eh_lock_for_update()
        self._eh_check_access('write')
        if self._MANAGER_POLICY_FIELDS & vals.keys():
            self._eh_require_budget_manager()
        frozen = self._FROZEN_BASIS_FIELDS & vals.keys()
        if (
            frozen
            and not self._eh_basis_engine_allowed()
            and self.filtered(lambda budget: budget.state != 'draft')
        ):
            raise UserError(_(
                "Confirmed budget basis (%(fields)s) is immutable. Create "
                "a new budget version for corrections.",
                fields=', '.join(sorted(frozen)),
            ))
        if 'company_id' in vals and self.filtered(
            lambda budget: budget.company_id.id != vals['company_id']
        ):
            raise UserError(_(
                "Budget company is immutable after creation. Create a new "
                "company-specific budget instead."
            ))
        result = super().write(vals)
        if 'code' in vals:
            # Keep the stable chain root aligned while the first version is
            # still an independent draft. Once approved or linked, the guard
            # above freezes both identifiers permanently.
            self.env.cr.execute(
                "UPDATE eh_budget_budget SET version_root_code = %s "
                "WHERE id = ANY(%s)",
                (vals['code'], self.ids),
            )
            self.invalidate_recordset(['version_root_code'])
        return result

    def unlink(self):
        self._eh_check_access('unlink')
        self._eh_require_budget_manager()
        self._eh_lock_for_update()
        if self.filtered(lambda budget: budget.state != 'draft'):
            raise UserError(_(
                "A confirmed, revised, or closed budget is retained as "
                "evidence. Delete only unused drafts."
            ))
        if self.filtered(
            lambda budget: (
                budget.parent_id
                or budget.revised_budget_id
                or self.search_count([
                    '|',
                    ('parent_id', '=', budget.id),
                    ('revised_budget_id', '=', budget.id),
                ])
            )
        ):
            raise UserError(_(
                "A budget participating in a version chain is retained as "
                "audit evidence."
            ))
        return super().unlink()

    def action_confirm(self):
        parents = self.mapped('parent_id')
        self._eh_check_access('write')
        self._eh_require_budget_manager()
        companies = (self | parents).mapped('company_id')
        companies._eh_lock_budget_governance(shared=False)
        self = self._eh_prepare_manager_transition(related=parents)
        for rec in self:
            if rec.state != 'draft':
                continue
            if (
                rec.eh_commitment_integrity_review_required
                or rec.eh_activity_integrity_review_required
                or rec.eh_version_integrity_review_required
                or rec.eh_basis_integrity_review_required
            ):
                raise UserError(_(
                    "This legacy budget contains quarantined control "
                    "evidence. Create an independent reviewed replacement "
                    "instead of approving it."
                ))
            if not rec.line_ids:
                raise UserError(_(
                    "Cannot confirm a budget with no lines: %s",
                ) % rec.name)
            rec._eh_assert_single_analytic_plan()
            rec._eh_assert_non_overlapping_actual_scopes()
            parent = rec.parent_id
            if parent:
                if parent.revised_budget_id != rec:
                    raise UserError(_(
                        "The budget revision link is incomplete. Reopen the "
                        "source budget and create one reviewed revision."
                    ))
                if parent.state == 'confirmed':
                    parent.state = 'revised'
                elif parent.state != 'closed':
                    raise UserError(_(
                        "Only a live or closed source budget can be "
                        "superseded by an approved revision."
                    ))
            rec.state = 'confirmed'
        companies._eh_refresh_budget_governance()
        return True

    def action_close(self):
        self._eh_check_access('write')
        self._eh_require_budget_manager()
        companies = self.mapped('company_id')
        companies._eh_lock_budget_governance(shared=False)
        self = self._eh_prepare_manager_transition()
        for rec in self:
            if rec.state not in ('confirmed', 'revised'):
                raise UserError(_(
                    "Only a confirmed or revised budget can be closed."
                ))
            rec.state = 'closed'
        companies._eh_refresh_budget_governance()
        return True

    def action_recompute_actuals(self):
        """Force a recompute of actual_amount on every line."""
        for rec in self:
            rec.line_ids.invalidate_recordset(['actual_amount'])
        return True

    def action_create_version(self):
        """Copy each budget as a new draft version with parent_id link."""
        if len(self) > 1:
            raise UserError(_("Create one version at a time."))
        self = self._eh_prepare_manager_transition()
        rec = self
        if rec.state == 'draft':
            raise UserError(_(
                "Approve the initial draft before creating a revision."
            ))
        if rec.state == 'revised' or rec.revised_budget_id:
            raise UserError(_(
                "This budget already participates in a successor chain. "
                "Continue from its latest version instead."
            ))
        if (
            rec.eh_version_integrity_review_required
            or rec.eh_basis_integrity_review_required
        ):
            raise UserError(_(
                "Legacy budget basis or version lineage is quarantined. "
                "Create a new, independent reviewed budget instead of "
                "extending it."
            ))
        new_sequence = rec.version_sequence + 1
        new_label = self._version_label(new_sequence)
        new_code = self._next_version_code(
            rec.version_root_code, new_sequence,
        )
        if self.search_count([
            ('company_id', '=', rec.company_id.id),
            ('code', '=', new_code),
        ]):
            raise UserError(_(
                "Canonical successor code %(code)s is already used in "
                "%(company)s. Rename the unrelated draft before creating "
                "this version.",
                code=new_code,
                company=rec.company_id.display_name,
            ))
        copy_values = {
            'name': "%s (%s)" % (rec.name, new_label),
            'code': new_code,
            'version_label': new_label,
            'version_root_code': rec.version_root_code,
            'version_sequence': new_sequence,
            'parent_id': rec.id,
            'state': 'draft',
        }
        if rec.eh_activity_integrity_review_required:
            copy_values['activity_period_ids'] = []
        copy = rec.with_context(**{
            _BUDGET_VERSION_CONTEXT_KEY: _BUDGET_VERSION_CAPABILITY,
        }).copy(copy_values)
        # Keep the approved source live while the replacement is only a
        # draft. The source is retired atomically by the replacement's
        # action_confirm after all approval checks pass.
        rec.write({'revised_budget_id': copy.id})
        return {
            'type': 'ir.actions.act_window',
            'name': _("New Budget Version"),
            'res_model': self._name,
            'view_mode': 'form',
            'views': [(False, 'form')],
            'res_id': copy.id,
            'target': 'current',
        }

    @staticmethod
    def _version_label(sequence):
        return "v%d" % int(sequence)

    @staticmethod
    def _next_version_code(root_code, sequence):
        return "%s_v%d" % (root_code, int(sequence))

    def action_open_split_wizard(self):
        """Open the grid split wizard (account x analytic x period)."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Split budget into a grid"),
            'res_model': 'eh.budget.split.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_budget_id': self.id},
        }

    def action_split_lines_by_month(self):
        """Replace each existing line with twelve monthly slices.

        Splits the line's budgeted_amount equally across the calendar
        months that overlap the budget period. Useful for converting an
        annual draft into monthly tracking lines.
        """
        from calendar import monthrange
        for budget in self:
            if budget.state != 'draft':
                raise UserError(_(
                    "Only draft budgets can be split (got %s).",
                ) % budget.state)
            unsafe_lines = budget.line_ids.filtered(
                lambda line: (
                    line.driver == 'revenue_line'
                    or line.driver_line_id
                    or line.quantity_evidence_provided
                    or line.budgeted_qty
                    or line.budgeted_unit_price
                    or line.actual_qty
                    or line.actual_unit_price
                )
            )
            if unsafe_lines:
                raise UserError(_(
                    "Monthly splitting is not safe for lines with a revenue "
                    "proxy driver or quantity/price evidence. Remove that "
                    "configuration first, or create explicitly reviewed "
                    "monthly lines so no driver or quantity basis is lost."
                ))
            monetary_handler = self.env[
                'eh.account.dynamic.report.handler'
            ]
            currency = budget.currency_id

            def _split_amount(total, count):
                """Allocate equal rounded slices plus one exact residual."""
                remaining = monetary_handler._eh_round_monetary(
                    total, currency=currency,
                )
                base_amount = monetary_handler._eh_round_monetary(
                    remaining / count, currency=currency,
                )
                amounts = []
                for index in range(count):
                    if index == count - 1:
                        amount = remaining
                    else:
                        # Rounding a small total upward can make
                        # base * (count - 1) exceed the source. Capping
                        # each regular slice keeps the final residual
                        # non-negative without losing a currency unit.
                        amount = min(base_amount, remaining)
                        amount = monetary_handler._eh_round_monetary(
                            amount, currency=currency,
                        )
                    amounts.append(amount)
                    remaining = monetary_handler._eh_round_monetary(
                        remaining - amount, currency=currency,
                    )
                return amounts

            def _split_capped_amount(total, capacities):
                """Split total exactly without exceeding paired capacities."""
                count = len(capacities)
                remaining = monetary_handler._eh_round_monetary(
                    total, currency=currency,
                )
                base_amount = monetary_handler._eh_round_monetary(
                    remaining / count, currency=currency,
                )
                amounts = []
                for capacity in capacities:
                    amount = min(base_amount, capacity, remaining)
                    amount = monetary_handler._eh_round_monetary(
                        max(0.0, amount), currency=currency,
                    )
                    amounts.append(amount)
                    remaining = monetary_handler._eh_round_monetary(
                        remaining - amount, currency=currency,
                    )
                # Equal rounding can place capacity in different slices from
                # the independent fixed split. Fill residual only where the
                # already-rounded budget slice has room.
                for index in reversed(range(count)):
                    if currency.is_zero(remaining):
                        break
                    room = monetary_handler._eh_round_monetary(
                        capacities[index] - amounts[index],
                        currency=currency,
                    )
                    if room <= 0.0:
                        continue
                    addition = monetary_handler._eh_round_monetary(
                        min(room, remaining), currency=currency,
                    )
                    amounts[index] = monetary_handler._eh_round_monetary(
                        amounts[index] + addition, currency=currency,
                    )
                    remaining = monetary_handler._eh_round_monetary(
                        remaining - addition, currency=currency,
                    )
                if not currency.is_zero(remaining):
                    raise UserError(_(
                        "The fixed portion cannot be allocated within the "
                        "monthly budget slices at currency precision."
                    ))
                return amounts

            new_lines = []
            for line in budget.line_ids:
                start = line.period_from
                end = line.period_to
                cur = start.replace(day=1)
                slices = []
                while cur <= end:
                    last = cur.replace(
                        day=monthrange(cur.year, cur.month)[1],
                    )
                    slices.append((max(cur, start), min(last, end)))
                    next_month = cur.month + 1
                    next_year = cur.year + (1 if next_month > 12 else 0)
                    next_month = next_month if next_month <= 12 else 1
                    cur = cur.replace(year=next_year, month=next_month, day=1)
                if not slices:
                    continue
                slice_amounts = _split_amount(
                    line.budgeted_amount, len(slices),
                )
                # Cost behaviour survives the split; a semi-variable
                # fixed portion is divided equally like the amount.
                slice_fixed_amounts = _split_capped_amount(
                    line.fixed_portion or 0.0, slice_amounts,
                )
                for i, ((sf, st), slice_amount, slice_fixed) in enumerate(zip(
                    slices, slice_amounts, slice_fixed_amounts,
                )):
                    new_lines.append({
                        'budget_id': budget.id,
                        'sequence': line.sequence * 100 + i,
                        'account_id': line.account_id.id,
                        'analytic_account_id': line.analytic_account_id.id,
                        'period_from': sf,
                        'period_to': st,
                        'budgeted_amount': slice_amount,
                        'behaviour': line.behaviour,
                        'fixed_portion': slice_fixed,
                        'driver': line.driver,
                        'notes': line.notes,
                    })
            if new_lines:
                budget.line_ids.unlink()
                self.env['eh.budget.line'].create(new_lines)
        return True

    def _eh_try_lock_roll_forward(self):
        """Claim one due budget without waiting on another cron worker."""
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM eh_budget_budget "
            "WHERE id = %s FOR UPDATE SKIP LOCKED",
            [self.id],
        )
        return bool(self.env.cr.fetchone())

    @api.model
    @api.private
    def cron_roll_forward(self):
        """Extend confirmed rolling budgets one calendar month forward.

        For each `eh.budget.budget` with `is_rolling=True` whose `date_to`
        is less than 31 days in the future, append one calendar month of
        lines copied from the most recent monthly slice. The cron runs
        with a per-budget savepoint so a single bad budget never freezes
        the queue.
        """
        from calendar import monthrange
        from datetime import timedelta as _td
        Budget = self.env['eh.budget.budget']
        today = fields.Date.context_today(Budget)
        horizon = today + _td(days=31)
        candidates = Budget.search([
            ('is_rolling', '=', True),
            ('state', '=', 'confirmed'),
            ('date_to', '<=', horizon),
        ])
        # Per-budget savepoint via the shared batch mixin so a single
        # bad budget rolls back only its own slice and never aborts the
        # rest of the queue.
        def _roll(budget):
            if not budget._eh_try_lock_roll_forward():
                return
            budget.invalidate_recordset([
                'state', 'is_rolling', 'date_to', 'line_ids',
            ])
            if (
                not budget.is_rolling
                or budget.state != 'confirmed'
                or budget.date_to > horizon
            ):
                return
            # Find the latest monthly slice on this budget; copy those
            # lines forward, shifted by one month.
            last_lines = budget.line_ids.filtered(
                lambda l: l.period_to == budget.date_to,
            )
            if not last_lines:
                return
            engine_context = {
                _BUDGET_BASIS_CONTEXT_KEY: _BUDGET_BASIS_CAPABILITY,
            }
            engine_budget = budget.sudo().with_context(**engine_context)
            next_from = budget.date_to + _td(days=1)
            next_year = next_from.year
            next_month = next_from.month
            next_to = next_from.replace(
                day=monthrange(next_year, next_month)[1],
            )
            new_vals = []
            for line in last_lines:
                new_vals.append({
                    'budget_id': budget.id,
                    'sequence': line.sequence,
                    'account_id': line.account_id.id,
                    'analytic_account_id': line.analytic_account_id.id,
                    'period_from': next_from,
                    'period_to': next_to,
                    'budgeted_amount': line.budgeted_amount,
                    # Carry the cost-behaviour classification forward;
                    # the driver stays on the activity register default
                    # because a proxy line reference would point at the
                    # prior month's line.
                    'behaviour': line.behaviour,
                    'fixed_portion': line.fixed_portion,
                    'budgeted_qty': line.budgeted_qty,
                    'budgeted_unit_price': line.budgeted_unit_price,
                    'notes': line.notes,
                })
            self.env['eh.budget.line'].sudo().with_context(
                **engine_context
            ).create(new_vals)
            engine_budget.write({'date_to': next_to})
            engine_budget.message_post(body=_(
                "Rolling budget extended forward to %s "
                "(cron_roll_forward)."
            ) % next_to.isoformat())

        self._eh_for_each_savepoint(
            candidates, _roll, log_label="Rolling budget roll-forward",
        )
        return True

    def action_seed_from_forecast(self):
        """Replace lines with one monthly line per income/expense account,
        budgeted_amount projected by Holt-Winters from the last 24 months
        of posted balances.

        Falls back to linear trend on accounts with fewer than two seasons
        of history; falls back to the prior-year mean for accounts with
        less than 12 months. The fallbacks are explicit, not silent: each
        line records the algorithm used in `notes` so the auditor can
        verify the projection's basis.
        """
        from calendar import monthrange
        from datetime import timedelta as _td
        from odoo.addons.eh_account_budget_pro.tools.forecast import (
            ForecastError, holt_winters_additive, linear_trend,
            project_trend,
        )

        for budget in self:
            if budget.state != 'draft':
                raise UserError(_(
                    "Only draft budgets can be seeded (got %s).",
                ) % budget.state)
            monetary_handler = self.env[
                'eh.account.dynamic.report.handler'
            ]
            currency = budget.currency_id
            company_id = budget.company_id.id
            forecast_to = budget.date_from - _td(days=1)
            # relativedelta keeps a 29-Feb base date valid when the target
            # year is not a leap year (date.replace(year=...) would raise).
            forecast_from = (
                forecast_to - relativedelta(years=2)
            ) + _td(days=1)
            history = self._fetch_monthly_history_per_account(
                company_id=company_id,
                date_from=forecast_from,
                date_to=forecast_to,
            )
            if not history:
                continue

            # Months covered in the budget window.
            months = self._months_in_range(budget.date_from, budget.date_to)
            if not months:
                continue

            new_lines = []
            seq = 10
            account_types = {
                account.id: account.account_type
                for account in self.env['account.account'].sudo().browse(
                    history.keys()
                ).exists()
            }
            for account_id, monthly_series in history.items():
                # GL balances use natural debit/credit signs while budget
                # values are positive economic magnitudes. Convert revenue
                # credits once before forecasting; do not use abs() on the
                # projection because a genuinely negative forecast must not
                # be turned into invented positive spend/revenue.
                direction = (
                    -1.0
                    if account_types.get(account_id) in _INCOME_LIKE
                    else 1.0
                )
                effective_series = [
                    direction * _eh_finite_float(value)
                    for value in monthly_series
                ]
                while effective_series and currency.is_zero(
                    effective_series[0]
                ):
                    effective_series.pop(0)
                if not effective_series:
                    continue
                projections, algo = self._project_account_series(
                    effective_series, len(months),
                    holt_winters_additive, linear_trend, project_trend,
                    ForecastError,
                )
                for i, (period_from, period_to) in enumerate(months):
                    projection = (
                        projections[i] if i < len(projections) else 0.0
                    )
                    amount = max(
                        0.0,
                        monetary_handler._eh_round_monetary(
                            _eh_finite_float(projection), currency=currency,
                        ),
                    )
                    new_lines.append({
                        'budget_id': budget.id,
                        'sequence': seq,
                        'account_id': account_id,
                        'period_from': period_from,
                        'period_to': period_to,
                        'budgeted_amount': amount,
                        'notes': "forecast: %s" % algo,
                    })
                    seq += 1

            if budget.line_ids:
                budget.line_ids.unlink()
            if new_lines:
                self.env['eh.budget.line'].create(new_lines)
        return True

    def action_reforecast(self):
        """Store a rolling reforecast snapshot; never touch the baseline.

        For every account carried by the budget's lines the snapshot
        holds one row per calendar month of the budget window:

        * elapsed months (period fully before today) carry the posted
          actual for that account and month, source 'actual';
        * remaining months carry a re-projection produced by the same
          engine stack as action_seed_from_forecast, run over the
          trailing 24 calendar months of posted history ending at the
          last elapsed month. Leading all-zero months are dropped from
          the series first so the algorithm choice (Holt-Winters >=24
          points, linear trend >=6, mean otherwise) reflects real
          history length, not empty pre-history.

        Amounts are stored as positive magnitudes (abs, rounded with the
        budget currency's precision),
        matching the budgeted_amount convention. The budget's own lines
        are the untouched baseline; variance reporting can compare
        actuals against either the baseline (variance_amount) or the
        latest revision (revision_variance).
        """
        from datetime import timedelta as _td
        from odoo.addons.eh_account_budget_pro.tools.forecast import (
            ForecastError, holt_winters_additive, linear_trend,
            project_trend,
        )
        self._eh_check_access('write')
        self._eh_require_budget_manager()
        self._eh_lock_for_update()
        self._eh_check_access('write')
        self._eh_require_budget_manager()
        revision = self.env['eh.budget.forecast.revision']
        for budget in self:
            if budget.state not in ('confirmed', 'revised'):
                raise UserError(_(
                    "Only confirmed budgets can be reforecast (got %s). "
                    "The baseline must be locked in before revisions "
                    "make sense.",
                ) % budget.state)
            monetary_handler = self.env[
                'eh.account.dynamic.report.handler'
            ]
            currency = budget.currency_id
            if budget.eh_basis_integrity_review_required:
                raise UserError(_(
                    "Legacy approved budget basis is quarantined. Create a "
                    "new reviewed budget before generating forecasts."
                ))
            if not budget.line_ids:
                raise UserError(_(
                    "Cannot reforecast a budget with no lines: %s",
                ) % budget.name)
            today = fields.Date.context_today(budget)
            months = self._months_in_range(budget.date_from, budget.date_to)
            elapsed = [m for m in months if m[1] < today]
            remaining = [m for m in months if m[1] >= today]
            budget._eh_assert_single_analytic_plan()
            budget._eh_assert_non_overlapping_actual_scopes()
            scopes = sorted({
                (
                    line.account_id.id,
                    line.analytic_account_id.id or False,
                )
                for line in budget.line_ids
            })

            actuals = {}
            if elapsed:
                actuals = self._fetch_monthly_history_per_scope(
                    company_id=budget.company_id.id,
                    date_from=elapsed[0][0],
                    date_to=elapsed[-1][1],
                    scopes=scopes,
                )
            history = {}
            if remaining:
                hist_end = (
                    elapsed[-1][1] if elapsed
                    else budget.date_from - _td(days=1)
                )
                hist_from = hist_end.replace(day=1) - relativedelta(months=23)
                history = self._fetch_monthly_history_per_scope(
                    company_id=budget.company_id.id,
                    date_from=hist_from,
                    date_to=hist_end,
                    scopes=scopes,
                )
                hist_months = len(self._months_in_range(hist_from, hist_end))

            line_vals = []
            for account_id, analytic_id in scopes:
                scope = (account_id, analytic_id)
                actual_series = actuals.get(scope, [0.0] * len(elapsed))
                for i, (period_from, period_to) in enumerate(elapsed):
                    actual_value = (
                        actual_series[i] if i < len(actual_series) else 0.0
                    )
                    line_vals.append({
                        'account_id': account_id,
                        'analytic_account_id': analytic_id,
                        'period_from': period_from,
                        'period_to': period_to,
                        'amount': abs(
                            monetary_handler._eh_round_monetary(
                                _eh_finite_float(actual_value),
                                currency=currency,
                            )
                        ),
                        'source': 'actual',
                    })
                if not remaining:
                    continue
                series = list(history.get(scope, [0.0] * hist_months))
                # Drop leading all-zero months: an account first posted
                # to six months ago has six points of history, not 24.
                while series and not series[0]:
                    series.pop(0)
                if series:
                    projections, algo = self._project_account_series(
                        series, len(remaining),
                        holt_winters_additive, linear_trend, project_trend,
                        ForecastError,
                    )
                else:
                    projections, algo = [0.0] * len(remaining), 'mean'
                for i, (period_from, period_to) in enumerate(remaining):
                    projection = (
                        projections[i] if i < len(projections) else 0.0
                    )
                    line_vals.append({
                        'account_id': account_id,
                        'analytic_account_id': analytic_id,
                        'period_from': period_from,
                        'period_to': period_to,
                        'amount': abs(
                            monetary_handler._eh_round_monetary(
                                _eh_finite_float(projection),
                                currency=currency,
                            )
                        ),
                        'source': algo,
                    })

            revision = self.env[
                'eh.budget.forecast.revision'
            ].sudo().with_context(**{
                _BUDGET_BASIS_CONTEXT_KEY: _BUDGET_BASIS_CAPABILITY,
            }).create({
                'budget_id': budget.id,
                'name': "R%d (%s)" % (
                    len(budget.forecast_revision_ids) + 1,
                    today.isoformat(),
                ),
                'revision_date': today,
                'line_ids': [(0, 0, vals) for vals in line_vals],
            })
            budget.message_post(body=_(
                "Rolling reforecast stored as %s: %d elapsed month(s) at "
                "actuals, %d remaining month(s) re-projected. Baseline "
                "lines unchanged."
            ) % (revision.name, len(elapsed), len(remaining)))
        if len(self) == 1 and revision:
            return {
                'type': 'ir.actions.act_window',
                'name': _("Forecast Revision"),
                'res_model': 'eh.budget.forecast.revision',
                'view_mode': 'form',
                'views': [(False, 'form')],
                'res_id': revision.id,
                'target': 'current',
            }
        return True

    @api.model
    def _months_in_range(self, date_from, date_to):
        """Yield (start, end) pairs for each calendar month touched by
        the closed interval [date_from, date_to]."""
        from calendar import monthrange
        out = []
        cur = date_from.replace(day=1)
        while cur <= date_to:
            last = cur.replace(day=monthrange(cur.year, cur.month)[1])
            slice_to = min(last, date_to)
            slice_from = max(cur, date_from)
            out.append((slice_from, slice_to))
            next_month = cur.month + 1
            next_year = cur.year + (1 if next_month > 12 else 0)
            next_month = next_month if next_month <= 12 else 1
            cur = cur.replace(year=next_year, month=next_month, day=1)
        return out

    @api.model
    def _fetch_monthly_history_per_account(
        self, company_id, date_from, date_to,
    ):
        """Return {account_id: [m0_total, m1_total, ...]} where each list
        is one entry per calendar month between date_from and date_to.

        Income and expense accounts only. Months with no posting receive
        zero so the series stays evenly spaced for forecasting.
        """
        query = MoveLineQuery(self.env, company_ids=[company_id])
        query.where_date_range(date_from=date_from, date_to=date_to)
        query.where_posted_only()
        query.where_account_types((
            'income', 'income_other',
            'expense', 'expense_other', 'expense_depreciation',
            'expense_direct_cost',
        ))
        query.select_field('account_id')
        query.select(SQL("date_trunc('month', aml.date)"), 'month_start')
        query.select(SQL("SUM(aml.balance)"), 'balance')
        query.group_by(SQL("aml.account_id"), SQL("date_trunc('month', aml.date)"))
        rows = query.execute()
        if not rows:
            return {}

        months = self._months_in_range(date_from, date_to)
        by_account = {}
        index_by_month = {m[0].replace(day=1): i for i, m in enumerate(months)}
        for row in rows:
            account_id = row['account_id']
            ms = row['month_start']
            if hasattr(ms, 'date'):
                ms = ms.date()
            month_key = ms.replace(day=1)
            idx = index_by_month.get(month_key)
            if idx is None:
                continue
            series = by_account.setdefault(
                account_id, [0.0] * len(months),
            )
            balance = _eh_finite_float(row.get('balance'), default=None)
            if balance is None:
                continue
            series[idx] = balance
        return by_account

    @api.model
    def _fetch_monthly_history_per_scope(
        self, company_id, date_from, date_to, scopes,
    ):
        """Return monthly balances for account plus optional analytic scopes.

        Composite analytic-distribution keys represent parallel analytic plans.
        A requested analytic scope receives the percentage carried by every key
        containing its id; an account-only scope receives the full GL balance.
        Grouping by distribution keeps this to one SQL pass without expanding
        one query per analytic account.
        """
        scope_set = {
            (int(account_id), int(analytic_id) if analytic_id else False)
            for account_id, analytic_id in (scopes or ())
        }
        if not scope_set:
            return {}
        account_ids = sorted({scope[0] for scope in scope_set})
        query = MoveLineQuery(self.env, company_ids=[company_id])
        query.where_date_range(date_from=date_from, date_to=date_to)
        query.where_posted_only()
        query.where_accounts(account_ids)
        query.select_field('account_id')
        query.select(SQL("date_trunc('month', aml.date)"), 'month_start')
        query.select(SQL("SUM(aml.balance)"), 'balance')
        has_analytic_scopes = any(
            analytic_id for _account_id, analytic_id in scope_set
        )
        if has_analytic_scopes:
            query.select_field('analytic_distribution')
            query.group_by(
                SQL("aml.account_id"),
                SQL("date_trunc('month', aml.date)"),
                SQL("aml.analytic_distribution"),
            )
        else:
            # Hot path: account-only budgets aggregate monthly in SQL and
            # never group or transfer analytic JSON.
            query.group_by(
                SQL("aml.account_id"),
                SQL("date_trunc('month', aml.date)"),
            )
        rows = query.execute()
        if not rows:
            return {}

        months = self._months_in_range(date_from, date_to)
        index_by_month = {
            month[0].replace(day=1): index
            for index, month in enumerate(months)
        }
        series_by_scope = {
            scope: [0.0] * len(months) for scope in scope_set
        }
        analytic_ids_by_account = defaultdict(set)
        for account_id, analytic_id in scope_set:
            if analytic_id:
                analytic_ids_by_account[account_id].add(analytic_id)

        for row in rows:
            try:
                account_id = int(row.get('account_id'))
                month_start = fields.Date.to_date(row.get('month_start'))
                month_key = month_start.replace(day=1)
            except (AttributeError, TypeError, ValueError, OverflowError):
                continue
            index = index_by_month.get(month_key)
            if index is None:
                continue
            try:
                balance = float(row.get('balance') or 0.0)
            except (TypeError, ValueError, OverflowError):
                continue
            if not isfinite(balance):
                continue
            account_scope = (account_id, False)
            if account_scope in scope_set:
                current = series_by_scope[account_scope][index]
                candidate = current + balance
                if isfinite(candidate):
                    series_by_scope[account_scope][index] = candidate

            requested_ids = analytic_ids_by_account.get(account_id, set())
            if not requested_ids:
                continue
            distribution = row.get('analytic_distribution') or {}
            if not isinstance(distribution, dict):
                continue
            for key, raw_percentage in distribution.items():
                percentage = _eh_analytic_percentage(raw_percentage)
                if percentage is None:
                    continue
                token_ids = set()
                for token in str(key).split(','):
                    try:
                        token_ids.add(int(token.strip()))
                    except (TypeError, ValueError):
                        continue
                for analytic_id in requested_ids.intersection(token_ids):
                    contribution = balance * percentage / 100.0
                    if not isfinite(contribution) or contribution == 0.0:
                        continue
                    scope = (account_id, analytic_id)
                    current = series_by_scope[scope][index]
                    candidate = current + contribution
                    if isfinite(candidate):
                        series_by_scope[scope][index] = candidate
        return series_by_scope

    @api.model
    def _project_account_series(
        self, series, periods_ahead,
        holt_winters_fn, linear_fn, project_fn, forecast_error,
    ):
        """Pick a forecasting algorithm appropriate to series length.

        Returns (projections, algorithm_name). Algorithms preferred in
        order: Holt-Winters when there are at least two seasons of
        monthly data; linear trend when there are at least 6 points;
        prior-year mean otherwise.
        """
        n = len(series)
        if n >= 24:
            try:
                return (
                    holt_winters_fn(series, 12, periods_ahead),
                    'holt_winters',
                )
            except (forecast_error, ArithmeticError, TypeError, ValueError):
                pass
        if n >= 6:
            try:
                slope, intercept = linear_fn(series)
                return (
                    [project_fn(slope, intercept, n + i)
                     for i in range(periods_ahead)],
                    'linear_trend',
                )
            except (forecast_error, ArithmeticError, TypeError, ValueError):
                pass
        # Last resort: mean of available history.
        try:
            mean = sum(series) / max(n, 1)
        except (ArithmeticError, TypeError, ValueError):
            mean = 0.0
        mean = _eh_finite_float(mean)
        return [mean] * periods_ahead, 'mean'

    def action_seed_from_prior_year(self):
        """Replace lines with one line per account, budgeted_amount equal
        to that account's posted balance over the prior 12 months.

        Adds a starting baseline so users can adjust amounts up or down
        rather than typing each line from scratch. Only runs on empty or
        draft budgets to avoid destroying user work.
        """
        from datetime import timedelta
        for budget in self:
            if budget.state != 'draft':
                raise UserError(_(
                    "Only draft budgets can be seeded (got %s).",
                ) % budget.state)
            prior_to = budget.date_from - timedelta(days=1)
            # relativedelta avoids a 29-Feb replace() crash in non-leap years.
            prior_from = (prior_to - relativedelta(years=1)) + timedelta(days=1)
            query = MoveLineQuery(
                self.env, company_ids=[budget.company_id.id],
            )
            query.where_date_range(date_from=prior_from, date_to=prior_to)
            query.where_posted_only()
            query.where_account_types((
                'income', 'income_other',
                'expense', 'expense_other', 'expense_depreciation',
                'expense_direct_cost',
            ))
            query.select_field('account_id')
            query.select(SQL("SUM(aml.balance)"), 'balance')
            query.group_by(SQL("aml.account_id"))
            rows = query.execute()
            if not rows:
                continue
            new_lines = []
            for i, row in enumerate(rows):
                amount = abs(_eh_finite_float(
                    row.get('balance'), default=0.0,
                ))
                if not amount:
                    continue
                new_lines.append({
                    'budget_id': budget.id,
                    'sequence': 10 + i,
                    'account_id': row['account_id'],
                    'period_from': budget.date_from,
                    'period_to': budget.date_to,
                    'budgeted_amount': amount,
                })
            if budget.line_ids:
                budget.line_ids.unlink()
            if new_lines:
                self.env['eh.budget.line'].create(new_lines)
        return True

    def action_view_lines(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Budget Lines"),
            'res_model': 'eh.budget.line',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('budget_id', '=', self.id)],
            'context': {'default_budget_id': self.id},
        }

    def action_open_quantity_actuals(self):
        """Open the controlled actual-quantity evidence entry surface."""
        self.ensure_one()
        self._eh_check_access('read')
        if self.state != 'confirmed':
            raise UserError(_(
                "Quantity actuals can be updated only on a live confirmed "
                "budget."
            ))
        view = self.env.ref(
            'eh_account_budget_pro.view_eh_budget_line_quantity_actuals',
        )
        return {
            'type': 'ir.actions.act_window',
            'name': _("Update Quantity Actuals"),
            'res_model': 'eh.budget.line',
            'view_mode': 'list',
            'views': [(view.id, 'list')],
            'domain': [('budget_id', '=', self.id)],
            'context': {'default_budget_id': self.id},
        }


class EhBudgetLine(models.Model):
    _name = 'eh.budget.line'
    _description = "Budget line"
    _order = 'sequence, period_from, id'

    # Direct related company anchor lets check_company on commitment targets
    # operate without adding a duplicate stored column.
    company_id = fields.Many2one(
        related='budget_id.company_id', readonly=True, store=False,
    )

    budget_id = fields.Many2one(
        'eh.budget.budget',
        required=True,
        ondelete='cascade',
        index=True,
        check_company=True,
    )
    sequence = fields.Integer(default=10)

    account_id = fields.Many2one(
        'account.account',
        required=True,
        ondelete='restrict',
        index=True,
        check_company=True,
    )
    analytic_account_id = fields.Many2one(
        'account.analytic.account',
        ondelete='restrict',
        index=True,
        check_company=True,
        help=(
            "Optional analytic account. When set, the actual_amount only "
            "counts journal items whose analytic_distribution allocates to "
            "this analytic account. Lines with the same account but "
            "different analytic accounts are independent budgets."
        ),
    )
    period_from = fields.Date(required=True)
    period_to = fields.Date(required=True)

    budgeted_amount = fields.Monetary(
        required=True,
        currency_field='currency_id',
    )
    currency_id = fields.Many2one(
        related='budget_id.currency_id',
        store=False,
        readonly=True,
    )

    actual_amount = fields.Monetary(
        compute='_compute_actual',
        currency_field='currency_id',
        store=False,
        aggregator='sum',
        help=(
            "Sum of posted journal item balances on this account in the "
            "line's period. Computed in batch per budget."
        ),
    )
    normalised_actual_amount = fields.Monetary(
        string="Actual",
        compute='_compute_variance',
        currency_field='currency_id',
        aggregator='sum',
        help=(
            "Posted actual expressed in the budget's positive-magnitude "
            "convention. Credit-natured revenue, liability, and equity "
            "balances are sign-flipped once here so line and header totals "
            "use the same arithmetic contract."
        ),
    )
    commitment_ids = fields.One2many(
        'eh.budget.commitment', 'budget_line_id',
        help=(
            "Open commitments (purchase orders, contracts) reserving "
            "availability against this line."
        ),
    )
    committed_amount = fields.Monetary(
        compute='_compute_committed',
        currency_field='currency_id',
        store=False,
        aggregator='sum',
        help=(
            "Sum of reserved commitments on this line. Released and "
            "draft commitments do not count: released ones have "
            "moved into actual_amount, draft ones have not yet been "
            "approved."
        ),
    )
    available_amount = fields.Monetary(
        compute='_compute_committed',
        currency_field='currency_id',
        store=False,
        aggregator='sum',
        help=(
            "budgeted_amount minus actual_amount minus "
            "committed_amount. Negative when the budget is "
            "overcommitted."
        ),
    )
    variance_amount = fields.Monetary(
        compute='_compute_variance',
        currency_field='currency_id',
        aggregator='sum',
    )
    variance_pct = fields.Float(
        compute='_compute_variance',
        digits=(8, 2),
        aggregator='avg',
    )

    # ---- cost behaviour / flexible budget ----
    behaviour = fields.Selection(
        [
            ('fixed', "Fixed"),
            ('variable', "Variable"),
            ('semi_variable', "Semi-variable"),
        ],
        default='fixed', required=True,
        help=(
            "Cost behaviour classification. Fixed lines never flex "
            "(flexed = budgeted, the historical behaviour). Variable "
            "lines flex fully with activity. Semi-variable lines keep "
            "fixed_portion constant and flex the remainder."
        ),
    )
    fixed_portion = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Fixed component of a semi-variable line's budgeted amount. "
            "The variable component (budgeted_amount - fixed_portion) "
            "flexes with activity. Ignored for fixed and variable lines."
        ),
    )
    driver = fields.Selection(
        [
            ('activity', "Activity register"),
            ('revenue_line', "Revenue line proxy"),
        ],
        default='activity', required=True,
        help=(
            "Where the activity measure comes from. Activity register: "
            "sum the budget's activity rows overlapping this line's "
            "period. Revenue line proxy: use another line on the same "
            "budget (typically revenue) as a units-sold proxy, with "
            "budgeted activity = its budgeted amount and actual "
            "activity = its sign-normalised actual."
        ),
    )
    driver_line_id = fields.Many2one(
        'eh.budget.line',
        string="Driver Line",
        ondelete='set null',
        index=True,
        check_company=True,
        help="Line on the same budget acting as the activity proxy.",
    )
    budgeted_activity = fields.Float(
        compute='_compute_flex', digits=(16, 4),
        help="Budgeted activity level resolved from the driver.",
    )
    actual_activity = fields.Float(
        compute='_compute_flex', digits=(16, 4),
        help="Actual activity level resolved from the driver.",
    )
    activity_ratio = fields.Float(
        compute='_compute_flex', digits=(16, 4),
        help=(
            "actual_activity / budgeted_activity. 1.0 when no activity "
            "data exists, so unflexed budgets behave exactly as before."
        ),
    )
    flexed_amount = fields.Monetary(
        compute='_compute_flex',
        currency_field='currency_id',
        aggregator='sum',
        help=(
            "Budget restated at the actual activity level: fixed "
            "component + variable component x activity ratio."
        ),
    )
    flexed_variance = fields.Monetary(
        compute='_compute_flex',
        currency_field='currency_id',
        aggregator='sum',
        help=(
            "Actual minus flexed budget (sign-normalised): the "
            "spending/price side of the static variance. Positive means "
            "over the flexed allowance on expense lines. Ties exactly: "
            "static variance = flexed variance + volume variance."
        ),
    )
    volume_variance = fields.Monetary(
        compute='_compute_flex',
        currency_field='currency_id',
        aggregator='sum',
        help=(
            "Flexed minus static budget: the part of the static "
            "variance explained purely by the activity level moving."
        ),
    )

    # ---- price / efficiency split (variable lines with quantity data) ----
    budgeted_qty = fields.Float(
        digits=(16, 4),
        help="Budgeted input quantity behind the budgeted amount.",
    )
    budgeted_unit_price = fields.Float(
        digits=(16, 4),
        help="Budgeted (standard) price per input unit.",
    )
    actual_qty = fields.Float(
        digits=(16, 4),
        help="Actual input quantity consumed.",
    )
    actual_unit_price = fields.Float(
        digits=(16, 4),
        help="Actual price paid per input unit.",
    )
    quantity_evidence_provided = fields.Boolean(
        string="Actual quantity evidence provided",
        help=(
            "Enable when actual quantity evidence is present even when the "
            "measured quantity and unit price are both zero. Non-zero actual "
            "quantity or price values remain backward-compatible evidence."
        ),
    )
    flexed_qty = fields.Float(
        compute='_compute_flex', digits=(16, 4),
        help=(
            "Quantity allowance at actual activity: budgeted_qty x "
            "activity ratio."
        ),
    )
    price_variance = fields.Monetary(
        compute='_compute_flex',
        currency_field='currency_id',
        aggregator='sum',
        help=(
            "(actual price - budgeted price) x actual quantity. "
            "Positive = adverse on expense lines. Zero unless the line "
            "is variable with quantity data."
        ),
    )
    efficiency_variance = fields.Monetary(
        compute='_compute_flex',
        currency_field='currency_id',
        aggregator='sum',
        help=(
            "(actual quantity - flexed quantity) x budgeted price. "
            "Positive = adverse on expense lines. Zero unless the line "
            "is variable with quantity data."
        ),
    )
    split_residual = fields.Monetary(
        compute='_compute_flex',
        currency_field='currency_id',
        aggregator='sum',
        help=(
            "flexed variance - price variance - efficiency variance. "
            "Zero when the posted actual equals actual_qty x actual "
            "price and the budgeted amount equals budgeted_qty x "
            "budgeted price; a residual flags inconsistent quantity "
            "data against the ledger."
        ),
    )

    # ---- reforecast comparison ----
    revision_amount = fields.Monetary(
        compute='_compute_revision',
        currency_field='currency_id',
        aggregator='sum',
        help=(
            "Amount from the budget's latest forecast revision for this "
            "line's account and analytic scope, summed over the revision "
            "months overlapping the line period. Zero when no matching "
            "revision exists."
        ),
    )
    revision_variance = fields.Monetary(
        compute='_compute_revision',
        currency_field='currency_id',
        aggregator='sum',
        help=(
            "Actual (sign-normalised) minus the latest revision amount: "
            "variance against the rolling reforecast instead of the "
            "baseline."
        ),
    )

    elapsed_pct = fields.Float(
        compute='_compute_theoretical', digits=(8, 2),
        help="Fraction of the line period elapsed as of today (0-100).",
    )
    theoretical_amount = fields.Monetary(
        compute='_compute_theoretical', currency_field='currency_id',
        aggregator='sum',
        help=(
            "Budget that should have been consumed by now if spending "
            "tracked the calendar evenly: budgeted x elapsed fraction."
        ),
    )
    pace_pct = fields.Float(
        compute='_compute_theoretical', digits=(8, 2),
        help=(
            "Actual as a percentage of the theoretical to-date amount. "
            "Over 100 means spending ahead of an even run-rate."
        ),
    )

    notes = fields.Char()

    _ACTUAL_EVIDENCE_FIELDS = frozenset({
        'actual_qty', 'actual_unit_price', 'quantity_evidence_provided',
    })
    _BASIS_FIELDS = frozenset({
        'budget_id', 'sequence', 'account_id', 'analytic_account_id',
        'period_from', 'period_to', 'budgeted_amount', 'behaviour',
        'fixed_portion', 'driver', 'driver_line_id', 'budgeted_qty',
        'budgeted_unit_price', 'notes',
    })
    _REPORT_CACHE_FIELDS = frozenset({
        'budget_id', 'account_id', 'analytic_account_id',
        'period_from', 'period_to', 'budgeted_amount', 'actual_qty',
        'actual_unit_price', 'quantity_evidence_provided',
    })

    @api.model
    def _eh_reference_company_ids(self, record, target_company=None):
        check_company_domain = getattr(
            record, '_check_company_domain', None,
        )
        filtered_domain = getattr(record, 'filtered_domain', None)
        if (
            target_company
            and callable(check_company_domain)
            and callable(filtered_domain)
        ):
            return (
                {target_company.id}
                if record.filtered_domain(
                    check_company_domain(target_company),
                )
                else set()
            )
        companies = getattr(record, 'company_ids', None)
        if companies is not None:
            company_ids = set(companies.ids)
        else:
            company = getattr(record, 'company_id', None)
            company_ids = {company.id} if company else set()
        if target_company and not company_ids:
            return {target_company.id}
        return company_ids

    @api.model
    def _eh_validate_line_values(self, vals, current=None):
        budget_id = vals.get(
            'budget_id', current.budget_id.id if current else False,
        )
        budget = self.env['eh.budget.budget'].browse(budget_id)
        if budget:
            if not self.env.su:
                budget._eh_check_access('write')
            budget._eh_validate_company_scope(budget.company_id.id)

        account_id = vals.get(
            'account_id', current.account_id.id if current else False,
        )
        account = self.env['account.account'].browse(account_id)
        if account:
            if not self.env.su:
                account._eh_check_access('read')
            account_companies = self._eh_reference_company_ids(
                account, budget.company_id if budget else None,
            )
            if (
                budget
                and budget.company_id.id not in account_companies
            ):
                raise UserError(_(
                    "Budget-line account must belong to the budget company."
                ))

        analytic_id = vals.get(
            'analytic_account_id',
            current.analytic_account_id.id if current else False,
        )
        analytic = self.env['account.analytic.account'].browse(analytic_id)
        if analytic:
            if not self.env.su:
                analytic._eh_check_access('read')
            analytic_companies = self._eh_reference_company_ids(
                analytic, budget.company_id if budget else None,
            )
            if (
                budget
                and budget.company_id.id not in analytic_companies
            ):
                raise UserError(_(
                    "Budget-line analytic account must belong to the budget "
                    "company."
                ))
        return budget

    def _eh_lock_parent_budgets(self, extra_budget_ids=()):
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
                "SELECT id FROM eh_budget_line WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (ids,),
            )
            self.invalidate_recordset()
        return budgets

    @api.model
    def _eh_assert_parent_draft(self, budgets):
        if not budgets._eh_basis_engine_allowed() and budgets.filtered(
            lambda budget: budget.state != 'draft'
        ):
            raise UserError(_(
                "Lines of a confirmed, revised, or closed budget are "
                "immutable. Create a new budget version for corrections."
            ))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._eh_validate_line_values(vals)
        budgets = self._eh_lock_parent_budgets(
            [vals.get('budget_id') for vals in vals_list]
        )
        self._eh_assert_parent_draft(budgets)
        for vals in vals_list:
            self._eh_validate_line_values(vals)
        lines = super().create(vals_list)
        budgets._eh_bump_report_cache_version()
        return lines

    def write(self, vals):
        for line in self:
            self._eh_validate_line_values(vals, current=line)
        budgets = self._eh_lock_parent_budgets((vals.get('budget_id'),))
        for line in self:
            self._eh_validate_line_values(vals, current=line)
        if self._BASIS_FIELDS & vals.keys():
            self._eh_assert_parent_draft(budgets)
        if (
            self._ACTUAL_EVIDENCE_FIELDS & vals.keys()
            and budgets.filtered(
                lambda budget: budget.state not in ('draft', 'confirmed')
            )
        ):
            raise UserError(_(
                "Actual quantity evidence is immutable after a budget is "
                "revised or closed."
            ))
        result = super().write(vals)
        if self._REPORT_CACHE_FIELDS.intersection(vals):
            budgets._eh_bump_report_cache_version()
        return result

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        budgets = self._eh_lock_parent_budgets()
        self._eh_assert_parent_draft(budgets)
        result = super().unlink()
        budgets._eh_bump_report_cache_version()
        return result

    _period_range = models.Constraint(
        'check(period_from <= period_to)',
        'Line period_from must be before or equal to period_to.',
    )
    # Budget figures are entered as positive magnitudes regardless of the
    # underlying account's natural sign (an income budget of 10k means
    # "we plan to earn 10k"; an expense budget of 5k means "we plan to
    # spend 5k"). A negative figure is almost always a data-entry error
    # and silently corrupts variance maths because _compute_variance
    # interprets the sign as direction. Block at the database level so
    # ORM bypass paths cannot insert it either.
    _amount_non_negative = models.Constraint(
        'check(budgeted_amount >= 0)',
        'Budgeted amount must be zero or positive. Reductions to a '
        'budget should lower the existing line, not be entered as a '
        'negative figure.',
    )
    # Quantity data feeds the price/efficiency split multiplicatively, so
    # a negative quantity or unit price silently flips variance signs.
    # NULLs pass a CHECK, so unset rows are unaffected.
    _qty_data_non_negative = models.Constraint(
        'check(budgeted_qty >= 0 AND actual_qty >= 0 AND '
        'budgeted_unit_price >= 0 AND actual_unit_price >= 0)',
        'Quantities and unit prices must be zero or positive.',
    )

    @api.constrains('budget_id', 'analytic_account_id')
    def _check_single_analytic_plan(self):
        self.mapped('budget_id')._eh_assert_single_analytic_plan()

    @api.constrains('behaviour', 'fixed_portion', 'budgeted_amount')
    def _check_fixed_portion(self):
        monetary_handler = self.env['eh.account.dynamic.report.handler']
        for rec in self:
            if rec.behaviour != 'semi_variable':
                continue
            fixed = rec.fixed_portion or 0.0
            budgeted = rec.budgeted_amount or 0.0
            exceeds_budget = fixed > budgeted and not (
                monetary_handler._eh_is_zero_monetary(
                    fixed - budgeted, currency=rec.currency_id,
                )
            )
            if fixed < 0.0 or exceeds_budget:
                raise ValidationError(_(
                    "Semi-variable line on %(account)s: fixed portion "
                    "(%(fixed)s) must sit between zero and the "
                    "budgeted amount (%(budgeted)s).",
                ) % {
                    'account': rec.account_id.display_name,
                    'fixed': format_amount(
                        self.env, fixed, rec.currency_id,
                    ),
                    'budgeted': format_amount(
                        self.env, budgeted, rec.currency_id,
                    ),
                })

    @api.constrains('driver', 'driver_line_id', 'budget_id')
    def _check_driver_line(self):
        for rec in self:
            if rec.driver != 'revenue_line':
                continue
            if not rec.driver_line_id:
                raise ValidationError(_(
                    "Line on %s uses the revenue-line proxy driver but "
                    "no driver line is set.",
                ) % rec.account_id.display_name)
            if rec.driver_line_id == rec:
                raise ValidationError(_(
                    "A line cannot be its own activity driver (%s).",
                ) % rec.account_id.display_name)
            if rec.driver_line_id.budget_id != rec.budget_id:
                raise ValidationError(_(
                    "Driver line must belong to the same budget "
                    "(line on %s).",
                ) % rec.account_id.display_name)

    @api.depends(
        'account_id', 'analytic_account_id',
        'period_from', 'period_to',
        'budget_id.company_id',
    )
    def _compute_actual(self):
        if not self:
            return
        # Group lines by budget for one batch SQL per budget.
        by_budget = defaultdict(list)
        for line in self:
            if line.budget_id:
                by_budget[line.budget_id].append(line)
            else:
                line.actual_amount = 0.0
        for budget, lines in by_budget.items():
            actuals = self._fetch_actuals_batch(budget, lines)
            for line in lines:
                key = (
                    line.account_id.id,
                    line.analytic_account_id.id or False,
                    line.period_from,
                    line.period_to,
                )
                line.actual_amount = actuals.get(key, 0.0)

    @api.depends(
        'commitment_ids.amount', 'commitment_ids.state',
        'budgeted_amount', 'normalised_actual_amount',
    )
    def _compute_committed(self):
        """Sum reserved commitments and derive availability.

        Income-vs-expense sign normalisation mirrors
        _compute_variance: actual on income / liability / equity
        accounts is signed credit (negative in Odoo), so we flip it
        before subtracting from the (always-positive) budget so
        availability reads in the natural economic direction.
        Commitment amount is always positive.
        """
        for line in self:
            committed = sum(
                line.commitment_ids
                .filtered(lambda c: c.state == 'reserved')
                .mapped('amount'),
            )
            actual = line.normalised_actual_amount or 0.0
            line.committed_amount = committed
            line.available_amount = (
                (line.budgeted_amount or 0.0) - actual - committed
            )

    @api.depends('budgeted_amount', 'actual_amount', 'account_id')
    def _compute_variance(self):
        """Variance = actual minus budget, normalised by account sign.

        Income accounts post a credit-side balance (negative number in
        Odoo convention). The budget_amount is always entered as a
        positive figure ("we plan to earn 10k"). Without normalisation,
        a $10k income budget vs $10k actual revenue would render as
        -10k - 10k = -20k variance, ie a 200% overrun. Normalise by
        flipping the actual sign on income / liability / equity
        accounts so both sides are in the same economic direction.
        """
        monetary_handler = self.env['eh.account.dynamic.report.handler']
        for line in self:
            actual = line.actual_amount or 0.0
            if line.account_id and line.account_id.account_type in _INCOME_LIKE:
                actual = -actual
            actual = monetary_handler._eh_round_monetary(
                actual, currency=line.currency_id,
            )
            budgeted = monetary_handler._eh_round_monetary(
                line.budgeted_amount or 0.0, currency=line.currency_id,
            )
            variance = monetary_handler._eh_round_monetary(
                actual - budgeted,
                currency=line.currency_id,
            )
            line.normalised_actual_amount = actual
            line.variance_amount = variance
            line.variance_pct = EhBudget._safe_variance_pct(
                budgeted, actual,
            )

    def _normalised_actual(self):
        """Sign-normalised actual: credit-natured accounts flip so the
        figure reads in the same economic direction as the (positive)
        budget, exactly as _compute_variance does."""
        self.ensure_one()
        return self.normalised_actual_amount or 0.0

    def _activity_pair(self):
        """Return (budgeted_activity, actual_activity) for this line.

        Driver 'revenue_line': the proxy line's budgeted_amount and
        sign-normalised actual serve as the activity pair (a revenue
        proxy for units sold). Driver 'activity': sum the budget's
        activity register rows whose period overlaps this line's
        period. Returns (0.0, 0.0) when nothing resolves, which the
        flex compute treats as "no activity data, do not flex".
        """
        self.ensure_one()
        if self.driver == 'revenue_line' and self.driver_line_id:
            proxy = self.driver_line_id
            budgeted = proxy.budgeted_amount or 0.0
            actual = proxy._normalised_actual()
            return budgeted, actual
        if not (self.budget_id and self.period_from and self.period_to):
            return 0.0, 0.0
        if self.budget_id.eh_activity_integrity_review_required:
            return 0.0, 0.0
        rows = self.budget_id.activity_period_ids.filtered(
            lambda a: a.period_from and a.period_to
            and a.period_from <= self.period_to
            and a.period_to >= self.period_from,
        )
        return (
            sum(rows.mapped('budgeted_activity')),
            sum(rows.mapped('actual_activity')),
        )

    @api.depends(
        'behaviour', 'fixed_portion', 'budgeted_amount', 'actual_amount',
        'account_id', 'driver',
        'driver_line_id.budgeted_amount', 'driver_line_id.actual_amount',
        'driver_line_id.account_id',
        'period_from', 'period_to',
        'budget_id.activity_period_ids.period_from',
        'budget_id.activity_period_ids.period_to',
        'budget_id.activity_period_ids.budgeted_activity',
        'budget_id.activity_period_ids.actual_activity',
        'budget_id.eh_activity_integrity_review_required',
        'budgeted_qty', 'budgeted_unit_price',
        'actual_qty', 'actual_unit_price', 'quantity_evidence_provided',
    )
    def _compute_flex(self):
        """Flexible budget, three-way variance tie, and the price /
        efficiency split.

        Conventions (documented here, asserted in the golden tests):

        * activity ratio = actual_activity / budgeted_activity; 1.0
          when budgeted activity is zero or missing, so a line without
          activity data never flexes (flexed = budgeted, the historical
          behaviour).
        * flexed = budgeted (fixed); budgeted x ratio (variable);
          fixed_portion + (budgeted - fixed_portion) x ratio
          (semi-variable).
        * volume variance = flexed - budgeted; flexed variance is
          derived as static - volume so the identity
          static = flexed variance + volume variance holds by
          construction, not merely to floating-point tolerance.
        * flexed quantity = budgeted_qty x ratio. Price variance uses
          ACTUAL quantity ((ap - bp) x aq); efficiency variance uses
          BUDGETED price ((aq - flexed qty) x bp). With consistent data
          (actual cost = aq x ap, budget = bq x bp) the two sum exactly
          to the flexed variance:
          (ap-bp)*aq + (aq-fq)*bp = aq*ap - fq*bp = actual - flexed.
        * split computed only for variable-behaviour lines carrying a
          budgeted quantity and explicit actual-quantity evidence. A
          non-zero actual quantity/price remains implicit evidence for
          backward compatibility; the Boolean flag distinguishes a real
          measured zero from missing evidence.
        """
        monetary_handler = self.env['eh.account.dynamic.report.handler']
        for line in self:
            currency = line.currency_id

            def _round_monetary(value):
                return monetary_handler._eh_round_monetary(
                    value, currency=currency,
                )

            budgeted_act, actual_act = line._activity_pair()
            line.budgeted_activity = budgeted_act
            line.actual_activity = actual_act
            ratio = (actual_act / budgeted_act) if budgeted_act else 1.0
            line.activity_ratio = ratio

            budgeted = _round_monetary(line.budgeted_amount or 0.0)
            if line.behaviour == 'variable':
                flexed = budgeted * ratio
            elif line.behaviour == 'semi_variable':
                fixed_part = _round_monetary(min(
                    max(line.fixed_portion or 0.0, 0.0), budgeted,
                ))
                flexed = fixed_part + (budgeted - fixed_part) * ratio
            else:  # fixed
                flexed = budgeted
            flexed = _round_monetary(flexed)
            line.flexed_amount = flexed

            static = line.variance_amount
            volume = _round_monetary(flexed - budgeted)
            line.volume_variance = volume
            flexed_var = _round_monetary(static - volume)
            line.flexed_variance = flexed_var

            flexed_qty = (line.budgeted_qty or 0.0) * ratio
            line.flexed_qty = flexed_qty
            has_qty_data = (
                line.behaviour == 'variable'
                and line.budgeted_qty
                and (
                    line.quantity_evidence_provided
                    or line.actual_qty
                    or line.actual_unit_price
                )
            )
            if has_qty_data:
                price_var = _round_monetary((
                    (line.actual_unit_price or 0.0)
                    - (line.budgeted_unit_price or 0.0)
                ) * line.actual_qty)
                efficiency_var = _round_monetary((
                    (line.actual_qty - flexed_qty)
                    * (line.budgeted_unit_price or 0.0)
                ))
                line.price_variance = price_var
                line.efficiency_variance = efficiency_var
                line.split_residual = _round_monetary(
                    flexed_var - price_var - efficiency_var,
                )
            else:
                line.price_variance = 0.0
                line.efficiency_variance = 0.0
                line.split_residual = 0.0

    @api.depends(
        'budget_id.forecast_revision_ids.revision_date',
        'budget_id.forecast_revision_ids.line_ids.amount',
        'budget_id.forecast_revision_ids.line_ids.analytic_account_id',
        'account_id', 'analytic_account_id', 'period_from', 'period_to',
        'normalised_actual_amount',
    )
    def _compute_revision(self):
        """Variance against the latest reforecast revision instead of
        the baseline. The revision snapshot is account x optional analytic x
        month; a line picks up rows from exactly the same scope overlapping
        its own period.

        A revision row is shared between adjacent partial-period budget lines
        in proportion to their inclusive overlap days. Cumulative currency
        rounding assigns any residual to a deterministic chronological slice,
        so a complete partition ties exactly to the monthly snapshot amount.
        """
        budgets = self.mapped('budget_id')
        active_revision_by_budget = {
            budget.id: budget.active_revision_id
            for budget in budgets
            if budget.active_revision_id
        }
        revision_ids = [
            revision.id for revision in active_revision_by_budget.values()
        ]
        revision_lines = self.env[
            'eh.budget.forecast.revision.line'
        ].search([('revision_id', 'in', revision_ids)]) if revision_ids else (
            self.env['eh.budget.forecast.revision.line']
        )
        rows_by_scope = defaultdict(list)
        for revision_line in revision_lines:
            if not (
                revision_line.period_from and revision_line.period_to
            ):
                continue
            rows_by_scope[(
                revision_line.revision_id.id,
                revision_line.account_id.id,
                revision_line.analytic_account_id.id or False,
            )].append(revision_line)

        budget_lines_by_scope = defaultdict(list)
        candidate_lines = self.search([
            ('budget_id', 'in', budgets.ids),
            ('account_id', '!=', False),
            ('period_from', '!=', False),
            ('period_to', '!=', False),
        ]) if budgets else self.browse()
        for candidate in candidate_lines:
            budget_lines_by_scope[(
                candidate.budget_id.id,
                candidate.account_id.id,
                candidate.analytic_account_id.id or False,
            )].append(candidate)

        monetary_handler = self.env['eh.account.dynamic.report.handler']
        revision_by_id = {
            revision.id: revision
            for revision in active_revision_by_budget.values()
        }
        amount_by_line_id = defaultdict(float)
        for scope, scope_rows in rows_by_scope.items():
            revision_id, account_id, analytic_id = scope
            revision = revision_by_id[revision_id]
            currency = revision.currency_id

            def _round_monetary(value):
                return monetary_handler._eh_round_monetary(
                    value, currency=currency,
                )

            scope_lines = budget_lines_by_scope.get((
                revision.budget_id.id, account_id, analytic_id,
            ), ())
            for revision_line in sorted(
                scope_rows,
                key=lambda row: (row.period_from, row.period_to, row.id),
            ):
                period_days = (
                    revision_line.period_to - revision_line.period_from
                ).days + 1
                if period_days <= 0:
                    continue
                overlaps = []
                for candidate in scope_lines:
                    overlap_from = max(
                        candidate.period_from, revision_line.period_from,
                    )
                    overlap_to = min(
                        candidate.period_to, revision_line.period_to,
                    )
                    if overlap_from > overlap_to:
                        continue
                    overlaps.append((
                        overlap_from,
                        overlap_to,
                        candidate.id,
                        (overlap_to - overlap_from).days + 1,
                    ))
                overlaps.sort(key=lambda item: item[:3])
                if not overlaps:
                    continue

                row_amount = _round_monetary(revision_line.amount or 0.0)
                allocated_amount = 0.0
                cumulative_days = 0
                for _start, _end, line_id, overlap_days in overlaps:
                    # Confirmed budgets reject overlapping identical scopes.
                    # Cap nevertheless keeps imported legacy overlap from
                    # allocating more than one monthly snapshot amount.
                    cumulative_days = min(
                        period_days, cumulative_days + overlap_days,
                    )
                    cumulative_amount = _round_monetary(
                        row_amount * cumulative_days / period_days,
                    )
                    share = _round_monetary(
                        cumulative_amount - allocated_amount,
                    )
                    amount_by_line_id[line_id] = _round_monetary(
                        amount_by_line_id[line_id] + share,
                    )
                    allocated_amount = cumulative_amount

        for line in self:
            amount = amount_by_line_id.get(line.id, 0.0)
            line.revision_amount = amount
            line.revision_variance = line._normalised_actual() - amount

    @api.depends('budgeted_amount', 'normalised_actual_amount',
                 'period_from', 'period_to')
    def _compute_theoretical(self):
        """Time-prorated pacing: how much of the budget should be spent
        by today, and how the actual compares to that run-rate."""
        today = fields.Date.context_today(self)
        for line in self:
            fraction = line._elapsed_fraction(today)
            line.elapsed_pct = fraction * 100.0
            theoretical = (line.budgeted_amount or 0.0) * fraction
            line.theoretical_amount = theoretical
            actual = line.normalised_actual_amount or 0.0
            line.pace_pct = (
                (actual / theoretical * 100.0) if theoretical else 0.0
            )

    def _elapsed_fraction(self, today):
        """Fraction (0..1) of this line's period elapsed as of `today`."""
        self.ensure_one()
        start, end = self.period_from, self.period_to
        if not start or not end:
            return 0.0
        if today <= start:
            return 0.0
        if today >= end:
            return 1.0
        span = (end - start).days
        if span <= 0:
            return 1.0
        return (today - start).days / span

    @api.model
    def _fetch_actuals_batch(self, budget, lines):
        """Return a dict keyed by (account_id, analytic_account_id_or_false,
        period_from, period_to) carrying the SUM(balance) for posted journal
        items in the matching period, account, and (when set) analytic
        account.

        One SQL pass per budget, independent of line and analytic-account
        count. SQL groups matching journal items by account, date, and
        analytic distribution. Python expands each distribution once, then
        prefix sums answer every line period without rescanning journal rows.
        Keys may be one id or a comma-separated cross-plan combination; only
        analytic scopes present on this already single-plan budget are kept.
        """
        valid_lines = [
            line for line in lines
            if line.account_id and line.period_from and line.period_to
        ]
        if not valid_lines:
            return {}
        account_ids = sorted({line.account_id.id for line in valid_lines})
        period_from = min(line.period_from for line in valid_lines)
        period_to = max(line.period_to for line in valid_lines)
        requested_scopes = {
            (
                line.account_id.id,
                line.analytic_account_id.id or False,
            )
            for line in valid_lines
        }
        analytic_ids_by_account = defaultdict(set)
        for account_id, analytic_id in requested_scopes:
            if analytic_id:
                analytic_ids_by_account[account_id].add(analytic_id)

        query = MoveLineQuery(
            self.env,
            company_ids=[budget.company_id.id],
        )
        query.where_date_range(date_from=period_from, date_to=period_to)
        query.where_accounts(account_ids)
        query.where_posted_only()
        # Analytic-only requests need not scan distributions that cannot
        # contribute. A non-analytic line intentionally counts every matching
        # journal item, so only apply this union predicate when every requested
        # scope is analytic.
        if requested_scopes and all(
                analytic_id for _account_id, analytic_id in requested_scopes):
            _eh_where_analytic_keys(query, sorted({
                analytic_id
                for _account_id, analytic_id in requested_scopes
            }))
        query.select_field('account_id')
        query.select_field('date')
        query.select(SQL("SUM(aml.balance)"), 'balance')
        has_analytic_scopes = bool(analytic_ids_by_account)
        if has_analytic_scopes:
            query.select_field('analytic_distribution')
            query.group_by('account_id', 'date', 'analytic_distribution')
        else:
            # Most budgets have no analytic scope. Aggregate daily totals in
            # SQL without exploding them by JSON distribution value.
            query.group_by('account_id', 'date')
        rows = query.execute()

        daily_by_scope = defaultdict(lambda: defaultdict(float))
        for row in rows:
            try:
                account_id = int(row.get('account_id'))
                posting_date = fields.Date.to_date(row.get('date'))
                if not posting_date:
                    continue
            except (TypeError, ValueError, OverflowError):
                continue
            try:
                balance = float(row.get('balance') or 0.0)
            except (TypeError, ValueError, OverflowError):
                continue
            if not isfinite(balance):
                continue

            account_scope = (account_id, False)
            if account_scope in requested_scopes:
                current = daily_by_scope[account_scope][posting_date]
                candidate = current + balance
                if isfinite(candidate):
                    daily_by_scope[account_scope][posting_date] = candidate

            requested_analytics = analytic_ids_by_account.get(account_id)
            distribution = row.get('analytic_distribution') or {}
            if not requested_analytics or not isinstance(distribution, dict):
                continue
            for key, raw_percentage in distribution.items():
                percentage = _eh_analytic_percentage(raw_percentage)
                if percentage is None:
                    continue
                token_ids = set()
                for token in str(key).split(','):
                    try:
                        token_ids.add(int(token.strip()))
                    except (TypeError, ValueError):
                        continue
                for analytic_id in requested_analytics.intersection(token_ids):
                    contribution = balance * percentage / 100.0
                    if not isfinite(contribution) or contribution == 0.0:
                        continue
                    scope = (account_id, analytic_id)
                    current = daily_by_scope[scope][posting_date]
                    candidate = current + contribution
                    if isfinite(candidate):
                        daily_by_scope[scope][posting_date] = candidate

        prefix_by_scope = {}
        for scope in requested_scopes:
            dated_amounts = sorted(daily_by_scope.get(scope, {}).items())
            dates = []
            prefix = [0.0]
            for posting_date, amount in dated_amounts:
                candidate = prefix[-1] + amount
                if not isfinite(candidate):
                    continue
                dates.append(posting_date)
                prefix.append(candidate)
            prefix_by_scope[scope] = (dates, prefix)

        result = {}
        for line in valid_lines:
            account_id = line.account_id.id
            analytic_id = line.analytic_account_id.id or False
            dates, prefix = prefix_by_scope[(account_id, analytic_id)]
            start = bisect_left(dates, line.period_from)
            stop = bisect_right(dates, line.period_to)
            amount = prefix[stop] - prefix[start]
            result[(
                account_id, analytic_id,
                line.period_from, line.period_to,
            )] = amount if isfinite(amount) else 0.0
        return result

    def action_view_actuals(self):
        """Open the journal items list filtered to the line's account
        and period for inspection."""
        self.ensure_one()
        domain = [
            ('account_id', '=', self.account_id.id),
            ('date', '>=', self.period_from),
            ('date', '<=', self.period_to),
            ('move_id.state', '=', 'posted'),
            ('company_id', '=', self.budget_id.company_id.id),
        ]
        if self.analytic_account_id:
            query = MoveLineQuery(
                self.env,
                company_ids=[self.budget_id.company_id.id],
            )
            query.where_date_range(
                date_from=self.period_from, date_to=self.period_to,
            )
            query.where_accounts([self.account_id.id])
            query.where_posted_only()
            # Key presence is insufficient: malformed, zero, underflowed, or
            # overflowed percentages contribute nothing to _compute_actual.
            # Filter them in SQL so the 20k safety limit counts only journal
            # items that can contribute, including composite analytic keys.
            query.where_raw(SQL(
                "EXISTS ("
                "SELECT 1 "
                "FROM jsonb_each(CASE "
                "WHEN jsonb_typeof(aml.analytic_distribution) = 'object' "
                "THEN aml.analytic_distribution ELSE '{}'::jsonb END"
                ") AS analytic_entry(key, value) "
                "WHERE %s = ANY(regexp_split_to_array("
                "btrim(analytic_entry.key), '\\s*,\\s*')) "
                "AND CASE "
                "WHEN jsonb_typeof(analytic_entry.value) = 'number' "
                "THEN ABS((analytic_entry.value::text)::numeric) "
                "BETWEEN %s::numeric AND %s::numeric "
                "AND ABS(aml.balance::numeric "
                "* (analytic_entry.value::text)::numeric / 100.0) "
                "BETWEEN %s::numeric AND %s::numeric "
                "ELSE FALSE END"
                ")",
                str(self.analytic_account_id.id),
                '4.9406564584124654e-324',
                '1.7976931348623157e308',
                '4.9406564584124654e-324',
                '1.7976931348623157e308',
            ))
            query.select_field('id', alias='aml_id')
            query.order_by('id', 'ASC')
            query.limit(20001)
            rows = query.execute()
            if len(rows) > 20000:
                raise UserError(_(
                    "This budget line contains more than 20,000 journal "
                    "items. Narrow its period before opening actuals."
                ))
            domain = [('id', 'in', [row['aml_id'] for row in rows])]
        return {
            'type': 'ir.actions.act_window',
            'name': _("Posted Journal Items"),
            'res_model': 'account.move.line',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': domain,
        }
