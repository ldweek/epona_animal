# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.account.dynamic.report.handler.builder: generic handler that interprets
an eh.report.builder record at compute time.

The orchestrator passes the report code via context (eh_report_code).
This handler resolves the matching builder, walks its line_ids in
sequence, and produces a payload in the standard shape that the OWL
viewer, XLSX writer, and PDF Qweb template all already understand.

Line dispatch:

* section_header  -> section header line at level 0.
* account_aggregate -> SQL aggregation through MoveLineQuery using the
  line's account_codes or account_types and sign.
* formula -> safe_eval_formula() against accumulated line values.

Each line that has a code field set publishes its computed value into a
shared dict so subsequent formula lines can reference it.
"""

import ast

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL

from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery

from .report_builder import (
    _LEGACY_NUMERIC_NODES,
    _validated_formula_tree,
    safe_eval_formula,
)


class EhBuilderHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.builder'
    _inherit = 'eh.account.dynamic.report.handler.sectioned'
    _description = "Generic handler for builder defined custom reports"

    REPORT_CODE = ''
    REPORT_NAME = "Custom Report"

    @api.model
    def compute(self, options):
        builder = self._resolve_builder()
        date_from = self._extract_date(options, 'date_from')
        date_to = self._extract_date(options, 'date_to')
        company_ids = options.get('company_ids') or [self.env.company.id]
        posted_only = bool(options.get('posted_only', True))
        show_zero = bool(options.get('show_zero', False))
        primary_company_id = (
            options.get('primary_company_id') or company_ids[0]
        )
        companies = self.env['res.company'].browse(
            [int(company_id) for company_id in company_ids],
        ).exists()
        if len(companies) > 1:
            source_currency_ids = set(companies.currency_id.ids)
            try:
                target_currency_id = int(
                    options.get('presentation_currency_id') or 0,
                )
            except (TypeError, ValueError, OverflowError) as exc:
                raise UserError(_(
                    "The presentation currency is invalid."
                )) from exc
            if (
                len(source_currency_ids) != 1
                or target_currency_id
                and target_currency_id not in source_currency_ids
            ):
                raise UserError(_(
                    "Custom builder reports cannot aggregate multiple "
                    "companies with different ledger currencies or apply "
                    "one presentation-currency rate across companies. "
                    "Select one company."
                ))
        currency = self.env['res.company'].browse(
            int(primary_company_id),
        ).currency_id

        line_values = {}
        rendered_lines = []
        total_figure_types = {}
        active_section_id = None
        aggregate_lines = builder.line_ids.filtered(
            lambda line: line.line_type == 'account_aggregate'
        )
        aggregate_values = self._evaluate_account_aggregates(
            aggregate_lines,
            options=options,
            date_from=date_from,
            date_to=date_to,
            company_ids=company_ids,
            posted_only=posted_only,
        )

        for builder_line in builder.line_ids.sorted(lambda l: l.sequence):
            if builder_line.line_type == 'section_header':
                active_section_id = self._builder_section_id(builder_line)
                rendered_lines.append(self._render_section_header(builder_line))
                continue

            if builder_line.line_type == 'account_aggregate':
                value = currency.round(
                    aggregate_values.get(builder_line.id, 0.0),
                )
                if builder_line.code:
                    line_values[builder_line.code] = value
                    total_figure_types[builder_line.code] = 'monetary'
                if not show_zero and currency.is_zero(value):
                    continue
                rendered_lines.append(
                    self._render_account_aggregate(builder_line, value),
                )
                continue

            if builder_line.line_type == 'formula':
                value = safe_eval_formula(builder_line.formula, line_values)
                figure_type = builder_line.figure_type or 'monetary'
                if figure_type == 'monetary':
                    value = currency.round(value)
                if builder_line.code:
                    line_values[builder_line.code] = value
                    total_figure_types[builder_line.code] = figure_type
                rendered_lines.append(
                    self._render_formula(
                        builder_line, value,
                        section_id=active_section_id,
                        additive_terms=(
                            self._forecast_additive_terms(
                                builder_line.formula,
                            )
                            if figure_type == 'monetary' else None
                        ),
                    ),
                )
                if builder_line.is_section_total:
                    active_section_id = None
                continue

        return {
            'columns': [
                {
                    'expression_label': 'description',
                    'name': builder.label_column_name or "Description",
                    'figure_type': 'string',
                },
                {
                    'expression_label': 'amount',
                    'name': builder.amount_column_name or "Amount",
                    'figure_type': 'monetary',
                },
            ],
            'lines': rendered_lines,
            'totals': self._compute_totals(line_values),
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': {
                'report_code': builder.code,
                'builder_id': builder.id,
                'date_from': self._iso_date(date_from),
                'date_to': self._iso_date(date_to),
                'company_ids': sorted(int(c) for c in company_ids),
                'posted_only': posted_only,
                'show_zero': show_zero,
                'total_figure_types': total_figure_types,
            },
        }

    # ---- internals ----

    @api.model
    def _resolve_builder(self):
        code = self.env.context.get('eh_report_code')
        if not code:
            raise UserError(_(
                "Builder handler invoked without a report code in context. "
                "Render through the eh.account.dynamic.report orchestrator."
            ))
        builder = self.env['eh.report.builder'].search(
            [('code', '=', code)], limit=1,
        )
        if not builder:
            raise UserError(_(
                "No builder record matches code %r. Was the builder "
                "deleted while the published report still references it?",
            ) % code)
        return builder

    def _evaluate_account_aggregate(self, builder_line, options,
                                    date_from, date_to,
                                    company_ids, posted_only):
        return self._evaluate_account_aggregates(
            builder_line,
            options=options,
            date_from=date_from,
            date_to=date_to,
            company_ids=company_ids,
            posted_only=posted_only,
        ).get(builder_line.id, 0.0)

    def _evaluate_account_aggregates(
        self, builder_lines, options, date_from, date_to,
        company_ids, posted_only,
    ):
        """Evaluate every aggregate line from one account-grouped query.

        Builder line count is author-controlled and unbounded. Issuing one
        SUM query per line makes render/export/schedule latency O(lines).
        Fetch one balance per account, then evaluate authored code/type scopes
        against that bounded account set in Python. Identical scopes reuse a
        memoized subtotal.
        """
        if not builder_lines:
            return {}
        query = MoveLineQuery(self.env, company_ids=company_ids)
        # One bounded account query serves both authored bases. Closing
        # balance needs all history through Date To; period movement uses a
        # conditional aggregate from Date From. This preserves one round trip
        # regardless of line count without misusing a movement as a balance.
        query.where_date_range(date_to=date_to)
        if posted_only:
            query.where_posted_only()
        self.apply_common_filters(query, options)
        # Keep one round trip without widening a narrow custom report into a
        # full-ledger GROUP BY. Push union of every authored code/type scope
        # into SQL, then apply individual line scopes to returned account
        # aggregates in Python.
        code_prefixes = sorted({
            self._escape_like_prefix(prefix)
            for line in builder_lines
            if line.account_scope == 'codes'
            for prefix in self._split_csv(line.account_codes)
        })
        account_types = sorted({
            account_type
            for line in builder_lines
            if line.account_scope == 'types'
            for account_type in self._split_csv(line.account_types)
        })
        scope_clauses = [
            query._account_code_like_sql('%s%%' % prefix)
            for prefix in code_prefixes
        ]
        if account_types:
            query.join_account()
            scope_clauses.append(SQL(
                "acc.account_type IN %s", tuple(account_types),
            ))
        # Published rows created by current code are selector-validated, but
        # an upgraded or directly-corrupted legacy row can still carry an
        # empty selector. Never turn that malformed definition into an
        # unbounded company/date GROUP BY; its fail-closed value is zero.
        if not scope_clauses:
            return {line.id: 0.0 for line in builder_lines}
        query.where_raw(SQL("(%s)", SQL(" OR ").join(scope_clauses)))
        query.select_field('account_id')
        query.select_account_field('code', alias='account_code')
        query.select_account_field('account_type', alias='account_type')
        query.select(SQL(
            "SUM(CASE WHEN aml.date >= %s THEN aml.balance ELSE 0 END)",
            date_from,
        ), 'period_balance')
        query.select(SQL("SUM(aml.balance)"), 'closing_balance')
        query.group_by(
            SQL("aml.account_id"),
            query._account_code_sql(),
            SQL("acc.account_type"),
        )
        rows = query.execute()

        scope_values = {}
        values = {}
        for builder_line in builder_lines:
            balance_key = (
                'closing_balance'
                if builder_line.aggregate_basis == 'closing'
                else 'period_balance'
            )
            if builder_line.account_scope == 'codes':
                prefixes = tuple(self._split_csv(
                    builder_line.account_codes,
                ))
                scope_key = (balance_key, 'codes', prefixes)
                if scope_key not in scope_values:
                    scope_values[scope_key] = sum(
                        float(row.get(balance_key) or 0.0)
                        for row in rows
                        if str(row.get('account_code') or '').startswith(
                            prefixes,
                        )
                    ) if prefixes else 0.0
            else:
                account_types = tuple(sorted(set(self._split_csv(
                    builder_line.account_types,
                ))))
                scope_key = (balance_key, 'types', account_types)
                if scope_key not in scope_values:
                    scope_values[scope_key] = sum(
                        float(row.get(balance_key) or 0.0)
                        for row in rows
                        if row.get('account_type') in account_types
                    ) if account_types else 0.0
            sign = 1.0 if builder_line.sign == '+' else -1.0
            values[builder_line.id] = scope_values[scope_key] * sign
        return values

    def _build_account_aggregate_query(
        self, builder_line, options, date_from, date_to,
        company_ids, posted_only,
    ):
        """Build one authoritative query for render and drilldown."""
        query = MoveLineQuery(self.env, company_ids=company_ids)
        query.where_date_range(
            date_from=(
                None
                if builder_line.aggregate_basis == 'closing'
                else date_from
            ),
            date_to=date_to,
        )
        if posted_only:
            query.where_posted_only()
        if builder_line.account_scope == 'codes':
            codes = self._split_csv(builder_line.account_codes)
            if not codes:
                return None
            query.where_account_codes([
                self._escape_like_prefix(code) for code in codes
            ])
        else:
            types = self._split_csv(builder_line.account_types)
            if not types:
                return None
            query.where_account_types(tuple(types))
        self.apply_common_filters(query, options)
        return query

    @api.model
    def get_drilldown_action(self, options, line_id):
        if not isinstance(line_id, str) or not line_id.startswith('line-'):
            return None
        try:
            line_number = int(line_id.split('-', 1)[1])
            date_from = self._extract_date(options, 'date_from')
            date_to = self._extract_date(options, 'date_to')
        except (TypeError, ValueError, UserError):
            return None
        builder = self._resolve_builder()
        builder_line = builder.line_ids.filtered(
            lambda item: item.id == line_number
            and item.line_type == 'account_aggregate'
        )
        if len(builder_line) != 1:
            return None
        company_ids = options.get('company_ids') or [self.env.company.id]
        query = self._build_account_aggregate_query(
            builder_line,
            options,
            date_from,
            date_to,
            company_ids,
            bool(options.get('posted_only', True)),
        )
        if query is None:
            return None
        query.select_field('id', alias='aml_id')
        query.order_by('id', 'ASC')
        query.limit(20001)
        rows = query.execute()
        if len(rows) > 20000:
            raise UserError(_(
                "This custom-report cell contains more than 20,000 journal "
                "items. Narrow date or filters before opening details."
            ))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Items"),
            'res_model': 'account.move.line',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('id', 'in', [row['aml_id'] for row in rows])],
            'context': {'search_default_group_move': 1},
        }

    @staticmethod
    def _split_csv(value):
        return [v.strip() for v in (value or '').split(',') if v.strip()]

    @staticmethod
    def _escape_like_prefix(value):
        """Keep authored account-code prefixes literal, never LIKE syntax."""
        return str(value).replace('\\', '\\\\').replace(
            '%', '\\%',
        ).replace('_', '\\_')

    @staticmethod
    def _builder_section_id(builder_line):
        return builder_line.code or ("line-%s" % builder_line.id)

    @staticmethod
    def _forecast_additive_terms(formula):
        """Expose only safe named +/- dependencies, never authored formula.

        Forecast rounding may reconcile a monetary formula only when it is a
        linear sum of earlier named lines with coefficients exactly +1/-1.
        Constants other than zero, multiplication, division, or repeated
        names producing larger coefficients fail closed with no metadata.
        """
        tree = _validated_formula_tree(formula)
        if tree is None:
            return None
        coefficients = {}

        def _collect(node, coefficient=1):
            if isinstance(node, ast.Expression):
                return _collect(node.body, coefficient)
            if isinstance(node, ast.Name):
                coefficients.setdefault(node.id, 0)
                coefficients[node.id] += coefficient
                return True
            if isinstance(node, ast.Constant):
                return float(node.value) == 0.0
            if (
                _LEGACY_NUMERIC_NODES
                and isinstance(node, _LEGACY_NUMERIC_NODES)
            ):
                return float(node.n) == 0.0
            if isinstance(node, ast.UnaryOp):
                if isinstance(node.op, ast.UAdd):
                    return _collect(node.operand, coefficient)
                if isinstance(node.op, ast.USub):
                    return _collect(node.operand, -coefficient)
                return False
            if isinstance(node, ast.BinOp):
                if isinstance(node.op, ast.Add):
                    return (
                        _collect(node.left, coefficient)
                        and _collect(node.right, coefficient)
                    )
                if isinstance(node.op, ast.Sub):
                    return (
                        _collect(node.left, coefficient)
                        and _collect(node.right, -coefficient)
                    )
                return False
            return False

        if not _collect(tree):
            return None
        terms = [
            {'code': code, 'coefficient': coefficient}
            for code, coefficient in coefficients.items()
            if coefficient
        ]
        if not terms or any(
            term['coefficient'] not in (-1, 1) for term in terms
        ):
            return None
        return terms

    def _render_section_header(self, builder_line):
        section_id = self._builder_section_id(builder_line)
        return {
            'id': "section-%s-header" % section_id,
            'name': builder_line.name,
            'level': 0,
            'columns': [
                {'expression_label': 'amount', 'value': ''},
            ],
            'unfoldable': False,
            'meta': {
                'kind': 'section_header',
                'section_id': section_id,
                'builder_line_id': builder_line.id,
            },
        }

    def _render_account_aggregate(self, builder_line, value):
        return {
            'id': "line-%s" % builder_line.id,
            'name': builder_line.name,
            'level': max(int(builder_line.level or 1), 1),
            'columns': [
                {'expression_label': 'amount', 'value': value},
            ],
            'unfoldable': False,
            'meta': {
                'kind': 'account_aggregate',
                'builder_line_id': builder_line.id,
                'builder_line_code': builder_line.code or '',
            },
        }

    def _render_formula(
        self, builder_line, value, section_id=None, additive_terms=None,
    ):
        kind = (
            'section_total'
            if builder_line.is_section_total
            else 'computed'
        )
        meta = {
            'kind': kind,
            'builder_line_id': builder_line.id,
            'builder_line_code': builder_line.code or '',
        }
        if kind == 'section_total' and section_id:
            # Explicit additive boundary for deterministic forecast rounding.
            meta['section_id'] = section_id
        if additive_terms:
            meta['forecast_additive_terms'] = additive_terms
        return {
            'id': "line-%s" % builder_line.id,
            'name': builder_line.name,
            'level': int(builder_line.level or 0),
            'columns': [
                {
                    'expression_label': 'amount',
                    'value': value,
                    'figure_type': builder_line.figure_type or 'monetary',
                },
            ],
            'unfoldable': False,
            'meta': meta,
        }

    @staticmethod
    def _compute_totals(line_values):
        return {
            code: value
            for code, value in line_values.items()
            if isinstance(value, (int, float))
        }
