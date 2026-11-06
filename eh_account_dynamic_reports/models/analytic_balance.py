# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Analytic Balance handler.

Analytic-as-primary-axis report. The other reports treat analytic
account / plan as a filter dimension layered onto an
account-of-accounts axis; this one inverts: analytic account is the
header axis, with each row showing the per-analytic balance allocated
across the standard account types.

Layout:

* One section per analytic plan that has any contribution in the
  period.
* One row per analytic account inside each section, with the period
  signed balance (income negated, expenses positive, so a positive row
  means net cost on the analytic and a negative row means net
  contribution).
* Each section ends with a section total; a final computed line shows
  the sum across all analytic accounts.

Allocation maths: each contributing journal item carries an
`analytic_distribution` jsonb keyed by analytic_account_id (string)
with the percentage allocated to that account. The handler weights
each row by that percentage so a 70/30 split posts seventy percent of
the line's balance to one analytic account and thirty to the other,
matching the source-of-truth that the rest of Odoo uses.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL
from odoo.tools.translate import LazyTranslate
from odoo.exceptions import AccessError


def _readable(records, operation='read'):
    """Subset of `records` the current user may read. Uses has_access on
    Odoo 18+ and falls back to the model-/record-level access checks on
    older series, which do not ship has_access."""
    if not records:
        return records
    if hasattr(records, 'has_access'):
        return records.filtered(lambda r: r.has_access(operation))

    def _ok(rec):
        try:
            rec.check_access_rights(operation)
            rec.check_access_rule(operation)
            return True
        except AccessError:
            return False
    return records.filtered(_ok)

_lt = LazyTranslate(__name__)


class EhAnalyticBalanceHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.analytic_balance'
    _inherit = 'eh.account.dynamic.report.handler'
    _description = "Analytic Balance report handler"

    REPORT_CODE = 'analytic_balance'
    REPORT_NAME = _lt("Analytic Balance")

    @api.model
    def compute(self, options):
        date_from = self._extract_date(options, 'date_from')
        date_to = self._extract_date(options, 'date_to')
        company_ids = options.get('company_ids') or [self.env.company.id]
        posted_only = bool(options.get('posted_only', True))
        show_zero = bool(options.get('show_zero', False))
        currency = self._eh_monetary_currency(
            options=options, company_ids=company_ids,
        )

        if 'account.analytic.account' not in self.env:
            raise UserError(_(
                "The Analytic Balance report requires the analytic addon "
                "to be installed."
            ))

        rows = self._fetch_analytic_rows(
            company_ids=company_ids,
            date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
        )

        # Group rows by plan id, plan name.
        by_plan = {}
        for row in rows:
            key = (row['plan_id'], row['plan_name'])
            by_plan.setdefault(key, []).append(row)

        lines = []
        grand_total = 0.0
        for (plan_id, plan_name), plan_rows in sorted(
            by_plan.items(), key=lambda kv: (kv[0][1] or '', kv[0][0]),
        ):
            section_id = "plan-%s" % (plan_id or 'unplanned')
            lines.append(self._section_header_line(
                plan_name or _("Unassigned plan"), section_id=section_id,
            ))
            section_total = 0.0
            for r in sorted(plan_rows, key=lambda r: r['analytic_name'] or ''):
                amount = self._eh_round_monetary(
                    r['amount'] or 0.0, currency=currency,
                )
                if not show_zero and self._eh_is_zero_monetary(
                        amount, currency=currency):
                    continue
                section_total += amount
                lines.append({
                    'id': "analytic-%s" % r['analytic_id'],
                    'name': r['analytic_name'] or '?',
                    'level': 1,
                    'columns': [
                        {'expression_label': 'amount', 'value': amount},
                    ],
                    'unfoldable': False,
                    'meta': {
                        'analytic_account_id': r['analytic_id'],
                        'plan_id': plan_id,
                    },
                })
            lines.append(self._section_total_line(
                _("Total %s") % (plan_name or _("Unassigned")),
                section_total,
                section_id=section_id,
                currency=currency,
            ))
            grand_total += section_total

        lines.append(self._computed_line(
            'analytic_total', _("Net analytic impact"),
            grand_total, kind='computed_total', currency=currency,
        ))

        return {
            'columns': self._build_two_column_layout(
                label_name=_("Analytic account"),
                amount_name=_("Net amount"),
            ),
            'lines': lines,
            'totals': {
                'analytic_total': self._eh_round_monetary(
                    grand_total, currency=currency,
                ),
                'amount': self._eh_round_monetary(
                    grand_total, currency=currency,
                ),
            },
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': {
                'report_code': self.REPORT_CODE,
                'date_from': self._iso_date(date_from),
                'date_to': self._iso_date(date_to),
                'company_ids': sorted(int(c) for c in company_ids),
                'posted_only': posted_only,
                'show_zero': show_zero,
            },
        }

    @api.model
    def _section_header_line(self, name, section_id):
        # Reuse the sectioned helper without inheriting it; copy the shape.
        return {
            'id': "section-%s-header" % section_id,
            'name': name,
            'level': 0,
            'columns': [{'expression_label': 'amount', 'value': ''}],
            'unfoldable': False,
            'meta': {'kind': 'section_header', 'section_id': section_id},
        }

    @api.model
    def _section_total_line(self, name, total, section_id, currency=None):
        currency = currency or self._eh_monetary_currency()
        return {
            'id': "section-%s-total" % section_id,
            'name': name,
            'level': 0,
            'columns': [
                {
                    'expression_label': 'amount',
                    'value': self._eh_round_monetary(
                        total, currency=currency,
                    ),
                },
            ],
            'unfoldable': False,
            'meta': {'kind': 'section_total', 'section_id': section_id},
        }

    @api.model
    def _computed_line(
        self, line_id, name, amount, kind='computed_total', currency=None,
    ):
        currency = currency or self._eh_monetary_currency()
        return {
            'id': line_id,
            'name': name,
            'level': 0,
            'columns': [
                {
                    'expression_label': 'amount',
                    'value': self._eh_round_monetary(
                        amount, currency=currency,
                    ),
                },
            ],
            'unfoldable': False,
            'meta': {'kind': kind},
        }

    @api.model
    def _build_two_column_layout(self, label_name=None, amount_name=None):
        return [
            {'expression_label': 'analytic',
             'name': label_name if label_name is not None else _("Analytic account"),
             'figure_type': 'string'},
            {'expression_label': 'amount',
             'name': amount_name if amount_name is not None else _("Net amount"),
             'figure_type': 'monetary'},
        ]

    @api.model
    def _fetch_analytic_rows(
        self, company_ids, date_from, date_to, posted_only, options,
    ):
        """One SQL pass per analytic-bearing journal item, weighted by
        percentage allocation. Sums over all rows by analytic account
        and joins back to the analytic account name and plan.

        Uses LATERAL joins to expand the analytic_distribution jsonb and split
        cross-plan composite keys (for example ``"12,34"``) into one row per
        analytic account before aggregating. Every split account keeps the
        combination's original percentage: a 25% cross-plan allocation is 25%
        on each dimension, not 12.5% each. PostgreSQL rejects
        SUM(... set_returning_function() ...) with "aggregate function
        calls cannot contain set-returning function calls"; the LATERAL
        join produces a normal row per (line, analytic) pair so the
        SUM aggregates over plain column references.
        """
        # Reuse MoveLineQuery's WHERE primitives via build()-then-splice
        # is awkward; instead compose WHERE fragments directly here so
        # the FROM clause can carry the LATERAL join. Identical filter
        # set to the other report queries.
        wheres = [SQL(
            "aml.company_id IN %s",
            tuple(int(c) for c in company_ids),
        )]
        if posted_only:
            wheres.append(SQL("am.state = %s", 'posted'))
        else:
            wheres.append(SQL("am.state != %s", 'cancel'))
        if date_from:
            wheres.append(SQL("aml.date >= %s", date_from))
        if date_to:
            wheres.append(SQL("aml.date <= %s", date_to))
        wheres.append(SQL("aml.analytic_distribution IS NOT NULL"))
        if options.get('journal_ids'):
            wheres.append(SQL(
                "aml.journal_id IN %s",
                tuple(int(i) for i in options['journal_ids']),
            ))
        if options.get('partner_ids'):
            wheres.append(SQL(
                "aml.partner_id IN %s",
                tuple(int(i) for i in options['partner_ids']),
            ))
        if options.get('account_ids'):
            wheres.append(SQL(
                "aml.account_id IN %s",
                tuple(int(i) for i in options['account_ids']),
            ))
        if options.get('analytic_account_ids'):
            wheres.append(SQL(
                "btrim(analytic_token.token) IN %s",
                tuple(str(int(i)) for i in options['analytic_account_ids']),
            ))
        if options.get('analytic_plan_ids'):
            wheres.append(SQL(
                "EXISTS ("
                "SELECT 1 FROM account_analytic_account filter_analytic "
                "WHERE filter_analytic.id::text = "
                "btrim(analytic_token.token) "
                "AND filter_analytic.plan_id IN %s"
                ")",
                tuple(int(i) for i in options['analytic_plan_ids']),
            ))
        types = options.get('analytic_account_types') or (
            'income', 'income_other',
            'expense', 'expense_other', 'expense_depreciation',
            'expense_direct_cost',
        )
        wheres.append(SQL("acc.account_type IN %s", tuple(types)))
        if options.get('account_type_ids'):
            wheres.append(SQL(
                "acc.account_type IN %s",
                tuple(options['account_type_ids']),
            ))

        sql = SQL(
            "SELECT (btrim(analytic_token.token))::int AS analytic_id, "
            "SUM(aml.balance * (kv.value)::float / 100.0) AS amount "
            "FROM account_move_line aml "
            "JOIN account_account acc ON acc.id = aml.account_id "
            "JOIN account_move am ON am.id = aml.move_id "
            "CROSS JOIN LATERAL jsonb_each_text(aml.analytic_distribution) AS kv "
            "CROSS JOIN LATERAL unnest(string_to_array(kv.key, ',')) "
            "AS analytic_token(token) "
            "WHERE %s "
            "AND btrim(analytic_token.token) ~ '^[0-9]+$' "
            "GROUP BY (btrim(analytic_token.token))::int",
            SQL(" AND ").join(wheres),
        )

        self.env.flush_all()
        cr = self.env.cr
        cr.execute(sql)
        raw_rows = cr.fetchall()
        if not raw_rows:
            return []
        rows = [
            {'analytic_id': row[0], 'amount': row[1]}
            for row in raw_rows
        ]

        # Read analytic accounts and plans WITHOUT sudo so the user's
        # own analytic ACL applies. Rows the user is not allowed to see
        # come back as None entries; we redact those names rather than
        # leaking confidential cost-centre identities (Sprint-2 audit
        # finding).
        ids = [int(r['analytic_id']) for r in rows]
        Analytic = self.env['account.analytic.account']
        accessible = _readable(Analytic.browse(ids), 'read')
        analytics = accessible.read(['name', 'plan_id']) if accessible else []
        by_id = {a['id']: a for a in analytics}

        Plan = self.env['account.analytic.plan']
        plan_ids = [a['plan_id'][0] for a in analytics if a.get('plan_id')]
        accessible_plans = _readable(Plan.browse(plan_ids), 'read')
        plans = accessible_plans.read(['name']) if accessible_plans else []
        plan_by_id = {p['id']: p['name'] for p in plans}

        result = []
        for r in rows:
            aid = int(r['analytic_id'])
            ainfo = by_id.get(aid)
            # Raw SQL can see analytic distributions beyond ORM record rules.
            # Never retain their ids or amounts as an "unknown" row: even a
            # redacted label leaks hidden dimension existence and balance.
            if not ainfo:
                continue
            plan_tuple = ainfo.get('plan_id')
            plan_id = plan_tuple[0] if plan_tuple else False
            if plan_id and plan_id not in plan_by_id:
                continue
            result.append({
                'analytic_id': aid,
                'analytic_name': ainfo.get('name'),
                'plan_id': plan_id,
                'plan_name': plan_by_id.get(plan_id) if plan_id else None,
                # Preserve the ledger convention: credits (income) are
                # negative and debits (costs) are positive. This makes an
                # analytic slice reconcile directly to its GL lines.
                'amount': float(r['amount'] or 0.0),
            })
        return result
