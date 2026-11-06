# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Executive Summary handler (board-pack KPI / ratio statement).

A single as-of statement that restates the live dashboard numbers into a
printable period summary: profitability, cash and liquidity, and the core
balance-sheet ratios. It reads the same ledger the Profit and Loss, Balance
Sheet and dashboard read, so the figures reconcile to those reports by
construction.

Sign conventions (authored from double-entry first principles):

* Income accounts carry credit balances (negative); revenue and other
  income are flipped with sign=-1 so they present positive.
* Expense accounts carry debit balances (positive); sign=+1.
* Cash and receivable balances are cumulative debit balances up to
  date_to (sign=+1); payables are cumulative credit balances flipped to a
  positive "owed" figure (sign=-1).

Ratio rows mix figure types (monetary, percentage, days).  Each value cell
therefore carries its own ``figure_type`` override while the shared column
remains a generic numeric column.  Consumers fall back to the column type for
legacy payloads.  Values stay numeric through screen, PDF and XLSX rendering,
which also lets presentation-currency conversion identify only money cells.

Profitability is a period flow (date_from..date_to). Cash, receivables and
payables are point-in-time balances cumulative to date_to. This mirrors
dashboard._compute_period_pl (period flow) and the cumulative balance reads
the Balance Sheet uses, so Executive Summary reconciles to the board.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.translate import LazyTranslate

_lt = LazyTranslate(__name__)

class EhExecutiveSummaryHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.executive_summary'
    _inherit = 'eh.account.dynamic.report.handler.sectioned'
    _description = "Executive Summary report handler"

    REPORT_CODE = 'executive_summary'
    REPORT_NAME = _lt("Executive Summary")
    _EH_COLUMN_AXIS_CAPABILITIES = frozenset({'comparison'})
    # Executive Summary mixes period flows with as-of balances.  It owns
    # both translations explicitly: flows use P&L's day-weighted period
    # average, while balance KPIs use the closing spot rate for each scope.
    _EH_SQL_PRESENTATION_CURRENCY = True

    INCOME_TYPES = ('income', 'income_other')
    EXPENSE_TYPES = (
        'expense', 'expense_other', 'expense_depreciation',
        'expense_direct_cost',
    )
    # Direct costs / cost of sales drive the gross-margin split. Authored
    # from the standard income-statement layout, not transcribed.
    COST_OF_SALES_TYPES = ('expense_direct_cost',)
    OPERATING_EXPENSE_TYPES = (
        'expense', 'expense_other', 'expense_depreciation',
    )
    CASH_TYPES = ('asset_cash',)
    RECEIVABLE_TYPES = ('asset_receivable',)
    PAYABLE_TYPES = ('liability_payable',)
    CURRENT_ASSET_TYPES = (
        'asset_cash', 'asset_receivable', 'asset_current', 'asset_prepayments',
    )
    CURRENT_LIABILITY_TYPES = (
        'liability_payable', 'liability_current', 'liability_credit_card',
    )
    QUICK_ASSET_TYPES = ('asset_cash', 'asset_receivable', 'asset_current')
    TOTAL_ASSET_TYPES = (
        'asset_cash', 'asset_receivable', 'asset_current',
        'asset_prepayments', 'asset_non_current', 'asset_fixed',
    )

    # ---- ratio helpers ----

    @staticmethod
    def _safe_ratio(numerator, denominator):
        """Divide guarding against a zero/absent denominator.

        Returns None (not 0.0) when the ratio is undefined so the caller
        can render 'n/a' rather than a misleading zero. Never raises
        ZeroDivisionError.
        """
        try:
            if not denominator:
                return None
            return float(numerator) / float(denominator)
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    @api.model
    def compute(self, options):
        date_from = self._extract_date(options, 'date_from')
        date_to = self._extract_date(options, 'date_to')
        company_ids = options.get('company_ids') or [self.env.company.id]
        posted_only = bool(options.get('posted_only', True))
        comparison = options.get('comparison') or 'none'
        flow_currency_table, snapshot_currency_table = (
            self._resolve_mixed_currency_tables(
                options, company_ids, date_from, date_to,
            )
        )
        presentation_converted = bool(
            flow_currency_table is not None
            or snapshot_currency_table is not None
        )
        currency = self._eh_monetary_currency(
            options=options, company_ids=company_ids,
            presentation_converted=presentation_converted,
        )

        if self._eh_column_axis_requested(options, allow_analytic=False):
            return self._compute_period_axis(
                options=options,
                date_from=date_from,
                date_to=date_to,
                company_ids=company_ids,
                posted_only=posted_only,
                currency=currency,
            )

        current = self._compute_scalars(
            options=options, company_ids=company_ids,
            date_from=date_from, date_to=date_to, posted_only=posted_only,
            currency=currency,
            flow_currency_table=flow_currency_table,
            snapshot_currency_table=snapshot_currency_table,
        )

        meta = {
            'report_code': self.REPORT_CODE,
            'date_from': self._iso_date(date_from),
            'date_to': self._iso_date(date_to),
            'company_ids': sorted(int(c) for c in company_ids),
            'posted_only': posted_only,
            'comparison': comparison,
            **self._mixed_currency_meta(
                flow_currency_table, snapshot_currency_table,
                label='current',
            ),
        }

        prior = None
        prior_label = ''
        if comparison and comparison != 'none':
            prior_from, prior_to, prior_label = self._resolve_comparison_dates(
                comparison, date_from, date_to,
            )
            if prior_from and prior_to:
                prior_flow_table, prior_snapshot_table = (
                    self._resolve_mixed_currency_tables(
                        options, company_ids, prior_from, prior_to,
                    )
                )
                prior = self._compute_scalars(
                    options=options, company_ids=company_ids,
                    date_from=prior_from, date_to=prior_to,
                    posted_only=posted_only,
                    currency=currency,
                    flow_currency_table=prior_flow_table,
                    snapshot_currency_table=prior_snapshot_table,
                )
                meta['prior_date_from'] = self._iso_date(prior_from)
                meta['prior_date_to'] = self._iso_date(prior_to)
                meta['comparison_label'] = prior_label
                prior_translation = self._mixed_currency_period_meta(
                    prior_flow_table, prior_snapshot_table,
                    label='prior_1',
                )
                current_translation = self._mixed_currency_period_meta(
                    flow_currency_table, snapshot_currency_table,
                    label='current',
                )
                if current_translation or prior_translation:
                    meta['currency_translation_periods'] = [
                        item for item in (
                            current_translation, prior_translation,
                        ) if item
                    ]

        lines = self._build_lines(current, prior)
        meta['total_figure_types'] = {
            'revenue': 'monetary',
            'net_profit': 'monetary',
            'amount': 'monetary',
        }
        columns = self._build_columns(
            current_label=_("%s to %s") % (
                self._iso_date(date_from), self._iso_date(date_to)),
            prior_label=prior_label if prior else None,
        )

        return {
            'columns': columns,
            'lines': lines,
            'totals': {
                'revenue': current['revenue'],
                'net_profit': current['net_profit'],
                'amount': current['net_profit'],
            },
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': meta,
        }

    @api.model
    def _compute_period_axis(
        self, options, date_from, date_to, company_ids, posted_only, currency,
    ):
        """Render one complete KPI value column for every period scope."""
        periods = self._eh_resolve_period_scopes(
            options, date_from, date_to,
            snapshot=False, max_periods=self._MAX_COMPARISON_PERIODS,
        )
        value_scopes = self._eh_build_value_scopes(
            periods, [], include_total=False,
        )
        scoped_values = []
        translation_periods = []
        current = None
        for value_scope in value_scopes:
            scoped_options = self._eh_scope_options(options, value_scope)
            scope_from = self._extract_date(scoped_options, 'date_from')
            scope_to = self._extract_date(scoped_options, 'date_to')
            flow_currency_table, snapshot_currency_table = (
                self._resolve_mixed_currency_tables(
                    scoped_options, company_ids, scope_from, scope_to,
                )
            )
            values = self._compute_scalars(
                options=scoped_options,
                company_ids=company_ids,
                date_from=scope_from,
                date_to=scope_to,
                posted_only=posted_only,
                currency=currency,
                flow_currency_table=flow_currency_table,
                snapshot_currency_table=snapshot_currency_table,
            )
            scoped_values.append((value_scope, values))
            translation = self._mixed_currency_period_meta(
                flow_currency_table, snapshot_currency_table,
                label=value_scope['key'],
            )
            if translation:
                translation_periods.append(translation)
            if value_scope.get('period_key') == 'period_current':
                current = values
        current = current or {
            'revenue': 0.0,
            'net_profit': 0.0,
        }
        meta = {
            'report_code': self.REPORT_CODE,
            'date_from': self._iso_date(date_from),
            'date_to': self._iso_date(date_to),
            'company_ids': sorted(int(c) for c in company_ids),
            'posted_only': posted_only,
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
            'total_figure_types': {
                'revenue': 'monetary',
                'net_profit': 'monetary',
                'amount': 'monetary',
            },
            **self._mixed_currency_meta(
                *self._resolve_mixed_currency_tables(
                    options, company_ids, date_from, date_to,
                ),
                label='current',
            ),
        }
        if translation_periods:
            meta['currency_translation_periods'] = translation_periods
        totals = {
            'revenue': current['revenue'],
            'net_profit': current['net_profit'],
            'amount': current['net_profit'],
            'column_scopes': {
                value_scope['key']: values
                for value_scope, values in scoped_values
            },
        }
        totals.update({
            value_scope['key']: values['net_profit']
            for value_scope, values in scoped_values
        })
        return {
            'columns': [
                {
                    'expression_label': 'metric',
                    'name': _("Metric"),
                    'figure_type': 'string',
                },
                *[
                    {
                        'expression_label': value_scope['key'],
                        'name': value_scope['label'],
                        'figure_type': 'float',
                        'scope': value_scope['scope'],
                    }
                    for value_scope, _values in scoped_values
                ],
            ],
            'lines': self._build_axis_lines(scoped_values),
            'totals': totals,
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': meta,
        }

    # ---- column layout ----
    #
    # Built manually (not _build_two_column_layout) because each value column
    # contains a mix of money, percentages, ratios and day counts. ``float``
    # gives the header a numeric alignment; each data cell overrides it with
    # the exact figure type.

    @api.model
    def _build_columns(self, current_label, prior_label=None):
        columns = [
            {'expression_label': 'metric', 'name': _("Metric"),
             'figure_type': 'string'},
            {'expression_label': 'value',
             'name': current_label or _("Value"),
             'figure_type': 'float'},
        ]
        if prior_label:
            columns.append({
                'expression_label': 'prior_value',
                'name': prior_label, 'figure_type': 'float',
            })
        return columns

    # ---- scalar reads ----

    @api.model
    def _resolve_mixed_currency_tables(
        self, options, company_ids, date_from, date_to,
    ):
        """Resolve exact flow-average and snapshot-closing rate tables."""
        flow_table = self.env[
            'eh.account.dynamic.report.handler.profit_and_loss'
        ]._resolve_flow_currency_table(
            options, company_ids, date_from, date_to,
        )
        snapshot_table = self._resolve_currency_table(
            options, company_ids, as_of_date=date_to,
        )
        return flow_table, snapshot_table

    @api.model
    def _mixed_currency_period_meta(
        self, flow_currency_table, snapshot_currency_table, label,
    ):
        if flow_currency_table is None and snapshot_currency_table is None:
            return None
        flow_meta = (
            flow_currency_table.period_metadata(label)
            if flow_currency_table is not None else None
        )
        snapshot_meta = (
            snapshot_currency_table.period_metadata(label)
            if snapshot_currency_table is not None else None
        )
        return {
            'label': label,
            'flow': flow_meta or {'label': label, 'policy': 'identity'},
            'snapshot': snapshot_meta or {
                'label': label, 'policy': 'identity',
            },
        }

    @api.model
    def _mixed_currency_meta(
        self, flow_currency_table, snapshot_currency_table, label,
    ):
        table = flow_currency_table or snapshot_currency_table
        if table is None:
            return {}
        return {
            'presentation_currency_converted': True,
            'presentation_currency_id': table.presentation_currency_id,
            'multi_currency': not table.is_monocurrency,
            'currency_translation_policy': (
                'mixed_flow_average_and_closing_spot'
            ),
            'currency_translation_periods': [
                self._mixed_currency_period_meta(
                    flow_currency_table, snapshot_currency_table, label,
                ),
            ],
        }

    @api.model
    def _compute_scalars(
        self, options, company_ids, date_from, date_to, posted_only,
        currency=None, flow_currency_table=None,
        snapshot_currency_table=None,
    ):
        """Single-pass SUM aggregates, one per KPI. O(1) in result rows.

        Period flow figures (revenue, expenses) use the date window;
        balance figures (cash, AR, AP) are cumulative to date_to via a
        date_to-only window, mirroring the Balance Sheet's snapshot read.
        """
        revenue = self._fetch_aggregate_balance(
            account_types=self.INCOME_TYPES, sign=-1,
            company_ids=company_ids, date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
            currency_table=flow_currency_table,
        )
        cost_of_sales = self._fetch_aggregate_balance(
            account_types=self.COST_OF_SALES_TYPES, sign=+1,
            company_ids=company_ids, date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
            currency_table=flow_currency_table,
        )
        operating_expense = self._fetch_aggregate_balance(
            account_types=self.OPERATING_EXPENSE_TYPES, sign=+1,
            company_ids=company_ids, date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
            currency_table=flow_currency_table,
        )
        total_expense = self._fetch_aggregate_balance(
            account_types=self.EXPENSE_TYPES, sign=+1,
            company_ids=company_ids, date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
            currency_table=flow_currency_table,
        )

        # Balances cumulative to date_to (no date_from). Mirrors the
        # Balance Sheet snapshot read so the figures reconcile.
        cash = self._fetch_aggregate_balance(
            account_types=self.CASH_TYPES, sign=+1,
            company_ids=company_ids, date_from=None, date_to=date_to,
            posted_only=posted_only, options=options,
            currency_table=snapshot_currency_table,
        )
        receivables = self._fetch_aggregate_balance(
            account_types=self.RECEIVABLE_TYPES, sign=+1,
            company_ids=company_ids, date_from=None, date_to=date_to,
            posted_only=posted_only, options=options,
            currency_table=snapshot_currency_table,
        )
        payables = self._fetch_aggregate_balance(
            account_types=self.PAYABLE_TYPES, sign=-1,
            company_ids=company_ids, date_from=None, date_to=date_to,
            posted_only=posted_only, options=options,
            currency_table=snapshot_currency_table,
        )
        current_assets = self._fetch_aggregate_balance(
            account_types=self.CURRENT_ASSET_TYPES, sign=+1,
            company_ids=company_ids, date_from=None, date_to=date_to,
            posted_only=posted_only, options=options,
            currency_table=snapshot_currency_table,
        )
        current_liabilities = self._fetch_aggregate_balance(
            account_types=self.CURRENT_LIABILITY_TYPES, sign=-1,
            company_ids=company_ids, date_from=None, date_to=date_to,
            posted_only=posted_only, options=options,
            currency_table=snapshot_currency_table,
        )
        quick_assets = self._fetch_aggregate_balance(
            account_types=self.QUICK_ASSET_TYPES, sign=+1,
            company_ids=company_ids, date_from=None, date_to=date_to,
            posted_only=posted_only, options=options,
            currency_table=snapshot_currency_table,
        )
        total_assets = self._fetch_aggregate_balance(
            account_types=self.TOTAL_ASSET_TYPES, sign=+1,
            company_ids=company_ids, date_from=None, date_to=date_to,
            posted_only=posted_only, options=options,
            currency_table=snapshot_currency_table,
        )

        currency = currency or self._eh_monetary_currency(
            options=options, company_ids=company_ids)
        gross_profit = self._eh_round_monetary(
            revenue - cost_of_sales, currency=currency)
        operating_profit = self._eh_round_monetary(
            gross_profit - operating_expense, currency=currency)
        net_profit = self._eh_round_monetary(
            revenue - total_expense, currency=currency)
        working_capital = self._eh_round_monetary(
            current_assets - current_liabilities, currency=currency)

        # Average period length in days for DSO / DPO. Inclusive of both
        # endpoints, floored at 1 to avoid a zero-length window.
        try:
            period_days = max(1, (date_to - date_from).days + 1)
        except TypeError:
            period_days = 1

        # DSO / DPO: balance / period-flow * days. Guard the divisions.
        dso = self._safe_ratio(receivables * period_days, revenue)
        # Purchases proxy = total expenses recognised in the period.
        dpo = self._safe_ratio(payables * period_days, total_expense)

        return {
            'revenue': self._eh_round_monetary(
                revenue, currency=currency),
            'cost_of_sales': self._eh_round_monetary(
                cost_of_sales, currency=currency),
            'gross_profit': gross_profit,
            'operating_profit': operating_profit,
            'total_expense': self._eh_round_monetary(
                total_expense, currency=currency),
            'net_profit': net_profit,
            'gross_margin': self._safe_ratio(gross_profit, revenue),
            'operating_margin': self._safe_ratio(operating_profit, revenue),
            'net_margin': self._safe_ratio(net_profit, revenue),
            'cash': self._eh_round_monetary(cash, currency=currency),
            'receivables': self._eh_round_monetary(
                receivables, currency=currency),
            'payables': self._eh_round_monetary(
                payables, currency=currency),
            'working_capital': working_capital,
            'current_ratio': self._safe_ratio(
                current_assets, current_liabilities),
            'quick_ratio': self._safe_ratio(
                quick_assets, current_liabilities),
            'dso': dso,
            'dpo': dpo,
            'return_on_assets': self._safe_ratio(net_profit, total_assets),
        }

    # ---- line factories ----

    @api.model
    def _build_ratio_rows(self):
        """Return the row spec: (id, label, key, formatter, kind).

        Authored from the standard KPI set (IAS 1 presentation + common
        liquidity / efficiency ratios). formatter names map to the _fmt_*
        helpers; kind drives styling and which raw value is exported.
        """
        return [
            ('section_profitability', _("Profitability"), None, None,
             'section_header'),
            ('revenue', _("Revenue"), 'revenue', 'money', 'metric'),
            ('cost_of_sales', _("Cost of Sales"), 'cost_of_sales', 'money',
             'metric'),
            ('gross_profit', _("Gross Profit"), 'gross_profit', 'money',
             'metric'),
            ('operating_profit', _("Operating Profit"), 'operating_profit',
             'money', 'metric'),
            ('total_expense', _("Total Expenses"), 'total_expense', 'money',
             'metric'),
            ('net_profit', _("Net Profit"), 'net_profit', 'money', 'metric'),
            ('gross_margin', _("Gross Margin"), 'gross_margin', 'pct',
             'ratio'),
            ('operating_margin', _("Operating Margin"), 'operating_margin',
             'pct', 'ratio'),
            ('net_margin', _("Net Margin"), 'net_margin', 'pct', 'ratio'),

            ('section_liquidity', _("Cash & Liquidity"), None, None,
             'section_header'),
            ('cash', _("Cash Position"), 'cash', 'money', 'metric'),
            ('receivables', _("Receivables"), 'receivables', 'money',
             'metric'),
            ('payables', _("Payables"), 'payables', 'money', 'metric'),
            ('working_capital', _("Net Working Capital"), 'working_capital',
             'money', 'metric'),

            ('section_ratios', _("Ratios"), None, None, 'section_header'),
            ('current_ratio', _("Current Ratio"), 'current_ratio', 'ratio',
             'ratio'),
            ('quick_ratio', _("Quick Ratio"), 'quick_ratio', 'ratio',
             'ratio'),
            ('dso', _("Days Sales Outstanding (DSO)"), 'dso', 'days',
             'ratio'),
            ('dpo', _("Days Payable Outstanding (DPO)"), 'dpo', 'days',
             'ratio'),
            ('return_on_assets', _("Return on Assets"), 'return_on_assets',
             'pct', 'ratio'),
        ]

    @api.model
    def _cell_figure_type(self, formatter):
        return {
            'money': 'monetary',
            'pct': 'percentage',
            'days': 'integer',
            'ratio': 'float',
        }.get(formatter, 'float')

    @api.model
    def _numeric_cell(self, expression_label, formatter, raw):
        return {
            'expression_label': expression_label,
            # Undefined ratios are deliberately textual.  Keeping their
            # semantic figure type still gives every consumer one truthful
            # type contract while all formatters pass the sentinel through.
            'value': raw if raw is not None else _("n/a"),
            'figure_type': self._cell_figure_type(formatter),
        }

    @api.model
    def _build_lines(self, current, prior):
        lines = []
        for row_id, label, key, formatter, kind in self._build_ratio_rows():
            if kind == 'section_header':
                lines.append({
                    'id': "section-%s" % row_id,
                    'name': label,
                    'level': 0,
                    'columns': self._blank_columns(prior is not None),
                    'unfoldable': False,
                    'meta': {'kind': 'section_header', 'section_id': row_id},
                })
                continue
            raw = current.get(key)
            columns = [self._numeric_cell('value', formatter, raw)]
            line_meta = {
                'kind': kind,
                'metric': key,
            }
            if prior is not None:
                prior_raw = prior.get(key)
                columns.append(self._numeric_cell(
                    'prior_value', formatter, prior_raw,
                ))
            lines.append({
                'id': "exec-%s" % row_id,
                'name': label,
                'level': 1,
                'columns': columns,
                'unfoldable': False,
                'meta': line_meta,
            })
        return lines

    @api.model
    def _build_axis_lines(self, scoped_values):
        """Build mixed-figure KPI rows for an ordered period axis."""
        lines = []
        for row_id, label, key, formatter, kind in self._build_ratio_rows():
            if kind == 'section_header':
                lines.append({
                    'id': "section-%s" % row_id,
                    'name': label,
                    'level': 0,
                    'columns': [
                        {
                            'expression_label': value_scope['key'],
                            'value': '',
                            'figure_type': 'string',
                        }
                        for value_scope, _values in scoped_values
                    ],
                    'unfoldable': False,
                    'meta': {'kind': 'section_header', 'section_id': row_id},
                })
                continue
            lines.append({
                'id': "exec-%s" % row_id,
                'name': label,
                'level': 1,
                'columns': [
                    self._numeric_cell(
                        value_scope['key'], formatter, values.get(key),
                    )
                    for value_scope, values in scoped_values
                ],
                'unfoldable': False,
                'meta': {'kind': kind, 'metric': key},
            })
        return lines

    @staticmethod
    def _blank_columns(has_prior):
        cols = [{
            'expression_label': 'value', 'value': '',
            'figure_type': 'string',
        }]
        if has_prior:
            cols.append({
                'expression_label': 'prior_value', 'value': '',
                'figure_type': 'string',
            })
        return cols

    # ---- drilldown ----

    @api.model
    def get_drilldown_action(self, options, line_id):
        """Cash / AR / AP metric rows drill to the underlying journal items
        by account_type and date. Section headers, ratios and margins are
        aggregates and do not drill.
        """
        # A gross account.move.line domain cannot reconstruct weighted
        # analytic allocation. Match the shared handler's fail-closed rule.
        if (
            options.get('analytic_account_ids')
            or options.get('analytic_plan_ids')
            or options.get('_eh_analytic_column_account_ids')
            or options.get('_eh_analytic_column_plan_ids')
        ):
            return None
        if not line_id or not isinstance(line_id, str):
            return None
        drill_map = {
            'exec-cash': self.CASH_TYPES,
            'exec-receivables': self.RECEIVABLE_TYPES,
            'exec-payables': self.PAYABLE_TYPES,
        }
        account_types = drill_map.get(line_id)
        if not account_types:
            return None
        try:
            date_to = self._extract_date(options, 'date_to')
        except UserError:
            return None
        company_ids = options.get('company_ids') or [self.env.company.id]
        domain = [
            ('account_id.account_type', 'in', list(account_types)),
            ('company_id', 'in', list(company_ids)),
            ('date', '<=', self._iso_date(date_to)),
        ]
        if options.get('posted_only', True):
            domain.append(('parent_state', '=', 'posted'))
        domain += self._eh_drilldown_filter_domain(options)
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Items"),
            'res_model': 'account.move.line',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': domain,
            'context': {'search_default_group_move': 1},
        }
