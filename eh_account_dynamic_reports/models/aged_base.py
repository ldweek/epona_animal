# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Base for aged receivable / payable reports.

Aged reports answer: "as of date_to, what is each partner's open balance,
broken into aging buckets by days overdue?" The bucket layout is fixed at
five tiers (Not Due, 0 to 30, 31 to 60, 61 to 90, 91 plus) which matches
the convention used by the AU, NZ, UK, and GCC markets.

Subclasses set:

* ACCOUNT_TYPES: tuple of account_type strings to include.
* SIGN: +1 for receivable (positive amounts owed to us), -1 for payable
  (positive amounts we owe).

The handler then:

1. Fetches per line data: partner_id, due date, balance, amount_residual,
   journal, move name, label.
2. Filters to lines whose amount_residual is non zero (still open in
   today's reconciliation state, see Phase 2 note below).
3. Filters to date <= date_to (the line existed by the report date).
4. Aggregates open residuals per partner into the five buckets, classifying
   each line by days overdue (date_to minus due_date if set, otherwise
   minus posting date).
5. Renders one row per partner with seven monetary columns and a final
   Totals row.

Historical accuracy: the residual is recomputed as of date_to by
rewinding every partial reconciliation whose accounting max_date falls after
date_to. A back-dated aged report therefore reflects what was open on
that date, not what is open today. The rewind uses a correlated subquery
on account.partial.reconcile filtered to max_date <= date_to.
"""

import datetime

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL

from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery


_MAX_AGING_BUCKET_COUNT = 24
_MAX_AGING_INTERVAL_DAYS = 3650


class EhAgedBaseHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.aged_base'
    _inherit = 'eh.account.dynamic.report.handler'
    _description = "Base for Aged Receivable / Payable handlers"

    ACCOUNT_TYPES = ()
    SIGN = 1

    @api.model
    def compute(self, options):
        date_to = self._extract_date(options, 'date_to')
        company_ids = options.get('company_ids') or [self.env.company.id]
        posted_only = bool(options.get('posted_only', True))
        show_zero = bool(options.get('show_zero', False))
        currency = self._eh_monetary_currency(
            options=options, company_ids=company_ids,
        )

        buckets = self._get_buckets(options)
        aging_basis = (
            options.get('aging_basis') or 'maturity')
        reconcile_state = self._resolve_reconcile_state(options)

        rows = self._fetch_open_lines(
            company_ids=company_ids, date_to=date_to,
            posted_only=posted_only, options=options,
            reconcile_state=reconcile_state,
        )
        partner_buckets = self._aggregate_into_buckets(
            rows, date_to, buckets, aging_basis,
        )

        lines, totals = self._build_lines_and_totals(
            partner_buckets, show_zero, buckets, options,
            currency=currency,
        )

        return {
            'columns': self._build_columns(buckets),
            'lines': lines,
            'totals': totals,
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': {
                'report_code': self.REPORT_CODE,
                'date_to': self._iso_date(date_to),
                'company_ids': sorted(int(c) for c in company_ids),
                'posted_only': posted_only,
                'show_zero': show_zero,
                'aging_basis': aging_basis,
                'aging_interval': options.get('aging_interval') or 30,
                'aging_bucket_count': options.get('aging_bucket_count') or 4,
                'reconcile_state': reconcile_state,
            },
        }

    @api.model
    def _resolve_reconcile_state(self, options):
        """Normalise the reconcile-state filter to 'open' or 'all'.

        'open'  -> only lines whose historical residual is non zero as of
                   date_to (the classic aged report: still-open items).
        'all'   -> drop the residual<>0 filter so lines reconciled after
                   date_to also surface, for audit of what later cleared.

        Anything unrecognised (or missing) degrades to 'open', the safe
        default that bounds the line set and matches prior behaviour.
        """
        value = (options or {}).get('reconcile_state')
        if value in ('open', 'all'):
            return value
        return 'open'

    @api.model
    def _get_buckets(self, options):
        """Build the aging bucket definitions from the options.

        Returns a list of (key, label, day_min, day_max) tuples. The
        first bucket is always 'not_due' (days_overdue <= 0); then
        `count` buckets of width `interval` days, the last open-ended.
        Defaults (interval 30, count 4) reproduce the classic
        not_due / 0-30 / 31-60 / 61-90 / 91+ layout and its keys.
        """
        try:
            interval = int(options.get('aging_interval') or 30)
            count = int(options.get('aging_bucket_count') or 4)
        except (TypeError, ValueError, OverflowError) as exc:
            raise UserError(_(
                "Aging interval and bucket count must be whole numbers."
            )) from exc
        if interval < 1:
            interval = 30
        if count < 1:
            count = 4
        if interval > _MAX_AGING_INTERVAL_DAYS:
            raise UserError(_(
                "Aging interval cannot exceed %(maximum)s days.",
                maximum=_MAX_AGING_INTERVAL_DAYS,
            ))
        if count > _MAX_AGING_BUCKET_COUNT:
            raise UserError(_(
                "Aged reports support at most %(maximum)s buckets.",
                maximum=_MAX_AGING_BUCKET_COUNT,
            ))
        buckets = [('not_due', _("Not Due"), None, 0)]
        for i in range(1, count + 1):
            day_min = (i - 1) * interval + 1
            if i < count:
                day_max = i * interval
                key = 'bucket_%d' % day_max
                label = _("%(min)s to %(max)s Days",
                          min=day_min, max=day_max)
            else:
                day_max = None
                key = 'bucket_older'
                label = _("%(min)s Plus Days", min=day_min)
            buckets.append((key, label, day_min, day_max))
        return buckets

    @api.model
    def get_drilldown_action(self, options, line_id):
        """partner-N opens the journal items list filtered to the partner
        and account types of this aged report; aml-N opens the single
        journal entry. Other ids return None.
        """
        if line_id and line_id.startswith('aml-'):
            rest = line_id.split('-', 1)[1]
            if not rest.isdigit():
                return None
            aml = self.env['account.move.line'].browse(int(rest)).exists()
            if not aml:
                return None
            return {
                'type': 'ir.actions.act_window',
                'name': _("Journal Entry"),
                'res_model': 'account.move',
                'res_id': aml.move_id.id,
                'view_mode': 'form',
                'views': [(False, 'form')],
            }
        if not line_id or not line_id.startswith('partner-'):
            return None
        rest = line_id.split('-', 1)[1]
        if rest == 'none':
            partner_id = 0
        elif rest.isdigit():
            partner_id = int(rest)
        else:
            return None
        try:
            date_to = self._extract_date(options, 'date_to')
        except Exception:
            return None
        company_ids = options.get('company_ids') or [self.env.company.id]
        # Resolve the exact SQL-backed row set used by the report. Today's
        # stored amount_residual cannot represent a historical cutoff: an
        # invoice open on date_to but settled later has residual 0 today.
        # Reusing _fetch_open_lines keeps the max_date rewind, reconcile-state,
        # company, posting state, and common filters aligned with the partner
        # total the user clicked.
        rows = self._fetch_open_lines(
            company_ids=company_ids,
            date_to=date_to,
            posted_only=bool(options.get('posted_only', True)),
            options=options,
            reconcile_state=self._resolve_reconcile_state(options),
            partner_ids=[partner_id],
        )
        column = options.get('_eh_column_expression') or 'total'
        buckets = self._get_buckets(options)
        bucket_keys = {key for key, _label, _minimum, _maximum in buckets}
        if column != 'total':
            if column not in bucket_keys:
                return None
            aging_basis = options.get('aging_basis') or 'maturity'
            matching_rows = []
            for row in rows:
                line_date = row.get('date')
                if isinstance(line_date, str):
                    line_date = datetime.date.fromisoformat(line_date[:10])
                due_date = line_date
                if aging_basis != 'invoice_date':
                    due_date = row.get('date_maturity') or line_date
                    if isinstance(due_date, str):
                        due_date = datetime.date.fromisoformat(due_date[:10])
                days_overdue = (date_to - due_date).days
                if self._classify_bucket(days_overdue, buckets) == column:
                    matching_rows.append(row)
            rows = matching_rows
        domain = [('id', 'in', [row['aml_id'] for row in rows])]
        return {
            'type': 'ir.actions.act_window',
            'name': _("Open Items"),
            'res_model': 'account.move.line',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': domain,
        }

    # ---- lazy per-partner open-item expand (WS2) ----

    @api.model
    def _expand_partner_id_from_line_id(self, line_id):
        """Parse ``partner-N`` or the partnerless ``partner-none`` row."""
        if not line_id or not isinstance(line_id, str):
            return None
        if not line_id.startswith('partner-'):
            return None
        tail = line_id.split('-', 1)[1]
        if tail == 'none':
            return 0
        try:
            return int(tail)
        except (ValueError, TypeError):
            return None

    @api.model
    def expand_account_line(self, options, line_id, offset=0, limit=None):
        """Aged override of the shared lazy-expand engine.

        Recognises a 'partner-N' line id and returns that partner's open
        (or, under reconcile_state='all', all) invoice lines as level-2
        children, each with its residual placed in the SAME aging bucket
        column the parent partner row counted it in, so children reconcile
        exactly to the parent's bucket cells. Paged via the same
        offset/limit cursor as the account-leaf engine.

        Any non-partner id (or any failure) falls through to super(), so an
        account-N leaf on some other report still expands normally and a
        broken aged expand degrades to an empty, collapsed page rather than
        raising (the §2 invariant: a failed expand never fans out).
        """
        partner_id = self._expand_partner_id_from_line_id(line_id)
        if partner_id is None:
            return super().expand_account_line(
                options, line_id, offset=offset, limit=limit)

        empty = {
            'child_lines': [], 'has_more': False,
            'next_offset': int(offset or 0), 'total_count': 0,
        }
        try:
            date_to = self._extract_date(options, 'date_to')
        except Exception:
            return empty

        try:
            company_ids = options.get('company_ids') or [self.env.company.id]
            posted_only = bool(options.get('posted_only', True))
            currency = self._eh_monetary_currency(
                options=options, company_ids=company_ids,
            )
            reconcile_state = self._resolve_reconcile_state(options)
            buckets = self._get_buckets(options)
            aging_basis = options.get('aging_basis') or 'maturity'

            if limit is None:
                limit = self._resolve_expand_page_size(options)
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                limit = self._resolve_expand_page_size(options)
            if limit <= 0:
                limit = self._resolve_expand_page_size(options)
            try:
                offset = max(0, int(offset or 0))
            except (TypeError, ValueError):
                offset = 0

            total_count = self._fetch_open_line_count(
                company_ids=company_ids, date_to=date_to,
                posted_only=posted_only, options=options,
                reconcile_state=reconcile_state, partner_ids=[partner_id],
            )
            page = self._fetch_open_lines_page(
                company_ids=company_ids, date_to=date_to,
                posted_only=posted_only, options=options,
                reconcile_state=reconcile_state, partner_ids=[partner_id],
                offset=offset, limit=limit,
            )
            has_more = len(page) > limit
            if has_more:
                page = page[:limit]

            child_lines = self._build_child_lines(
                partner_id, page, buckets, aging_basis, date_to,
                options=options, company_ids=company_ids,
                currency=currency,
            )
            return {
                'child_lines': child_lines,
                'has_more': has_more,
                'next_offset': offset + len(page),
                'total_count': total_count,
            }
        except Exception:
            return empty

    def _build_child_lines(
        self, partner_id, invoice_rows, buckets, aging_basis, date_to,
        options=None, company_ids=None, currency=None,
    ):
        """Build level-2 'aml-N' child line dicts for a partner's invoices.

        Each child's residual is placed in its matching aging-bucket column
        (zeros elsewhere) with the same sign convention (* SIGN) the parent
        bucket aggregate uses, so the children in a bucket sum to the parent
        bucket cell. Due date is surfaced in the row name; the foreign
        currency residual is carried in meta for the viewer.
        """
        currency = currency or self._eh_monetary_currency(
            options=options, company_ids=company_ids,
        )
        bucket_keys = [key for key, _l, _mn, _mx in buckets]
        parent_line_id = (
            "partner-%s" % partner_id if partner_id else "partner-none"
        )
        child_lines = []
        for row in invoice_rows:
            line_date = row.get('date')
            if isinstance(line_date, str):
                line_date = datetime.date.fromisoformat(line_date[:10])
            if aging_basis == 'invoice_date':
                due_date = line_date
            else:
                due_date = row.get('date_maturity') or line_date
                if isinstance(due_date, str):
                    due_date = datetime.date.fromisoformat(due_date[:10])
            days_overdue = (date_to - due_date).days
            bucket_key = self._classify_bucket(days_overdue, buckets)
            amount = self._eh_round_monetary(
                float(row.get('amount_residual') or 0.0) * self.SIGN,
                currency=currency,
            )

            columns = []
            for key in bucket_keys:
                columns.append({
                    'expression_label': key,
                    'value': amount if key == bucket_key else 0.0,
                })
            columns.append({'expression_label': 'total', 'value': amount})

            move_name = row.get('move_name') or ''
            due_label = self._iso_date(due_date) if due_date else ''
            name = move_name
            if due_label:
                name = "%s · due %s" % (move_name, due_label) if move_name \
                    else _("due %s", due_label)

            child_lines.append({
                'id': "aml-%s" % row.get('aml_id'),
                'name': name or "(line)",
                'level': 2,
                'parent_id': parent_line_id,
                'columns': columns,
                'unfoldable': False,
                'unfolded': False,
                'lazy': False,
                'meta': {
                    'kind': 'aml',
                    'aml_id': row.get('aml_id'),
                    'partner_id': partner_id,
                    'move': move_name,
                    'due_date': due_label,
                    'currency_id': row.get('currency_id'),
                    'amount_residual_currency': row.get(
                        'amount_residual_currency'),
                    'bucket': bucket_key,
                },
            })
        return child_lines

    # ---- internal helpers ----

    def _build_columns(self, buckets=None):
        # buckets is optional so the expand_line RPC wrapper can call
        # _build_columns() with no args to drive presentation-currency
        # restatement over the child bucket cells. When omitted, fall back
        # to the default bucket grid (the column SET is what the currency
        # pass needs; exact labels do not matter for child restatement).
        if buckets is None:
            buckets = self._get_buckets({})
        columns = [
            {'expression_label': 'partner', 'name': _("Partner"),
             'figure_type': 'string'},
        ]
        for key, label, _dmin, _dmax in buckets:
            columns.append({
                'expression_label': key, 'name': label,
                'figure_type': 'monetary',
            })
        columns.append({
            'expression_label': 'total', 'name': _("Total"),
            'figure_type': 'monetary',
        })
        return columns

    def _fetch_open_lines(
        self, company_ids, date_to, posted_only, options,
        reconcile_state='open', partner_ids=None,
    ):
        """Eager compute/export path: return complete open-item rowset."""
        query, historical_residual = self._build_open_lines_query(
            company_ids, date_to, posted_only, options,
            reconcile_state=reconcile_state, partner_ids=partner_ids,
        )
        self._select_open_line_fields(query, historical_residual)
        return query.execute()

    def _build_open_lines_query(
        self, company_ids, date_to, posted_only, options,
        reconcile_state='open', partner_ids=None,
    ):
        if not self.ACCOUNT_TYPES:
            raise ValueError(
                "%s.ACCOUNT_TYPES is empty; subclass must override."
                % type(self).__name__,
            )
        # Compute the residual *as of date_to* by reversing every partial
        # reconciliation whose accounting max_date is later than date_to. A line
        # that was open on date_to but has since been settled now shows a
        # zero amount_residual; without this rewind the historical aged
        # report would drop it. The expression mirrors Odoo's residual
        # computation: balance minus matched-debit-side plus matched-
        # credit-side, scoped to reconciliations whose matched accounting
        # entries are on or before
        # date_to.
        historical_residual = SQL("""(
            aml.balance
            - COALESCE((
                SELECT SUM(apr.amount)
                FROM account_partial_reconcile apr
                WHERE apr.debit_move_id = aml.id
                  AND apr.max_date <= %s
            ), 0)
            + COALESCE((
                SELECT SUM(apr.amount)
                FROM account_partial_reconcile apr
                WHERE apr.credit_move_id = aml.id
                  AND apr.max_date <= %s
            ), 0)
        )""", date_to, date_to)

        query = MoveLineQuery(self.env, company_ids=company_ids)
        query.where_date_range(date_to=date_to)
        query.where_account_types(self.ACCOUNT_TYPES)
        # Keep only lines whose historical residual is non zero. Wrap in
        # a CTE-style filter via where_raw so the same expression drives
        # both the WHERE and the SELECT. Reusing SQL keeps the binding
        # params consistent.
        residual_filter = SQL("""(
            aml.balance
            - COALESCE((
                SELECT SUM(apr.amount)
                FROM account_partial_reconcile apr
                WHERE apr.debit_move_id = aml.id
                  AND apr.max_date <= %s
            ), 0)
            + COALESCE((
                SELECT SUM(apr.amount)
                FROM account_partial_reconcile apr
                WHERE apr.credit_move_id = aml.id
                  AND apr.max_date <= %s
            ), 0)
        ) <> 0""", date_to, date_to)
        # reconcile_state 'open' (default) keeps the residual<>0 filter so
        # only still-open items appear; 'all' drops it so lines that were
        # open on date_to but reconciled afterwards also surface for audit.
        # The historical_residual SELECT is unchanged either way, so every
        # row still carries its as-of-date_to residual (zero for a line
        # cleared on/before date_to).
        if reconcile_state != 'all':
            query.where_raw(residual_filter)
        if partner_ids:
            partner_ids = [int(partner_id) for partner_id in partner_ids]
            real_partner_ids = [pid for pid in partner_ids if pid]
            if 0 in partner_ids and real_partner_ids:
                query.where_raw(SQL(
                    "(aml.partner_id IS NULL OR aml.partner_id IN %s)",
                    tuple(real_partner_ids),
                ))
            elif 0 in partner_ids:
                query.where_raw(SQL("aml.partner_id IS NULL"))
            else:
                query.where_partners(real_partner_ids)
        if posted_only:
            query.where_posted_only()
        self.apply_common_filters(query, options)
        return query, historical_residual

    @staticmethod
    def _select_open_line_fields(query, historical_residual):
        """Attach aged detail projection without changing row scope."""

        query.select_field('id', alias='aml_id')
        query.select_field('partner_id')
        query.select_field('date')
        query.select_field('date_maturity')
        query.select_field('move_id')
        query.select_field('currency_id')
        query.select_field('amount_residual_currency')
        query.select(historical_residual, 'amount_residual')
        query.join_partner()
        query.select(SQL("p.name"), 'partner_name')
        query.select(SQL("am.name"), 'move_name')
        return query

    def _build_open_lines_page_query(
        self, company_ids, date_to, posted_only, options,
        reconcile_state, partner_ids, offset, limit,
    ):
        """Return stable LIMIT+1/OFFSET query used only by lazy expand."""
        query, historical_residual = self._build_open_lines_query(
            company_ids, date_to, posted_only, options,
            reconcile_state=reconcile_state, partner_ids=partner_ids,
        )
        self._select_open_line_fields(query, historical_residual)
        query.order_by('date', 'ASC')
        query.order_by('id', 'ASC')
        query.limit(limit + 1)
        query.offset(offset)
        return query

    def _fetch_open_lines_page(
        self, company_ids, date_to, posted_only, options,
        reconcile_state, partner_ids, offset, limit,
    ):
        return self._build_open_lines_page_query(
            company_ids, date_to, posted_only, options,
            reconcile_state, partner_ids, offset, limit,
        ).execute()

    def _fetch_open_line_count(
        self, company_ids, date_to, posted_only, options,
        reconcile_state, partner_ids,
    ):
        query, _historical_residual = self._build_open_lines_query(
            company_ids, date_to, posted_only, options,
            reconcile_state=reconcile_state, partner_ids=partner_ids,
        )
        query.select_count(alias='total_count')
        rows = query.execute()
        return int(rows[0]['total_count'] or 0) if rows else 0

    def _aggregate_into_buckets(self, rows, date_to, buckets, aging_basis):
        """Return {partner_id: {bucket_key: amount, ..., 'partner_name'}}.

        aging_basis 'maturity' ages by date_maturity (falling back to the
        line date); 'invoice_date' ages by the line/posting date.
        """
        bucket_keys = [key for key, _l, _mn, _mx in buckets]
        partner_buckets = {}
        for row in rows:
            # ``0`` is an internal, non-persisted bucket for journal items
            # without a partner. Dropping those lines understated both the
            # detailed rows and the aged control total.
            partner_id = row.get('partner_id') or 0

            line_date = row.get('date')
            if isinstance(line_date, str):
                line_date = datetime.date.fromisoformat(line_date[:10])
            if aging_basis == 'invoice_date':
                due_date = line_date
            else:
                due_date = row.get('date_maturity') or line_date
                if isinstance(due_date, str):
                    due_date = datetime.date.fromisoformat(due_date[:10])
            days_overdue = (date_to - due_date).days

            bucket_key = self._classify_bucket(days_overdue, buckets)
            amount = float(row.get('amount_residual') or 0.0) * self.SIGN

            if partner_id not in partner_buckets:
                entry = {
                    'partner_name': (
                        row.get('partner_name')
                        or (_("No Partner") if not partner_id else '')
                    ),
                }
                for key in bucket_keys:
                    entry[key] = 0.0
                partner_buckets[partner_id] = entry
            partner_buckets[partner_id][bucket_key] += amount

        return partner_buckets

    @staticmethod
    def _classify_bucket(days_overdue, buckets):
        for key, _label, day_min, day_max in buckets:
            if day_min is None and days_overdue <= day_max:
                return key
            if day_max is None and days_overdue >= day_min:
                return key
            if day_min is not None and day_max is not None:
                if day_min <= days_overdue <= day_max:
                    return key
        return buckets[-1][0]

    def _build_lines_and_totals(
        self, partner_buckets, show_zero, buckets, options=None,
        currency=None,
    ):
        options = options or {}
        currency = currency or self._eh_monetary_currency(options=options)
        # Lazy unfold is opt-in via options['lazy_expand'] (set by the OWL
        # viewer). Without it the partner row keeps its legacy
        # unfoldable: False shape, so direct compute() callers, export, and
        # the existing test suite see byte-identical lines. Even with the
        # flag, eager_expand (PDF/XLSX) keeps partners non-expandable.
        lazy = bool(options.get('lazy_expand')) and not options.get(
            'eager_expand')
        bucket_keys = [key for key, _l, _mn, _mx in buckets]
        totals = {key: 0.0 for key in bucket_keys}
        totals['total'] = 0.0
        sorted_partner_ids = sorted(
            partner_buckets.keys(),
            key=lambda pid: partner_buckets[pid]['partner_name'].lower(),
        )

        lines = []
        for partner_id in sorted_partner_ids:
            data = partner_buckets[partner_id]
            row_total = self._eh_round_monetary(
                sum(data[key] for key in bucket_keys), currency=currency,
            )
            sum_all = sum(abs(data[key]) for key in bucket_keys)
            if not show_zero and self._eh_is_zero_monetary(
                    sum_all, currency=currency):
                continue

            columns = [
                {
                    'expression_label': key,
                    'value': self._eh_round_monetary(
                        data[key], currency=currency,
                    ),
                }
                for key in bucket_keys
            ]
            columns.append({'expression_label': 'total', 'value': row_total})
            line = {
                'id': (
                    "partner-%s" % partner_id
                    if partner_id else "partner-none"
                ),
                'name': data['partner_name'] or "(no name)",
                'level': 1,
                'columns': columns,
                'unfoldable': False,
                'meta': {
                    'kind': 'partner_aged',
                    'partner_id': partner_id or False,
                },
            }
            if lazy:
                # Mark the partner row as a lazy-expandable leaf. The OWL
                # viewer's existing lazy machinery (onToggleLine ->
                # loadChildren -> expand_line) then fetches this partner's
                # open invoices on demand via expand_account_line below.
                line['unfoldable'] = True
                line['unfolded'] = False
                line['lazy'] = True
                line['has_more'] = False
                line['meta']['expandable'] = True
            lines.append(line)

            for key in bucket_keys:
                totals[key] += data[key]
            totals['total'] += row_total

        totals = {
            key: self._eh_round_monetary(value, currency=currency)
            for key, value in totals.items()
        }
        return lines, totals
