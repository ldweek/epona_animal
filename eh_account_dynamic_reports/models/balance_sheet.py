# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Balance Sheet handler.

Cumulative point in time snapshot at date_to. Three sections (Assets,
Liabilities, Equity) plus three computed lines (Current Year Earnings,
Total Liabilities and Equity, Balance Check).

date_from is intentionally ignored: a balance sheet is a snapshot, not a
period activity report. Only date_to drives the cumulative aggregation.
The wizard still supplies date_from because the same wizard form is shared
across reports; the handler simply does not use it.

Sign convention:

* Asset accounts: debit balance is positive. sign=+1.
* Liability accounts: credit balance is positive in display. sign=-1.
* Equity accounts: credit balance is positive in display. sign=-1.
* Current Year Earnings: sum of -balance over income+expense accounts up
  to date_to. Equivalent to (income - expenses) before year end close.

Balance Check identity:

    Total Assets = Total Liabilities + Total Equity + Current Year Earnings

If Balance Check is not zero, the ledger has unbalanced postings or there
is a date or filter mismatch. The line is shown to the user so any drift
is immediately visible.

Localizations can _inherit this handler to add subsections (Current
Assets, Non Current Assets, etc.) or override the account_type tuples.
"""

from datetime import timedelta

from odoo import _, api, fields, models
from odoo.tools import SQL
from odoo.tools.translate import LazyTranslate

from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery

_lt = LazyTranslate(__name__)

class EhBalanceSheetHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.balance_sheet'
    _inherit = 'eh.account.dynamic.report.handler.sectioned'
    _description = "Balance Sheet report handler"

    REPORT_CODE = 'balance_sheet'
    REPORT_NAME = _lt("Balance Sheet")
    _EH_COLUMN_AXIS_CAPABILITIES = frozenset({
        'comparison', 'analytic_columns',
    })
    _EH_SQL_PRESENTATION_CURRENCY = True
    _EH_ANALYTIC_COLUMN_DRILLDOWN = True
    _EH_ANALYTIC_DRILLDOWN_SNAPSHOT = True

    # IAS 1.60/66 current vs non-current split. The grand ASSET_TYPES /
    # LIABILITY_TYPES tuples remain the union of the two subsections so the
    # grand totals and any localization that reads them are unaffected.
    CURRENT_ASSET_TYPES = (
        'asset_receivable', 'asset_cash', 'asset_current',
        'asset_prepayments',
    )
    NON_CURRENT_ASSET_TYPES = ('asset_non_current', 'asset_fixed')
    ASSET_TYPES = CURRENT_ASSET_TYPES + NON_CURRENT_ASSET_TYPES

    CURRENT_LIABILITY_TYPES = (
        'liability_payable', 'liability_credit_card', 'liability_current',
    )
    NON_CURRENT_LIABILITY_TYPES = ('liability_non_current',)
    LIABILITY_TYPES = CURRENT_LIABILITY_TYPES + NON_CURRENT_LIABILITY_TYPES

    EQUITY_TYPES = ('equity', 'equity_unaffected')
    INCOME_TYPES = ('income', 'income_other')
    EXPENSE_TYPES = (
        'expense', 'expense_other', 'expense_depreciation',
        'expense_direct_cost',
    )

    @api.model
    def _eh_analytic_drilldown_account_types(self):
        return self.ASSET_TYPES + self.LIABILITY_TYPES + self.EQUITY_TYPES

    @api.model
    def expand_account_line(self, options, line_id, offset=0, limit=None):
        """Balance-Sheet override: thread the as-of [epoch, date_to] window.

        A balance sheet is a snapshot, so its account cell is SUM(balance)
        for every line up to date_to (no lower bound). The shared engine
        windows on [date_from, date_to]; here we force date_from to the
        epoch so the paged children reconcile EXACTLY to the as-of cell
        regardless of whatever date_from the shared wizard supplied. Falls
        back to the base behaviour if the date block is malformed.
        """
        try:
            date_block = dict(options.get('date') or {})
            date_block['date_from'] = '0001-01-01'
            options = dict(options, date=date_block)
        except Exception:  # pragma: no cover - defensive
            pass
        return super().expand_account_line(
            options, line_id, offset=offset, limit=limit)

    @api.model
    def get_drilldown_action(self, options, line_id):
        """Open the same cumulative as-of window displayed by the cell."""
        try:
            date_block = dict(options.get('date') or {})
            date_block['date_from'] = '0001-01-01'
            options = dict(options, date=date_block)
        except Exception:  # pragma: no cover - defensive
            pass
        return super().get_drilldown_action(options, line_id)

    @api.model
    def compute(self, options):
        date_to = self._extract_date(options, 'date_to')
        company_ids = options.get('company_ids') or [self.env.company.id]
        posted_only = bool(options.get('posted_only', True))
        show_zero = bool(options.get('show_zero', False))
        comparison = options.get('comparison') or 'none'
        currency_table = self._resolve_currency_table(
            options, company_ids, as_of_date=date_to,
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

        comparison_from = fields.Date.to_date(
            (options.get('date') or {}).get('date_from')
        ) or date_to
        if self._eh_column_axis_requested(options, allow_analytic=True):
            return self._compute_column_axis(
                options=options,
                comparison_from=comparison_from,
                date_to=date_to,
                company_ids=company_ids,
                posted_only=posted_only,
                show_zero=show_zero,
                currency=currency,
                currency_table=currency_table,
            )

        lines, totals = self._build_snapshot_lines(
            options=options, company_ids=company_ids, date_to=date_to,
            posted_only=posted_only, show_zero=show_zero,
            currency=currency, currency_table=currency_table,
        )
        meta = {
            'report_code': self.REPORT_CODE,
            'date_basis': 'as_of',
            'date_to': self._iso_date(date_to),
            'company_ids': sorted(int(c) for c in company_ids),
            'posted_only': posted_only,
            'show_zero': show_zero,
            'comparison': comparison,
            **self._presentation_currency_meta(currency_table),
        }

        if comparison and comparison != 'none':
            # For a snapshot report we compare against an as-of point in
            # the past: end of prior period (period mode) or one year
            # earlier (year mode). date_from does not bound the current
            # balance, but it defines the selected period whose predecessor
            # ends at date_from - 1. Treating date_to as both ends would make
            # "previous period" mean yesterday.
            prior_from, prior_to, prior_label = self._resolve_comparison_dates(
                comparison, comparison_from, date_to,
            )
            if prior_to:
                prior_currency_table = self._resolve_currency_table(
                    options, company_ids, as_of_date=prior_to,
                )
                prior_lines, prior_totals = self._build_snapshot_lines(
                    options=options, company_ids=company_ids,
                    date_to=prior_to,
                    posted_only=posted_only, show_zero=show_zero,
                    currency=currency,
                    currency_table=prior_currency_table,
                )
                merged = self.merge_comparative_lines(
                    lines, prior_lines, currency=currency,
                )
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
                        current_label=_("As at %s") % self._iso_date(date_to),
                        prior_label=_("As at %s") % self._iso_date(prior_to),
                    ),
                    'lines': merged,
                    'totals': dict(totals, **{
                        'prior_assets': prior_totals['assets'],
                        'prior_liabilities': prior_totals['liabilities'],
                        'prior_equity': prior_totals['equity'],
                        'prior_previous_year_earnings': (
                            prior_totals['previous_year_earnings']
                        ),
                        'prior_current_year_earnings': (
                            prior_totals['current_year_earnings']
                        ),
                        'prior_total_equity': prior_totals['total_equity'],
                    }),
                    'generated_at': fields.Datetime.now().isoformat(),
                    'meta': meta,
                }

        return {
            'columns': self._build_two_column_layout(),
            'lines': lines,
            'totals': totals,
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': meta,
        }

    @api.model
    def _compute_column_axis(
        self, options, comparison_from, date_to, company_ids, posted_only,
        show_zero, currency, currency_table,
    ):
        """Compute as-of period x analytic columns from independent reads."""
        periods = self._eh_resolve_period_scopes(
            options, comparison_from, date_to,
            snapshot=True, max_periods=self._MAX_COMPARISON_PERIODS,
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
            scope_to = self._extract_date(scoped_options, 'date_to')
            scope_currency_table = self._resolve_currency_table(
                scoped_options, company_ids, as_of_date=scope_to,
            )
            scope_lines, scope_totals = self._build_snapshot_lines(
                options=scoped_options,
                company_ids=company_ids,
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
            total_key='assets',
        )
        current_summary = current_summary or {
            'assets': 0.0,
            'current_assets': 0.0,
            'non_current_assets': 0.0,
            'liabilities': 0.0,
            'current_liabilities': 0.0,
            'non_current_liabilities': 0.0,
            'equity': 0.0,
            'previous_year_earnings': 0.0,
            'current_year_earnings': 0.0,
            'total_equity': 0.0,
            'total_equity_liabilities': 0.0,
            'balance_check': 0.0,
        }
        totals = dict(current_summary)
        totals['column_scopes'] = result_totals
        totals.update(merged['totals'])
        meta = {
            'report_code': self.REPORT_CODE,
            'date_basis': 'as_of',
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
        if analytics:
            period_groups = []
            for value_scope in value_scopes:
                period_key = value_scope['period_key']
                if not period_groups or period_groups[-1][0] != period_key:
                    period_groups.append([
                        period_key, value_scope['period_label'], 0,
                    ])
                period_groups[-1][2] += 1
            payload['column_header_rows'] = [
                [
                    {'name': _("Account"), 'colspan': 1, 'rowspan': 2},
                    *[
                        {
                            'name': period_label,
                            'colspan': colspan,
                            'rowspan': 1,
                        }
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
        return payload

    @api.model
    def _build_snapshot_lines(
        self, options, company_ids, date_to, posted_only, show_zero,
        currency=None, currency_table=None,
    ):
        current_asset_rows = self._fetch_grouped_account_totals(
            account_types=self.CURRENT_ASSET_TYPES, sign=+1,
            company_ids=company_ids,
            date_to=date_to,
            posted_only=posted_only, options=options,
            currency_table=currency_table,
        )
        non_current_asset_rows = self._fetch_grouped_account_totals(
            account_types=self.NON_CURRENT_ASSET_TYPES, sign=+1,
            company_ids=company_ids,
            date_to=date_to,
            posted_only=posted_only, options=options,
            currency_table=currency_table,
        )
        current_liability_rows = self._fetch_grouped_account_totals(
            account_types=self.CURRENT_LIABILITY_TYPES, sign=-1,
            company_ids=company_ids,
            date_to=date_to,
            posted_only=posted_only, options=options,
            currency_table=currency_table,
        )
        non_current_liability_rows = self._fetch_grouped_account_totals(
            account_types=self.NON_CURRENT_LIABILITY_TYPES, sign=-1,
            company_ids=company_ids,
            date_to=date_to,
            posted_only=posted_only, options=options,
            currency_table=currency_table,
        )
        equity_rows = self._fetch_grouped_account_totals(
            account_types=self.EQUITY_TYPES, sign=-1,
            company_ids=company_ids,
            date_to=date_to,
            posted_only=posted_only, options=options,
            currency_table=currency_table,
        )
        current_year_earnings, previous_year_earnings = (
            self._fetch_fiscal_earnings(
                company_ids=company_ids,
                date_to=date_to,
                posted_only=posted_only,
                options=options, currency_table=currency_table,
            )
        )

        currency = currency or self._eh_monetary_currency(
            options=options,
            company_ids=company_ids,
            presentation_converted=True,
        )
        current_asset_total = self._eh_round_monetary(
            sum(r['amount'] for r in current_asset_rows), currency=currency)
        non_current_asset_total = self._eh_round_monetary(
            sum(r['amount'] for r in non_current_asset_rows),
            currency=currency,
        )
        asset_total = self._eh_round_monetary(
            current_asset_total + non_current_asset_total,
            currency=currency,
        )
        current_liability_total = self._eh_round_monetary(
            sum(r['amount'] for r in current_liability_rows),
            currency=currency,
        )
        non_current_liability_total = self._eh_round_monetary(
            sum(r['amount'] for r in non_current_liability_rows),
            currency=currency,
        )
        liability_total = self._eh_round_monetary(
            current_liability_total + non_current_liability_total,
            currency=currency,
        )
        equity_total = self._eh_round_monetary(
            sum(r['amount'] for r in equity_rows), currency=currency)
        current_year_earnings = self._eh_round_monetary(
            current_year_earnings, currency=currency)
        previous_year_earnings = self._eh_round_monetary(
            previous_year_earnings, currency=currency)
        total_equity = self._eh_round_monetary(
            equity_total + previous_year_earnings + current_year_earnings,
            currency=currency,
        )
        total_equity_liabilities = self._eh_round_monetary(
            liability_total + total_equity, currency=currency,
        )
        balance_check = self._eh_round_monetary(
            asset_total - total_equity_liabilities, currency=currency)

        hierarchical = bool(options.get('hierarchical_groups', True))
        unfolded_ids = set(options.get('unfolded_lines') or [])

        def _render(rows, section_id):
            if hierarchical:
                return self._render_account_lines_grouped(
                    rows, section_id=section_id, show_zero=show_zero,
                    unfolded_ids=unfolded_ids, options=options,
                    currency=currency,
                )
            return self._render_account_lines(
                rows, show_zero, options=options, currency=currency)

        lines = []
        # ---- Assets: current vs non-current (IAS 1.60/66) ----
        lines.append(self._section_header_line(_("Assets"), section_id='assets'))
        lines.append(self._section_header_line(
            _("Current Assets"), section_id='assets_current',
        ))
        lines.extend(_render(current_asset_rows, 'assets_current'))
        lines.append(self._section_total_line(
            _("Total Current Assets"), current_asset_total,
            section_id='assets_current',
            currency=currency,
        ))
        lines.append(self._section_header_line(
            _("Non-Current Assets"), section_id='assets_non_current',
        ))
        lines.extend(_render(non_current_asset_rows, 'assets_non_current'))
        lines.append(self._section_total_line(
            _("Total Non-Current Assets"), non_current_asset_total,
            section_id='assets_non_current',
            currency=currency,
        ))
        lines.append(self._section_total_line(
            _("Total Assets"), asset_total, section_id='assets',
            currency=currency,
        ))
        # ---- Liabilities: current vs non-current (IAS 1.60/69) ----
        lines.append(self._section_header_line(
            _("Liabilities"), section_id='liabilities',
        ))
        lines.append(self._section_header_line(
            _("Current Liabilities"), section_id='liabilities_current',
        ))
        lines.extend(_render(current_liability_rows, 'liabilities_current'))
        lines.append(self._section_total_line(
            _("Total Current Liabilities"), current_liability_total,
            section_id='liabilities_current',
            currency=currency,
        ))
        lines.append(self._section_header_line(
            _("Non-Current Liabilities"),
            section_id='liabilities_non_current',
        ))
        lines.extend(_render(
            non_current_liability_rows, 'liabilities_non_current'))
        lines.append(self._section_total_line(
            _("Total Non-Current Liabilities"), non_current_liability_total,
            section_id='liabilities_non_current',
            currency=currency,
        ))
        lines.append(self._section_total_line(
            _("Total Liabilities"), liability_total, section_id='liabilities',
            currency=currency,
        ))
        lines.append(self._section_header_line(_("Equity"), section_id='equity'))
        lines.extend(_render(equity_rows, 'equity'))
        lines.append(self._computed_line(
            'previous_year_earnings', _("Previous Years Earnings"),
            previous_year_earnings, kind='previous_year_earnings',
            currency=currency,
        ))
        lines.append(self._computed_line(
            'current_year_earnings', _("Current Year Earnings"),
            current_year_earnings, kind='current_year_earnings',
            currency=currency,
        ))
        lines.append(self._section_total_line(
            _("Total Equity"), total_equity, section_id='equity',
            currency=currency,
        ))
        lines.append(self._computed_line(
            'total_equity_liabilities', _("Total Liabilities and Equity"),
            total_equity_liabilities, kind='computed_total',
            currency=currency,
        ))
        lines.append(self._computed_line(
            'balance_check', _("Balance Check"),
            balance_check, kind='balance_check',
            currency=currency,
        ))
        return lines, {
            'assets': asset_total,
            'current_assets': current_asset_total,
            'non_current_assets': non_current_asset_total,
            'liabilities': liability_total,
            'current_liabilities': current_liability_total,
            'non_current_liabilities': non_current_liability_total,
            'equity': equity_total,
            'previous_year_earnings': previous_year_earnings,
            'current_year_earnings': current_year_earnings,
            'total_equity': total_equity,
            'total_equity_liabilities': total_equity_liabilities,
            'balance_check': balance_check,
        }

    @api.model
    def _fetch_fiscal_earnings(
        self, company_ids, date_to, posted_only, options,
        currency_table=None,
    ):
        """Return (current-FY, previous-years) P&L, presentation-signed.

        One conditional aggregate preserves mixed fiscal calendars and
        converts each company's ledger balance before consolidation.  Both
        buckets are required: current earnings must be truthful, while older
        unclosed P&L still belongs in equity so the statement keeps balancing.
        """
        currency_table = currency_table or self._resolve_currency_table(
            options, company_ids, as_of_date=date_to,
        )
        if options.get('cash_basis'):
            current = 0.0
            previous = 0.0
            fiscal_starts = self._fiscalyear_starts(company_ids, date_to)
            for company_id in company_ids:
                fiscal_start = fiscal_starts[int(company_id)]
                current_rows = self._cash_basis_grouped_totals(
                    account_types=self.INCOME_TYPES + self.EXPENSE_TYPES,
                    company_ids=[company_id],
                    date_from=fiscal_start,
                    date_to=date_to,
                    posted_only=posted_only,
                    options=options,
                    sign=-1,
                    currency_table=currency_table,
                )
                previous_rows = self._cash_basis_grouped_totals(
                    account_types=self.INCOME_TYPES + self.EXPENSE_TYPES,
                    company_ids=[company_id],
                    date_from=None,
                    date_to=fiscal_start - timedelta(days=1),
                    posted_only=posted_only,
                    options=options,
                    sign=-1,
                    currency_table=currency_table,
                )
                current += sum(
                    float(row.get('amount') or 0.0)
                    for row in current_rows)
                previous += sum(
                    float(row.get('amount') or 0.0)
                    for row in previous_rows)
            return current, previous
        query = MoveLineQuery(
            self.env,
            company_ids=company_ids,
            currency_table=currency_table,
        )
        query.where_date_range(date_to=date_to)
        if posted_only:
            query.where_posted_only()
        query.where_account_types(self.INCOME_TYPES + self.EXPENSE_TYPES)
        self.apply_common_filters(query, options)
        fiscal_starts = self._fiscalyear_starts(company_ids, date_to)
        fiscal_case = self._fiscalyear_start_case(fiscal_starts, date_to)
        converted = (
            currency_table is not None
            and not currency_table.is_monocurrency
        )
        balance_sql = (
            SQL("(aml.balance) * %s", currency_table.rate_expr())
            if converted else SQL("aml.balance")
        )
        # Conditional fiscal buckets bypass MoveLineQuery's standard monetary
        # selectors, so apply same analytic allocation explicitly.  Without
        # this, 60/40 P&L allocations appear at 100% in both BS analytic
        # columns and current-year earnings no longer balance assets.
        balance_sql = query._analytic_weighted(balance_sql)
        query.select(
            SQL(
                "COALESCE(SUM(CASE WHEN aml.date >= (%s) "
                "THEN (%s) ELSE 0 END), 0)",
                fiscal_case, balance_sql,
            ),
            'current_balance',
        )
        query.select(
            SQL(
                "COALESCE(SUM(CASE WHEN aml.date < (%s) "
                "THEN (%s) ELSE 0 END), 0)",
                fiscal_case, balance_sql,
            ),
            'previous_balance',
        )
        rows = query.execute()
        row = rows[0] if rows else {}
        return (
            -float(row.get('current_balance') or 0.0),
            -float(row.get('previous_balance') or 0.0),
        )
