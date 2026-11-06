# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Profit and Loss handler.

Period income statement with two sections (Income, Expenses) and a Net
Profit row. Inherits the sectioned handler base, so query, line, and
section formatting come for free.

Sign convention:

* Income accounts carry credit balances (negative). The sectioned base
  query is called with sign=-1 to flip the display to a positive amount.
* Expense accounts carry debit balances (positive); sign=+1.
* Net Profit = Income - Expenses, displayed as a single computed line at
  the bottom.

Section structure:

* Income (account_type in 'income', 'income_other')
* Expenses (account_type in 'expense', 'expense_other',
  'expense_depreciation', 'expense_direct_cost')
* Net Profit (computed)

Localizations can _inherit this handler to add more sections (Cost of
Sales, Other Income, etc.) or override the account_type tuples to match
local chart of accounts conventions.
"""

import datetime
import math

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.translate import LazyTranslate

from odoo.addons.eh_account_base.tools.currency_table import CurrencyTable

_lt = LazyTranslate(__name__)

class _PeriodAverageCurrencyTable(CurrencyTable):
    """CurrencyTable carrying an IAS 21 period-average flow rate."""

    def __init__(self, *args, date_from=None, components=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.date_from = fields.Date.to_date(date_from)
        self._average_components = components or {}

    def translation_metadata(self):
        if self.is_monocurrency:
            return {}
        self.rate_map
        return {
            'currency_translation_policy': 'period_average',
            'currency_translation_date_from': fields.Date.to_string(
                self.date_from),
            'currency_translation_date_to': fields.Date.to_string(
                self.as_of_date),
            'currency_translation_rate_components': {
                str(company_id): values
                for company_id, values in self._average_components.items()
            },
        }

    def period_metadata(self, label):
        metadata = self.translation_metadata()
        if not metadata:
            return None
        return {
            'label': label,
            'policy': metadata['currency_translation_policy'],
            'date_from': metadata['currency_translation_date_from'],
            'date_to': metadata['currency_translation_date_to'],
            'rate_components': metadata[
                'currency_translation_rate_components'],
        }


class EhProfitAndLossHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.profit_and_loss'
    _inherit = 'eh.account.dynamic.report.handler.sectioned'
    _description = "Profit and Loss report handler"

    REPORT_CODE = 'profit_and_loss'
    REPORT_NAME = _lt("Profit and Loss")
    _EH_COLUMN_AXIS_CAPABILITIES = frozenset({
        'comparison', 'analytic_columns',
    })
    _EH_SQL_PRESENTATION_CURRENCY = True
    _EH_ANALYTIC_COLUMN_DRILLDOWN = True

    INCOME_TYPES = ('income', 'income_other')
    # ``expense_other`` was added by Odoo 19.  Keeping it in this SQL
    # selection tuple is harmless on 16--18 (no account can carry that
    # value there) and prevents Other Expenses disappearing on 19+.
    EXPENSE_TYPES = (
        'expense', 'expense_other', 'expense_depreciation',
        'expense_direct_cost',
    )

    @api.model
    def _eh_analytic_drilldown_account_types(self):
        return self.INCOME_TYPES + self.EXPENSE_TYPES

    @api.model
    def _eh_analytic_drilldown_currency_table(
        self, options, company_ids, date_from, date_to,
    ):
        return self._resolve_flow_currency_table(
            options, company_ids, date_from, date_to,
        )

    # By-function (IAS 1.103) presentation splits expenses by role. Cost of
    # Sales is the direct-cost bucket; Operating Expenses is the remaining
    # overhead. Finance Costs and Tax Expense have no dedicated account_type
    # and are resolved from the per-company account mappings on res.company
    # (eh_pnl_finance_cost_account_ids / eh_pnl_tax_expense_account_ids).
    COST_OF_SALES_TYPES = ('expense_direct_cost',)
    OPERATING_EXPENSE_TYPES = (
        'expense', 'expense_other', 'expense_depreciation',
    )

    @api.model
    def _resolve_flow_currency_table(
        self, options, company_ids, date_from, date_to,
    ):
        """Return a day-weighted average conversion table for P&L flows.

        IAS 21 translates income and expense at transaction-date rates and
        permits an average rate when it is a reasonable approximation.  A
        piecewise day-weighted average over every source/target rate change
        is deterministic, audit-visible, and avoids applying the period-end
        closing rate to the whole flow.
        """
        options = options or {}
        presentation_currency_id = options.get(
            'presentation_currency_id')
        if not presentation_currency_id:
            return None
        try:
            presentation_currency_id = int(presentation_currency_id)
            company_ids = [int(company_id) for company_id in company_ids]
        except (TypeError, ValueError):
            return None
        presentation = self.env['res.currency'].sudo().browse(
            presentation_currency_id).exists()
        if not presentation:
            return None
        date_from = fields.Date.to_date(date_from)
        date_to = fields.Date.to_date(date_to)
        probe = CurrencyTable(
            self.env,
            company_ids=company_ids,
            presentation_currency_id=presentation_currency_id,
            as_of_date=date_from,
        )
        if probe.is_monocurrency:
            return _PeriodAverageCurrencyTable(
                self.env,
                company_ids=company_ids,
                presentation_currency_id=presentation_currency_id,
                as_of_date=date_to,
                date_from=date_from,
                rate_map={company_id: 1.0 for company_id in company_ids},
            )

        companies = self.env['res.company'].sudo().browse(company_ids)
        Currency = self.env['res.currency'].sudo()
        Rate = self.env['res.currency.rate'].sudo()
        rate_map = {}
        evidence = {}
        for company in companies:
            source = company.currency_id
            if source.id == presentation_currency_id:
                rate_map[company.id] = 1.0
                evidence[company.id] = []
                continue

            # Fail closed at the first day before asking core for rates; this
            # blocks its future-rate/identity fallbacks just like the shared
            # closing-spot CurrencyTable.
            company_probe = CurrencyTable(
                self.env,
                company_ids=[company.id],
                presentation_currency_id=presentation_currency_id,
                as_of_date=date_from,
            )
            company_probe.rate_map
            root = getattr(company, 'root_id', False) or company
            changes = Rate.search([
                ('currency_id', 'in', [source.id, presentation.id]),
                ('company_id', 'in', [False, root.id]),
                ('name', '>', date_from),
                ('name', '<=', date_to),
            ]).mapped('name')
            boundaries = sorted({date_from, *(
                fields.Date.to_date(value) for value in changes)})
            total_days = (date_to - date_from).days + 1
            weighted = 0.0
            components = []
            for index, boundary in enumerate(boundaries):
                next_boundary = (
                    boundaries[index + 1]
                    if index + 1 < len(boundaries)
                    else date_to + datetime.timedelta(days=1)
                )
                days = (next_boundary - boundary).days
                rate = Currency._get_conversion_rate(
                    source, presentation, company, boundary)
                try:
                    rate = float(rate)
                except (TypeError, ValueError, OverflowError):
                    rate = 0.0
                if not math.isfinite(rate) or rate <= 0.0:
                    raise UserError(_(
                        "No valid %(source)s to %(target)s exchange rate "
                        "exists for %(company)s on %(date)s.",
                        source=source.display_name,
                        target=presentation.display_name,
                        company=company.display_name,
                        date=fields.Date.to_string(boundary),
                    ))
                weighted += rate * days
                components.append({
                    'from': fields.Date.to_string(boundary),
                    'days': days,
                    'rate': rate,
                })
            rate_map[company.id] = weighted / total_days
            evidence[company.id] = components

        table = _PeriodAverageCurrencyTable(
            self.env,
            company_ids=company_ids,
            presentation_currency_id=presentation_currency_id,
            as_of_date=date_to,
            date_from=date_from,
            rate_map=rate_map,
            components=evidence,
        )
        table.rate_map
        return table

    @api.model
    def compute(self, options):
        date_from = self._extract_date(options, 'date_from')
        date_to = self._extract_date(options, 'date_to')
        company_ids = options.get('company_ids') or [self.env.company.id]
        posted_only = bool(options.get('posted_only', True))
        show_zero = bool(options.get('show_zero', False))
        comparison = options.get('comparison') or 'none'
        comparison_number = self._eh_normalize_comparison_number(
            options.get('comparison_number', 1),
        )
        currency_table = self._resolve_flow_currency_table(
            options, company_ids, date_from, date_to,
        )
        presentation_converted = bool(
            currency_table is not None
            and not currency_table.is_monocurrency
        )
        currency = self._eh_monetary_currency(
            options=options,
            company_ids=company_ids,
            presentation_converted=presentation_converted,
        )
        if comparison_number > self._MAX_COMPARISON_PERIODS:
            raise UserError(_(
                "Reports support at most %(maximum)s comparison periods.",
                maximum=self._MAX_COMPARISON_PERIODS,
            ))

        if self._eh_column_axis_requested(options, allow_analytic=True):
            return self._compute_column_axis(
                options=options,
                date_from=date_from,
                date_to=date_to,
                company_ids=company_ids,
                posted_only=posted_only,
                show_zero=show_zero,
                currency=currency,
                currency_table=currency_table,
            )

        lines, totals = self._build_period_lines(
            options=options,
            company_ids=company_ids,
            date_from=date_from, date_to=date_to,
            posted_only=posted_only, show_zero=show_zero,
            currency=currency, currency_table=currency_table,
        )

        meta = {
            'report_code': self.REPORT_CODE,
            'date_from': self._iso_date(date_from),
            'date_to': self._iso_date(date_to),
            'company_ids': sorted(int(c) for c in company_ids),
            'posted_only': posted_only,
            'show_zero': show_zero,
            'comparison': comparison,
            **self._presentation_currency_meta(currency_table),
        }

        # N-period (more than one prior period): side-by-side amount
        # columns rather than the single current/prior/variance layout.
        if comparison != 'none' and comparison_number > 1:
            periods = self._resolve_comparison_periods(
                comparison, date_from, date_to, comparison_number)
            if periods:
                prior_line_lists = []
                period_labels = []
                prior_totals_by_index = {}
                for index, (prior_from, prior_to, _plabel) in enumerate(
                        periods, start=1):
                    prior_currency_table = self._resolve_flow_currency_table(
                        options, company_ids, prior_from, prior_to,
                    )
                    prior_lines, prior_totals = self._build_period_lines(
                        options=options, company_ids=company_ids,
                        date_from=prior_from, date_to=prior_to,
                        posted_only=posted_only, show_zero=show_zero,
                        currency=currency,
                        currency_table=prior_currency_table,
                    )
                    prior_line_lists.append(prior_lines)
                    period_labels.append("%s to %s" % (
                        self._iso_date(prior_from),
                        self._iso_date(prior_to)))
                    prior_totals_by_index[
                        'prior_%d' % index] = prior_totals['net_profit']
                    period_meta = prior_currency_table and (
                        prior_currency_table.period_metadata(
                            'prior_%d' % index,
                        )
                    )
                    if period_meta:
                        meta.setdefault(
                            'currency_translation_periods',
                            [currency_table.period_metadata('current')],
                        ).append(period_meta)
                merged = self.merge_n_period_lines(lines, prior_line_lists)
                meta['comparison_number'] = comparison_number
                meta['comparison_periods'] = [
                    {'from': self._iso_date(pf), 'to': self._iso_date(pt)}
                    for pf, pt, _l in periods
                ]
                totals_payload = {
                    'income': totals['income'],
                    'expenses': totals['expenses'],
                    'net_profit': totals['net_profit'],
                    'amount': totals['net_profit'],
                }
                totals_payload.update(prior_totals_by_index)
                return {
                    'columns': self._build_n_period_column_layout(
                        _("%s to %s") % (
                            self._iso_date(date_from),
                            self._iso_date(date_to)),
                        period_labels,
                    ),
                    'lines': merged,
                    'totals': totals_payload,
                    'generated_at': fields.Datetime.now().isoformat(),
                    'meta': meta,
                }

        # Horizontal column groups: one amount column per company.
        if options.get('horizontal_group_by') == 'company' and (
                len(company_ids) > 1):
            group_line_lists = []
            group_labels = []
            group_totals = {}
            for index, company_id in enumerate(company_ids, start=1):
                group_lines, group_t = self._build_period_lines(
                    options=options, company_ids=[company_id],
                    date_from=date_from, date_to=date_to,
                    posted_only=posted_only, show_zero=show_zero,
                    currency=currency, currency_table=currency_table,
                )
                group_line_lists.append(group_lines)
                group_labels.append(
                    self.env['res.company'].browse(company_id).name)
                group_totals['group_%d' % index] = group_t['net_profit']
            merged = self.merge_horizontal_groups(
                group_line_lists, currency=currency,
            )
            meta['horizontal_group_by'] = 'company'
            totals_payload = {
                'net_profit': totals['net_profit'],
                'amount': totals['net_profit'],
            }
            totals_payload.update(group_totals)
            return {
                'columns': self._build_horizontal_column_layout(group_labels),
                'lines': merged,
                'totals': totals_payload,
                'generated_at': fields.Datetime.now().isoformat(),
                'meta': meta,
            }

        if comparison and comparison != 'none':
            prior_from, prior_to, prior_label = self._resolve_comparison_dates(
                comparison, date_from, date_to,
            )
            if prior_from and prior_to:
                prior_currency_table = self._resolve_flow_currency_table(
                    options, company_ids, prior_from, prior_to,
                )
                prior_lines, prior_totals = self._build_period_lines(
                    options=options,
                    company_ids=company_ids,
                    date_from=prior_from, date_to=prior_to,
                    posted_only=posted_only, show_zero=show_zero,
                    currency=currency,
                    currency_table=prior_currency_table,
                )
                merged = self.merge_comparative_lines(
                    lines, prior_lines, currency=currency,
                )
                meta['prior_date_from'] = self._iso_date(prior_from)
                meta['prior_date_to'] = self._iso_date(prior_to)
                meta['comparison_label'] = prior_label
                prior_period_meta = prior_currency_table and (
                    prior_currency_table.period_metadata('prior_1')
                )
                if prior_period_meta:
                    meta['currency_translation_periods'] = [
                        currency_table.period_metadata('current'),
                        prior_period_meta,
                    ]
                return {
                    'columns': self._build_comparative_column_layout(
                        label_name=_("Account"),
                        current_label=_("%s to %s") % (
                            self._iso_date(date_from),
                            self._iso_date(date_to),
                        ),
                        prior_label=_("%s to %s") % (
                            self._iso_date(prior_from),
                            self._iso_date(prior_to),
                        ),
                    ),
                    'lines': merged,
                    'totals': {
                        'income': totals['income'],
                        'expenses': totals['expenses'],
                        'net_profit': totals['net_profit'],
                        'amount': totals['net_profit'],
                        'prior_income': prior_totals['income'],
                        'prior_expenses': prior_totals['expenses'],
                        'prior_net_profit': prior_totals['net_profit'],
                    },
                    'generated_at': fields.Datetime.now().isoformat(),
                    'meta': meta,
                }

        return {
            'columns': self._build_two_column_layout(),
            'lines': lines,
            'totals': {
                'income': totals['income'],
                'expenses': totals['expenses'],
                'net_profit': totals['net_profit'],
                'amount': totals['net_profit'],
            },
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': meta,
        }

    @api.model
    def _compute_column_axis(
        self, options, date_from, date_to, company_ids, posted_only,
        show_zero, currency, currency_table,
    ):
        """Compute period x analytic columns without overlapping totals.

        Every cell is built by running the normal P&L engine under one
        deterministic scope.  The Total column is therefore an independent
        baseline query; it is never the sum of analytic columns, which may
        overlap or leave part of a distribution unallocated.
        """
        periods = self._eh_resolve_period_scopes(
            options, date_from, date_to,
            snapshot=False, max_periods=self._MAX_COMPARISON_PERIODS,
        )
        analytics = self._eh_resolve_analytic_column_scopes(
            options, company_ids,
        )
        value_scopes = self._eh_build_value_scopes(
            periods, analytics, include_total=True,
        )
        scoped_results = []
        result_totals = {}
        translation_periods = {}
        current_summary = None
        for value_scope in value_scopes:
            scoped_options = self._eh_scope_options(options, value_scope)
            scoped_options['lazy_expand'] = False
            scope_from = self._extract_date(scoped_options, 'date_from')
            scope_to = self._extract_date(scoped_options, 'date_to')
            scope_currency_table = self._resolve_flow_currency_table(
                scoped_options, company_ids, scope_from, scope_to,
            )
            scope_lines, scope_totals = self._build_period_lines(
                options=scoped_options,
                company_ids=company_ids,
                date_from=scope_from,
                date_to=scope_to,
                posted_only=posted_only,
                show_zero=show_zero,
                currency=currency,
                currency_table=scope_currency_table,
            )
            scoped_results.append({
                'scope': value_scope,
                'lines': scope_lines,
                'totals': scope_totals,
            })
            result_totals[value_scope['key']] = scope_totals
            if (
                value_scope.get('period_key') == 'period_current'
                and (not analytics or value_scope.get('is_total'))
            ):
                current_summary = scope_totals
            period_key = value_scope.get('period_key')
            period_meta = scope_currency_table and (
                scope_currency_table.period_metadata(period_key)
            )
            if period_meta and period_key not in translation_periods:
                translation_periods[period_key] = period_meta

        merged = self.merge_scoped_results(
            scoped_results,
            options=options,
            presentation_converted=bool(
                currency_table is not None
                and not currency_table.is_monocurrency
            ),
            currency=currency,
            total_key='net_profit',
        )
        current_summary = current_summary or {
            'income': 0.0, 'expenses': 0.0, 'net_profit': 0.0,
        }
        totals = {
            'income': current_summary['income'],
            'expenses': current_summary['expenses'],
            'net_profit': current_summary['net_profit'],
            'amount': current_summary['net_profit'],
            'column_scopes': result_totals,
        }
        totals.update(merged['totals'])
        meta = {
            'report_code': self.REPORT_CODE,
            'date_from': self._iso_date(date_from),
            'date_to': self._iso_date(date_to),
            'company_ids': sorted(int(c) for c in company_ids),
            'posted_only': posted_only,
            'show_zero': show_zero,
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
            'column_axis': True,
            **self._presentation_currency_meta(currency_table),
        }
        if translation_periods:
            meta['currency_translation_periods'] = [
                translation_periods[period['key']]
                for period in periods
                if period['key'] in translation_periods
            ]
        payload = {
            'columns': self._eh_build_scope_column_layout(
                value_scopes, label_name=_("Account"),
            ),
            'lines': merged['lines'],
            'totals': totals,
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': meta,
        }
        header_rows = self._eh_axis_header_rows(
            value_scopes, analytics, label=_("Account"),
        )
        if header_rows:
            payload['column_header_rows'] = header_rows
        return payload

    @api.model
    def _eh_axis_header_rows(self, value_scopes, analytics, label):
        """Two-level period / analytic headers for product columns."""
        if not analytics:
            return None
        period_groups = []
        for value_scope in value_scopes:
            period_key = value_scope['period_key']
            if not period_groups or period_groups[-1][0] != period_key:
                period_groups.append([
                    period_key, value_scope['period_label'], 0,
                ])
            period_groups[-1][2] += 1
        return [
            [
                {'name': label, 'colspan': 1, 'rowspan': 2},
                *[
                    {'name': period_label, 'colspan': colspan, 'rowspan': 1}
                    for _period_key, period_label, colspan in period_groups
                ],
            ],
            [
                {
                    'name': value_scope['analytic_label'],
                    'colspan': 1,
                    'rowspan': 1,
                }
                for value_scope in value_scopes
            ],
        ]

    @api.model
    def _build_period_lines(
        self, options, company_ids, date_from, date_to,
        posted_only, show_zero, currency=None, currency_table=None,
    ):
        """Compute one period's section lines and totals.

        Dispatches on options['pnl_presentation']:

        * 'by_nature' (the default) keeps the classic Income / Expenses /
          Net Profit layout unchanged.
        * 'by_function' emits the IAS 1.82/85 subtotals (Gross Profit,
          Operating Profit, Profit Before Tax, Profit for the Period).

        Both branches return the same totals keys ('income', 'expenses',
        'net_profit') so the comparison, N-period, and horizontal-group
        code paths in compute() are presentation-agnostic.
        """
        presentation = options.get('pnl_presentation') or 'by_nature'
        if presentation == 'by_function':
            return self._build_period_lines_by_function(
                options=options, company_ids=company_ids,
                date_from=date_from, date_to=date_to,
                posted_only=posted_only, show_zero=show_zero,
                currency=currency, currency_table=currency_table,
            )
        return self._build_period_lines_by_nature(
            options=options, company_ids=company_ids,
            date_from=date_from, date_to=date_to,
            posted_only=posted_only, show_zero=show_zero,
            currency=currency, currency_table=currency_table,
        )

    @api.model
    def _build_period_lines_by_nature(
        self, options, company_ids, date_from, date_to,
        posted_only, show_zero, currency=None, currency_table=None,
    ):
        """Compute one period's section lines and totals."""
        income_rows = self._fetch_grouped_account_totals(
            account_types=self.INCOME_TYPES, sign=-1,
            company_ids=company_ids,
            date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
            currency_table=currency_table,
        )
        expense_rows = self._fetch_grouped_account_totals(
            account_types=self.EXPENSE_TYPES, sign=+1,
            company_ids=company_ids,
            date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
            currency_table=currency_table,
        )

        currency = currency or self._eh_monetary_currency(
            options=options,
            company_ids=company_ids,
            presentation_converted=True,
        )
        income_total = self._eh_round_monetary(
            sum(r['amount'] for r in income_rows), currency=currency)
        expense_total = self._eh_round_monetary(
            sum(r['amount'] for r in expense_rows), currency=currency)
        net_profit = self._eh_round_monetary(
            income_total - expense_total, currency=currency)

        hierarchical = bool(options.get('hierarchical_groups', True))
        unfolded_ids = set(options.get('unfolded_lines') or [])

        def _render(rows, section_id):
            if hierarchical:
                rendered = self._render_account_lines_grouped(
                    rows, section_id=section_id, show_zero=show_zero,
                    unfolded_ids=unfolded_ids, options=options,
                    currency=currency,
                )
            else:
                rendered = self._render_account_lines(
                    rows, show_zero, options=options, currency=currency)
            # Current/deferred tax are nested under the Tax Expense header
            # without their own visible headers. Preserve explicit section
            # ownership on every account/group row so downstream additive
            # consumers can reconcile each child subtotal without guessing
            # from display order.
            for line in rendered:
                line.setdefault('meta', {})['section_id'] = section_id
            return rendered

        def _tag_higher_is_better(line_list, flag):
            # Optional directional hint for the WS5 viewer's variance
            # colouring: income lines read favourable when they RISE
            # (higher_is_better=True), expense lines when they FALL
            # (higher_is_better=False). Stamped on meta so the client can
            # colour a comparison column by favourability rather than raw
            # sign; absent it, the client falls back to sign-only (never
            # worse than before). Additive: never removes existing meta keys.
            for ln in line_list:
                meta = ln.setdefault('meta', {})
                meta['higher_is_better'] = flag
            return line_list

        income_lines = _tag_higher_is_better(_render(income_rows, 'income'), True)
        expense_lines = _tag_higher_is_better(_render(expense_rows, 'expenses'), False)

        lines = []
        income_header = self._section_header_line(_("Income"), section_id='income')
        income_header.setdefault('meta', {})['higher_is_better'] = True
        lines.append(income_header)
        lines.extend(income_lines)
        income_total_line = self._section_total_line(
            _("Total Income"), income_total, section_id='income',
            currency=currency,
        )
        income_total_line.setdefault('meta', {})['higher_is_better'] = True
        lines.append(income_total_line)
        expense_header = self._section_header_line(
            _("Expenses"), section_id='expenses',
        )
        expense_header.setdefault('meta', {})['higher_is_better'] = False
        lines.append(expense_header)
        lines.extend(expense_lines)
        expense_total_line = self._section_total_line(
            _("Total Expenses"), expense_total, section_id='expenses',
            currency=currency,
        )
        expense_total_line.setdefault('meta', {})['higher_is_better'] = False
        lines.append(expense_total_line)
        net_line = self._computed_line(
            'net_profit', _("Net Profit"), net_profit, kind='net_profit',
            currency=currency,
        )
        # A higher net profit is favourable.
        net_line.setdefault('meta', {})['higher_is_better'] = True
        lines.append(net_line)
        return lines, {
            'income': income_total,
            'expenses': expense_total,
            'net_profit': net_profit,
        }

    @api.model
    def _build_period_lines_by_function(
        self, options, company_ids, date_from, date_to,
        posted_only, show_zero, currency=None, currency_table=None,
    ):
        """By-function income statement with IAS 1.82/85 subtotals.

        Section order:

        * Revenue (INCOME_TYPES)
        * Cost of Sales (expense_direct_cost)
        * Gross Profit = Revenue - Cost of Sales (computed)
        * Operating Expenses (expense, expense_other,
          expense_depreciation) less any
          accounts mapped to Finance Costs or Tax Expense
        * Operating Profit = Gross Profit - Operating Expenses (computed)
        * Finance Costs (per-company mapping; zero when unmapped)
        * Profit Before Tax = Operating Profit - Finance Costs (computed)
        * Tax Expense (per-company mapping; zero when unmapped). When any
          deferred-tax account is mapped
          (res.company.eh_pnl_deferred_tax_account_ids), this is split into
          a Current Tax and a Deferred Tax line (IAS 1.82 / IAS 12.81(c))
          that sum to the total tax; otherwise a single Tax Expense line
          is shown.
        * Profit for the Period = Profit Before Tax - Tax Expense

        Finance Costs and Tax Expense are carved out of Operating Expenses
        so nothing is double counted, which keeps Profit for the Period
        identical to the by-nature Net Profit
        (Revenue - all expenses). The totals payload mirrors the by-nature
        branch (income / expenses / net_profit) so compute() is agnostic.
        """
        income_rows = self._fetch_grouped_account_totals(
            account_types=self.INCOME_TYPES, sign=-1,
            company_ids=company_ids,
            date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
            currency_table=currency_table,
        )
        cos_rows = self._fetch_grouped_account_totals(
            account_types=self.COST_OF_SALES_TYPES, sign=+1,
            company_ids=company_ids,
            date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
            currency_table=currency_table,
        )
        opex_candidate_rows = self._fetch_grouped_account_totals(
            account_types=self.OPERATING_EXPENSE_TYPES, sign=+1,
            company_ids=company_ids,
            date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
            currency_table=currency_table,
        )

        # Resolve the Finance Cost / Tax Expense account carve-outs from the
        # per-company mapping. With more than one company in scope, the union
        # of every in-scope company's mapping is used; unmapped -> empty set.
        companies = self.env['res.company'].sudo().browse(
            [int(c) for c in company_ids])
        finance_ids = set(companies.mapped(
            'eh_pnl_finance_cost_account_ids').ids)
        tax_ids = set(companies.mapped('eh_pnl_tax_expense_account_ids').ids)
        # Deferred-tax accounts are a subset of the tax mapping (IAS 12.81(c)):
        # the deferred portion of Tax Expense. Finance Costs is carved out
        # first, so an account mapped to both finance and deferred tax is a
        # finance cost only, never counted as tax.
        deferred_tax_ids = set(companies.mapped(
            'eh_pnl_deferred_tax_account_ids').ids) & tax_ids
        deferred_tax_ids -= finance_ids
        # An account mapped to both buckets must land in exactly one, or it is
        # subtracted twice (finance_total and tax_total), double counted in
        # expense_total, and emits a duplicate 'account-<id>' line. Finance
        # Costs wins so the account is carved out once; Tax Expense drops it.
        tax_ids -= finance_ids
        # Current tax = the tax mapping less the deferred-tax subset.
        current_tax_ids = tax_ids - deferred_tax_ids

        finance_rows = [
            r for r in opex_candidate_rows if r['account_id'] in finance_ids]
        tax_rows = [
            r for r in opex_candidate_rows if r['account_id'] in tax_ids]
        deferred_tax_rows = [
            r for r in tax_rows if r['account_id'] in deferred_tax_ids]
        current_tax_rows = [
            r for r in tax_rows if r['account_id'] in current_tax_ids]
        # Operating Expenses excludes anything mapped to Finance or Tax so
        # the subtotals do not overlap.
        carved_ids = finance_ids | tax_ids
        opex_rows = [
            r for r in opex_candidate_rows
            if r['account_id'] not in carved_ids]

        currency = currency or self._eh_monetary_currency(
            options=options,
            company_ids=company_ids,
            presentation_converted=True,
        )
        revenue_total = self._eh_round_monetary(
            sum(r['amount'] for r in income_rows), currency=currency)
        cos_total = self._eh_round_monetary(
            sum(r['amount'] for r in cos_rows), currency=currency)
        opex_total = self._eh_round_monetary(
            sum(r['amount'] for r in opex_rows), currency=currency)
        finance_total = self._eh_round_monetary(
            sum(r['amount'] for r in finance_rows), currency=currency)
        tax_total = self._eh_round_monetary(
            sum(r['amount'] for r in tax_rows), currency=currency)
        deferred_tax_total = self._eh_round_monetary(
            sum(r['amount'] for r in deferred_tax_rows), currency=currency)
        current_tax_total = self._eh_round_monetary(
            sum(r['amount'] for r in current_tax_rows), currency=currency)

        gross_profit = self._eh_round_monetary(
            revenue_total - cos_total, currency=currency)
        operating_profit = self._eh_round_monetary(
            gross_profit - opex_total, currency=currency)
        profit_before_tax = self._eh_round_monetary(
            operating_profit - finance_total, currency=currency)
        profit_for_period = self._eh_round_monetary(
            profit_before_tax - tax_total, currency=currency)

        # Total expenses of every kind, for the totals payload parity with
        # the by-nature branch (Net Profit == Revenue - all expenses).
        expense_total = self._eh_round_monetary(
            cos_total + opex_total + finance_total + tax_total,
            currency=currency,
        )

        hierarchical = bool(options.get('hierarchical_groups', True))
        unfolded_ids = set(options.get('unfolded_lines') or [])

        def _render(rows, section_id):
            if hierarchical:
                rendered = self._render_account_lines_grouped(
                    rows, section_id=section_id, show_zero=show_zero,
                    unfolded_ids=unfolded_ids, options=options,
                    currency=currency,
                )
            else:
                rendered = self._render_account_lines(
                    rows, show_zero, options=options, currency=currency)
            # Current/deferred tax are nested under one visible Tax Expense
            # header. Stamp their otherwise headerless ownership so typed
            # forecast reconciliation can keep each leaf/subtotal footed.
            for line in rendered:
                line.setdefault('meta', {}).setdefault(
                    'section_id', section_id,
                )
            return rendered

        def _tag(line_list, flag):
            for ln in line_list:
                ln.setdefault('meta', {})['higher_is_better'] = flag
            return line_list

        lines = []

        # Revenue.
        revenue_header = self._section_header_line(
            _("Revenue"), section_id='income')
        revenue_header.setdefault('meta', {})['higher_is_better'] = True
        lines.append(revenue_header)
        lines.extend(_tag(_render(income_rows, 'income'), True))
        revenue_total_line = self._section_total_line(
            _("Total Revenue"), revenue_total, section_id='income',
            currency=currency)
        revenue_total_line.setdefault('meta', {})['higher_is_better'] = True
        lines.append(revenue_total_line)

        # Cost of Sales.
        cos_header = self._section_header_line(
            _("Cost of Sales"), section_id='cost_of_sales')
        cos_header.setdefault('meta', {})['higher_is_better'] = False
        lines.append(cos_header)
        lines.extend(_tag(_render(cos_rows, 'cost_of_sales'), False))
        cos_total_line = self._section_total_line(
            _("Total Cost of Sales"), cos_total, section_id='cost_of_sales',
            currency=currency)
        cos_total_line.setdefault('meta', {})['higher_is_better'] = False
        lines.append(cos_total_line)

        # Gross Profit.
        gross_line = self._computed_line(
            'gross_profit', _("Gross Profit"), gross_profit,
            kind='subtotal', currency=currency)
        gross_line.setdefault('meta', {})['higher_is_better'] = True
        lines.append(gross_line)

        # Operating Expenses.
        opex_header = self._section_header_line(
            _("Operating Expenses"), section_id='operating_expenses')
        opex_header.setdefault('meta', {})['higher_is_better'] = False
        lines.append(opex_header)
        lines.extend(_tag(_render(opex_rows, 'operating_expenses'), False))
        opex_total_line = self._section_total_line(
            _("Total Operating Expenses"), opex_total,
            section_id='operating_expenses', currency=currency)
        opex_total_line.setdefault('meta', {})['higher_is_better'] = False
        lines.append(opex_total_line)

        # Operating Profit.
        operating_line = self._computed_line(
            'operating_profit', _("Operating Profit"), operating_profit,
            kind='subtotal', currency=currency)
        operating_line.setdefault('meta', {})['higher_is_better'] = True
        lines.append(operating_line)

        # Finance Costs.
        finance_header = self._section_header_line(
            _("Finance Costs"), section_id='finance_costs')
        finance_header.setdefault('meta', {})['higher_is_better'] = False
        lines.append(finance_header)
        lines.extend(_tag(_render(finance_rows, 'finance_costs'), False))
        finance_total_line = self._section_total_line(
            _("Total Finance Costs"), finance_total,
            section_id='finance_costs', currency=currency)
        finance_total_line.setdefault('meta', {})['higher_is_better'] = False
        lines.append(finance_total_line)

        # Profit Before Tax.
        pbt_line = self._computed_line(
            'profit_before_tax', _("Profit Before Tax"), profit_before_tax,
            kind='subtotal', currency=currency)
        pbt_line.setdefault('meta', {})['higher_is_better'] = True
        lines.append(pbt_line)

        # Tax Expense (IAS 1.82 / IAS 12.81(c)). When any deferred-tax account
        # is mapped, the Tax Expense subtotal is split into Current Tax and
        # Deferred Tax lines that sum to the total tax; otherwise a single Tax
        # Expense line is shown exactly as before. Both presentations carve the
        # same accounts out of Operating Expenses, so Profit for the Period is
        # identical either way.
        tax_header = self._section_header_line(
            _("Tax Expense"), section_id='tax_expense')
        tax_header.setdefault('meta', {})['higher_is_better'] = False
        lines.append(tax_header)
        if deferred_tax_ids:
            # Current Tax.
            lines.extend(_tag(
                _render(current_tax_rows, 'current_tax'), False))
            current_tax_line = self._section_total_line(
                _("Current Tax"), current_tax_total,
                section_id='current_tax', currency=currency)
            current_tax_line.setdefault(
                'meta', {})['higher_is_better'] = False
            lines.append(current_tax_line)
            # Deferred Tax.
            lines.extend(_tag(
                _render(deferred_tax_rows, 'deferred_tax'), False))
            deferred_tax_line = self._section_total_line(
                _("Deferred Tax"), deferred_tax_total,
                section_id='deferred_tax', currency=currency)
            deferred_tax_line.setdefault(
                'meta', {})['higher_is_better'] = False
            lines.append(deferred_tax_line)
        else:
            lines.extend(_tag(_render(tax_rows, 'tax_expense'), False))
        tax_total_line = self._section_total_line(
            _("Total Tax Expense"), tax_total, section_id='tax_expense',
            currency=currency)
        tax_total_line.setdefault('meta', {})['higher_is_better'] = False
        lines.append(tax_total_line)

        # Profit for the Period. Kept under the 'net_profit' id so any
        # downstream consumer keyed on that id (annotations, drill-down
        # guards, exports) keeps working across both presentations.
        net_line = self._computed_line(
            'net_profit', _("Profit for the Period"), profit_for_period,
            kind='net_profit', currency=currency)
        net_line.setdefault('meta', {})['higher_is_better'] = True
        lines.append(net_line)

        return lines, {
            'income': revenue_total,
            'expenses': expense_total,
            'net_profit': profit_for_period,
            'revenue': revenue_total,
            'cost_of_sales': cos_total,
            'gross_profit': gross_profit,
            'operating_expenses': opex_total,
            'operating_profit': operating_profit,
            'finance_costs': finance_total,
            'profit_before_tax': profit_before_tax,
            'tax_expense': tax_total,
            'current_tax': current_tax_total,
            'deferred_tax': deferred_tax_total,
        }
