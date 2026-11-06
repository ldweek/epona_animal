# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.report.forecast: calendar-month forecast scenarios.

A forecast picks a base report (typically Profit and Loss or Cash Flow),
a complete calendar-month baseline, and a growth method. project() runs the
base report once, then projects horizon_months complete calendar months. Only
values explicitly typed as monetary are grown; percentages, ratios, day counts
and other numeric measures remain unchanged.

v1 supports two growth methods:

* flat: all forward periods equal the baseline.
* compound: each forward period is the baseline times (1 + growth)^n where
  n is the period index.

Seasonal patterns (per month factor) and scenario stacking (baseline vs
optimistic vs pessimistic side by side) are deferred to v1.1; the model
fields are placeholders ready for that work.
"""

import base64
import copy
import json
import math
from collections import defaultdict
from datetime import timedelta

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


_MAX_FORECAST_HORIZON = 24
_MAX_FORECAST_BASELINE_LINES = 2000
_MAX_FORECAST_PROJECTED_CELLS = 120000
_MAX_FORECAST_ESTIMATED_BYTES = 24 * 1024 * 1024
_MIN_MONTHLY_GROWTH_PCT = -100.0
_MAX_MONTHLY_GROWTH_PCT = 1000.0
_MONETARY_FIGURE_TYPE = 'monetary'
_SUPPORTED_FORECAST_HANDLER_MODELS = frozenset({
    'eh.account.dynamic.report.handler.balance_sheet',
    'eh.account.dynamic.report.handler.builder',
    'eh.account.dynamic.report.handler.cash_flow',
    'eh.account.dynamic.report.handler.executive_summary',
    'eh.account.dynamic.report.handler.profit_and_loss',
})
_RESERVED_OPTION_KEYS = frozenset({
    '_cache_context',
    'company_ids',
    'date',
    'eager_expand',
    'lazy_expand',
    'primary_company_id',
})


class EhReportForecast(models.Model):
    _name = 'eh.report.forecast'
    _description = "Multi period report forecast scenario"
    _order = 'name'

    name = fields.Char(required=True)
    scenario_label = fields.Char(default="Baseline")
    company_id = fields.Many2one(
        'res.company',
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )

    base_report_id = fields.Many2one(
        'eh.account.dynamic.report',
        required=True,
        ondelete='cascade',
        index=True,
    )
    base_date_from = fields.Date(
        required=True,
        help="First day of the complete calendar month used as baseline.",
    )
    base_date_to = fields.Date(
        required=True,
        help="Last day of the same calendar month used as baseline.",
    )

    horizon_months = fields.Integer(
        required=True,
        default=lambda self: (
            self.env.company.eh_forecast_default_horizon or 12
        ),
        help="Number of complete future calendar months to project.",
    )

    growth_method = fields.Selection(
        [
            ('flat', "Flat (no change)"),
            ('compound', "Compound monthly growth %"),
        ],
        default='compound',
        required=True,
    )
    monthly_growth_pct = fields.Float(
        default=0.0,
        help=(
            "Monthly percentage compounded once per projected month. "
            "5.0 means baseline x 1.05 in month one and x 1.1025 in month "
            "two."
        ),
    )

    last_run = fields.Datetime(readonly=True)

    _bounded_horizon = models.Constraint(
        'check(horizon_months between 1 and 24)',
        'Forecast horizon must be between one and 24 months.',
    )
    _bounded_monthly_growth = models.Constraint(
        'check(monthly_growth_pct between -100.0 and 1000.0)',
        'Monthly forecast growth must be between -100% and 1,000%.',
    )

    @api.model
    def _eh_validate_horizon_value(self, value):
        try:
            horizon = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValidationError(_(
                "Forecast horizon must be a whole number."
            )) from exc
        if not 1 <= horizon <= _MAX_FORECAST_HORIZON:
            raise ValidationError(_(
                "Forecast horizon must be between one and "
                "%(maximum)s months.",
                maximum=_MAX_FORECAST_HORIZON,
            ))
        return horizon

    @api.model
    def _eh_validate_growth_value(self, value):
        try:
            growth = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValidationError(_(
                "Monthly forecast growth must be a finite number."
            )) from exc
        if not math.isfinite(growth):
            raise ValidationError(_(
                "Monthly forecast growth must be a finite number."
            ))
        if not _MIN_MONTHLY_GROWTH_PCT <= growth <= _MAX_MONTHLY_GROWTH_PCT:
            raise ValidationError(_(
                "Monthly forecast growth must be between %(minimum)s%% "
                "and %(maximum)s%%.",
                minimum=int(_MIN_MONTHLY_GROWTH_PCT),
                maximum=int(_MAX_MONTHLY_GROWTH_PCT),
            ))
        return growth

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('growth_method') == 'linear':
                vals['growth_method'] = 'compound'
            if 'horizon_months' in vals:
                self._eh_validate_horizon_value(vals['horizon_months'])
            if 'monthly_growth_pct' in vals:
                self._eh_validate_growth_value(vals['monthly_growth_pct'])
        return super().create(vals_list)

    def write(self, vals):
        vals = dict(vals)
        if vals.get('growth_method') == 'linear':
            vals['growth_method'] = 'compound'
        if 'horizon_months' in vals:
            self._eh_validate_horizon_value(vals['horizon_months'])
        if 'monthly_growth_pct' in vals:
            self._eh_validate_growth_value(vals['monthly_growth_pct'])
        return super().write(vals)

    @api.constrains('horizon_months')
    def _check_horizon_months(self):
        for forecast in self:
            forecast._eh_validate_horizon_value(forecast.horizon_months)

    @api.constrains('monthly_growth_pct')
    def _check_monthly_growth_pct(self):
        for forecast in self:
            forecast._eh_validate_growth_value(
                forecast.monthly_growth_pct,
            )

    @api.constrains('base_report_id', 'company_id')
    def _check_base_report_company(self):
        for forecast in self:
            owner = forecast.base_report_id.company_id
            if owner and owner != forecast.company_id:
                raise ValidationError(_(
                    "Forecast company must match the custom base report's "
                    "company."
                ))

    def _eh_validate_horizon(self):
        self.ensure_one()
        if not 1 <= self.horizon_months <= _MAX_FORECAST_HORIZON:
            raise UserError(_(
                "Forecast %(forecast)s has an unsafe horizon. Set it to "
                "between one and %(maximum)s months before projecting.",
                forecast=self.display_name,
                maximum=_MAX_FORECAST_HORIZON,
            ))
        return True

    def _eh_validate_growth(self):
        self.ensure_one()
        try:
            self._eh_validate_growth_value(self.monthly_growth_pct)
        except ValidationError as exc:
            raise UserError(_(
                "Forecast %(forecast)s has unsafe monthly growth: "
                "%(reason)s",
                forecast=self.display_name,
                reason=str(exc),
            )) from exc
        return True

    # ---- public api ----

    def project(self, options=None):
        """Run the base report and project horizon_months forward.

        :param options: optional overrides merged into the base options
            (posted_only, report layout, analytic filters, etc.). Company and
            date scope always come from the forecast record.
        :return: dict with keys: baseline, periods, meta.
        """
        self.ensure_one()
        self._eh_check_access('write')
        self._eh_validate_horizon()
        self._eh_validate_growth()
        self._eh_validate_baseline_month()
        self._eh_validate_base_report_kind()
        base_options = self._build_base_options(options or {})
        baseline = self.base_report_id.render(base_options)
        self._eh_validate_baseline_size(baseline)
        periods = self._project_periods(baseline)
        self.last_run = fields.Datetime.now()
        return {
            'baseline': baseline,
            'periods': periods,
            'meta': {
                'forecast_id': self.id,
                'name': self.name,
                'scenario_label': self.scenario_label,
                'horizon_months': self.horizon_months,
                'growth_method': self.growth_method,
                'monthly_growth_pct': self.monthly_growth_pct,
                'base_date_from': self.base_date_from.isoformat(),
                'base_date_to': self.base_date_to.isoformat(),
                'period_granularity': 'calendar_month',
                'growth_scope': 'monetary_only',
            },
        }

    def action_project_now(self):
        """Run and expose exact JSON through an owner-scoped transient.

        Keeping bytes in a non-attachment Binary field makes result usable
        without creating a permanent ``ir.attachment`` blob. Odoo's transient
        vacuum removes payload automatically after its short useful life.
        """
        self.ensure_one()
        result = self.project()
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        ).encode('utf-8')
        result_wizard = self.env['eh.report.forecast.result'].create({
            'forecast_id': self.id,
            'filename': 'forecast_%s_%s.json' % (
                self.id,
                self.base_date_to.isoformat(),
            ),
            'file_data': base64.b64encode(encoded),
            'summary': _(
                "%(count)s monthly periods projected from baseline "
                "%(date_from)s to %(date_to)s. Monetary values use the "
                "configured growth; non-monetary measures are unchanged.",
                count=self.horizon_months,
                date_from=self.base_date_from.isoformat(),
                date_to=self.base_date_to.isoformat(),
            ),
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _("Forecast Result"),
            'res_model': 'eh.report.forecast.result',
            'view_mode': 'form',
            'res_id': result_wizard.id,
            'target': 'new',
        }

    # ---- internals ----

    def _eh_validate_base_report_kind(self):
        self.ensure_one()
        owner = self.base_report_id.company_id
        if owner and owner != self.company_id:
            raise UserError(_(
                "Forecast company must match the custom base report's "
                "company."
            ))
        if (
            self.base_report_id.handler_model
            not in _SUPPORTED_FORECAST_HANDLER_MODELS
        ):
            raise UserError(_(
                "%(report)s cannot be used as a forecast baseline because "
                "its layout has no proven additive identities for safe "
                "scalar monetary growth. Choose Profit and Loss, Balance "
                "Sheet, Cash Flow, Executive Summary, or a bounded custom "
                "builder.",
                report=self.base_report_id.display_name,
            ))
        return True

    def _eh_validate_baseline_size(self, baseline):
        """Bound projection amplification before cloning future periods."""
        self.ensure_one()
        if not isinstance(baseline, dict):
            raise UserError(_("Forecast baseline returned an invalid payload."))
        lines = baseline.get('lines') or []
        if not isinstance(lines, list):
            raise UserError(_("Forecast baseline lines must be a list."))
        line_count = len(lines)
        cell_count = sum(
            len(line.get('columns') or [])
            for line in lines
            if isinstance(line, dict)
        )
        projected_cells = cell_count * (self.horizon_months + 1)
        baseline_bytes = len(json.dumps(
            baseline,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode('utf-8'))
        estimated_bytes = baseline_bytes * (self.horizon_months + 1)
        if (
            line_count > _MAX_FORECAST_BASELINE_LINES
            or projected_cells > _MAX_FORECAST_PROJECTED_CELLS
            or estimated_bytes > _MAX_FORECAST_ESTIMATED_BYTES
        ):
            raise UserError(_(
                "Forecast baseline is too large to project safely "
                "(%(lines)s lines, %(cells)s projected cells). Narrow the "
                "report or reduce the forecast horizon.",
                lines=line_count,
                cells=projected_cells,
            ))
        return True

    def _build_base_options(self, overrides):
        """Merge public report options while fixing forecast scope.

        Report options grow as handlers gain dimensions. Starting from the
        handler defaults and retaining every public caller override avoids a
        brittle allow-list that silently drops supported filters. Date,
        company and expansion mode are forecast-owned and cannot be replaced.
        """
        if not isinstance(overrides, dict):
            raise UserError(_("Forecast options must be a dictionary."))
        options = copy.deepcopy(self.base_report_id.get_default_options())
        for key, value in overrides.items():
            if not isinstance(key, str):
                raise UserError(_("Forecast option names must be text."))
            if key.startswith('_') or key in _RESERVED_OPTION_KEYS:
                continue
            options[key] = copy.deepcopy(value)
        options.update({
            'date': {
                'mode': 'range',
                'date_from': self.base_date_from.isoformat(),
                'date_to': self.base_date_to.isoformat(),
            },
            'company_ids': [self.company_id.id],
            'primary_company_id': self.company_id.id,
            # Forecasts require every baseline line. Never persist a viewer's
            # lazy skeleton as though it were a complete scenario.
            'eager_expand': True,
        })
        options.pop('lazy_expand', None)
        options.pop('_cache_context', None)
        return options

    def _eh_validate_baseline_month(self):
        self.ensure_one()
        date_from = self.base_date_from
        date_to = self.base_date_to
        if not date_from or not date_to:
            raise UserError(_(
                "Forecast baseline requires both a start and end date.",
            ))
        expected_to = date_from + relativedelta(months=1) - timedelta(days=1)
        if date_from.day != 1 or date_to != expected_to:
            raise UserError(_(
                "Forecast baseline must be one complete calendar month "
                "(first day through last day of the same month).",
            ))
        return True

    def _project_periods(self, baseline):
        periods = []
        currency = self._forecast_currency(baseline)
        is_cash_flow = (
            self.base_report_id.handler_model
            == 'eh.account.dynamic.report.handler.cash_flow'
        )
        if is_cash_flow and not currency.is_zero(
            self._forecast_cash_amount(
                baseline.get('lines') or [], 'cash_balance_check',
            )
        ):
            raise UserError(_(
                "Cash Flow forecast baseline does not reconcile. Resolve "
                "its non-zero cash balance check before projecting it."
            ))
        cash_opening = (
            self._forecast_cash_amount(
                baseline.get('lines') or [], 'closing_cash_balance',
            )
            if is_cash_flow else None
        )
        for index in range(self.horizon_months):
            period_from = self.base_date_from + relativedelta(
                months=index + 1,
            )
            period_to = (
                period_from + relativedelta(months=1) - timedelta(days=1)
            )
            factor = self._growth_factor(index + 1)
            projected = self._apply_factor(
                baseline, factor, currency=currency,
            )
            if is_cash_flow:
                cash_opening = self._roll_forward_cash_flow_period(
                    projected, cash_opening, currency,
                )
            periods.append({
                'period_index': index + 1,
                'date_from': period_from.isoformat(),
                'date_to': period_to.isoformat(),
                'period_label': period_from.strftime('%Y-%m'),
                'growth_factor': round(factor, 6),
                'lines': projected['lines'],
                'totals': projected['totals'],
            })
        return periods

    @api.model
    def _forecast_cash_amount(self, lines, line_id):
        matches = [line for line in lines if line.get('id') == line_id]
        if len(matches) != 1:
            raise UserError(_(
                "Cash Flow forecast baseline has an incomplete cash "
                "roll-forward identity."
            ))
        cells = [
            cell for cell in (matches[0].get('columns') or [])
            if cell.get('expression_label') == 'amount'
            and self._is_numeric(cell.get('value'))
        ]
        if len(cells) != 1:
            raise UserError(_(
                "Cash Flow forecast baseline has an incomplete cash "
                "roll-forward identity."
            ))
        return cells[0]['value']

    @api.model
    def _roll_forward_cash_flow_period(
        self, projected, opening_value, currency,
    ):
        """Carry cash continuously across projected calendar months."""
        lines = projected.get('lines') or []
        by_id = {line.get('id'): line for line in lines if line.get('id')}

        def _amount_cell(line_id, required=True):
            line = by_id.get(line_id)
            cells = [
                cell for cell in ((line or {}).get('columns') or [])
                if cell.get('expression_label') == 'amount'
                and self._is_numeric(cell.get('value'))
            ]
            if len(cells) == 1:
                return line, cells[0]
            if not required and line is None:
                return None, None
            raise UserError(_(
                "Cash Flow forecast baseline has an incomplete cash "
                "roll-forward identity."
            ))

        opening_line, opening = _amount_cell('opening_cash_balance')
        _net_line, net_change = _amount_cell('net_change_in_cash')
        closing_line, closing = _amount_cell('closing_cash_balance')
        check_line, check = _amount_cell('cash_balance_check')
        fx_line, fx_effect = _amount_cell(
            'fx_effect_on_cash', required=False,
        )
        opening['value'] = currency.round(opening_value)
        fx_value = fx_effect['value'] if fx_effect else 0.0
        closing['value'] = currency.round(
            opening['value'] + net_change['value'] + fx_value,
        )
        check['value'] = currency.round(
            closing['value'] - opening['value']
            - net_change['value'] - fx_value,
        )
        for line in filter(None, (
            opening_line, closing_line, check_line, fx_line,
        )):
            self._recompute_comparison_cells(
                line.get('columns') or [], currency,
            )
        totals = projected.setdefault('totals', {})
        totals['opening_cash_balance'] = opening['value']
        totals['closing_cash_balance'] = closing['value']
        totals['balance_check'] = check['value']
        if fx_effect is not None and 'fx_effect_on_cash' in totals:
            totals['fx_effect_on_cash'] = fx_effect['value']
        return closing['value']

    def _growth_factor(self, period_n):
        if self.growth_method == 'flat':
            return 1.0
        if self.growth_method == 'compound':
            monthly = 1.0 + (self.monthly_growth_pct / 100.0)
            return monthly ** period_n
        return 1.0

    @api.model
    def _apply_factor(self, baseline, factor, currency=None):
        # Resolve/validate once. A large forecast may contain 120k monetary
        # cells; browsing the same currency from every cell turns projection
        # into an avoidable O(cells) SQL path.
        currency = currency or self._forecast_currency(baseline)
        column_types = {
            col.get('expression_label'): col.get('figure_type', 'string')
            for col in (baseline.get('columns') or [])
            if isinstance(col, dict) and col.get('expression_label')
        }
        out_lines = []
        for line in baseline.get('lines') or []:
            out_columns = []
            for col in line.get('columns') or []:
                value = col.get('value')
                figure_type = (
                    col.get('figure_type')
                    or column_types.get(col.get('expression_label'), 'string')
                )
                if (
                    figure_type == _MONETARY_FIGURE_TYPE
                    and self._is_numeric(value)
                    and self._is_projected_expression(
                        col.get('expression_label'))
                ):
                    out_columns.append(
                        dict(
                            col,
                            value=self._round_monetary(
                                value * factor, currency,
                            ),
                        ),
                    )
                else:
                    out_columns.append(dict(col))
            out_lines.append(dict(line, columns=out_columns))

        # Currency-rounding is not additive: two USD 0.01 leaves projected
        # by 0.5 independently become 0.01 + 0.01 while their projected
        # total is 0.01. Reconcile only relationships explicitly proven by
        # existing section/parent metadata and already footed in the source
        # payload. Unknown or non-footing layouts remain byte-for-byte on the
        # former independent-rounding path.
        self._reconcile_projected_footing(
            baseline.get('lines') or [], out_lines, factor, currency,
            column_types,
        )
        # References never move, but their derived variances must use any
        # one-minor-unit allocation applied above.
        for line in out_lines:
            self._recompute_comparison_cells(
                line.get('columns') or [], currency,
            )

        total_types = self._total_figure_types(baseline, column_types)
        out_totals = {}
        for k, v in (baseline.get('totals') or {}).items():
            if k == 'column_scopes':
                out_totals[k] = self._scale_column_scope_totals(
                    v, total_types, factor, currency,
                )
            elif (
                total_types.get(k) == _MONETARY_FIGURE_TYPE
                and self._is_projected_expression(k)
            ):
                out_totals[k] = self._scale_total_value(
                    v, factor, currency,
                )
            else:
                out_totals[k] = copy.deepcopy(v)
        self._sync_builder_projection_totals(
            out_lines, out_totals, total_types, column_types,
            currency=currency,
        )
        return {'lines': out_lines, 'totals': out_totals}

    @api.model
    def _reconcile_projected_footing(
        self, baseline_lines, projected_lines, factor, currency,
        column_types,
    ):
        """Allocate projection-rounding residuals through typed line trees.

        Relationships come from payload contracts shared by aggregate
        reports: authored builder +/- dependencies, matching
        ``section_header``/``section_total`` pairs, and explicit
        ``parent_id`` trees. A relationship is used only when its baseline
        target already equals its signed summands at currency precision. This
        keeps formula, ratio, disclosure-only, and custom layouts fail-closed
        instead of guessing accounting semantics.
        """
        if not baseline_lines or len(baseline_lines) != len(projected_lines):
            return projected_lines

        # One cell-map build per line keeps reconciliation linear in payload
        # cells even for bounded custom reports with many monetary columns.
        baseline_cells = [
            self._forecast_projected_cells(line, column_types)
            for line in baseline_lines
        ]
        projected_cells = [
            self._forecast_projected_cells(line, column_types)
            for line in projected_lines
        ]
        relations = self._forecast_statement_relations(baseline_lines)
        relations.extend(self._forecast_builder_relations(baseline_lines))
        relations.extend(self._forecast_section_relations(baseline_lines))
        relations.extend(self._forecast_parent_relations(baseline_lines))
        relations.sort(key=lambda relation: relation[0])
        for _order, target_index, candidate_indices in relations:
            self._reconcile_projected_relation(
                baseline_cells, projected_cells, target_index,
                candidate_indices, factor, currency,
            )
        return projected_lines

    @api.model
    def _forecast_statement_relations(self, lines):
        """Return signed cross-section identities for supported statements.

        Section and parent relations prove footing inside each block. These
        identities connect those blocks to statement-level computed rows.
        Every relation is still baseline-proven by
        ``_reconcile_projected_relation`` before any projected value moves.
        """
        positions = defaultdict(list)
        for index, line in enumerate(lines):
            line_id = line.get('id')
            if line_id:
                positions[line_id].append(index)

        relations = []

        def _add(target_id, terms):
            target_positions = positions.get(target_id) or []
            if len(target_positions) != 1:
                return False
            candidates = []
            for line_id, coefficient in terms:
                candidate_positions = positions.get(line_id) or []
                if len(candidate_positions) != 1:
                    return False
                candidates.append((candidate_positions[0], coefficient))
            target_index = target_positions[0]
            relations.append((
                (-2000, -target_index, target_index),
                target_index,
                candidates,
            ))
            return True

        # Profit and Loss: by-function chain when present, otherwise the
        # standard by-nature Income - Expenses identity.
        _add('gross_profit', (
            ('section-income-total', 1),
            ('section-cost_of_sales-total', -1),
        ))
        _add('operating_profit', (
            ('gross_profit', 1),
            ('section-operating_expenses-total', -1),
        ))
        _add('profit_before_tax', (
            ('operating_profit', 1),
            ('section-finance_costs-total', -1),
        ))
        _add('section-tax_expense-total', (
            ('section-current_tax-total', 1),
            ('section-deferred_tax-total', 1),
        ))
        if not _add('net_profit', (
            ('profit_before_tax', 1),
            ('section-tax_expense-total', -1),
        )):
            _add('net_profit', (
                ('section-income-total', 1),
                ('section-expenses-total', -1),
            ))

        # Balance Sheet: Assets = Liabilities + Equity and check = zero.
        _add('total_equity_liabilities', (
            ('section-liabilities-total', 1),
            ('section-equity-total', 1),
        ))
        _add('balance_check', (
            ('section-assets-total', 1),
            ('total_equity_liabilities', -1),
        ))

        # Cash Flow: activity bridge and IAS 7 opening/closing identity.
        _add('net_change_in_cash', (
            ('section-operating-total', 1),
            ('section-investing-total', 1),
            ('section-financing-total', 1),
        ))
        cash_terms = [
            ('opening_cash_balance', 1),
            ('net_change_in_cash', 1),
        ]
        if len(positions.get('fx_effect_on_cash') or []) == 1:
            cash_terms.append(('fx_effect_on_cash', 1))
        _add('closing_cash_balance', cash_terms)
        check_terms = [
            ('closing_cash_balance', 1),
            ('opening_cash_balance', -1),
            ('net_change_in_cash', -1),
        ]
        if len(positions.get('fx_effect_on_cash') or []) == 1:
            check_terms.append(('fx_effect_on_cash', -1))
        _add('cash_balance_check', check_terms)
        return relations

    @api.model
    def _forecast_builder_relations(self, lines):
        """Return authored formula dependencies from outer to inner.

        Builder evaluation permits references only to prior named lines, so
        reverse target order is a safe topological order: final formulas
        retain their independently projected values, then any adjusted
        intermediate target pushes its residual into its own dependencies.
        Malformed or incomplete metadata fails closed.
        """
        indices_by_code = {}
        for index, line in enumerate(lines):
            code = (line.get('meta') or {}).get('builder_line_code')
            if code:
                indices_by_code.setdefault(code, []).append(index)

        relations = []
        for target_index, line in enumerate(lines):
            raw_terms = (line.get('meta') or {}).get(
                'forecast_additive_terms',
            )
            if not isinstance(raw_terms, list) or not raw_terms:
                continue
            candidates = []
            seen_codes = set()
            valid = True
            for term in raw_terms:
                if not isinstance(term, dict):
                    valid = False
                    break
                code = term.get('code')
                coefficient = term.get('coefficient')
                indices = indices_by_code.get(code) or []
                if (
                    not isinstance(code, str)
                    or not code
                    or code in seen_codes
                    or coefficient not in (-1, 1)
                    or len(indices) != 1
                    or indices[0] >= target_index
                ):
                    valid = False
                    break
                seen_codes.add(code)
                candidates.append((indices[0], coefficient))
            if valid and candidates:
                relations.append((
                    (-1000, -target_index, target_index),
                    target_index,
                    candidates,
                ))
        return relations

    @api.model
    def _forecast_section_relations(self, lines):
        """Return outer-to-inner additive relations from section metadata."""
        total_positions = {}
        for index, line in enumerate(lines):
            meta = line.get('meta') or {}
            if meta.get('kind') == 'section_total' and meta.get('section_id'):
                total_positions.setdefault(meta['section_id'], []).append(
                    index,
                )

        nodes = []
        stack = []
        for index, line in enumerate(lines):
            meta = line.get('meta') or {}
            kind = meta.get('kind')
            section_id = meta.get('section_id')
            if kind == 'section_header' and section_id:
                future_totals = total_positions.get(section_id) or []
                if not any(position > index for position in future_totals):
                    continue
                node = {
                    'section_id': section_id,
                    'start': index,
                    'total': None,
                    'depth': len(stack),
                    'children': [],
                }
                if stack:
                    stack[-1]['children'].append(node)
                nodes.append(node)
                stack.append(node)
                continue
            if kind != 'section_total' or not section_id:
                continue
            matching = next(
                (
                    position for position in range(len(stack) - 1, -1, -1)
                    if stack[position]['section_id'] == section_id
                ),
                None,
            )
            if matching is None:
                continue
            # Malformed crossing sections carry no safe additive contract.
            for abandoned in stack[matching + 1:]:
                abandoned['total'] = None
            node = stack[matching]
            node['total'] = index
            stack = stack[:matching]

        relations = []
        valid_nodes = [node for node in nodes if node['total'] is not None]
        for node in valid_nodes:
            # Builder formulas carry exact signed authored dependencies.
            # Never replace them with visible-row summation, which would
            # double-count computed intermediates inside the same section.
            if (lines[node['total']].get('meta') or {}).get(
                'forecast_additive_terms',
            ):
                continue
            direct = self._forecast_direct_section_lines(node, lines)
            if not direct:
                continue
            subtotals = [
                position for position, line_index in enumerate(direct)
                if (lines[line_index].get('meta') or {}).get('kind')
                == 'section_subtotal'
            ]
            if not subtotals:
                relations.append((
                    (node['depth'], 0, node['start']),
                    node['total'],
                    direct,
                ))
                continue

            # A section subtotal is cumulative over direct lines since the
            # preceding subtotal. Final section total then adds lines after
            # the last subtotal. Baseline-footing proof below rejects any
            # handler whose subtotal contract differs.
            previous_subtotal = None
            for subtotal_position in subtotals:
                start = (
                    0 if previous_subtotal is None
                    else previous_subtotal + 1
                )
                candidates = direct[start:subtotal_position]
                if previous_subtotal is not None:
                    candidates.insert(0, direct[previous_subtotal])
                if candidates:
                    relations.append((
                        # Later cumulative subtotals must reconcile first;
                        # they may push a residual into an earlier subtotal.
                        # The earlier relation then repairs its own children.
                        (node['depth'], 1, -direct[subtotal_position]),
                        direct[subtotal_position],
                        candidates,
                    ))
                previous_subtotal = subtotal_position
            final_candidates = [direct[subtotals[-1]]]
            final_candidates.extend(direct[subtotals[-1] + 1:])
            relations.append((
                (node['depth'], 0, node['start']),
                node['total'],
                final_candidates,
            ))

        # Some statement layouts expose a child subtotal under a parent
        # section without rendering another child header (current/deferred
        # tax under Tax Expense). Handler-stamped section ownership supplies
        # a safe relation for that exact shape. Select only top-level roots;
        # account-group relations reconcile their descendants later.
        visible_header_sections = {
            (line.get('meta') or {}).get('section_id')
            for line in lines
            if (line.get('meta') or {}).get('kind') == 'section_header'
        }
        for section_id, target_indices in total_positions.items():
            if section_id in visible_header_sections or len(target_indices) != 1:
                continue
            target_index = target_indices[0]
            members = [
                index for index, line in enumerate(lines[:target_index])
                if (line.get('meta') or {}).get('section_id') == section_id
                and (line.get('meta') or {}).get('kind') not in {
                    'section_header', 'section_total', 'warning',
                }
            ]
            member_ids = {
                lines[index].get('id') for index in members
                if lines[index].get('id')
            }
            roots = [
                index for index in members
                if not lines[index].get('parent_id')
                or lines[index].get('parent_id') not in member_ids
            ]
            if roots:
                relations.append((
                    (0, 2, target_index), target_index, roots,
                ))
        return relations

    @api.model
    def _forecast_direct_section_lines(self, node, lines):
        child_by_start = {
            child['start']: child
            for child in node['children']
            if child.get('total') is not None
        }
        direct = []
        index = node['start'] + 1
        section_header_id = lines[node['start']].get('id')
        while index < node['total']:
            child = child_by_start.get(index)
            if child:
                direct.append(child['total'])
                index = child['total'] + 1
                continue
            line = lines[index]
            meta = line.get('meta') or {}
            if meta.get('kind') in {
                'section_header', 'section_total', 'warning',
            }:
                index += 1
                continue
            parent_id = line.get('parent_id')
            # Hierarchical reports expose parent aggregates and leaves. Only
            # direct roots feed section total; child footing is reconciled by
            # explicit parent relations in a later, inner pass.
            if parent_id and parent_id != section_header_id:
                index += 1
                continue
            direct.append(index)
            index += 1
        return direct

    @api.model
    def _forecast_parent_relations(self, lines):
        index_by_id = {
            line.get('id'): index
            for index, line in enumerate(lines)
            if line.get('id')
        }
        children_by_parent = {}
        for index, line in enumerate(lines):
            parent_index = index_by_id.get(line.get('parent_id'))
            if parent_index is not None:
                children_by_parent.setdefault(parent_index, []).append(index)

        relations = []
        for parent_index, child_indices in children_by_parent.items():
            parent = lines[parent_index]
            if (parent.get('meta') or {}).get('kind') != 'account_group':
                continue
            depth = int(parent.get('level') or 0)
            relations.append((
                (1000 + depth, 0, parent_index),
                parent_index,
                child_indices,
            ))
        return relations

    @api.model
    def _reconcile_projected_relation(
        self, baseline_cells, projected_cells, target_index,
        candidate_indices, factor, currency,
    ):
        baseline_target_cells = baseline_cells[target_index]
        projected_target_cells = projected_cells[target_index]
        for expression, baseline_target in baseline_target_cells.items():
            projected_target = projected_target_cells.get(expression)
            if not projected_target:
                continue
            candidates = []
            baseline_sum = 0.0
            complete = True
            for candidate_term in candidate_indices:
                if isinstance(candidate_term, int):
                    candidate_index = candidate_term
                    coefficient = 1
                elif (
                    isinstance(candidate_term, (list, tuple))
                    and len(candidate_term) == 2
                    and candidate_term[1] in (-1, 1)
                ):
                    candidate_index, coefficient = candidate_term
                else:
                    complete = False
                    break
                baseline_cell = baseline_cells[candidate_index].get(
                    expression,
                )
                projected_cell = projected_cells[candidate_index].get(
                    expression,
                )
                if baseline_cell is None or projected_cell is None:
                    complete = False
                    break
                baseline_sum += coefficient * baseline_cell['value']
                candidates.append((
                    candidate_index, baseline_cell, projected_cell,
                    coefficient,
                ))
            if not complete or not candidates:
                continue
            # Reconcile rounding only; never hide an existing baseline
            # accounting discrepancy or infer an undocumented formula.
            if not currency.is_zero(
                baseline_target['value'] - baseline_sum,
            ):
                continue
            self._allocate_projected_residual(
                candidates, projected_target['value'], factor, currency,
            )

    @api.model
    def _forecast_projected_cells(self, line, column_types):
        cells = {}
        for cell in line.get('columns') or []:
            expression = cell.get('expression_label')
            figure_type = (
                cell.get('figure_type')
                or column_types.get(expression, 'string')
            )
            if (
                expression
                and figure_type == _MONETARY_FIGURE_TYPE
                and self._is_projected_expression(expression)
                and self._is_numeric(cell.get('value'))
            ):
                cells[expression] = cell
        return cells

    @api.model
    def _allocate_projected_residual(
        self, candidates, target_value, factor, currency,
    ):
        projected_sum = sum(
            item[3] * item[2]['value'] for item in candidates
        )
        delta = currency.round(target_value - projected_sum)
        if currency.is_zero(delta):
            return
        quantum = float(currency.rounding or 0.0)
        if not quantum:
            return
        units = int(round(delta / quantum))
        if not units:
            return

        direction = 1 if units > 0 else -1
        ordered = sorted(
            candidates,
            key=lambda item: (
                -direction * (
                    item[3] * (
                        (item[1]['value'] * factor) - item[2]['value']
                    )
                ),
                item[0],
            ),
        )
        whole, remainder = divmod(abs(units), len(ordered))
        if whole:
            contribution = direction * quantum * whole
            for (
                _index, _baseline_cell, projected_cell, coefficient,
            ) in ordered:
                projected_cell['value'] = currency.round(
                    projected_cell['value'] + coefficient * contribution,
                )
        contribution_step = direction * quantum
        for (
            _index, _baseline_cell, projected_cell, coefficient,
        ) in ordered[:remainder]:
            projected_cell['value'] = currency.round(
                projected_cell['value']
                + coefficient * contribution_step,
            )

        # Float representation can leave a sub-quantum residue. Replacing
        # one selected cell from target-minus-peers keeps final value on
        # currency precision without an unbounded correction loop.
        remaining = currency.round(
            target_value - sum(
                item[3] * item[2]['value'] for item in candidates
            ),
        )
        if not currency.is_zero(remaining):
            selected = ordered[0][2]
            selected_coefficient = ordered[0][3]
            other_sum = sum(
                item[3] * item[2]['value'] for item in candidates
                if item[2] is not selected
            )
            selected['value'] = currency.round(
                selected_coefficient * (target_value - other_sum),
            )

    @api.model
    def _forecast_explicit_total_keys(self):
        """Map visible statement rows to differently named totals keys."""
        return {
            'section-income-total': ('income', 'revenue'),
            'section-expenses-total': ('expenses',),
            'section-cost_of_sales-total': ('cost_of_sales',),
            'gross_profit': ('gross_profit',),
            'section-operating_expenses-total': ('operating_expenses',),
            'operating_profit': ('operating_profit',),
            'section-finance_costs-total': ('finance_costs',),
            'profit_before_tax': ('profit_before_tax',),
            'section-tax_expense-total': ('tax_expense',),
            'section-current_tax-total': ('current_tax',),
            'section-deferred_tax-total': ('deferred_tax',),
            'net_profit': ('net_profit', 'amount'),
            'section-assets_current-total': ('current_assets',),
            'section-assets_non_current-total': ('non_current_assets',),
            'section-assets-total': ('assets',),
            'section-liabilities_current-total': ('current_liabilities',),
            'section-liabilities_non_current-total': (
                'non_current_liabilities',
            ),
            'section-liabilities-total': ('liabilities',),
            'section-equity-total': ('total_equity',),
            'previous_year_earnings': ('previous_year_earnings',),
            'current_year_earnings': ('current_year_earnings',),
            'total_equity_liabilities': ('total_equity_liabilities',),
            'balance_check': ('balance_check',),
            'section-operating-total': ('operating',),
            'section-investing-total': ('investing',),
            'section-financing-total': ('financing',),
            'net_change_in_cash': ('net_change_in_cash',),
            'fx_effect_on_cash': ('fx_effect_on_cash',),
            'opening_cash_balance': ('opening_cash_balance',),
            'closing_cash_balance': ('closing_cash_balance',),
            'cash_balance_check': ('balance_check',),
            'indirect-cgo': ('cash_generated_from_operations',),
            'section-noncash_register-total': ('noncash_register',),
        }

    @api.model
    def _sync_builder_projection_totals(
        self, lines, totals, total_types, column_types, currency=None,
    ):
        """Keep typed totals aligned with reconciled visible statement rows."""
        explicit_total_keys = self._forecast_explicit_total_keys()
        amount_by_line_id = {}
        axis_amounts_by_expression = defaultdict(dict)
        column_scopes = totals.get('column_scopes')
        if not isinstance(column_scopes, dict):
            column_scopes = {}
        for line in lines:
            line_id = line.get('id')
            meta = line.get('meta') or {}
            semantic_keys = set(explicit_total_keys.get(line_id, ()))
            semantic_keys.update(filter(None, (
                meta.get('builder_line_code'),
                meta.get('metric'),
            )))
            if line_id and line_id in totals:
                semantic_keys.add(line_id)
            if (
                not explicit_total_keys.get(line_id)
                and meta.get('kind') == 'section_total'
                and meta.get('section_id')
            ):
                semantic_keys.add(meta['section_id'])
            cells = self._forecast_projected_cells(line, column_types)
            amount = cells.get('amount') or cells.get('value')
            if amount is not None:
                if line_id:
                    amount_by_line_id[line_id] = amount['value']
                for key in semantic_keys:
                    if (
                        key in totals
                        and total_types.get(key) == _MONETARY_FIGURE_TYPE
                        and self._is_projected_expression(key)
                    ):
                        totals[key] = amount['value']

            # New column-axis payloads use one expression per scope instead
            # of ``amount``. Reconciliation may move one minor unit between
            # visible rows, so copy those authoritative row values back into
            # each current-scope semantic summary after footing.
            for expression, axis_amount in cells.items():
                scope_totals = column_scopes.get(expression)
                if not isinstance(scope_totals, dict):
                    continue
                if line_id:
                    axis_amounts_by_expression[expression][line_id] = (
                        axis_amount['value']
                    )
                for key in semantic_keys:
                    if (
                        key in scope_totals
                        and total_types.get(key) == _MONETARY_FIGURE_TYPE
                    ):
                        scope_totals[key] = axis_amount['value']

            if meta.get('kind') == 'disclosure_line' and amount is not None:
                item = meta.get('item')
                disclosures = totals.get('disclosures')
                if isinstance(disclosures, dict) and item in disclosures:
                    disclosures[item] = amount['value']

        if currency:
            # By-function P&L exposes no single Total Expenses row; derive
            # its semantic total from reconciled visible section totals.
            expense_ids = (
                'section-cost_of_sales-total',
                'section-operating_expenses-total',
                'section-finance_costs-total',
                'section-tax_expense-total',
            )
            if 'expenses' in totals and all(
                    line_id in amount_by_line_id for line_id in expense_ids):
                totals['expenses'] = currency.round(sum(
                    amount_by_line_id[line_id] for line_id in expense_ids
                ))

            # Balance Sheet's raw equity total excludes current/prior
            # earnings and has no visible subtotal row. Rebuild it from the
            # reconciled Total Equity identity rather than retaining an
            # independently rounded scalar.
            equity_ids = (
                'section-equity-total',
                'previous_year_earnings',
                'current_year_earnings',
            )
            if 'equity' in totals and all(
                    line_id in amount_by_line_id for line_id in equity_ids):
                totals['equity'] = currency.round(
                    amount_by_line_id['section-equity-total']
                    - amount_by_line_id['previous_year_earnings']
                    - amount_by_line_id['current_year_earnings']
                )

        current_axis_expressions = [
            expression for expression in axis_amounts_by_expression
            if '__period_current' in expression
        ]
        current_summary_expression = next((
            expression for expression in current_axis_expressions
            if '__analytic_total' in expression
        ), None)
        if current_summary_expression is None:
            current_summary_expression = next((
                expression for expression in current_axis_expressions
                if expression == 'amount__period_current'
            ), None)
        if (
            current_summary_expression is None
            and len(current_axis_expressions) == 1
        ):
            current_summary_expression = current_axis_expressions[0]

        for expression, scoped_amounts in (
                axis_amounts_by_expression.items()):
            scope_totals = column_scopes.get(expression)
            if not isinstance(scope_totals, dict):
                continue
            if currency:
                expense_ids = (
                    'section-cost_of_sales-total',
                    'section-operating_expenses-total',
                    'section-finance_costs-total',
                    'section-tax_expense-total',
                )
                if 'expenses' in scope_totals and all(
                        line_id in scoped_amounts
                        for line_id in expense_ids):
                    scope_totals['expenses'] = currency.round(sum(
                        scoped_amounts[line_id] for line_id in expense_ids
                    ))
                equity_ids = (
                    'section-equity-total',
                    'previous_year_earnings',
                    'current_year_earnings',
                )
                if 'equity' in scope_totals and all(
                        line_id in scoped_amounts
                        for line_id in equity_ids):
                    scope_totals['equity'] = currency.round(
                        scoped_amounts['section-equity-total']
                        - scoped_amounts['previous_year_earnings']
                        - scoped_amounts['current_year_earnings']
                    )

            axis_total_key = (
                'net_profit' if 'net_profit' in scope_totals
                else ('assets' if 'assets' in scope_totals else None)
            )
            if axis_total_key and expression in totals:
                totals[expression] = scope_totals[axis_total_key]

            if expression == current_summary_expression:
                for key, value in scope_totals.items():
                    if (
                        key in totals
                        and total_types.get(key) == _MONETARY_FIGURE_TYPE
                    ):
                        totals[key] = value
        return totals

    @staticmethod
    def _is_projected_expression(expression):
        """Only scale forecast values, never historical/reference cells."""
        expression = expression or ''
        if expression.startswith('prior_'):
            return False
        # Column-axis expressions embed period identity after the measure
        # prefix. Historical slices are references, regardless of analytic
        # group suffixes, and must never be forecast forward.
        if '__period_comparison_' in expression:
            return False
        if expression.endswith('_budget') or expression.endswith(
                '_budget_variance'):
            return False
        return expression not in {
            'budget', 'variance', 'variance_pct', 'budget_variance',
        }

    @api.model
    def _scale_column_scope_totals(
        self, column_scopes, total_types, factor, currency,
    ):
        """Keep axis summary totals aligned with projected visible cells.

        Scope keys carry current/comparison identity. Nested semantic keys
        carry their own figure types, so monetary KPIs grow while ratios,
        days, and other reference measures remain unchanged.
        """
        if not isinstance(column_scopes, dict):
            return copy.deepcopy(column_scopes)
        projected = {}
        for expression, scope_totals in column_scopes.items():
            if (
                not self._is_projected_expression(expression)
                or not isinstance(scope_totals, dict)
            ):
                projected[expression] = copy.deepcopy(scope_totals)
                continue
            projected[expression] = {}
            for semantic_key, value in scope_totals.items():
                if total_types.get(semantic_key) == _MONETARY_FIGURE_TYPE:
                    projected[expression][semantic_key] = (
                        self._scale_total_value(value, factor, currency)
                    )
                else:
                    projected[expression][semantic_key] = copy.deepcopy(
                        value,
                    )
        return projected

    @api.model
    def _recompute_comparison_cells(self, columns, currency):
        """Rebuild derived comparison cells after current value projection."""
        by_expression = {
            col.get('expression_label'): col
            for col in columns
            if isinstance(col, dict) and col.get('expression_label')
        }
        current = by_expression.get('amount')
        if not current or not self._is_numeric(current.get('value')):
            return columns
        current_value = current.get('value')
        budget = by_expression.get('budget')
        budget_variance = by_expression.get('budget_variance')
        if (
            budget is not None
            and budget_variance is not None
            and self._is_numeric(budget.get('value'))
        ):
            budget_variance['value'] = self._round_monetary(
                current_value - budget['value'], currency,
            )
        prior = by_expression.get('prior_amount')
        if not prior or not self._is_numeric(prior.get('value')):
            return columns
        prior_value = prior.get('value')
        variance = by_expression.get('variance')
        if variance is not None:
            variance['value'] = self._round_monetary(
                current_value - prior_value, currency,
            )
        variance_pct = by_expression.get('variance_pct')
        if variance_pct is not None:
            if prior_value:
                value = (current_value - prior_value) / abs(prior_value)
            else:
                value = 1.0 if current_value else 0.0
            variance_pct['value'] = value
        return columns

    @api.model
    def _forecast_currency(self, baseline):
        currency_info = (
            baseline.get('currency') if isinstance(baseline, dict) else {}
        ) or {}
        raw_currency_id = currency_info.get('id')
        try:
            currency_id = int(raw_currency_id or 0)
        except (TypeError, ValueError, OverflowError):
            currency_id = 0
        currency = self.env['res.currency'].browse(currency_id).exists()
        if currency:
            return currency
        forecast = self[:1]
        return (
            forecast.company_id.currency_id
            if forecast else self.env.company.currency_id
        )

    @api.model
    def _round_monetary(self, value, currency):
        return currency.round(value)

    @staticmethod
    def _is_numeric(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    @api.model
    def _line_figure_type(self, line, column_types):
        types = set()
        for col in line.get('columns') or []:
            if not self._is_numeric(col.get('value')):
                continue
            figure_type = (
                col.get('figure_type')
                or column_types.get(col.get('expression_label'), 'string')
            )
            # Variance columns may add percentage alongside monetary cells.
            # First monetary proof is enough to type monetary row totals.
            if figure_type == _MONETARY_FIGURE_TYPE:
                return figure_type
            types.add(figure_type)
        return next(iter(types)) if len(types) == 1 else None

    @api.model
    def _total_figure_types(self, baseline, column_types):
        """Infer total semantics from explicit payload cell contracts.

        No numeric value is treated as money by shape alone. Handlers may
        provide ``meta.total_figure_types`` directly; otherwise conventional
        total keys are tied to a typed column, line id, section id, metric, or
        builder line code. Unknown totals stay unchanged.
        """
        total_types = dict(column_types)
        total_types.update(
            (baseline.get('meta') or {}).get('total_figure_types') or {},
        )
        explicit_total_keys = self._forecast_explicit_total_keys()
        for line in baseline.get('lines') or []:
            if not isinstance(line, dict):
                continue
            figure_type = self._line_figure_type(line, column_types)
            if not figure_type:
                continue
            line_id = line.get('id')
            if line_id:
                total_types.setdefault(line_id, figure_type)
            meta = line.get('meta') or {}
            for semantic_key in (
                meta.get('metric'),
                meta.get('builder_line_code'),
            ):
                if semantic_key:
                    total_types.setdefault(semantic_key, figure_type)
            if meta.get('kind') == 'section_total' and meta.get('section_id'):
                total_types.setdefault(meta['section_id'], figure_type)
            for semantic_key in explicit_total_keys.get(line_id, ()):
                total_types.setdefault(semantic_key, figure_type)

        # Comparison totals commonly prefix the same base semantic with
        # ``prior_``. Only inherit an already-proven type.
        for key in (baseline.get('totals') or {}):
            if key in total_types:
                continue
            for prefix in ('prior_', 'current_'):
                if key.startswith(prefix) and key[len(prefix):] in total_types:
                    total_types[key] = total_types[key[len(prefix):]]
                    break
        return total_types

    @api.model
    def _scale_total_value(self, value, factor, currency):
        if self._is_numeric(value):
            return self._round_monetary(value * factor, currency)
        if isinstance(value, dict):
            return {
                key: self._scale_total_value(item, factor, currency)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                self._scale_total_value(item, factor, currency)
                for item in value
            ]
        return copy.deepcopy(value)


class EhReportForecastResult(models.TransientModel):
    _name = 'eh.report.forecast.result'
    _description = "Temporary forecast projection result"
    _transient_max_hours = 1.0

    forecast_id = fields.Many2one(
        'eh.report.forecast', required=True, readonly=True, ondelete='cascade',
    )
    summary = fields.Text(readonly=True)
    filename = fields.Char(required=True, readonly=True)
    file_data = fields.Binary(
        string="Projection JSON",
        required=True,
        readonly=True,
        attachment=False,
    )
