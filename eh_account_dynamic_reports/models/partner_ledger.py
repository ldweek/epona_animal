# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Partner Ledger handler.

Mirrors the General Ledger structure but groups by partner instead of by
account. For each partner that has activity in the period (or an opening
balance from before), the report renders:

* A partner header line (level 0).
* An opening balance line (level 1) reflecting all lines strictly before
  date_from.
* One entry line (level 1) per journal item in the period, with date,
  journal code, move name, account label, line label, debit, credit, and
  running balance.
* A partner total line (level 0) showing the closing balance.

By default only receivable/payable journal items that have a partner_id are
included, matching Odoo Enterprise's partner-ledger scope. Explicit account
or account-type filters replace that default, so a caller can still request
cash or any other partner-referenced account deliberately.

Drill down:

* partner-N: opens the journal items list filtered to the partner and
  the active date range.
* aml-X: opens the specific journal entry form.
"""

import datetime

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL
from odoo.tools.translate import LazyTranslate

from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery

_lt = LazyTranslate(__name__)


class EhPartnerLedgerHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.partner_ledger'
    _inherit = 'eh.account.dynamic.report.handler'
    _description = "Partner Ledger report handler"

    REPORT_CODE = 'partner_ledger'
    REPORT_NAME = _lt("Partner Ledger")
    DEFAULT_ACCOUNT_TYPES = ('asset_receivable', 'liability_payable')

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

        # Lazy screen mode: emit only partner_header (lazy/unfoldable/
        # collapsed) + opening + total per partner, NEVER the per-aml rows.
        # The 38k+ journal items of a real partner ledger come from
        # expand_account_line on demand. Opt-in via options['lazy_expand']
        # (set by the OWL viewer); eager_expand (PDF/XLSX) forces the inlined
        # path so paper output is byte-identical, and direct compute()
        # callers (no flag) keep the legacy inlined shape and the existing
        # test suite stays green.
        lazy = bool(options.get('lazy_expand')) and not options.get(
            'eager_expand')
        if lazy:
            return self._compute_lazy(
                options, company_ids, date_from, date_to,
                posted_only, show_zero,
            )

        opening_by_partner = self._fetch_opening_balances(
            company_ids=company_ids, date_from=date_from,
            posted_only=posted_only, options=options,
        )
        entries = self._fetch_line_entries(
            company_ids=company_ids,
            date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
        )

        company_currency_ids = set(
            self.env['res.company'].browse(
                [int(c) for c in company_ids]).mapped('currency_id').ids)
        currency_ids = {
            e['currency_id'] for e in entries
            if e.get('currency_id')
            and e['currency_id'] not in company_currency_ids
        }
        currency_by_id = {
            c.id: c for c in
            self.env['res.currency'].browse(list(currency_ids))
        }

        lines = self._build_lines(
            opening_by_partner, entries, show_zero, currency_by_id,
            options=options, company_ids=company_ids, currency=currency,
        )

        return {
            'columns': self._build_columns(),
            'lines': lines,
            'totals': {},
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

    # ---- lazy screen path (Wave 0 engine contract) ----

    @api.model
    def _compute_lazy(
        self, options, company_ids, date_from, date_to,
        posted_only, show_zero,
    ):
        """Build the O(partners) skeleton: header + opening + total only.

        No per-aml rows are materialised; each partner header is a
        lazy/unfoldable/collapsed leaf whose children come from
        expand_account_line. The opening balance and the per-partner
        debit/credit period aggregates are cheap GROUP BY queries, so the
        whole render stays O(partners) regardless of journal-item volume.
        """
        currency = self._eh_monetary_currency(
            options=options, company_ids=company_ids,
        )
        opening_by_partner = self._fetch_opening_balances(
            company_ids=company_ids, date_from=date_from,
            posted_only=posted_only, options=options,
        )
        period_by_partner = self._fetch_period_aggregates(
            company_ids=company_ids, date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
        )

        partner_ids = set(opening_by_partner) | set(period_by_partner)
        partner_meta = {}
        if partner_ids:
            for partner in self.env['res.partner'].browse(
                    sorted(partner_ids)).sorted('display_name'):
                partner_meta[partner.id] = {
                    'name': partner.display_name or partner.name or '',
                }

        ordered_partner_ids = sorted(
            partner_ids,
            key=lambda i: partner_meta.get(i, {}).get('name', ''))

        lines = []
        for partner_id in ordered_partner_ids:
            opening = self._eh_round_monetary(
                opening_by_partner.get(partner_id, 0.0), currency=currency,
            )
            agg = period_by_partner.get(partner_id)
            period_debit = self._eh_round_monetary(
                agg['debit'], currency=currency,
            ) if agg else 0.0
            period_credit = self._eh_round_monetary(
                agg['credit'], currency=currency,
            ) if agg else 0.0
            has_activity = bool(agg and agg['count'])
            if not show_zero and self._eh_is_zero_monetary(
                    opening, currency=currency) and not has_activity:
                continue
            closing = self._eh_round_monetary(
                opening + period_debit - period_credit,
                currency=currency,
            )
            meta = partner_meta.get(partner_id, {'name': ''})

            header = self._partner_header_line(partner_id, meta)
            # Flip the header into a lazy-expandable leaf via the shared
            # hook (no-op for any multi-column safety; partner ledger is a
            # single fixed layout). The children come from
            # expand_account_line('partner-N') on demand.
            header['unfoldable'] = True
            header['unfolded'] = False
            header['lazy'] = True
            header['has_more'] = False
            header['meta']['expandable'] = True
            lines.append(header)
            lines.append(self._opening_line(partner_id, opening))
            lines.append(
                self._partner_total_line(partner_id, meta, closing))

        return {
            'columns': self._build_columns(),
            'lines': lines,
            'totals': {},
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': {
                'report_code': self.REPORT_CODE,
                'date_from': self._iso_date(date_from),
                'date_to': self._iso_date(date_to),
                'company_ids': sorted(int(c) for c in company_ids),
                'posted_only': posted_only,
                'show_zero': show_zero,
                'lazy': True,
            },
        }

    @api.model
    def _fetch_period_aggregates(
        self, company_ids, date_from, date_to, posted_only, options,
    ):
        """Per-partner {debit, credit, count} for the period window.

        Cheap GROUP BY aggregate that tells the lazy builder which partners
        have activity (and their closing) without materialising any journal
        item. Same filters as the entry fetch so the figures reconcile to
        what an expand will page.
        """
        query = MoveLineQuery(self.env, company_ids=company_ids)
        query.where_date_range(date_from=date_from, date_to=date_to)
        query.where_raw(SQL("aml.partner_id IS NOT NULL"))
        if posted_only:
            query.where_posted_only()
        self._apply_partner_ledger_filters(query, options)

        query.select_field('partner_id')
        query.select(SQL("SUM(aml.debit)"), 'period_debit')
        query.select(SQL("SUM(aml.credit)"), 'period_credit')
        query.select(SQL("COUNT(aml.id)"), 'row_count')
        query.group_by(SQL("aml.partner_id"))

        rows = query.execute()
        return {
            r['partner_id']: {
                'debit': float(r['period_debit'] or 0.0),
                'credit': float(r['period_credit'] or 0.0),
                'count': int(r['row_count'] or 0),
            }
            for r in rows
        }

    # ---- lazy per-partner aml expand ----

    @api.model
    def _expand_partner_id_from_line_id(self, line_id):
        """Parse 'partner-N' into N. Returns None on any other shape.

        Rejects the compound 'partner-N-opening' / 'partner-N-total' ids:
        only the bare 'partner-N' header is expandable.
        """
        if not line_id or not isinstance(line_id, str):
            return None
        if not line_id.startswith('partner-'):
            return None
        tail = line_id.split('-', 1)[1]
        try:
            return int(tail)
        except (ValueError, TypeError):
            return None

    @api.model
    def expand_account_line(self, options, line_id, offset=0, limit=None):
        """Partner-ledger override of the shared lazy-expand engine.

        Recognises a 'partner-N' line id and returns THAT partner's journal
        items (opening then period entries) ordered by (date, id) as level-2
        'aml-N' children carrying the same debit / credit / running-balance
        columns the inlined path produces, so the children reconcile exactly
        to the partner_total cell (opening + sum(debit) - sum(credit) =
        closing). Paged via the same offset/limit cursor as the account-leaf
        engine: fetch limit+1 to probe has_more.

        Any non-partner id falls through to super() (so an account-N leaf on
        some other report still expands), and any failure returns an empty,
        collapsed page rather than raising (the invariant: a broken expand
        never fans out or crashes the report).
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
            date_from = self._extract_date(options, 'date_from')
            date_to = self._extract_date(options, 'date_to')
        except UserError:
            return empty

        try:
            company_ids = options.get('company_ids') or [self.env.company.id]
            posted_only = bool(options.get('posted_only', True))
            currency = self._eh_monetary_currency(
                options=options, company_ids=company_ids,
            )

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

            opening_by_partner = self._fetch_opening_balances(
                company_ids=company_ids, date_from=date_from,
                posted_only=posted_only, options=options,
                partner_ids=[partner_id],
            )
            opening = self._eh_round_monetary(
                opening_by_partner.get(partner_id, 0.0), currency=currency,
            )

            total_count = self._fetch_line_entry_count(
                company_ids=company_ids,
                date_from=date_from, date_to=date_to,
                posted_only=posted_only, options=options,
                partner_ids=[partner_id],
            )

            prefix_balance = self._fetch_line_entries_prefix_balance(
                company_ids=company_ids,
                date_from=date_from, date_to=date_to,
                posted_only=posted_only, options=options,
                partner_ids=[partner_id], offset=offset,
            )
            entries = self._fetch_line_entries_page(
                company_ids=company_ids,
                date_from=date_from, date_to=date_to,
                posted_only=posted_only, options=options,
                partner_ids=[partner_id], offset=offset, limit=limit,
            )
            has_more = len(entries) > limit
            if has_more:
                entries = entries[:limit]

            # Resolve foreign-currency codes for the bounded page only,
            # mirroring the inlined path's currency badge.
            company_currency_ids = set(
                self.env['res.company'].browse(
                    [int(c) for c in company_ids]).mapped('currency_id').ids)
            currency_ids = {
                e['currency_id'] for e in entries
                if e.get('currency_id')
                and e['currency_id'] not in company_currency_ids
            }
            currency_by_id = {
                c.id: c for c in
                self.env['res.currency'].browse(list(currency_ids))
            }

            # SQL sums only rows preceding this page. No prior journal items
            # are materialised merely to continue the running balance.
            running = self._eh_round_monetary(
                opening + prefix_balance, currency=currency,
            )

            parent_line_id = "partner-%s" % partner_id
            child_lines = []
            for entry in entries:
                debit = self._eh_round_monetary(
                    float(entry.get('debit') or 0.0), currency=currency,
                )
                credit = self._eh_round_monetary(
                    float(entry.get('credit') or 0.0), currency=currency,
                )
                running = self._eh_round_monetary(
                    running + debit - credit, currency=currency,
                )
                child = self._entry_line(
                    entry, debit, credit, running, currency_by_id)
                child['level'] = 2
                child['parent_id'] = parent_line_id
                child['unfolded'] = False
                child['lazy'] = False
                child_lines.append(child)

            return {
                'child_lines': child_lines,
                'has_more': has_more,
                'next_offset': offset + len(entries),
                'total_count': total_count,
            }
        except Exception:
            return empty

    @api.model
    def get_drilldown_action(self, options, line_id):
        """aml-X opens the journal entry form. partner-N opens the journal
        items list filtered to that partner. Other ids return None.
        """
        if line_id and line_id.startswith('aml-'):
            try:
                aml_id = int(line_id.split('-', 1)[1])
            except ValueError:
                return None
            aml = self.env['account.move.line'].browse(aml_id).exists()
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
        if line_id and line_id.startswith('partner-'):
            rest = line_id.split('-', 1)[1]
            if not rest.isdigit():
                return None
            partner_id = int(rest)
            try:
                date_from = self._extract_date(options, 'date_from')
                date_to = self._extract_date(options, 'date_to')
            except Exception:
                return None
            company_ids = options.get('company_ids') or [self.env.company.id]
            domain = [
                ('partner_id', '=', partner_id),
                ('company_id', 'in', list(company_ids)),
                ('date', '>=', self._iso_date(date_from)),
                ('date', '<=', self._iso_date(date_to)),
            ]
            if options.get('posted_only', True):
                domain.append(('parent_state', '=', 'posted'))
            domain += self._partner_ledger_drilldown_filter_domain(options)
            return {
                'type': 'ir.actions.act_window',
                'name': _("Journal Items"),
                'res_model': 'account.move.line',
                'view_mode': 'list,form',
                'domain': domain,
            }
        return None

    # ---- internal helpers ----

    def _apply_partner_ledger_filters(self, query, options):
        """Apply explicit dimensions or Enterprise-style AR/AP default."""
        self.apply_common_filters(query, options)
        if not options.get('account_ids') and not options.get(
                'account_type_ids'):
            query.where_account_types(self.DEFAULT_ACCOUNT_TYPES)
        return query

    def _partner_ledger_drilldown_filter_domain(self, options):
        """Mirror SQL account scope in partner-header drilldown."""
        domain = self._eh_drilldown_filter_domain(options)
        if options.get('account_ids'):
            domain.append(('account_id', 'in', list(options['account_ids'])))
        elif not options.get('account_type_ids'):
            domain.append((
                'account_id.account_type',
                'in',
                list(self.DEFAULT_ACCOUNT_TYPES),
            ))
        return domain

    def _build_columns(self):
        return [
            {'expression_label': 'description', 'name': _("Description"),
             'figure_type': 'string'},
            {'expression_label': 'date', 'name': _("Date"),
             'figure_type': 'string'},
            {'expression_label': 'journal', 'name': _("Journal"),
             'figure_type': 'string'},
            {'expression_label': 'move', 'name': _("Move"),
             'figure_type': 'string'},
            {'expression_label': 'account', 'name': _("Account"),
             'figure_type': 'string'},
            {'expression_label': 'label', 'name': _("Label"),
             'figure_type': 'string'},
            {'expression_label': 'debit', 'name': _("Debit"),
             'figure_type': 'monetary'},
            {'expression_label': 'credit', 'name': _("Credit"),
             'figure_type': 'monetary'},
            {'expression_label': 'balance', 'name': _("Balance"),
             'figure_type': 'monetary'},
            {'expression_label': 'foreign', 'name': _("Amount in Currency"),
             'figure_type': 'string'},
        ]

    def _fetch_opening_balances(
        self, company_ids, date_from, posted_only, options,
        partner_ids=None,
    ):
        query = MoveLineQuery(self.env, company_ids=company_ids)
        query.where_raw(SQL("aml.date < %s", date_from))
        query.where_raw(SQL("aml.partner_id IS NOT NULL"))
        if partner_ids:
            query.where_partners(partner_ids)
        if posted_only:
            query.where_posted_only()
        self._apply_partner_ledger_filters(query, options)

        query.select_field('partner_id')
        query.select(SQL("SUM(aml.balance)"), 'opening_balance')
        query.group_by(SQL("aml.partner_id"))

        rows = query.execute()
        return {
            r['partner_id']: float(r['opening_balance'] or 0.0)
            for r in rows
        }

    def _build_line_entries_query(
        self, company_ids, date_from, date_to, posted_only, options,
        partner_ids=None,
    ):
        query = MoveLineQuery(self.env, company_ids=company_ids)
        query.where_date_range(date_from=date_from, date_to=date_to)
        query.where_raw(SQL("aml.partner_id IS NOT NULL"))
        if partner_ids:
            query.where_partners(partner_ids)
        if posted_only:
            query.where_posted_only()
        self._apply_partner_ledger_filters(query, options)
        return query

    def _select_line_entry_fields(self, query):
        """Attach eager/page projection without changing its row scope."""

        query.select_field('id', alias='aml_id')
        query.select_field('partner_id')
        query.select_field('account_id')
        query.select_field('date')
        query.select_field('debit')
        query.select_field('credit')
        query.select_field('balance')
        query.select_field('amount_currency')
        query.select_field('currency_id')
        query.select_field('name', alias='line_label')
        query.select_field('ref')

        query.select_account_field('code', alias='account_code')
        query.select_account_field('name', alias='account_name')

        query.join_journal()
        query.select(SQL("aj.code"), 'journal_code')
        query.join_partner()
        query.select(SQL("p.name"), 'partner_name')
        query.select(SQL("am.name"), 'move_name')
        return query

    @staticmethod
    def _order_line_entries(query):
        query.order_by('date', 'ASC')
        query.order_by('id', 'ASC')
        return query

    def _fetch_line_entries(
        self, company_ids, date_from, date_to, posted_only, options,
        partner_ids=None,
    ):
        """Eager/export path: preserve complete in-memory rowset."""
        query = self._build_line_entries_query(
            company_ids, date_from, date_to, posted_only, options,
            partner_ids=partner_ids,
        )
        self._select_line_entry_fields(query)

        query.order_by(SQL("aml.partner_id"), 'ASC')
        self._order_line_entries(query)

        return query.execute()

    def _build_line_entries_page_query(
        self, company_ids, date_from, date_to, posted_only, options,
        partner_ids, offset, limit,
    ):
        """Return stable, bounded lazy-page query for shape assertions."""
        query = self._build_line_entries_query(
            company_ids, date_from, date_to, posted_only, options,
            partner_ids=partner_ids,
        )
        self._select_line_entry_fields(query)
        self._order_line_entries(query)
        query.limit(limit + 1)
        query.offset(offset)
        return query

    def _fetch_line_entries_page(
        self, company_ids, date_from, date_to, posted_only, options,
        partner_ids, offset, limit,
    ):
        return self._build_line_entries_page_query(
            company_ids, date_from, date_to, posted_only, options,
            partner_ids, offset, limit,
        ).execute()

    def _fetch_line_entry_count(
        self, company_ids, date_from, date_to, posted_only, options,
        partner_ids,
    ):
        query = self._build_line_entries_query(
            company_ids, date_from, date_to, posted_only, options,
            partner_ids=partner_ids,
        )
        query.select_count(alias='total_count')
        rows = query.execute()
        return int(rows[0]['total_count'] or 0) if rows else 0

    def _fetch_line_entries_prefix_balance(
        self, company_ids, date_from, date_to, posted_only, options,
        partner_ids, offset,
    ):
        """SQL sum of rows before page; never materialise prefix in Python."""
        if offset <= 0:
            return 0.0
        query = self._build_line_entries_query(
            company_ids, date_from, date_to, posted_only, options,
            partner_ids=partner_ids,
        )
        query.select_field('balance')
        self._order_line_entries(query)
        query.limit(offset)
        self.env.flush_all()
        self.env.cr.execute(SQL(
            "SELECT COALESCE(SUM(prefix.balance), 0) FROM (%s) prefix",
            query.build(),
        ))
        row = self.env.cr.fetchone()
        return float(row[0] or 0.0) if row else 0.0

    def _build_lines(
        self, opening_by_partner, entries, show_zero,
        currency_by_id=None, options=None, company_ids=None, currency=None,
    ):
        currency = currency or self._eh_monetary_currency(
            options=options, company_ids=company_ids,
        )
        # Group entries by partner, preserving order.
        entries_by_partner = {}
        partner_meta = {}
        for entry in entries:
            entries_by_partner.setdefault(
                entry['partner_id'], [],
            ).append(entry)
            partner_meta.setdefault(entry['partner_id'], {
                'name': entry.get('partner_name') or '',
            })

        # Partners with opening only need their names fetched separately.
        opening_only_ids = (
            set(opening_by_partner.keys()) - set(entries_by_partner.keys())
        )
        if opening_only_ids:
            for partner in self.env['res.partner'].browse(
                list(opening_only_ids),
            ).sorted('display_name'):
                partner_meta[partner.id] = {
                    'name': partner.display_name or partner.name or '',
                }

        ordered_partner_ids = list(entries_by_partner.keys())
        ordered_partner_ids += sorted(
            opening_only_ids,
            key=lambda i: partner_meta[i]['name'],
        )

        lines = []
        for partner_id in ordered_partner_ids:
            opening = self._eh_round_monetary(
                opening_by_partner.get(partner_id, 0.0), currency=currency,
            )
            partner_entries = entries_by_partner.get(partner_id, [])
            meta = partner_meta[partner_id]

            if not show_zero and self._eh_is_zero_monetary(
                    opening, currency=currency) and not partner_entries:
                continue

            lines.append(self._partner_header_line(partner_id, meta))
            lines.append(self._opening_line(partner_id, opening))

            running = opening
            for entry in partner_entries:
                debit = self._eh_round_monetary(
                    float(entry.get('debit') or 0.0), currency=currency,
                )
                credit = self._eh_round_monetary(
                    float(entry.get('credit') or 0.0), currency=currency,
                )
                running = self._eh_round_monetary(
                    running + debit - credit, currency=currency,
                )
                lines.append(self._entry_line(
                    entry, debit, credit, running, currency_by_id or {}))

            lines.append(self._partner_total_line(partner_id, meta, running))
        return lines

    def _partner_header_line(self, partner_id, meta):
        return {
            'id': "partner-%s" % partner_id,
            'name': meta['name'] or "(no name)",
            'level': 0,
            'columns': [
                {'expression_label': 'date', 'value': ''},
                {'expression_label': 'journal', 'value': ''},
                {'expression_label': 'move', 'value': ''},
                {'expression_label': 'account', 'value': ''},
                {'expression_label': 'label', 'value': ''},
                {'expression_label': 'debit', 'value': ''},
                {'expression_label': 'credit', 'value': ''},
                {'expression_label': 'balance', 'value': ''},
                {'expression_label': 'foreign', 'value': ''},
            ],
            'unfoldable': False,
            'meta': {
                'kind': 'partner_header',
                'partner_id': partner_id,
            },
        }

    def _opening_line(self, partner_id, opening):
        return {
            'id': "partner-%s-opening" % partner_id,
            'name': _("Initial Balance"),
            'level': 1,
            'columns': [
                {'expression_label': 'date', 'value': ''},
                {'expression_label': 'journal', 'value': ''},
                {'expression_label': 'move', 'value': ''},
                {'expression_label': 'account', 'value': ''},
                {'expression_label': 'label', 'value': ''},
                {'expression_label': 'debit', 'value': ''},
                {'expression_label': 'credit', 'value': ''},
                {'expression_label': 'balance', 'value': opening},
                {'expression_label': 'foreign', 'value': ''},
            ],
            'unfoldable': False,
            'meta': {'kind': 'opening_balance', 'partner_id': partner_id},
        }

    def _entry_line(self, entry, debit, credit, running_balance,
                    currency_by_id=None):
        currency_by_id = currency_by_id or {}
        foreign = ''
        cur_id = entry.get('currency_id')
        amount_currency = entry.get('amount_currency')
        foreign_currency = currency_by_id.get(cur_id)
        if foreign_currency and not self._eh_is_zero_monetary(
                float(amount_currency or 0.0), currency=foreign_currency):
            rounded = self._eh_round_monetary(
                float(amount_currency), currency=foreign_currency,
            )
            decimals = max(0, int(foreign_currency.decimal_places or 0))
            foreign = "%.*f %s" % (
                decimals, rounded, foreign_currency.name,
            )
        return {
            'id': "aml-%s" % entry['aml_id'],
            'name': entry.get('ref') or entry.get('line_label') or '',
            'level': 1,
            'columns': [
                {'expression_label': 'date',
                 'value': self._iso_date(entry['date']) if entry.get('date') else None},
                {'expression_label': 'journal',
                 'value': entry.get('journal_code') or ''},
                {'expression_label': 'move',
                 'value': entry.get('move_name') or ''},
                {'expression_label': 'account',
                 'value': "%s %s" % (
                     entry.get('account_code') or '',
                     entry.get('account_name') or '',
                 )},
                {'expression_label': 'label',
                 'value': entry.get('line_label') or ''},
                {'expression_label': 'debit', 'value': debit},
                {'expression_label': 'credit', 'value': credit},
                {'expression_label': 'balance', 'value': running_balance},
                {'expression_label': 'foreign', 'value': foreign},
            ],
            'unfoldable': False,
            'meta': {
                'kind': 'aml',
                'aml_id': entry['aml_id'],
                'partner_id': entry['partner_id'],
                'account_id': entry['account_id'],
            },
        }

    def _partner_total_line(self, partner_id, meta, closing):
        return {
            'id': "partner-%s-total" % partner_id,
            'name': _("Total %s") % (meta['name'] or _("(no name)")),
            'level': 0,
            'columns': [
                {'expression_label': 'date', 'value': ''},
                {'expression_label': 'journal', 'value': ''},
                {'expression_label': 'move', 'value': ''},
                {'expression_label': 'account', 'value': ''},
                {'expression_label': 'label', 'value': ''},
                {'expression_label': 'debit', 'value': ''},
                {'expression_label': 'credit', 'value': ''},
                {'expression_label': 'balance', 'value': closing},
                {'expression_label': 'foreign', 'value': ''},
            ],
            'unfoldable': False,
            'meta': {'kind': 'partner_total', 'partner_id': partner_id},
        }
