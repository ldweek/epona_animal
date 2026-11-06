# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Trial Balance handler.

Single SQL pass. Conditional aggregation splits the same set of journal
lines into three buckets per account in one scan:

* opening_balance: lines whose date is strictly before date_from.
* period_debit and period_credit: lines whose date falls within
  [date_from, date_to].

Closing balance is then derived in Python as
opening_balance + period_debit - period_credit. The split into debit and
credit display columns happens at presentation time based on the sign of
the underlying balance, which matches accounting convention.

The handler honours all standard filters (companies, journals, partners,
accounts, posted_only, show_zero) by composing them through MoveLineQuery,
so SQL safety, multi company scoping, and cancelled exclusion are inherited
automatically.
"""

import math
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.osv import expression
from odoo.tools import SQL
from odoo.tools.translate import LazyTranslate

from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery

_lt = LazyTranslate(__name__)


class EhTrialBalanceHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.trial_balance'
    _inherit = 'eh.account.dynamic.report.handler'
    _description = "Trial Balance report handler"

    REPORT_CODE = 'trial_balance'
    REPORT_NAME = _lt("Trial Balance")
    _EH_COLUMN_AXIS_CAPABILITIES = frozenset({
        'comparison', 'analytic_columns',
    })
    _EH_ANALYTIC_COLUMN_DRILLDOWN = True
    _MAX_ANALYTIC_DRILLDOWN_ROWS = 20_000

    @api.model
    def compute(self, options):
        date_from = self._extract_date(options, 'date_from')
        date_to = self._extract_date(options, 'date_to')
        company_ids = options.get('company_ids') or [self.env.company.id]
        posted_only = bool(options.get('posted_only', True))
        show_zero = bool(options.get('show_zero', False))
        hierarchical = bool(options.get('hierarchical_groups', True))
        comparison = options.get('comparison') or 'none'
        unfolded_ids = set(options.get('unfolded_lines') or [])

        # The six Trial Balance measures describe one fiscal-year block. A
        # plain period that crosses a fiscal year end is cut into one block
        # per fiscal year; any other period is validated before choosing the
        # legacy or axis execution path so a report cannot silently carry
        # completed P&L years into its closing balance.
        fiscal_periods = self._tb_fiscal_year_periods(
            options, date_from, date_to, company_ids,
        )
        if not fiscal_periods:
            self._validate_tb_period_fiscal_boundaries([{
                'date_from': self._iso_date(date_from),
                'date_to': self._iso_date(date_to),
            }], company_ids)

        # Multi-currency consolidation table (WS4). Monocurrency /
        # single-company yields a no-op table, so the figures are unchanged.
        currency_table = self._resolve_currency_table(options, company_ids)
        presentation_converted = bool(
            currency_table is not None
            and not currency_table.is_monocurrency
        )
        currency = self._eh_monetary_currency(
            options=options,
            company_ids=company_ids,
            presentation_converted=presentation_converted,
        )

        # Every comparison uses complete opening/movement/closing blocks.
        # Keeping the common N=1 descending request on the legacy
        # prior-closing/variance-only surface would understate Enterprise's
        # Trial Balance comparison contract and make period activity opaque.
        if (
            fiscal_periods
            or comparison != 'none'
            or self._eh_column_axis_requested(options, allow_analytic=True)
        ):
            return self._compute_period_axis(
                options=options,
                date_from=date_from,
                date_to=date_to,
                company_ids=company_ids,
                posted_only=posted_only,
                show_zero=show_zero,
                hierarchical=hierarchical,
                unfolded_ids=unfolded_ids,
                currency=currency,
                currency_table=currency_table,
                presentation_converted=presentation_converted,
            )

        rows = self._fetch_account_buckets(
            company_ids=company_ids,
            date_from=date_from,
            date_to=date_to,
            posted_only=posted_only,
            options=options,
            currency_table=currency_table,
        )

        # Roll prior-year P&L onto each company's existing
        # ``equity_unaffected`` account, matching the General Ledger's real,
        # drillable identity. Only a genuinely unconfigured company falls
        # back to the synthetic disclosure row.
        unaffected_by_company = self._fetch_unaffected_earnings_by_company(
            company_ids=company_ids, date_from=date_from,
            posted_only=posted_only, options=options,
            currency_table=currency_table,
        )
        rows, unaffected = self._merge_unaffected_into_rows(
            rows, unaffected_by_company, company_ids, options,
        )
        rows = self._tb_finalize_account_rows(rows, company_ids, options)

        if hierarchical:
            lines, totals = self._build_hierarchical_lines_and_totals(
                rows, show_zero, unfolded_ids, options=options,
                unaffected=unaffected,
                presentation_converted=presentation_converted,
                currency=currency,
            )
        else:
            lines, totals = self._build_lines_and_totals(
                rows, show_zero, options=options, unaffected=unaffected,
                presentation_converted=presentation_converted,
                currency=currency,
            )

        meta = {
            'report_code': self.REPORT_CODE,
            'date_from': self._iso_date(date_from),
            'date_to': self._iso_date(date_to),
            'company_ids': sorted(int(c) for c in company_ids),
            'posted_only': posted_only,
            'show_zero': show_zero,
            'hierarchical_groups': hierarchical,
            'comparison': comparison,
            **self._currency_meta(currency_table),
        }
        columns = self._build_columns()
        if comparison != 'none':
            sectioned = self.env[
                'eh.account.dynamic.report.handler.sectioned']
            prior_from, prior_to, label = (
                sectioned._resolve_comparison_dates(
                    comparison, date_from, date_to))
            if prior_from and prior_to:
                prior_currency_table = self._resolve_currency_table(
                    options, company_ids, as_of_date=prior_to)
                prior_rows = self._fetch_account_buckets(
                    company_ids=company_ids,
                    date_from=prior_from,
                    date_to=prior_to,
                    posted_only=posted_only,
                    options=options,
                    currency_table=prior_currency_table,
                )
                prior_unaffected_by_company = (
                    self._fetch_unaffected_earnings_by_company(
                        company_ids=company_ids,
                        date_from=prior_from,
                        posted_only=posted_only,
                        options=options,
                        currency_table=prior_currency_table,
                    )
                )
                prior_rows, prior_unaffected = (
                    self._merge_unaffected_into_rows(
                        prior_rows, prior_unaffected_by_company,
                        company_ids, options,
                    )
                )
                prior_rows = self._tb_finalize_account_rows(
                    prior_rows, company_ids, options)
                if hierarchical:
                    prior_lines, prior_totals = (
                        self._build_hierarchical_lines_and_totals(
                            prior_rows, show_zero, unfolded_ids,
                            options=options,
                            unaffected=prior_unaffected,
                            presentation_converted=presentation_converted,
                            currency=currency,
                        )
                    )
                else:
                    prior_lines, prior_totals = self._build_lines_and_totals(
                        prior_rows, show_zero, options=options,
                        unaffected=prior_unaffected,
                        presentation_converted=presentation_converted,
                        currency=currency,
                    )
                lines = self._merge_tb_comparison_lines(
                    lines, prior_lines, currency)
                totals = self._merge_tb_comparison_totals(
                    totals, prior_totals, currency)
                columns = self._build_columns(comparison=True)
                meta.update({
                    'prior_date_from': self._iso_date(prior_from),
                    'prior_date_to': self._iso_date(prior_to),
                    'comparison_label': label,
                })

        return {
            'columns': columns,
            'lines': lines,
            'totals': totals,
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': meta,
        }

    @api.model
    def _compute_period_axis(
        self, options, date_from, date_to, company_ids, posted_only,
        show_zero, hierarchical, unfolded_ids, currency, currency_table,
        presentation_converted,
    ):
        """Repeat six measures for every period x analytic value scope."""
        periods = self._tb_period_scopes(
            options, date_from, date_to, company_ids,
        )
        analytics = self._eh_resolve_analytic_column_scopes(
            options, company_ids,
        )
        value_scopes = self._eh_build_value_scopes(
            periods, analytics, include_total=bool(analytics),
        )
        value_column_count = len(value_scopes) * len(self._tb_measures())
        fiscal_blocks = sum(
            1 for period in periods
            if period['key'].startswith('period_fiscal_')
        )
        if fiscal_blocks and value_column_count > self._MAX_VALUE_COLUMNS:
            raise UserError(_(
                "Trial Balance selection produces %(count)s value columns "
                "because the period is cut into %(blocks)s fiscal year "
                "blocks; maximum is %(maximum)s. Choose fewer analytic "
                "columns or a shorter period.",
                count=value_column_count,
                blocks=fiscal_blocks + 1,
                maximum=self._MAX_VALUE_COLUMNS,
            ))
        if value_column_count > self._MAX_VALUE_COLUMNS:
            raise UserError(_(
                "Trial Balance selection produces %(count)s value columns; "
                "maximum is %(maximum)s.",
                count=value_column_count,
                maximum=self._MAX_VALUE_COLUMNS,
            ))
        scoped_inputs = []
        for value_scope in value_scopes:
            scoped_options = self._eh_scope_options(options, value_scope)
            scoped_options['lazy_expand'] = False
            scope_from = self._extract_date(scoped_options, 'date_from')
            scope_to = self._extract_date(scoped_options, 'date_to')
            scope_currency_table = self._resolve_currency_table(
                scoped_options, company_ids, as_of_date=scope_to,
            )
            rows = self._fetch_account_buckets(
                company_ids=company_ids,
                date_from=scope_from,
                date_to=scope_to,
                posted_only=posted_only,
                options=scoped_options,
                currency_table=scope_currency_table,
            )
            unaffected_by_company = (
                self._fetch_unaffected_earnings_by_company(
                    company_ids=company_ids,
                    date_from=scope_from,
                    posted_only=posted_only,
                    options=scoped_options,
                    currency_table=scope_currency_table,
                )
            )
            rows, unaffected = self._merge_unaffected_into_rows(
                rows, unaffected_by_company, company_ids, scoped_options,
            )
            rows = self._tb_finalize_account_rows(
                rows, company_ids, scoped_options)
            scoped_inputs.append({
                'scope': value_scope,
                'options': scoped_options,
                'currency_table': scope_currency_table,
                'rows': rows,
                'unaffected': unaffected,
            })

        # Every period and analytic scope lists the same accounts, so each
        # builds the same hierarchy (paths, parents and order). When zero
        # balances are hidden, an account zero in every scope is dropped
        # first so it cannot shape the hierarchy; the rest are padded with
        # zero rows and every scope is built with its zero rows kept.
        if not show_zero:
            active_ids = {
                row['account_id']
                for scoped_input in scoped_inputs
                for row in scoped_input['rows']
                if not self._tb_row_is_zero(row, currency)
            }
            for scoped_input in scoped_inputs:
                scoped_input['rows'] = [
                    row for row in scoped_input['rows']
                    if row['account_id'] in active_ids
                ]
        union_rows = {}
        for scoped_input in scoped_inputs:
            for row in scoped_input['rows']:
                union_rows.setdefault(row['account_id'], row)
        for scoped_input in scoped_inputs:
            present = {row['account_id'] for row in scoped_input['rows']}
            scoped_input['rows'] = sorted(scoped_input['rows'] + [
                dict(
                    row, opening_balance=0.0,
                    period_debit=0.0, period_credit=0.0,
                )
                for account_id, row in union_rows.items()
                if account_id not in present
            ], key=self._tb_row_sort_key)

        scoped_results = []
        translation_periods = {}
        current_totals = None
        for scoped_input in scoped_inputs:
            value_scope = scoped_input['scope']
            scoped_options = scoped_input['options']
            scope_currency_table = scoped_input['currency_table']
            rows = scoped_input['rows']
            unaffected = scoped_input['unaffected']
            if hierarchical:
                scope_lines, scope_totals = (
                    self._build_hierarchical_lines_and_totals(
                        rows, True, unfolded_ids,
                        options=scoped_options,
                        unaffected=unaffected,
                        presentation_converted=presentation_converted,
                        currency=currency,
                    )
                )
            else:
                scope_lines, scope_totals = self._build_lines_and_totals(
                    rows, True,
                    options=scoped_options,
                    unaffected=unaffected,
                    presentation_converted=presentation_converted,
                    currency=currency,
                )
            scoped_results.append({
                'scope': value_scope,
                'lines': scope_lines,
                'totals': scope_totals,
            })
            if (
                value_scope.get('period_key') == 'period_current'
                and (not analytics or value_scope.get('is_total'))
            ):
                current_totals = scope_totals
            period_key = value_scope.get('period_key')
            period_meta = scope_currency_table and (
                scope_currency_table.period_metadata(period_key)
            )
            if period_meta and period_key not in translation_periods:
                translation_periods[period_key] = period_meta

        lines = self._merge_tb_period_results(scoped_results)
        current_totals = current_totals or {
            measure: 0.0 for measure, _label in self._tb_measures()
        }
        totals = dict(current_totals)
        totals['column_scopes'] = {
            result['scope']['key']: result['totals']
            for result in scoped_results
        }
        for result in scoped_results:
            scope_key = result['scope']['key']
            for measure, _label in self._tb_measures():
                totals['%s__%s' % (scope_key, measure)] = (
                    result['totals'].get(measure, 0.0)
                )
        meta = {
            'report_code': self.REPORT_CODE,
            'date_from': self._iso_date(date_from),
            'date_to': self._iso_date(date_to),
            'company_ids': sorted(int(c) for c in company_ids),
            'posted_only': posted_only,
            'show_zero': show_zero,
            'hierarchical_groups': hierarchical,
            'comparison': options.get('comparison') or 'none',
            'comparison_number': len([
                period for period in periods
                if period.get('role') == 'comparison'
            ]),
            'comparison_order': options.get('comparison_order', 'descending'),
            'comparison_periods': [{
                'from': period['date_from'],
                'to': period['date_to'],
            } for period in periods if period.get('role') == 'comparison'],
            'analytic_column_groups': len(analytics),
            'column_axis': True,
            **self._currency_meta(currency_table),
        }
        if translation_periods:
            meta['currency_translation_periods'] = [
                translation_periods[period['key']]
                for period in periods
                if period['key'] in translation_periods
            ]
        return {
            'columns': self._build_tb_period_axis_columns(value_scopes),
            'column_header_rows': self._build_tb_period_header_rows(
                value_scopes, analytic_axis=bool(analytics),
            ),
            'lines': lines,
            'totals': totals,
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': meta,
        }

    @api.model
    def _tb_period_scopes(self, options, date_from, date_to, company_ids):
        """Return the validated value periods of one Trial Balance request.

        Shared by compute and the analytic cell drilldown so both resolve
        the same period keys for the same options.
        """
        periods = self._tb_fiscal_year_periods(
            options, date_from, date_to, company_ids,
        ) or self._eh_resolve_period_scopes(
            options, date_from, date_to,
            snapshot=False, max_periods=self._MAX_COMPARISON_PERIODS,
        )
        self._validate_tb_period_fiscal_boundaries(periods, company_ids)
        return periods

    @api.model
    def _tb_fiscal_year_periods(self, options, date_from, date_to, company_ids):
        """Cut a plain period that crosses a fiscal year end into blocks.

        Returns an empty list when there is a comparison, or when the period
        sits inside one fiscal year of every selected company. Otherwise the
        period is cut at every fiscal year start inside it, for any selected
        company, so each block lies inside one fiscal year of every company
        and keeps its own opening, movement and closing. The last block is
        the current period.
        """
        date_from = fields.Date.to_date(date_from)
        date_to = fields.Date.to_date(date_to)
        if (
            (options.get('comparison') or 'none') != 'none'
            or not date_from or not date_to or date_from > date_to
        ):
            return []
        company_ids = self._eh_normalize_id_set(company_ids, 'company_ids')
        companies = self.env['res.company'].browse(company_ids).exists()
        cuts = set()
        # Not named cursor: _() reads a local of that name as the database
        # cursor when it resolves the translation language.
        for company in companies.sudo():
            year_day = date_from
            while True:
                # Compare before stepping: a fiscal year ending on the last
                # representable date has no following day.
                fiscal_end = company.compute_fiscalyear_dates(
                    year_day)['date_to']
                if fiscal_end >= date_to or fiscal_end < year_day:
                    break
                year_day = fiscal_end + timedelta(days=1)
                cuts.add(year_day)
        if not cuts:
            return []
        starts = [date_from, *sorted(cuts)]
        ends = [start - timedelta(days=1) for start in starts[1:]] + [date_to]
        maximum = self._MAX_VALUE_COLUMNS // len(self._tb_measures())
        if len(starts) > maximum:
            raise UserError(_(
                "Trial Balance period %(date_from)s to %(date_to)s needs "
                "%(count)s fiscal year blocks across the selected companies' "
                "fiscal calendars. At most %(maximum)s fit in one report; "
                "shorten the period or select fewer companies.",
                date_from=self._iso_date(date_from),
                date_to=self._iso_date(date_to),
                count=len(starts),
                maximum=maximum,
            ))
        last = len(starts) - 1
        return [{
            'key': (
                'period_current' if index == last
                else 'period_fiscal_%d' % (index + 1)
            ),
            'label': _(
                "%(date_from)s to %(date_to)s",
                date_from=self._iso_date(block_from),
                date_to=self._iso_date(block_to),
            ),
            'role': 'current',
            'date_from': self._iso_date(block_from),
            'date_to': self._iso_date(block_to),
            'company_ids': company_ids,
            'comparison_index': 0,
            'is_current': index == last,
            'snapshot': False,
        } for index, (block_from, block_to) in enumerate(zip(starts, ends))]

    @api.model
    def _validate_tb_period_fiscal_boundaries(self, periods, company_ids):
        """Fail closed when one six-measure block crosses a fiscal year.

        Income/expense closing balances reset at every fiscal boundary.
        Publishing one block across multiple fiscal years would retain a
        completed year's P&L in closing instead of rolling it to unaffected
        earnings. A plain period is cut into fiscal year blocks before it
        reaches this check (_tb_fiscal_year_periods); a comparison period
        that crosses a fiscal year is still rejected.
        """
        companies = self.env['res.company'].browse(company_ids).exists()
        if set(companies.ids) != {int(value) for value in company_ids}:
            raise UserError(_("One or more selected companies no longer exist."))
        for period in periods:
            period_from = fields.Date.to_date(period['date_from'])
            period_to = fields.Date.to_date(period['date_to'])
            for company in companies:
                from_fiscal = company.compute_fiscalyear_dates(period_from)
                to_fiscal = company.compute_fiscalyear_dates(period_to)
                if from_fiscal['date_from'] != to_fiscal['date_from']:
                    raise UserError(_(
                        "Trial Balance period %(date_from)s to %(date_to)s "
                        "crosses a fiscal-year boundary for %(company)s. "
                        "Choose one fiscal year per comparison period.",
                        date_from=period['date_from'],
                        date_to=period['date_to'],
                        company=company.display_name,
                    ))

    @api.model
    def _tb_measures(self):
        return [
            ('opening_debit', _("Opening DB")),
            ('opening_credit', _("Opening CR")),
            ('period_debit', _("Movement DB")),
            ('period_credit', _("Movement CR")),
            ('closing_debit', _("Closing DB")),
            ('closing_credit', _("Closing CR")),
        ]

    @api.model
    def _build_tb_period_axis_columns(self, value_scopes):
        columns = [{
            'expression_label': 'account',
            'name': _("Account"),
            'figure_type': 'string',
        }]
        for value_scope in value_scopes:
            for measure, measure_label in self._tb_measures():
                columns.append({
                    'expression_label': '%s__%s' % (
                        value_scope['key'], measure,
                    ),
                    'name': _("%(period)s - %(measure)s", **{
                        'period': value_scope['label'],
                        'measure': measure_label,
                    }),
                    'figure_type': 'monetary',
                    'scope': value_scope['scope'],
                })
        return columns

    @api.model
    def _build_tb_period_header_rows(
        self, value_scopes, analytic_axis=False,
    ):
        if analytic_axis:
            period_groups = []
            for value_scope in value_scopes:
                period_key = value_scope['period_key']
                if not period_groups or period_groups[-1][0] != period_key:
                    period_groups.append([
                        period_key, value_scope['period_label'], 0,
                    ])
                period_groups[-1][2] += len(self._tb_measures())
            return [
                [
                    {'name': _("Account"), 'colspan': 1, 'rowspan': 3},
                    *[
                        {
                            'name': period_label,
                            'colspan': colspan,
                            'rowspan': 1,
                        }
                        for _period_key, period_label, colspan
                        in period_groups
                    ],
                ],
                [
                    {
                        'name': value_scope['analytic_label'],
                        'colspan': len(self._tb_measures()),
                        'rowspan': 1,
                    }
                    for value_scope in value_scopes
                ],
                [
                    {
                        'name': measure_label,
                        'colspan': 1,
                        'rowspan': 1,
                    }
                    for _value_scope in value_scopes
                    for _measure, measure_label in self._tb_measures()
                ],
            ]
        return [
            [
                {'name': _("Account"), 'colspan': 1, 'rowspan': 2},
                *[
                    {
                        'name': value_scope['label'],
                        'colspan': len(self._tb_measures()),
                        'rowspan': 1,
                    }
                    for value_scope in value_scopes
                ],
            ],
            [
                {
                    'name': measure_label,
                    'colspan': 1,
                    'rowspan': 1,
                }
                for _value_scope in value_scopes
                for _measure, measure_label in self._tb_measures()
            ],
        ]

    # ---- weighted analytic cell detail ----

    @api.model
    def _eh_tb_analytic_drilldown_scope(self, options, expression_label):
        date_from = self._extract_date(options, 'date_from')
        date_to = self._extract_date(options, 'date_to')
        company_ids = options.get('company_ids') or [self.env.company.id]
        periods = self._tb_period_scopes(
            options, date_from, date_to, company_ids,
        )
        analytics = self._eh_resolve_analytic_column_scopes(
            options, company_ids,
        )
        if not analytics:
            raise UserError(_(
                "The selected cell is not an analytic allocation column."
            ))
        value_scopes = self._eh_build_value_scopes(
            periods, analytics, include_total=True,
        )
        matches = []
        for value_scope in value_scopes:
            if value_scope.get('is_total'):
                continue
            for measure, _label in self._tb_measures():
                if expression_label == '%s__%s' % (
                    value_scope['key'], measure,
                ):
                    matches.append((value_scope, measure))
        if len(matches) != 1:
            raise UserError(_("The selected report column is no longer valid."))
        return matches[0]

    @api.model
    def _eh_tb_analytic_detail_query(
        self, options, account, measure, date_from, date_to,
        currency_table, global_account_ids, global_plan_ids,
        global_plan_account_ids,
    ):
        company_ids = options.get('company_ids') or [self.env.company.id]
        query = MoveLineQuery(
            self.env,
            company_ids=company_ids,
            currency_table=currency_table,
        )
        query.where_accounts([account.id])
        if options.get('posted_only', True):
            query.where_posted_only()
        safe_options = dict(options)
        safe_options['analytic_account_ids'] = list(global_account_ids)
        safe_options['analytic_plan_ids'] = []
        self.apply_common_filters(query, safe_options)
        if global_plan_ids:
            if global_plan_account_ids:
                query.where_analytic_accounts(global_plan_account_ids)
            else:
                query.where_raw(SQL("FALSE"))

        opening = measure.startswith('opening_')
        movement = measure.startswith('period_')
        if movement:
            query.where_date_range(date_from=date_from, date_to=date_to)
            if measure == 'period_debit':
                query.where_raw(SQL("aml.debit != 0.0"))
            else:
                query.where_raw(SQL("aml.credit != 0.0"))
        else:
            upper = date_from if opening else date_to
            operator = '<' if opening else '<='
            query.where_raw(SQL("aml.date " + operator + " %s", upper))
            query.where_raw(SQL("aml.balance != 0.0"))
            account_type = account.account_type or ''
            if account_type.startswith(('income', 'expense')):
                fy_starts = self._fiscalyear_starts(
                    company_ids, date_from,
                )
                fy_case = self._fiscalyear_start_case(
                    fy_starts, date_from,
                )
                query.where_raw(SQL("aml.date >= (%s)", fy_case))
        return query

    @api.model
    def _eh_tb_analytic_detail_payload(
        self, value_scope, weighted_rows, total, amounts,
        currency, offset, limit, line_by_id, move_by_id, partner_by_id,
        page_token,
    ):
        total_count = len(weighted_rows)
        page_rows = []
        page_end = min(total_count, offset + limit)
        for index in range(offset, page_end):
            weighted = weighted_rows[index]
            move_line_id = int(weighted['move_line_id'])
            line = line_by_id[move_line_id]
            move = move_by_id[line.move_id.id]
            partner = (
                partner_by_id.get(line.partner_id.id)
                if line.partner_id else None
            )
            page_rows.append({
                'id': 'aml-%d' % move_line_id,
                'move_line_id': move_line_id,
                'move_id': move.id,
                'values': {
                    'date': fields.Date.to_string(line.date),
                    'move': move.name or '',
                    'partner': partner.display_name if partner else '',
                    'label': line.ref or line.name or '',
                    'allocated_amount': amounts[index],
                },
            })
        return {
            'columns': [
                {'key': 'date', 'name': _("Date"), 'figure_type': 'date'},
                {'key': 'move', 'name': _("Journal Entry"),
                 'figure_type': 'string'},
                {'key': 'partner', 'name': _("Partner"),
                 'figure_type': 'string'},
                {'key': 'label', 'name': _("Label"),
                 'figure_type': 'string'},
                {'key': 'allocated_amount', 'name': _("Allocated Amount"),
                 'figure_type': 'monetary'},
            ],
            'rows': page_rows,
            'total': float(total),
            'offset': offset,
            'limit': limit,
            'total_count': total_count,
            'has_more': offset + len(page_rows) < total_count,
            'page_token': page_token,
            'currency': {
                'id': currency.id,
                'name': currency.name or '',
                'symbol': currency.symbol or '',
                'position': currency.position,
                'decimal_places': max(
                    0, min(6, int(currency.decimal_places or 0)),
                ),
            },
            'scope': dict(value_scope['scope']),
        }

    @api.model
    @api.private
    def _eh_get_analytic_column_drilldown_page(
        self, options, line_id, expression_label, offset=0, limit=80,
        page_token=None, snapshot_binding=None,
    ):
        if options.get('cash_basis'):
            raise UserError(_(
                "Weighted analytic detail is unavailable for cash-basis "
                "recognition."
            ))
        value_scope, measure = self._eh_tb_analytic_drilldown_scope(
            options, expression_label,
        )
        scoped_options = self._eh_scope_options(options, value_scope)
        account_id = self._expand_account_id_from_line_id(line_id)
        if account_id is None:
            raise UserError(_("The selected report row is not an account."))
        account = self.env['account.account'].search([
            ('id', '=', account_id),
        ], limit=1)
        if not account:
            raise AccessError(_("The selected account is not accessible."))
        account._eh_check_access('read')
        if account.account_type == 'equity_unaffected':
            raise UserError(_(
                "Weighted detail is unavailable for the retained-earnings "
                "roll-forward account."
            ))

        company_ids = scoped_options.get('company_ids') or [
            self.env.company.id,
        ]
        detail_helper = self.env[
            'eh.account.dynamic.report.handler.sectioned'
        ]
        if not detail_helper._eh_analytic_drilldown_account_matches_companies(
            account, company_ids,
        ):
            raise AccessError(_(
                "The selected account is outside company scope."
            ))
        date_from = self._extract_date(scoped_options, 'date_from')
        date_to = self._extract_date(scoped_options, 'date_to')
        currency_table = self._resolve_currency_table(
            scoped_options, company_ids, as_of_date=date_to,
        )
        presentation_converted = bool(
            currency_table is not None
            and not currency_table.is_monocurrency
        )
        currency = self._eh_monetary_currency(
            options=scoped_options,
            company_ids=company_ids,
            presentation_converted=presentation_converted,
        )
        global_accounts, global_plans, global_plan_accounts = (
            detail_helper._eh_analytic_drilldown_global_filters(
                scoped_options,
            )
        )

        rows_query = self._eh_tb_analytic_detail_query(
            scoped_options, account, measure, date_from, date_to,
            currency_table, global_accounts, global_plans,
            global_plan_accounts,
        )
        rows_query.select_field('id', alias='move_line_id')
        rows_query.select_field('move_id')
        if measure == 'period_debit':
            rows_query.select_debit_converted(alias='allocated_amount')
        elif measure == 'period_credit':
            rows_query.select_credit_converted(alias='allocated_amount')
        else:
            rows_query.select_balance_converted(alias='allocated_amount')
        rows_query.order_by('date', 'ASC')
        rows_query.order_by('id', 'ASC')
        rows_query.limit(self._MAX_ANALYTIC_DRILLDOWN_ROWS + 1)
        weighted_rows = rows_query.execute()
        if len(weighted_rows) > self._MAX_ANALYTIC_DRILLDOWN_ROWS:
            raise UserError(_(
                "This cell contains too many journal items for safe detail "
                "inspection. Narrow the report filters and try again."
            ))
        line_by_id, move_by_id, partner_by_id = (
            detail_helper._eh_analytic_drilldown_readable_rows(
                weighted_rows,
            )
        )

        total_query = self._eh_tb_analytic_detail_query(
            scoped_options, account, measure, date_from, date_to,
            currency_table, global_accounts, global_plans,
            global_plan_accounts,
        )
        if measure == 'period_debit':
            total_query.select_debit_sum_converted(alias='allocated_total')
        elif measure == 'period_credit':
            total_query.select_credit_sum_converted(alias='allocated_total')
        else:
            total_query.select_balance_sum_converted(alias='allocated_total')
        total_rows = total_query.execute()
        raw_amounts = [
            float(row.get('allocated_amount') or 0.0)
            for row in weighted_rows
        ]
        raw_total = float(
            total_rows[0].get('allocated_total') or 0.0
        ) if total_rows else 0.0
        rounding_quantum = max(
            float(currency.rounding or 0.0), 1e-9,
        )
        raw_reconciliation = raw_total - math.fsum(raw_amounts)
        if (
            not math.isfinite(raw_total)
            or not all(math.isfinite(amount) for amount in raw_amounts)
            or abs(raw_reconciliation) > rounding_quantum
        ):
            raise UserError(_(
                "The journal items changed while detail was being "
                "prepared. Refresh the report and try again."
            ))
        polarity = 1.0
        if measure in ('opening_credit', 'closing_credit'):
            polarity = -1.0
            correct_side = raw_total < 0.0
        elif measure in ('opening_debit', 'closing_debit'):
            correct_side = raw_total > 0.0
        else:
            correct_side = True
        if not correct_side:
            weighted_rows = []
            raw_amounts = []
            line_by_id, move_by_id, partner_by_id = {}, {}, {}
            raw_total = 0.0
        total = self._eh_round_monetary(
            raw_total * polarity, currency=currency,
        )
        amounts = [
            self._eh_round_monetary(
                amount * polarity, currency=currency,
            )
            for amount in raw_amounts
        ]
        if amounts:
            amounts[-1] = self._eh_round_monetary(
                float(total) - math.fsum(amounts[:-1]),
                currency=currency,
            )
            if self._eh_round_monetary(
                math.fsum(amounts), currency=currency,
            ) != float(total):
                raise UserError(_(
                    "The journal items changed while detail was being "
                    "prepared. Refresh the report and try again."
                ))
        expected_page_token = (
            detail_helper._eh_analytic_drilldown_page_token(
                weighted_rows,
                [amount * polarity for amount in raw_amounts],
                total,
                currency,
                value_scope,
                line_id,
                expression_label,
                limit,
                snapshot_binding,
            )
        )
        detail_helper._eh_assert_analytic_drilldown_page_token(
            offset, page_token, expected_page_token,
        )
        return self._eh_tb_analytic_detail_payload(
            value_scope, weighted_rows, total, amounts,
            currency, offset, limit, line_by_id, move_by_id, partner_by_id,
            expected_page_token,
        )

    @api.model
    def _merge_tb_period_results(self, scoped_results):
        """Merge six-cell period results by stable line id."""
        line_maps = [
            {line['id']: line for line in result['lines']}
            for result in scoped_results
        ]
        ordered_ids = []
        placed = set()
        templates = {}
        for scope_index, result in enumerate(scoped_results):
            result_lines = result['lines']
            result_ids = [line['id'] for line in result_lines]
            for position, line in enumerate(result_lines):
                line_id = line['id']
                if line_id in templates:
                    continue
                templates[line_id] = line
                placed.add(line_id)
                if not scope_index:
                    # The first scope sets the base order; later scopes only
                    # anchor the lines it does not have. Set lookups keep the
                    # merge linear on a chart of thousands of accounts.
                    ordered_ids.append(line_id)
                    continue
                # A line which exists only in another period still belongs at
                # its original hierarchy position. Anchor it before nearest
                # following known line (usually section/report total), then
                # fall back to nearest known predecessor.
                following = next((
                    candidate
                    for candidate in result_ids[position + 1:]
                    if candidate in placed
                ), None)
                if following is not None:
                    ordered_ids.insert(
                        ordered_ids.index(following), line_id,
                    )
                    continue
                preceding = next((
                    candidate
                    for candidate in reversed(result_ids[:position])
                    if candidate in placed
                ), None)
                if preceding is not None:
                    ordered_ids.insert(
                        ordered_ids.index(preceding) + 1, line_id,
                    )
                else:
                    ordered_ids.append(line_id)
        merged = []
        for line_id in ordered_ids:
            row = dict(templates[line_id])
            columns = []
            for result, line_map in zip(scoped_results, line_maps):
                scope_key = result['scope']['key']
                source = line_map.get(line_id)
                source_values = {
                    column.get('expression_label'): column
                    for column in (source or {}).get('columns') or ()
                }
                for measure, _label in self._tb_measures():
                    cell = dict(source_values.get(measure) or {'value': 0.0})
                    cell['expression_label'] = '%s__%s' % (
                        scope_key, measure,
                    )
                    columns.append(cell)
            row['columns'] = columns
            merged.append(row)
        return merged

    @api.model
    def _currency_meta(self, currency_table):
        """Disclose currency-conversion posture in the payload meta.

        Reports whether the run consolidated across currencies and, if any
        company's rate had to be defaulted, surfaces those flags so the
        figure is never silently approximate. Defensive: any failure yields
        an empty dict rather than breaking the payload.
        """
        if currency_table is None:
            return {}
        try:
            return self._presentation_currency_meta(currency_table)
        except Exception:  # pragma: no cover - defensive
            return {}

    @api.model
    def get_drilldown_action(self, options, line_id):
        """Open journal items forming the exact clicked TB column.

        Opening, movement, and closing cells represent different accounting
        windows.  The viewer passes the clicked expression in a transient
        option; validate it and replace the generic period domain with the
        matching window.  P&L openings/closings start at each company's
        fiscal-year boundary, while balance-sheet accounts remain cumulative.
        """
        action = super().get_drilldown_action(options, line_id)
        if not action:
            return None
        column = options.get('_eh_column_expression')
        if isinstance(column, str) and '__' in column:
            column = column.rsplit('__', 1)[-1]
        opening_columns = {'opening_debit', 'opening_credit'}
        movement_columns = {'period_debit', 'period_credit'}
        closing_columns = {
            'closing_debit', 'closing_credit',
            'prior_closing_debit', 'prior_closing_credit',
        }
        if column not in opening_columns | movement_columns | closing_columns:
            return None
        try:
            account_id = int(line_id.split('-', 1)[1])
            account = self.env['account.account'].browse(account_id).exists()
            if not account:
                return None
            date_from = self._extract_date(options, 'date_from')
            date_to = self._extract_date(options, 'date_to')
        except (UserError, ValueError, IndexError):
            return None

        domain = [
            term for term in action.get('domain', [])
            if not (
                isinstance(term, (tuple, list))
                and term
                and term[0] == 'date'
            )
        ]
        if column in opening_columns:
            domain.append(('date', '<', self._iso_date(date_from)))
        elif column in movement_columns:
            domain.extend([
                ('date', '>=', self._iso_date(date_from)),
                ('date', '<=', self._iso_date(date_to)),
            ])
            # Movement columns are gross debit and gross credit, not the
            # net balance used by opening/closing columns. Keep each click
            # tied to the exact side displayed in that cell.
            if column == 'period_debit':
                domain.append(('debit', '!=', 0.0))
            else:
                domain.append(('credit', '!=', 0.0))
        else:
            domain.append(('date', '<=', self._iso_date(date_to)))

        account_type = account.account_type or ''
        if (
            column in opening_columns | closing_columns
            and account_type.startswith(('income', 'expense'))
        ):
            company_ids = options.get('company_ids') or [self.env.company.id]
            starts = self._fiscalyear_starts(company_ids, date_from)
            lower_bounds = [
                [
                    ('company_id', '=', int(company_id)),
                    ('date', '>=', self._iso_date(start)),
                ]
                for company_id, start in starts.items()
            ]
            if lower_bounds:
                domain += expression.OR(lower_bounds)
        action['domain'] = domain
        return action

    # ---- internal helpers ----

    def _build_columns(self, comparison=False):
        columns = [
            {'expression_label': 'account', 'name': _("Account"),
             'figure_type': 'string'},
            {'expression_label': 'opening_debit', 'name': _("Opening DB"),
             'figure_type': 'monetary'},
            {'expression_label': 'opening_credit', 'name': _("Opening CR"),
             'figure_type': 'monetary'},
            {'expression_label': 'period_debit', 'name': _("Movement DB"),
             'figure_type': 'monetary'},
            {'expression_label': 'period_credit', 'name': _("Movement CR"),
             'figure_type': 'monetary'},
            {'expression_label': 'closing_debit', 'name': _("Closing DB"),
             'figure_type': 'monetary'},
            {'expression_label': 'closing_credit', 'name': _("Closing CR"),
             'figure_type': 'monetary'},
        ]
        if comparison:
            columns.extend([
                {'expression_label': 'prior_closing_debit',
                 'name': _("Prior Closing DB"),
                 'figure_type': 'monetary'},
                {'expression_label': 'prior_closing_credit',
                 'name': _("Prior Closing CR"),
                 'figure_type': 'monetary'},
                {'expression_label': 'closing_variance',
                 'name': _("Closing Variance"),
                 'figure_type': 'monetary'},
                {'expression_label': 'variance_pct',
                 'name': _("Var %"),
                 'figure_type': 'percentage'},
            ])
        return columns

    @staticmethod
    def _tb_column_value(line, expression_label):
        for column in (line or {}).get('columns') or ():
            if column.get('expression_label') == expression_label:
                return float(column.get('value') or 0.0)
        return 0.0

    def _merge_tb_comparison_lines(self, current_lines, prior_lines, currency):
        current_by_id = {line['id']: line for line in current_lines}
        prior_by_id = {line['id']: line for line in prior_lines}
        ordered_ids = list(current_by_id)
        ordered_ids.extend(
            line_id for line_id in prior_by_id if line_id not in current_by_id)
        merged = []
        for line_id in ordered_ids:
            current = current_by_id.get(line_id)
            prior = prior_by_id.get(line_id)
            if current is None:
                current = dict(prior)
                current['columns'] = [
                    {'expression_label': label, 'value': 0.0}
                    for label in (
                        'opening_debit', 'opening_credit',
                        'period_debit', 'period_credit',
                        'closing_debit', 'closing_credit',
                    )
                ]
            line = dict(current)
            current_net = (
                self._tb_column_value(current, 'closing_debit')
                - self._tb_column_value(current, 'closing_credit'))
            prior_debit = self._tb_column_value(prior, 'closing_debit')
            prior_credit = self._tb_column_value(prior, 'closing_credit')
            prior_net = prior_debit - prior_credit
            variance = self._eh_round_monetary(
                current_net - prior_net, currency=currency)
            variance_pct = (
                variance / abs(prior_net)
                if prior_net else (1.0 if current_net else 0.0))
            line['columns'] = list(current['columns']) + [
                {'expression_label': 'prior_closing_debit',
                 'value': prior_debit},
                {'expression_label': 'prior_closing_credit',
                 'value': prior_credit},
                {'expression_label': 'closing_variance', 'value': variance},
                {'expression_label': 'variance_pct', 'value': variance_pct},
            ]
            merged.append(line)
        return merged

    def _merge_tb_comparison_totals(self, current, prior, currency):
        totals = dict(current)
        current_net = (
            float(current.get('closing_debit') or 0.0)
            - float(current.get('closing_credit') or 0.0))
        prior_debit = float(prior.get('closing_debit') or 0.0)
        prior_credit = float(prior.get('closing_credit') or 0.0)
        prior_net = prior_debit - prior_credit
        variance = self._eh_round_monetary(
            current_net - prior_net, currency=currency)
        totals.update({
            'prior_closing_debit': prior_debit,
            'prior_closing_credit': prior_credit,
            'closing_variance': variance,
            'variance_pct': (
                variance / abs(prior_net)
                if prior_net else (1.0 if current_net else 0.0)),
        })
        return totals

    def _fetch_account_buckets(
        self, company_ids, date_from, date_to, posted_only, options,
        currency_table=None,
    ):
        query = MoveLineQuery(
            self.env, company_ids=company_ids, currency_table=currency_table)
        # Lines up to date_to participate. We need everything before
        # date_from for the opening balance plus everything in the period
        # for the movement columns.
        query.where_date_range(date_to=date_to)
        if posted_only:
            query.where_posted_only()
        self.apply_common_filters(query, options)
        query.join_account()

        # Fiscal-year-aware opening (WS4). P&L (income/expense) accounts reset
        # at the fiscal-year boundary, so their opening is only
        # current-FY-to-date (>= fiscal_year_start AND < date_from); their
        # prior-year movement is rolled into the unaffected-earnings line
        # (_fetch_unaffected_earnings). Balance-sheet accounts keep the
        # legacy all-time-before-date_from rule. Per-company fiscal-year start
        # is bound as a CASE so it stays one SQL pass across mixed calendars.
        fy_starts = self._fiscalyear_starts(company_ids, date_from)
        fy_case = self._fiscalyear_start_case(fy_starts, date_from)
        pl_predicate = SQL(
            "(acc.account_type LIKE %s OR acc.account_type LIKE %s)",
            'income%', 'expense%',
        )
        convert = (
            currency_table is not None and not currency_table.is_monocurrency)
        bal_sql = (
            SQL("(aml.balance) * %s", currency_table.rate_expr())
            if convert else SQL("aml.balance")
        )
        debit_sql = (
            SQL("(aml.debit) * %s", currency_table.rate_expr())
            if convert else SQL("aml.debit")
        )
        credit_sql = (
            SQL("(aml.credit) * %s", currency_table.rate_expr())
            if convert else SQL("aml.credit")
        )
        # Manual CASE aggregates must explicitly adopt the query's analytic
        # allocation weight.  Membership predicates alone would otherwise
        # show the gross 100% AML in every selected analytic column.
        bal_sql = query._analytic_weighted(bal_sql)
        debit_sql = query._analytic_weighted(debit_sql)
        credit_sql = query._analytic_weighted(credit_sql)

        query.select_field('account_id')
        query.select_account_field('code', alias='account_code')
        query.select_account_field('name', alias='account_name')
        query.select(
            SQL(
                "SUM(CASE "
                "WHEN %s THEN "
                "(CASE WHEN aml.date >= (%s) AND aml.date < %s "
                "THEN (%s) ELSE 0 END) "
                "ELSE (CASE WHEN aml.date < %s THEN (%s) ELSE 0 END) END)",
                pl_predicate, fy_case, date_from, bal_sql,
                date_from, bal_sql,
            ),
            'opening_balance',
        )
        query.select(
            SQL(
                "SUM(CASE WHEN aml.date >= %s AND aml.date <= %s "
                "THEN (%s) ELSE 0 END)",
                date_from, date_to, debit_sql,
            ),
            'period_debit',
        )
        query.select(
            SQL(
                "SUM(CASE WHEN aml.date >= %s AND aml.date <= %s "
                "THEN (%s) ELSE 0 END)",
                date_from, date_to, credit_sql,
            ),
            'period_credit',
        )
        # Group by all non aggregated SELECT expressions for portability.
        # The account_code and account_name expressions are resolved via the
        # MoveLineQuery helpers so GROUP BY matches SELECT verbatim, including
        # the per-language COALESCE branch on translated jsonb columns. A
        # mismatch here (e.g. hardcoding ``en_US`` in GROUP BY while SELECT
        # uses COALESCE for a non-en_US env.lang) causes PostgreSQL to
        # reject the query with "must appear in the GROUP BY clause", which
        # surfaces to the user as a generic "Odoo Server Error" with no
        # actionable detail. Keep the three call sites symmetric.
        query.group_by(SQL("aml.account_id"))
        query.group_by_account_field('code')
        query.group_by_account_field('name')
        query.order_by_account_field('code', 'ASC')
        return query.execute()

    def _fetch_unaffected_earnings(
        self, company_ids, date_from, posted_only, options,
        currency_table=None,
    ):
        """Net prior-year P&L to roll into the unaffected-earnings line.

        Single SUM (no GROUP BY, the cheapest possible query) over every
        income/expense line dated strictly before each company's fiscal-year
        start. Returns the signed net balance (positive = net credit = profit
        in balance terms, since income posts as a credit -> negative balance,
        so a profit yields a negative sum here, surfaced on the credit side
        by the line builder).

        Per-company fiscal-year start is honoured via the same CASE the
        opening uses, so a mid-year date_from still rolls exactly the
        prior-year portion. Multi-currency converts before summing.

        Failures deliberately propagate: omitting this balance would publish
        a Trial Balance that appears complete but does not foot.
        """
        return sum(self._fetch_unaffected_earnings_by_company(
            company_ids=company_ids,
            date_from=date_from,
            posted_only=posted_only,
            options=options,
            currency_table=currency_table,
        ).values())

    def _fetch_unaffected_earnings_by_company(
        self, company_ids, date_from, posted_only, options,
        currency_table=None,
    ):
        """Return prior-year P&L balance keyed by company.

        This balance is required for the Trial Balance to foot.  Never turn a
        query/configuration failure into an apparently valid zero: surfacing
        the error is safer than publishing a materially unbalanced report.
        """
        fy_starts = self._fiscalyear_starts(company_ids, date_from)
        fy_case = self._fiscalyear_start_case(fy_starts, date_from)
        convert = (
            currency_table is not None
            and not currency_table.is_monocurrency)
        bal_sql = (
            SQL("(aml.balance) * %s", currency_table.rate_expr())
            if convert else SQL("aml.balance")
        )
        query = MoveLineQuery(
            self.env, company_ids=company_ids,
            currency_table=currency_table)
        query.where_date_range(date_to=date_from)
        if posted_only:
            query.where_posted_only()
        # Account/account-type selectors choose visible destinations, not
        # the P&L source perimeter of retained earnings.
        source_options = dict(options or {})
        source_options.pop('account_ids', None)
        source_options.pop('account_type_ids', None)
        self.apply_common_filters(query, source_options)
        bal_sql = query._analytic_weighted(bal_sql)
        query.join_account()
        query.select_field('company_id')
        query.select(
            SQL(
                "SUM(CASE WHEN "
                "(acc.account_type LIKE %s "
                "OR acc.account_type LIKE %s) "
                "AND aml.date < (%s) THEN (%s) ELSE 0 END)",
                'income%', 'expense%', fy_case, bal_sql,
            ),
            'unaffected_balance',
        )
        query.group_by(SQL("aml.company_id"))
        rows = query.execute()
        return {
            int(row['company_id']): float(
                row.get('unaffected_balance') or 0.0)
            for row in rows
        }

    def _merge_unaffected_into_rows(
        self, rows, unaffected_by_company, company_ids, options,
    ):
        """Merge retained earnings into real chart accounts where possible."""
        rows = [dict(row) for row in rows]
        destinations = self.env[
            'eh.account.dynamic.report.handler.general_ledger'
        ]._unaffected_account_ids(company_ids)
        selected_accounts = {
            int(account_id) for account_id in options.get('account_ids') or ()
        }
        selected_types = set(options.get('account_type_ids') or ())
        by_account = {}
        synthetic = 0.0
        for company_id, amount in unaffected_by_company.items():
            destination = destinations.get(int(company_id))
            visible = (
                destination
                and (not selected_accounts or destination in selected_accounts)
                and (not selected_types or 'equity_unaffected' in selected_types)
            )
            if visible:
                by_account[destination] = (
                    by_account.get(destination, 0.0) + float(amount or 0.0)
                )
            elif not destination and not selected_accounts and not selected_types:
                synthetic += float(amount or 0.0)

        row_by_account = {int(row['account_id']): row for row in rows}
        for account_id, amount in by_account.items():
            row = row_by_account.get(account_id)
            if row is not None:
                row['opening_balance'] = (
                    float(row.get('opening_balance') or 0.0) + amount)
                continue
            account = self.env['account.account'].browse(account_id).exists()
            if not account:
                synthetic += amount
                continue
            rows.append({
                'account_id': account.id,
                'account_code': account.code or '',
                'account_name': account.name or '',
                'opening_balance': amount,
                'period_debit': 0.0,
                'period_credit': 0.0,
            })
        rows.sort(key=lambda row: (
            row.get('account_code') or '', int(row['account_id'])))
        return rows, synthetic

    @api.model
    def _tb_finalize_account_rows(self, rows, company_ids, options):
        """Return one row per account, labelled in the report company.

        The bucket query also groups by the account code, which Odoo 18 and
        later resolve per journal item company, so an account shared by
        companies whose codes differ comes back as one row per code. Those
        rows are summed here. When zero balance accounts are shown, every
        account of the report chart without a row is added at zero, so the
        report lists the whole chart and not only the accounts with entries.
        """
        merged = {}
        relabel = set()
        for row in rows:
            account_id = int(row['account_id'])
            current = merged.get(account_id)
            if current is None:
                merged[account_id] = dict(row, account_id=account_id)
                if not row.get('account_code'):
                    relabel.add(account_id)
                continue
            for key in ('opening_balance', 'period_debit', 'period_credit'):
                current[key] = (
                    float(current.get(key) or 0.0)
                    + float(row.get(key) or 0.0)
                )
            relabel.add(account_id)
        if options.get('show_zero'):
            added = [
                account_id
                for account_id in self._tb_chart_account_ids(
                    company_ids, options)
                if account_id not in merged
            ]
            for account_id in self._tb_drop_listed_parents(
                    added, merged, options):
                merged[account_id] = {
                    'account_id': account_id,
                    'account_code': '',
                    'account_name': '',
                    'opening_balance': 0.0,
                    'period_debit': 0.0,
                    'period_credit': 0.0,
                }
                relabel.add(account_id)
        if self._tb_spans_several_roots(company_ids):
            # Each journal item's code comes from its own company; label
            # every row in the report company so codes and order do not
            # depend on which company posted.
            relabel = set(merged)
        if relabel:
            labels = self._tb_account_labels(relabel, company_ids, options)
            for account_id in relabel:
                code, name = labels.get(account_id, ('', ''))
                row = merged[account_id]
                row['account_code'] = code or row.get('account_code') or ''
                row['account_name'] = name or row.get('account_name') or ''
        return sorted(merged.values(), key=self._tb_row_sort_key)

    @api.model
    def _tb_spans_several_roots(self, company_ids):
        companies = self.env['res.company'].sudo().browse(
            [int(company_id) for company_id in company_ids]).exists()
        if 'root_id' not in companies._fields:
            return len(companies) > 1
        return len(companies.mapped('root_id')) > 1

    @api.model
    def _tb_drop_listed_parents(self, added, merged, options):
        """Leave out a parent account whose child is listed.

        In the account hierarchy such a parent already shows as the group
        line above its child. In the flat layout, or when nothing below it
        is listed, or when it was selected explicitly, it is a row of its
        own like any other account.
        """
        Account = self.env['account.account']
        if (
            not added
            or 'parent_id' not in Account._fields
            or options.get('hierarchical_groups') is False
        ):
            return added
        listed = set(merged) | set(added)
        parent_ids = set(Account.sudo().with_context(active_test=False).search([
            ('id', 'in', sorted(listed)), ('parent_id', 'in', added),
        ]).parent_id.ids)
        selected = {
            int(account_id) for account_id in options.get('account_ids') or ()
        }
        return [
            account_id for account_id in added
            if account_id not in parent_ids or account_id in selected
        ]

    @api.model
    def _tb_chart_account_ids(self, company_ids, options):
        """Active accounts of the report chart, under the caller's access.

        The same account and account type selectors as the bucket query
        apply.
        """
        Account = self.env['account.account']
        company_ids = [int(company_id) for company_id in company_ids]
        if 'company_ids' in Account._fields:
            domain = [('company_ids', 'parent_of', company_ids)]
        elif 'parent_path' in self.env['res.company']._fields:
            domain = [('company_id', 'parent_of', company_ids)]
        else:
            domain = [('company_id', 'in', company_ids)]
        if 'deprecated' in Account._fields:
            domain.append(('deprecated', '=', False))
        if options.get('account_ids'):
            domain.append(('id', 'in', [
                int(account_id) for account_id in options['account_ids']
            ]))
        if options.get('account_type_ids'):
            domain.append((
                'account_type', 'in', list(options['account_type_ids']),
            ))
        accounts = Account.search(domain)
        if 'parent_path' in Account._fields and accounts:
            # The account hierarchy refuses an ancestor in the report scope
            # that the caller cannot read; an account below one is left out
            # here rather than failing a report it has no entries in.
            chains = {
                account.id: {
                    int(token)
                    for token in (account.parent_path or '').split('/')
                    if token and int(token) != account.id
                }
                for account in accounts
            }
            ancestor_ids = sorted(set().union(*chains.values()))
            if ancestor_ids:
                Ancestors = Account.with_context(
                    active_test=False, allowed_company_ids=company_ids)
                scope = [
                    ('id', 'in', ancestor_ids),
                    ('company_ids', 'parent_of', company_ids),
                ]
                blocked = (
                    set(Ancestors.sudo().search(scope).ids)
                    - set(Ancestors.search(scope).ids)
                )
                if blocked:
                    accounts = accounts.filtered(
                        lambda account: not chains[account.id] & blocked)
        return accounts.ids

    @api.model
    def _tb_label_company(self, company_ids, options):
        """Company whose chart codes label the report rows: the primary
        report company, else the first company in scope."""
        candidates = [options.get('primary_company_id'), *company_ids]
        for raw in candidates:
            try:
                company_id = int(raw or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if company_id and company_id in {int(c) for c in company_ids}:
                company = self.env['res.company'].browse(company_id).exists()
                if company:
                    return company
        return self.env.company

    @api.model
    def _tb_account_labels(self, account_ids, company_ids, options):
        """Return {account_id: (code, name)} read in the label company.

        Codes are company dependent from Odoo 18. An account without a code
        in the label company (for example another company's own account in a
        multi company report) takes its code from the first report company
        that has one, so it sorts and reads where its owner expects it.
        """
        label_company = self._tb_label_company(company_ids, options)
        Account = self.env['account.account'].sudo().with_context(
            active_test=False)
        account_ids = sorted(int(account_id) for account_id in account_ids)
        labels = {
            account.id: (account.code or '', account.name or '')
            for account in Account.with_company(label_company).browse(
                account_ids)
        }
        for company_id in company_ids:
            missing = [
                account_id for account_id, (code, _name) in labels.items()
                if not code
            ]
            if not missing:
                break
            if int(company_id) == label_company.id:
                continue
            for account in Account.with_company(int(company_id)).browse(
                    missing):
                if account.code:
                    labels[account.id] = (
                        account.code, labels[account.id][1] or account.name,
                    )
        return labels

    @api.model
    def _tb_row_is_zero(self, row, currency):
        """True when all six measures of an account row round to zero."""
        opening = float(row.get('opening_balance') or 0.0)
        debit = float(row.get('period_debit') or 0.0)
        credit = float(row.get('period_credit') or 0.0)
        return all(
            self._eh_is_zero_monetary(value, currency=currency)
            for value in (opening, debit, credit, opening + debit - credit)
        )

    @staticmethod
    def _tb_row_sort_key(row):
        return (
            row.get('account_code') or '', row.get('account_name') or '',
            row['account_id'],
        )

    def _unaffected_earnings_line(
        self, value, level=1, parent_id=None, options=None,
        presentation_converted=False, currency=None,
    ):
        """Synthetic Current Year Earnings (unallocated) row.

        Carries rolled-up prior-year P&L into opening_debit / opening_credit
        (and the same into closing, since it has no period movement of its
        own), placed under equity so the trial balance foots once P&L
        openings reset. ``value`` is the net prior-year balance: a credit
        (negative balance => net profit) lands on the credit side, a debit
        (net loss) on the debit side, matching accounting convention.
        """
        currency = currency or self._eh_monetary_currency(
            options=options,
            company_ids=(options or {}).get('company_ids'),
            presentation_converted=presentation_converted,
        )
        opening = self._eh_round_monetary(
            float(value or 0.0), currency=currency)
        opening_debit = opening if opening > 0 else 0.0
        opening_credit = -opening if opening < 0 else 0.0
        line = {
            'id': 'account-unaffected-earnings',
            'name': _("Current Year Earnings (unallocated)"),
            'level': level,
            'columns': [
                {'expression_label': 'opening_debit',
                 'value': opening_debit},
                {'expression_label': 'opening_credit',
                 'value': opening_credit},
                {'expression_label': 'period_debit', 'value': 0.0},
                {'expression_label': 'period_credit', 'value': 0.0},
                {'expression_label': 'closing_debit',
                 'value': opening_debit},
                {'expression_label': 'closing_credit',
                 'value': opening_credit},
            ],
            'unfoldable': False,
            'meta': {
                'kind': 'unaffected_earnings',
                'account_id': None,
                # account_code present (empty) so the common line-indexing
                # helpers that key on meta['account_code'] never KeyError on
                # this synthetic row.
                'account_code': '',
            },
        }
        if parent_id:
            line['parent_id'] = parent_id
        return line

    @api.model
    def _unaffected_contribution(
        self, value, options=None, presentation_converted=False,
        currency=None,
    ):
        """Six-column contribution of the unaffected line, for totals.

        Returns a dict matching the per-account column tuple so both the
        flat and hierarchical total roll-ups can add the unaffected line
        without duplicating sign logic.
        """
        currency = currency or self._eh_monetary_currency(
            options=options,
            company_ids=(options or {}).get('company_ids'),
            presentation_converted=presentation_converted,
        )
        opening = self._eh_round_monetary(
            float(value or 0.0), currency=currency)
        opening_debit = opening if opening > 0 else 0.0
        opening_credit = -opening if opening < 0 else 0.0
        return {
            'opening_debit': opening_debit,
            'opening_credit': opening_credit,
            'period_debit': 0.0,
            'period_credit': 0.0,
            'closing_debit': opening_debit,
            'closing_credit': opening_credit,
        }

    @api.model
    def _expand_child_columns(self, options, aml_row):
        """Trial-Balance override: one aml fills the movement columns.

        A single journal item is a period movement, so its debit / credit
        land in period_debit / period_credit; it has no opening or closing
        balance of its own, so those columns are blanked. The page of
        children therefore sums to the account's period_debit /
        period_credit cell (the reconciliation invariant).
        """
        currency_id = options.get('_eh_internal_monetary_currency_id')
        currency = (
            self.env['res.currency'].browse(int(currency_id))
            if currency_id else self._eh_monetary_currency(
                options=options,
                company_ids=options.get('company_ids'),
                presentation_converted=bool(
                    options.get('presentation_currency_id')),
            )
        )
        debit = self._eh_round_monetary(
            float(aml_row.get('debit') or 0.0), currency=currency)
        credit = self._eh_round_monetary(
            float(aml_row.get('credit') or 0.0), currency=currency)
        date_val = aml_row.get('date')
        return [{
            'id': "aml-%s" % aml_row.get('aml_id'),
            'name': aml_row.get('ref') or aml_row.get('line_label') or '',
            'level': 2,
            'columns': [
                {'expression_label': 'opening_debit', 'value': ''},
                {'expression_label': 'opening_credit', 'value': ''},
                {'expression_label': 'period_debit', 'value': debit},
                {'expression_label': 'period_credit', 'value': credit},
                {'expression_label': 'closing_debit', 'value': ''},
                {'expression_label': 'closing_credit', 'value': ''},
            ],
            'unfoldable': False,
            'unfolded': False,
            'lazy': False,
            'meta': {
                'kind': 'aml',
                'aml_id': aml_row.get('aml_id'),
                'account_id': aml_row.get('account_id'),
                'date': self._iso_date(date_val) if date_val else None,
                'move': aml_row.get('move_name') or '',
                'partner': aml_row.get('partner_name') or '',
            },
        }]

    def _build_lines_and_totals(
        self, rows, show_zero, options=None, unaffected=0.0,
        presentation_converted=False, currency=None,
    ):
        currency = currency or self._eh_monetary_currency(
            options=options,
            company_ids=(options or {}).get('company_ids'),
            presentation_converted=presentation_converted,
        )
        lines = []
        totals = {
            'opening_debit': 0.0, 'opening_credit': 0.0,
            'period_debit': 0.0, 'period_credit': 0.0,
            'closing_debit': 0.0, 'closing_credit': 0.0,
        }
        for row in rows:
            opening = float(row.get('opening_balance') or 0.0)
            period_debit = float(row.get('period_debit') or 0.0)
            period_credit = float(row.get('period_credit') or 0.0)
            closing = opening + period_debit - period_credit

            opening_debit = opening if opening > 0 else 0.0
            opening_credit = -opening if opening < 0 else 0.0
            closing_debit = closing if closing > 0 else 0.0
            closing_credit = -closing if closing < 0 else 0.0

            monetary_values = (
                opening_debit, opening_credit, period_debit,
                period_credit, closing_debit, closing_credit,
            )
            if not show_zero and all(
                self._eh_is_zero_monetary(value, currency=currency)
                for value in monetary_values
            ):
                continue

            leaf = {
                'id': "account-%s" % row['account_id'],
                'name': "%s %s" % (row['account_code'], row['account_name']),
                'level': 1,
                'columns': [
                    {'expression_label': 'opening_debit',
                     'value': self._eh_round_monetary(
                         opening_debit, currency=currency)},
                    {'expression_label': 'opening_credit',
                     'value': self._eh_round_monetary(
                         opening_credit, currency=currency)},
                    {'expression_label': 'period_debit',
                     'value': self._eh_round_monetary(
                         period_debit, currency=currency)},
                    {'expression_label': 'period_credit',
                     'value': self._eh_round_monetary(
                         period_credit, currency=currency)},
                    {'expression_label': 'closing_debit',
                     'value': self._eh_round_monetary(
                         closing_debit, currency=currency)},
                    {'expression_label': 'closing_credit',
                     'value': self._eh_round_monetary(
                         closing_credit, currency=currency)},
                ],
                'unfoldable': False,
                'meta': {
                    'account_id': row['account_id'],
                    'account_code': row['account_code'],
                },
            }
            if options is not None:
                self._eh_apply_leaf_lazy_flags(leaf, options)
            lines.append(leaf)

            totals['opening_debit'] += opening_debit
            totals['opening_credit'] += opening_credit
            totals['period_debit'] += period_debit
            totals['period_credit'] += period_credit
            totals['closing_debit'] += closing_debit
            totals['closing_credit'] += closing_credit

        # Append the unaffected-earnings (Current Year Earnings) line so the
        # TB foots once P&L openings have been reset to the fiscal year.
        if not self._eh_is_zero_monetary(unaffected, currency=currency):
            lines.append(self._unaffected_earnings_line(
                unaffected,
                options=options,
                presentation_converted=presentation_converted,
                currency=currency,
            ))
            contrib = self._unaffected_contribution(
                unaffected,
                options=options,
                presentation_converted=presentation_converted,
                currency=currency,
            )
            for k in totals:
                totals[k] += contrib[k]

        totals = {
            k: self._eh_round_monetary(v, currency=currency)
            for k, v in totals.items()
        }
        return lines, totals

    def _build_hierarchical_lines_and_totals(
        self, rows, show_zero, unfolded_ids, options=None, unaffected=0.0,
        presentation_converted=False, currency=None,
    ):
        """Multi-column hierarchical builder for Trial Balance.

        Computes the per-account 6-column tuple in Python first
        (opening_db / cr, period_db / cr, closing_db / cr), then walks
        account.account.group_id and account.group.parent_id to build
        the nested line list with parent_id linkage. Group totals are
        the sum of every descendant account's column tuple.

        Mirrors the shape of _render_account_lines_grouped on the
        sectioned base but is multi-column-aware and lives here
        because Trial Balance is the only multi-column report whose
        grouping aggregates across columns.
        """
        currency = currency or self._eh_monetary_currency(
            options=options,
            company_ids=(options or {}).get('company_ids'),
            presentation_converted=presentation_converted,
        )
        if not rows:
            return self._hierarchical_only_unaffected(
                unaffected,
                options=options,
                presentation_converted=presentation_converted,
                currency=currency,
            )
        # Per-account computed columns (six values each).
        per_account = {}
        for row in rows:
            opening = float(row.get('opening_balance') or 0.0)
            period_debit = float(row.get('period_debit') or 0.0)
            period_credit = float(row.get('period_credit') or 0.0)
            closing = opening + period_debit - period_credit
            opening_debit = opening if opening > 0 else 0.0
            opening_credit = -opening if opening < 0 else 0.0
            closing_debit = closing if closing > 0 else 0.0
            closing_credit = -closing if closing < 0 else 0.0
            monetary_values = (
                opening_debit, opening_credit, period_debit,
                period_credit, closing_debit, closing_credit,
            )
            if not show_zero and all(
                self._eh_is_zero_monetary(value, currency=currency)
                for value in monetary_values
            ):
                continue
            per_account[row['account_id']] = {
                'code': row['account_code'],
                'name': row['account_name'],
                'opening_debit': opening_debit,
                'opening_credit': opening_credit,
                'period_debit': period_debit,
                'period_credit': period_credit,
                'closing_debit': closing_debit,
                'closing_credit': closing_credit,
            }
        if not per_account:
            return self._hierarchical_only_unaffected(
                unaffected,
                options=options,
                presentation_converted=presentation_converted,
                currency=currency,
            )

        # Resolve group path per account. Reuses the same logic as
        # the sectioned helper.
        Account = self.env['account.account'].sudo()
        Group = self.env['account.group'].sudo()
        accounts = Account.browse(list(per_account.keys()))
        group_paths = {}
        for acc in accounts:
            chain = []
            grp = acc.group_id
            while grp:
                chain.append(grp.id)
                grp = grp.parent_id
            chain.reverse()
            group_paths[acc.id] = tuple(chain)

        # Aggregate per group (every prefix of every account's path).
        zero = {
            'opening_debit': 0.0, 'opening_credit': 0.0,
            'period_debit': 0.0, 'period_credit': 0.0,
            'closing_debit': 0.0, 'closing_credit': 0.0,
        }
        group_totals = {}
        accounts_by_group = {}
        for acc_id, vals in per_account.items():
            path = group_paths[acc_id]
            cumulative = ()
            for gid in path:
                cumulative = cumulative + (gid,)
                bucket = group_totals.setdefault(cumulative, dict(zero))
                for k in zero:
                    bucket[k] += vals[k]
            accounts_by_group.setdefault(path, []).append(acc_id)

        # Path identifiers and ordering.
        section_id = 'trial_balance'

        def _line_id_for_path(path_tuple):
            if not path_tuple:
                return "section-%s-header" % section_id
            return "section-%s-group-%s" % (
                section_id,
                "_".join(str(g) for g in path_tuple),
            )

        all_paths = set()
        for parent_path in accounts_by_group:
            cumulative = ()
            for g in parent_path:
                cumulative = cumulative + (g,)
                all_paths.add(cumulative)

        def _path_sort_key(p):
            keys = []
            for gid in p:
                grp = Group.browse(gid)
                keys.append((grp.code_prefix_start or '', gid))
            return keys
        ordered_paths = sorted(all_paths, key=_path_sort_key)

        lines = []
        # Ungrouped accounts attach directly under a synthetic header
        # at the top, matching the sectioned helper's behaviour.
        ungrouped = accounts_by_group.get((), [])
        if ungrouped:
            ungrouped.sort(key=lambda a: per_account[a]['code'] or '')
            for aid in ungrouped:
                vals = per_account[aid]
                lines.append(self._tb_account_line(
                    aid, vals, level=1, parent_id=None, options=options,
                    presentation_converted=presentation_converted,
                    currency=currency,
                ))

        for path in ordered_paths:
            grp = Group.browse(path[-1])
            depth = len(path)
            parent_id = _line_id_for_path(path[:-1]) if len(path) > 1 else None
            this_id = _line_id_for_path(path)
            unfolded = (not unfolded_ids) or this_id in unfolded_ids
            totals_at_path = group_totals[path]
            lines.append({
                'id': this_id,
                'name': "%s %s" % (
                    grp.code_prefix_start or '',
                    grp.display_name or grp.name or '',
                ),
                'level': depth,
                'parent_id': parent_id,
                'columns': [
                    {'expression_label': k,
                     'value': self._eh_round_monetary(
                         totals_at_path[k], currency=currency)}
                    for k in ('opening_debit', 'opening_credit',
                              'period_debit', 'period_credit',
                              'closing_debit', 'closing_credit')
                ],
                'unfoldable': True,
                'unfolded': unfolded,
                'meta': {
                    'kind': 'account_group',
                    'group_id': grp.id,
                    'depth': depth,
                },
            })
            for aid in sorted(
                accounts_by_group.get(path, []),
                key=lambda a: per_account[a]['code'] or '',
            ):
                vals = per_account[aid]
                lines.append(self._tb_account_line(
                    aid, vals, level=depth + 1, parent_id=this_id,
                    options=options,
                    presentation_converted=presentation_converted,
                    currency=currency,
                ))

        # Top-level totals across every account in the report.
        totals = {k: 0.0 for k in zero}
        for vals in per_account.values():
            for k in totals:
                totals[k] += vals[k]

        # Unaffected-earnings line at the top level so the grand total foots
        # at a fiscal-year boundary. Placed under equity (ungrouped, top
        # level) to keep the hierarchical builder's parenting simple; the
        # contribution is added to the grand totals so flat and hierarchical
        # presentations agree.
        if not self._eh_is_zero_monetary(unaffected, currency=currency):
            lines.append(self._unaffected_earnings_line(
                unaffected,
                options=options,
                presentation_converted=presentation_converted,
                currency=currency,
            ))
            contrib = self._unaffected_contribution(
                unaffected,
                options=options,
                presentation_converted=presentation_converted,
                currency=currency,
            )
            for k in totals:
                totals[k] += contrib[k]

        totals = {
            k: self._eh_round_monetary(v, currency=currency)
            for k, v in totals.items()
        }
        return lines, totals

    def _hierarchical_only_unaffected(
        self, unaffected, options=None, presentation_converted=False,
        currency=None,
    ):
        """Return value when there are no account rows but prior-year P&L
        must still surface as the unaffected-earnings line.

        Keeps the empty-report shape (empty lines, zero totals) byte-identical
        when there is nothing to roll, so the existing no-data tests are
        unaffected.
        """
        zero = {
            'opening_debit': 0.0, 'opening_credit': 0.0,
            'period_debit': 0.0, 'period_credit': 0.0,
            'closing_debit': 0.0, 'closing_credit': 0.0,
        }
        currency = currency or self._eh_monetary_currency(
            options=options,
            company_ids=(options or {}).get('company_ids'),
            presentation_converted=presentation_converted,
        )
        if self._eh_is_zero_monetary(unaffected, currency=currency):
            return [], zero
        line = self._unaffected_earnings_line(
            unaffected,
            options=options,
            presentation_converted=presentation_converted,
            currency=currency,
        )
        contrib = self._unaffected_contribution(
            unaffected,
            options=options,
            presentation_converted=presentation_converted,
            currency=currency,
        )
        totals = {
            k: self._eh_round_monetary(contrib[k], currency=currency)
            for k in zero
        }
        return [line], totals

    def _tb_account_line(self, account_id, vals, level, parent_id,
                         options=None, presentation_converted=False,
                         currency=None):
        currency = currency or self._eh_monetary_currency(
            options=options,
            company_ids=(options or {}).get('company_ids'),
            presentation_converted=presentation_converted,
        )
        line = {
            'id': "account-%s" % account_id,
            'name': "%s %s" % (vals['code'], vals['name']),
            'level': level,
            'columns': [
                {'expression_label': k,
                 'value': self._eh_round_monetary(
                     vals[k], currency=currency)}
                for k in ('opening_debit', 'opening_credit',
                          'period_debit', 'period_credit',
                          'closing_debit', 'closing_credit')
            ],
            'unfoldable': False,
            'meta': {
                'account_id': account_id,
                'account_code': vals['code'],
            },
        }
        if parent_id:
            line['parent_id'] = parent_id
        if options is not None:
            self._eh_apply_leaf_lazy_flags(line, options)
        return line
