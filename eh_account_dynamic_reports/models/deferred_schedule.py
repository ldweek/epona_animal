# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Deferred revenue / expense recognition schedule.

A period-by-period recognition waterfall for every deferral the ledger
carries. One row per deferral contract, one monetary column per period
bucket between date_from and date_to, plus an opening "Before" bucket
(recognition dated before the window), a "Later" bucket (dated after the
window), and a row Total.

Auto-discovery: deferrals are discovered from ERP Heritage's own asset
ledger (eh.asset with deferred_type in {deferred_revenue, deferred_expense}
and its eh.asset.depreciation.line recognition rows), not from any external
deferred-date field. The depreciation_date on each line is the recognition
date that drives the bucketing; that is the standard way to lay out a
deferral recognition waterfall over a reporting window.

SOFT DEPENDENCY: this report reads eh.asset, which ships in
eh_account_assets_pro. This module does NOT depend on it at the manifest
level so eh_account_dynamic_reports stays installable standalone. compute()
soft-probes 'eh.asset' in self.env at runtime; when the model is absent it
returns a warning payload rather than raising. Provider failures also return
an explicit warning row, making the condition visible in the viewer, XLSX,
and PDF instead of looking like a valid zero schedule.

posted_only: when set, unposted recognition lines are excluded. A deferral
with no posted recognition line is excluded entirely; a partially posted
deferral contains only its posted amounts and carries has_unposted metadata.

Big-data posture: the asset search is a small table (one row per contract,
not per move line), bounded by company_ids + deferred_type. Buckets are
resolved once and filled in Python over the contract's recognition lines
only. A very wide window is capped to keep the column count and payload
small (monthly up to ~36 months, then quarterly, then yearly).
"""

import calendar
import logging
from datetime import date as _date

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.translate import LazyTranslate

_lt = LazyTranslate(__name__)
_logger = logging.getLogger(__name__)

# Above this many months in the window we coarsen the bucket granularity so
# the grid and the payload stay bounded.
_MAX_MONTHLY_SPAN = 36
_MAX_QUARTERLY_SPAN = 36  # quarters; ~9 years before falling back to yearly


class EhDeferredScheduleBase(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.deferred_base'
    _inherit = 'eh.account.dynamic.report.handler'
    _description = "Base for deferred recognition schedule handlers"

    # Concrete subclasses set this to 'deferred_revenue' or
    # 'deferred_expense'. The base itself never renders directly.
    DEFERRED_TYPE = None
    _UNSUPPORTED_OPTION_KEYS = frozenset({
        'journal_ids',
        'partner_ids',
        'account_ids',
        'account_type_ids',
        'analytic_account_ids',
        'analytic_plan_ids',
        'show_zero',
    })

    @api.model
    def _eh_asset_available(self):
        """Soft-probe: is the optional eh.asset model installed?"""
        try:
            return 'eh.asset' in self.env
        except Exception:  # pragma: no cover - defensive
            return False

    @api.model
    def compute(self, options):
        date_from = self._extract_date(options, 'date_from')
        date_to = self._extract_date(options, 'date_to')
        company_ids = options.get('company_ids') or [self.env.company.id]
        posted_only = bool(options.get('posted_only', True))
        currency = self._eh_monetary_currency(
            options=options, company_ids=company_ids,
        )

        meta = {
            'report_code': self.REPORT_CODE,
            'deferred_type': self.DEFERRED_TYPE,
            'date_from': self._iso_date(date_from),
            'date_to': self._iso_date(date_to),
            'company_ids': sorted(int(c) for c in company_ids),
            'posted_only': posted_only,
            'unsupported_option_keys': sorted(
                self._UNSUPPORTED_OPTION_KEYS),
        }

        buckets = self._resolve_period_buckets(date_from, date_to)
        period_labels = (
            [_("Before")]
            + [b['label'] for b in buckets]
            + [_("Later")]
        )

        columns = self._build_period_columns(period_labels)

        # Optional dependency absent is expected deployment state, but must be
        # visible in every presentation rather than masquerading as zero data.
        if not self._eh_asset_available():
            meta['module_not_installed'] = True
            meta['note'] = _(
                "Deferred schedules require the Asset Management module "
                "(eh_account_assets_pro). Install it to populate this "
                "report.")
            return self._warning_payload(
                columns, meta, 'module_not_installed', meta['note'])

        try:
            lines, grand_total = self._build_schedule_lines(
                options=options, company_ids=company_ids,
                date_from=date_from, date_to=date_to,
                posted_only=posted_only, buckets=buckets,
                currency=currency,
            )
        except Exception:  # noqa: BLE001 - optional provider boundary
            _logger.warning(
                "Deferred schedule provider failed for report %s",
                self.REPORT_CODE,
                exc_info=True,
            )
            meta['provider_failed'] = True
            meta['note'] = _(
                "Deferred schedule could not be read from Asset Management. "
                "Check provider configuration and your access rights.")
            return self._warning_payload(
                columns, meta, 'provider_failed', meta['note'])

        return {
            'columns': columns,
            'lines': lines,
            'totals': {
                'amount': self._eh_round_monetary(
                    grand_total, currency=currency,
                ),
                'total': self._eh_round_monetary(
                    grand_total, currency=currency,
                ),
            },
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': meta,
        }

    @api.model
    def _warning_payload(self, columns, meta, code, message):
        """Return explicit, export-visible optional-provider warning."""
        warning = {'code': code, 'message': message}
        meta['warnings'] = [warning]
        return {
            'columns': columns,
            'lines': [{
                'id': 'warning-%s' % code.replace('_', '-'),
                'name': message,
                'level': 0,
                'columns': [
                    {
                        'expression_label': column.get('expression_label'),
                        'value': '',
                    }
                    for column in columns[1:]
                ],
                'unfoldable': False,
                'meta': {'kind': 'warning', 'warning_code': code},
            }],
            'totals': {'amount': 0.0, 'total': 0.0},
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': meta,
        }

    # ---- column layout ----

    @api.model
    def _build_period_columns(self, period_labels):
        """Account label column + one monetary column per bucket + Total.

        Reuses the sectioned base's horizontal-layout idiom but is built
        here so this handler need not inherit the sectioned base (its row
        layout is a recognition grid, not account sections).
        """
        columns = [
            {'expression_label': 'asset', 'name': _("Deferral"),
             'figure_type': 'string'},
        ]
        for idx, label in enumerate(period_labels, start=1):
            columns.append({
                'expression_label': 'period_%d' % idx,
                'name': label, 'figure_type': 'monetary',
            })
        columns.append({
            'expression_label': 'total', 'name': _("Total"),
            'figure_type': 'monetary',
        })
        return columns

    # ---- bucket resolution ----

    @api.model
    def _resolve_period_buckets(self, date_from, date_to):
        """Return an ordered list of in-window buckets.

        Each bucket is {'label', 'start', 'end'} with inclusive bounds.
        Granularity coarsens for wide windows: monthly up to ~36 months,
        quarterly up to ~9 years, yearly beyond. The 'Before' and 'Later'
        edge buckets are added by the caller, not here.
        """
        if not date_from or not date_to or date_to < date_from:
            return []
        months = ((date_to.year - date_from.year) * 12
                  + (date_to.month - date_from.month)) + 1
        if months <= _MAX_MONTHLY_SPAN:
            return self._month_buckets(date_from, date_to)
        quarters = (months + 2) // 3
        if quarters <= _MAX_QUARTERLY_SPAN:
            return self._quarter_buckets(date_from, date_to)
        return self._year_buckets(date_from, date_to)

    @staticmethod
    def _month_end(year, month):
        return _date(year, month, calendar.monthrange(year, month)[1])

    @api.model
    def _month_buckets(self, date_from, date_to):
        buckets = []
        year, month = date_from.year, date_from.month
        while (year, month) <= (date_to.year, date_to.month):
            start = _date(year, month, 1)
            end = self._month_end(year, month)
            # Clip the first/last bucket to the window edges.
            start = max(start, date_from)
            end = min(end, date_to)
            buckets.append({
                'label': "%04d-%02d" % (year, month),
                'start': start, 'end': end,
            })
            if month == 12:
                year, month = year + 1, 1
            else:
                month += 1
        return buckets

    @api.model
    def _quarter_buckets(self, date_from, date_to):
        buckets = []
        year = date_from.year
        quarter = (date_from.month - 1) // 3 + 1
        while True:
            q_start_month = (quarter - 1) * 3 + 1
            q_end_month = q_start_month + 2
            start = _date(year, q_start_month, 1)
            end = self._month_end(year, q_end_month)
            if start > date_to:
                break
            buckets.append({
                'label': "%04d-Q%d" % (year, quarter),
                'start': max(start, date_from),
                'end': min(end, date_to),
            })
            if quarter == 4:
                year, quarter = year + 1, 1
            else:
                quarter += 1
        return buckets

    @api.model
    def _year_buckets(self, date_from, date_to):
        buckets = []
        for year in range(date_from.year, date_to.year + 1):
            start = max(_date(year, 1, 1), date_from)
            end = min(_date(year, 12, 31), date_to)
            buckets.append({
                'label': "%04d" % year, 'start': start, 'end': end,
            })
        return buckets

    # ---- schedule build ----

    @api.model
    def _discover_assets(self, company_ids):
        """Return the eh.asset recordset in scope for this deferred type.

        Bounded by company + deferred_type. Excludes draft/cancelled-like
        states defensively when the state field is present.
        """
        # Never sudo an optional provider read. Its ACLs and company rules are
        # part of the disclosure boundary; an access failure becomes an
        # explicit warning payload in compute().
        Asset = self.env['eh.asset']
        return Asset.search(self._asset_discovery_domain(
            Asset, company_ids))

    @api.model
    def _asset_discovery_domain(self, Asset, company_ids):
        """Build the optional provider domain without requiring it installed."""
        domain = [
            ('deferred_type', '=', self.DEFERRED_TYPE),
            ('company_id', 'in', list(company_ids)),
        ]
        if 'state' in Asset._fields:
            selection = Asset._fields['state'].selection
            if isinstance(selection, str):
                selection = getattr(Asset, selection)()
            if callable(selection):
                selection = selection(Asset)
            available = {key for key, _label in (selection or ())}
            excluded = sorted(
                available.intersection({'draft', 'cancel', 'cancelled'}))
            if excluded:
                domain.append(('state', 'not in', excluded))
        return domain

    @api.model
    def _build_schedule_lines(
        self, options, company_ids, date_from, date_to, posted_only, buckets,
        currency=None,
    ):
        currency = currency or self._eh_monetary_currency(
            options=options, company_ids=company_ids,
        )
        assets = self._discover_assets(company_ids)
        lines = []
        grand_total = 0.0
        # Column count = Before + buckets + Later.
        n_periods = len(buckets) + 2

        for asset in assets:
            dep_lines = asset.depreciation_line_ids
            # bucket_amounts index 0 = Before, 1..len(buckets) = in-window,
            # last = Later.
            bucket_amounts = [0.0] * n_periods
            has_unposted = False
            included_lines = 0
            for dl in dep_lines:
                rec_date = dl.depreciation_date
                is_posted = bool(dl.is_posted)
                if not is_posted:
                    has_unposted = True
                    if posted_only:
                        continue
                if not rec_date:
                    continue
                included_lines += 1
                amount = float(dl.amount or 0.0)
                idx = self._bucket_index_for_date(
                    rec_date, date_from, date_to, buckets)
                bucket_amounts[idx] += amount

            # "Posted entries only" must not leave a zero row representing a
            # wholly unposted plan; it is outside posted-ledger scope.
            if posted_only and not included_lines:
                continue

            row_total = self._eh_round_monetary(
                sum(bucket_amounts), currency=currency,
            )
            grand_total += row_total

            columns = []
            for i in range(n_periods):
                columns.append({
                    'expression_label': 'period_%d' % (i + 1),
                    'value': self._eh_round_monetary(
                        bucket_amounts[i], currency=currency,
                    ),
                })
            columns.append({
                'expression_label': 'total', 'value': row_total,
            })

            # The P&L recognition account (revenue or expense) the schedule
            # releases into is depreciation_account_id for both deferred
            # flavours; asset_account_id is the balance-sheet holding side.
            recognition_account = asset.depreciation_account_id
            lines.append({
                'id': "asset-%s" % asset.id,
                'name': asset.display_name,
                'level': 1,
                'columns': columns,
                'unfoldable': False,
                'meta': {
                    'kind': 'deferral',
                    'asset_id': asset.id,
                    'has_unposted': has_unposted,
                    'posted_only': posted_only,
                    'recognition_account_id': (
                        recognition_account.id if recognition_account
                        else False),
                },
            })

        return lines, grand_total

    @staticmethod
    def _bucket_index_for_date(rec_date, date_from, date_to, buckets):
        """Return the column index for a recognition date.

        0 = Before window, 1..len(buckets) = the matching in-window
        bucket, len(buckets)+1 = Later. A date that falls in the window
        but (defensively) matches no bucket lands in Later so the Total
        still foots.
        """
        if rec_date < date_from:
            return 0
        if rec_date > date_to:
            return len(buckets) + 1
        for i, bucket in enumerate(buckets):
            if bucket['start'] <= rec_date <= bucket['end']:
                return i + 1
        return len(buckets) + 1

    # ---- drilldown ----

    @api.model
    def get_drilldown_action(self, options, line_id):
        """A deferral row opens the journal items posted by that asset's
        recognition moves within the report window."""
        if not line_id or not isinstance(line_id, str):
            return None
        if not line_id.startswith('asset-'):
            return None
        if not self._eh_asset_available():
            return None
        try:
            asset_id = int(line_id.split('-', 1)[1])
        except (ValueError, IndexError):
            return None
        try:
            date_from = self._extract_date(options, 'date_from')
            date_to = self._extract_date(options, 'date_to')
        except UserError:
            return None
        try:
            asset = self.env['eh.asset'].browse(asset_id).exists()
            if not asset:
                return None
            asset._eh_check_access('read')
            requested_companies = {
                int(company_id)
                for company_id in (
                    options.get('company_ids') or [self.env.company.id]
                )
            }
            if (
                asset.company_id not in self.env.companies
                or asset.company_id.id not in requested_companies
            ):
                return None
            move_ids = asset.depreciation_line_ids.mapped('move_id').ids
        except Exception:  # pragma: no cover - defensive
            return None
        if not move_ids:
            return None
        column = options.get('_eh_column_expression') or 'total'
        buckets = self._resolve_period_buckets(date_from, date_to)
        period_count = len(buckets) + 2
        domain = [('move_id', 'in', move_ids)]
        if column == 'total':
            pass
        elif isinstance(column, str) and column.startswith('period_'):
            try:
                period_index = int(column.split('_', 1)[1])
            except (TypeError, ValueError, IndexError):
                return None
            if not 1 <= period_index <= period_count:
                return None
            if period_index == 1:
                domain.append(('date', '<', self._iso_date(date_from)))
            elif period_index == period_count:
                domain.append(('date', '>', self._iso_date(date_to)))
            else:
                bucket = buckets[period_index - 2]
                domain.extend([
                    ('date', '>=', self._iso_date(bucket['start'])),
                    ('date', '<=', self._iso_date(bucket['end'])),
                ])
        else:
            return None
        if options.get('posted_only', True):
            domain.append(('parent_state', '=', 'posted'))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Recognition Entries"),
            'res_model': 'account.move.line',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': domain,
            'context': {'search_default_group_move': 1},
        }


class EhDeferredRevenueHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.deferred_revenue'
    _inherit = 'eh.account.dynamic.report.handler.deferred_base'
    _description = "Deferred Revenue schedule handler"

    REPORT_CODE = 'deferred_revenue'
    REPORT_NAME = _lt("Deferred Revenue")
    DEFERRED_TYPE = 'deferred_revenue'


class EhDeferredExpenseHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.deferred_expense'
    _inherit = 'eh.account.dynamic.report.handler.deferred_base'
    _description = "Deferred Expense schedule handler"

    REPORT_CODE = 'deferred_expense'
    REPORT_NAME = _lt("Deferred Expense")
    DEFERRED_TYPE = 'deferred_expense'
