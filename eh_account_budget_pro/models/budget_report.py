# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Budget vs Actual analytics report.

eh.budget.line carries non-stored compute fields for actual_amount and
variance_amount. Non-stored compute fields cannot be aggregated by
SQL, so the graph and pivot views cannot use them as measures (the
JS web client throws "No aggregate function has been provided").

This module exposes a read-only SQL VIEW that aggregates the same
data with stored, SQL-aggregable columns so the graph and pivot
views work natively. One row per (budget_line, account, period); the
actual is computed by set-based posted-move aggregates limited to each
budget's company and each line's account and period range. Analytic JSON
is expanded by one function scan and weighted inside the grouped branch.
"""

from psycopg2 import sql

from odoo import _, api, fields, models, release, tools
from odoo.exceptions import UserError


_INCOME_LIKE_ACCOUNT_TYPES = (
    'income', 'income_other', 'liability_payable',
    'liability_credit_card', 'liability_current',
    'liability_non_current', 'equity', 'equity_unaffected',
)

_CURRENCY_GUARD_CONTEXT_KEY = 'eh_budget_currency_guard_capability'
_CURRENCY_GUARD_CAPABILITY = object()
_NON_MONETARY_AGGREGATES = frozenset({
    'count', 'count_distinct', 'recordset', 'array_agg',
})
_CURRENCY_DETERMINING_GROUPS = frozenset({
    'budget_id', 'company_id', 'currency_id',
})


def _group_field_name(spec):
    return str(spec or '').split(':', 1)[0]


def _monetary_aggregate_requested(recordset, aggregate_specs, groupby=()):
    """Return whether specs combine monetary values, not merely count them."""
    grouped_fields = {_group_field_name(spec) for spec in (groupby or ())}
    for raw_spec in aggregate_specs or ():
        spec = str(raw_spec or '')
        if not spec or spec == '__count':
            continue
        field_name = spec
        aggregate = None
        if ':' in spec:
            left, right = spec.split(':', 1)
            if '(' in right and right.endswith(')'):
                aggregate, field_name = right.split('(', 1)
                field_name = field_name[:-1]
            else:
                field_name, aggregate = left, right
        if field_name in grouped_fields:
            continue
        field = recordset._fields.get(field_name)
        if not field or field.type != 'monetary':
            continue
        aggregate = aggregate or getattr(
            field, 'aggregator', getattr(field, 'group_operator', None),
        )
        if aggregate not in _NON_MONETARY_AGGREGATES:
            return True
    return False


def _guard_monetary_aggregate(recordset, domain, aggregate_specs,
                              grouping_sets):
    """Probe currencies only for a grouping that may mix monetary values."""
    if not any(
        _monetary_aggregate_requested(recordset, aggregate_specs, grouping)
        for grouping in grouping_sets
    ):
        return
    # Any unsafe grouping set requires a single-currency domain. Grouping by
    # company/currency/budget partitions every result into one currency; an
    # empty pivot grand-total set deliberately fails this condition.
    if all(
        _CURRENCY_DETERMINING_GROUPS.intersection({
            _group_field_name(spec) for spec in grouping
        })
        for grouping in grouping_sets
    ):
        return
    _assert_single_currency_aggregate(recordset, domain)


def _guard_already_applied(recordset):
    return recordset.env.context.get(
        _CURRENCY_GUARD_CONTEXT_KEY,
    ) is _CURRENCY_GUARD_CAPABILITY


def _guard_context(recordset):
    return recordset.with_context(**{
        _CURRENCY_GUARD_CONTEXT_KEY: _CURRENCY_GUARD_CAPABILITY,
    })


def _legacy_read_group_request(args, kwargs):
    aggregate_specs = args[0] if args else kwargs.get('fields', ())
    groupby = args[1] if len(args) > 1 else kwargs.get('groupby', ())
    lazy = args[5] if len(args) > 5 else kwargs.get('lazy', True)
    groupby = [groupby] if isinstance(groupby, str) else list(groupby or ())
    return aggregate_specs, (groupby[:1] if lazy else groupby)


def _modern_read_group_request(args, kwargs):
    groupby = args[0] if args else kwargs.get('groupby', ())
    aggregates = args[1] if len(args) > 1 else kwargs.get('aggregates', ())
    groupby = [groupby] if isinstance(groupby, str) else list(groupby or ())
    return aggregates, groupby


def _assert_single_currency_aggregate(recordset, domain):
    """Fail closed only when records selected by ``domain`` mix currencies.

    Checking ``env.companies`` alone rejects a safe single-company domain
    whenever another allowed company uses a different currency.  Resolve one
    matching row, then probe for any row with a different currency. This
    honours record rules and the full domain in two bounded existence queries.
    """
    first = recordset.search(domain, limit=1)
    if not first:
        return
    currency_field = (
        'currency_id'
        if recordset._name == 'eh.budget.report'
        else 'budget_id.currency_id'
    )
    first_currency = (
        first.currency_id
        if recordset._name == 'eh.budget.report'
        else first.budget_id.currency_id
    )
    if hasattr(fields, 'Domain'):
        mixed_domain = fields.Domain.AND([
            domain,
            fields.Domain(currency_field, '!=', first_currency.id),
        ])
    else:
        # Odoo 16-18 use legacy domain lists.
        from odoo.osv import expression
        mixed_domain = expression.AND([
            domain,
            [(currency_field, '!=', first_currency.id)],
        ])
    if recordset.search(mixed_domain, limit=1):
        raise UserError(_(
            "Budget graph and pivot measures cannot combine companies with "
            "different currencies. Select one company, or select companies "
            "that share the same currency."
        ))


class EhBudgetReport(models.Model):
    _name = 'eh.budget.report'
    _description = "Budget vs Actual report (SQL view)"
    _auto = False
    _order = 'period_from desc, account_id'
    _rec_name = 'account_id'

    budget_id = fields.Many2one(
        'eh.budget.budget', string="Budget", readonly=True,
    )
    line_id = fields.Many2one(
        'eh.budget.line', string="Budget Line", readonly=True,
    )
    company_id = fields.Many2one(
        'res.company', string="Company", readonly=True,
    )
    currency_id = fields.Many2one(
        'res.currency', string="Currency", readonly=True,
    )
    account_id = fields.Many2one(
        'account.account', string="Account", readonly=True,
    )
    analytic_account_id = fields.Many2one(
        'account.analytic.account', string="Analytic Account", readonly=True,
    )
    period_from = fields.Date(string="Period From", readonly=True)
    period_to = fields.Date(string="Period To", readonly=True)
    budgeted_amount = fields.Monetary(
        string="Budgeted",
        currency_field='currency_id',
        readonly=True,
        aggregator='sum',
    )
    actual_amount = fields.Monetary(
        string="Actual",
        currency_field='currency_id',
        readonly=True,
        aggregator='sum',
    )
    variance_amount = fields.Monetary(
        string="Variance",
        currency_field='currency_id',
        readonly=True,
        aggregator='sum',
        help=(
            "Actual minus Budgeted. Sign is normalised so income and "
            "liability accounts read as the natural economic direction."
        ),
    )

    @api.model
    def read_group(self, domain, fields, groupby, *args, **kwargs):
        if not _guard_already_applied(self):
            lazy = args[3] if len(args) > 3 else kwargs.get('lazy', True)
            groups = [groupby] if isinstance(groupby, str) else list(
                groupby or (),
            )
            groups = groups[:1] if lazy else groups
            _guard_monetary_aggregate(self, domain, fields, [groups])
            guarded = _guard_context(self)
            return super(EhBudgetReport, guarded).read_group(
                domain, fields, groupby, *args, **kwargs
            )
        return super().read_group(domain, fields, groupby, *args, **kwargs)

    @api.model
    def _read_group(self, domain, *args, **kwargs):
        # Odoo 16 routes read_group through the legacy _read_group signature;
        # 17+ graph/pivot code calls the newer aggregate signature directly.
        if not _guard_already_applied(self):
            if release.version_info[0] <= 16:
                aggregates, groupby = _legacy_read_group_request(args, kwargs)
            else:
                aggregates, groupby = _modern_read_group_request(args, kwargs)
            _guard_monetary_aggregate(self, domain, aggregates, [groupby])
            guarded = _guard_context(self)
            return super(EhBudgetReport, guarded)._read_group(
                domain, *args, **kwargs
            )
        return super()._read_group(domain, *args, **kwargs)

    @api.model
    def _read_grouping_sets(self, domain, *args, **kwargs):
        # Odoo 19 pivot totals use grouping sets and bypass _read_group.
        if not _guard_already_applied(self):
            grouping_sets = (
                args[0] if args else kwargs.get('grouping_sets', ())
            )
            aggregates = (
                args[1] if len(args) > 1 else kwargs.get('aggregates', ())
            )
            _guard_monetary_aggregate(
                self, domain, aggregates, grouping_sets,
            )
            guarded = _guard_context(self)
            return super(EhBudgetReport, guarded)._read_grouping_sets(
                domain, *args, **kwargs
            )
        return super()._read_grouping_sets(domain, *args, **kwargs)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        # Identifier interpolation goes through psycopg2's sql module
        # (safe quoting of the view name); the income-like list goes
        # through a normal %s parameter so the values cross the
        # adapter boundary as bound parameters, not string literals.
        query = sql.SQL("""
CREATE OR REPLACE VIEW {table} AS (
SELECT
    rounded.id AS id,
    rounded.line_id AS line_id,
    rounded.budget_id AS budget_id,
    rounded.company_id AS company_id,
    rounded.currency_id AS currency_id,
    rounded.account_id AS account_id,
    rounded.analytic_account_id AS analytic_account_id,
    rounded.period_from AS period_from,
    rounded.period_to AS period_to,
    rounded.budgeted_amount AS budgeted_amount,
    rounded.actual_amount AS actual_amount,
    rounded.actual_amount - rounded.budgeted_amount AS variance_amount
FROM (
SELECT
    normalised.id AS id,
    normalised.line_id AS line_id,
    normalised.budget_id AS budget_id,
    normalised.company_id AS company_id,
    normalised.currency_id AS currency_id,
    normalised.account_id AS account_id,
    normalised.analytic_account_id AS analytic_account_id,
    normalised.period_from AS period_from,
    normalised.period_to AS period_to,
    CASE WHEN report_currency.rounding > 0
        THEN ROUND(
            normalised.budgeted_amount / report_currency.rounding
        ) * report_currency.rounding
        ELSE normalised.budgeted_amount
    END AS budgeted_amount,
    CASE WHEN report_currency.rounding > 0
        THEN ROUND(
            normalised.actual_amount / report_currency.rounding
        ) * report_currency.rounding
        ELSE normalised.actual_amount
    END AS actual_amount
FROM (
SELECT
    grouped_raw.id AS id,
    grouped_raw.line_id AS line_id,
    grouped_raw.budget_id AS budget_id,
    grouped_raw.company_id AS company_id,
    grouped_raw.currency_id AS currency_id,
    grouped_raw.account_id AS account_id,
    grouped_raw.analytic_account_id AS analytic_account_id,
    grouped_raw.period_from AS period_from,
    grouped_raw.period_to AS period_to,
    grouped_raw.budgeted_amount AS budgeted_amount,
    CASE
        WHEN grouped_raw.account_type IN %s THEN -grouped_raw.actual_amount
        ELSE grouped_raw.actual_amount
    END AS actual_amount
FROM (
    SELECT
        bl.id AS id,
        bl.id AS line_id,
        bl.budget_id AS budget_id,
        b.company_id AS company_id,
        b.currency_id AS currency_id,
        bl.account_id AS account_id,
        bl.analytic_account_id AS analytic_account_id,
        bl.period_from AS period_from,
        bl.period_to AS period_to,
        bl.budgeted_amount AS budgeted_amount,
        aa.account_type AS account_type,
        COALESCE(SUM(
            CASE
                WHEN am.id IS NOT NULL
                     AND ABS(aml.balance) <= %s::numeric
                    THEN aml.balance
                ELSE 0.0
            END
        ), 0.0) AS actual_amount
    FROM eh_budget_line bl
    JOIN eh_budget_budget b ON b.id = bl.budget_id
    LEFT JOIN account_account aa ON aa.id = bl.account_id
    LEFT JOIN account_move_line aml
      ON aml.company_id = b.company_id
     AND aml.account_id = bl.account_id
     AND aml.date BETWEEN bl.period_from AND bl.period_to
    LEFT JOIN account_move am
      ON am.id = aml.move_id
     AND am.state = 'posted'
    WHERE bl.analytic_account_id IS NULL
    GROUP BY
        bl.id,
        bl.budget_id,
        b.company_id,
        b.currency_id,
        bl.account_id,
        bl.analytic_account_id,
        bl.period_from,
        bl.period_to,
        bl.budgeted_amount,
        aa.account_type

    UNION ALL

    SELECT
        bl.id AS id,
        bl.id AS line_id,
        bl.budget_id AS budget_id,
        b.company_id AS company_id,
        b.currency_id AS currency_id,
        bl.account_id AS account_id,
        bl.analytic_account_id AS analytic_account_id,
        bl.period_from AS period_from,
        bl.period_to AS period_to,
        bl.budgeted_amount AS budgeted_amount,
        aa.account_type AS account_type,
        COALESCE(SUM(
            CASE
                WHEN am.id IS NOT NULL
                     AND jsonb_typeof(analytic_entry.value) = 'number'
                    THEN CASE
                        WHEN ABS(
                            (analytic_entry.value::text)::numeric
                        ) BETWEEN %s::numeric AND %s::numeric
                         AND ABS(
                            aml.balance
                            * (analytic_entry.value::text)::numeric
                            / 100.0
                         ) BETWEEN %s::numeric AND %s::numeric
                            THEN aml.balance
                                 * (analytic_entry.value::text)::numeric
                                 / 100.0
                        ELSE 0.0
                    END
                ELSE 0.0
            END
        ), 0.0) AS actual_amount
    FROM eh_budget_line bl
    JOIN eh_budget_budget b ON b.id = bl.budget_id
    LEFT JOIN account_account aa ON aa.id = bl.account_id
    LEFT JOIN account_move_line aml
      ON aml.company_id = b.company_id
     AND aml.account_id = bl.account_id
     AND aml.date BETWEEN bl.period_from AND bl.period_to
    LEFT JOIN account_move am
      ON am.id = aml.move_id
     AND am.state = 'posted'
    LEFT JOIN jsonb_each(CASE
        WHEN jsonb_typeof(aml.analytic_distribution) = 'object'
            THEN aml.analytic_distribution
        ELSE '{{}}'::jsonb
    END) AS analytic_entry(key, value)
      ON bl.analytic_account_id::text = ANY(
          regexp_split_to_array(
              btrim(analytic_entry.key), '\\s*,\\s*'
          )
      )
    WHERE bl.analytic_account_id IS NOT NULL
    GROUP BY
        bl.id,
        bl.budget_id,
        b.company_id,
        b.currency_id,
        bl.account_id,
        bl.analytic_account_id,
        bl.period_from,
        bl.period_to,
        bl.budgeted_amount,
        aa.account_type
) grouped_raw
) normalised
JOIN res_currency report_currency
  ON report_currency.id = normalised.currency_id
) rounded
)
""").format(table=sql.Identifier(self._table))
        self.env.cr.execute(query, [
            _INCOME_LIKE_ACCOUNT_TYPES,
            '1.7976931348623157e308',
            '4.9406564584124654e-324',
            '1.7976931348623157e308',
            '4.9406564584124654e-324',
            '1.7976931348623157e308',
        ])


class EhBudgetLineAggregateCurrencyGuard(models.Model):
    _inherit = 'eh.budget.line'

    @api.model
    def read_group(self, domain, fields, groupby, *args, **kwargs):
        if not _guard_already_applied(self):
            lazy = args[3] if len(args) > 3 else kwargs.get('lazy', True)
            groups = [groupby] if isinstance(groupby, str) else list(
                groupby or (),
            )
            groups = groups[:1] if lazy else groups
            _guard_monetary_aggregate(self, domain, fields, [groups])
            guarded = _guard_context(self)
            return super(
                EhBudgetLineAggregateCurrencyGuard, guarded,
            ).read_group(domain, fields, groupby, *args, **kwargs)
        return super().read_group(domain, fields, groupby, *args, **kwargs)

    @api.model
    def _read_group(self, domain, *args, **kwargs):
        if not _guard_already_applied(self):
            if release.version_info[0] <= 16:
                aggregates, groupby = _legacy_read_group_request(args, kwargs)
            else:
                aggregates, groupby = _modern_read_group_request(args, kwargs)
            _guard_monetary_aggregate(self, domain, aggregates, [groupby])
            guarded = _guard_context(self)
            return super(
                EhBudgetLineAggregateCurrencyGuard, guarded,
            )._read_group(domain, *args, **kwargs)
        return super()._read_group(domain, *args, **kwargs)

    @api.model
    def _read_grouping_sets(self, domain, *args, **kwargs):
        if not _guard_already_applied(self):
            grouping_sets = (
                args[0] if args else kwargs.get('grouping_sets', ())
            )
            aggregates = (
                args[1] if len(args) > 1 else kwargs.get('aggregates', ())
            )
            _guard_monetary_aggregate(
                self, domain, aggregates, grouping_sets,
            )
            guarded = _guard_context(self)
            return super(
                EhBudgetLineAggregateCurrencyGuard, guarded,
            )._read_grouping_sets(domain, *args, **kwargs)
        return super()._read_grouping_sets(domain, *args, **kwargs)
