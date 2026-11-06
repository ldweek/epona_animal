# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Customer / vendor statement handler.

Renders a partner-scoped statement designed to be sent TO the partner: a
list of every open invoice and payment on their receivable / payable
account at date_to, plus an opening balance line and a final amount due.

Two flavours selected via options['statement_type']:

* 'open_item' (default): only lines whose historical residual at date_to
  is non zero. The "what you owe us right now" view.
* 'activity': every line with date_from <= date <= date_to, regardless of
  reconciliation status. The "everything that happened in this period"
  view.

The handler uses the same partial-reconcile rewind as the aged report so
historical statements (date_to in the past) are accurate. Sign is always
positive: a customer statement shows what the customer owes us, a vendor
statement shows what we owe the vendor.

Drill down: each line opens the underlying journal entry form.
"""

import datetime

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL
from odoo.tools.translate import LazyTranslate

from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery

_lt = LazyTranslate(__name__)


class EhCustomerStatementHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.customer_statement'
    _inherit = 'eh.account.dynamic.report.handler'
    _description = "Customer statement report handler"

    REPORT_CODE = 'customer_statement'
    REPORT_NAME = _lt("Customer Statement")
    ACCOUNT_TYPE = 'asset_receivable'
    SIGN = 1
    DIRECTION_LABEL = _lt("owes us")

    @api.model
    def build_default_options(self):
        opts = super().build_default_options()
        # Statement default: month to date, single partner via options.
        today = fields.Date.context_today(self)
        opts['date'] = {
            'mode': 'range',
            'date_from': today.replace(day=1).isoformat(),
            'date_to': today.isoformat(),
        }
        opts['statement_type'] = 'open_item'
        opts['partner_id'] = False
        return opts

    @api.model
    def compute(self, options):
        partner_id = options.get('partner_id')
        if not partner_id:
            raise UserError(_(
                "%s requires options['partner_id'].",
                self.REPORT_NAME,
            ))
        partner = self.env['res.partner'].browse(int(partner_id)).exists()
        if not partner:
            raise UserError(_("Unknown partner %s.") % partner_id)

        date_from = self._extract_date(options, 'date_from')
        date_to = self._extract_date(options, 'date_to')
        company_ids = options.get('company_ids') or [self.env.company.id]
        posted_only = bool(options.get('posted_only', True))
        statement_type = options.get('statement_type', 'open_item')
        currency = self._eh_monetary_currency(
            options=options, company_ids=company_ids,
        )

        opening = self._fetch_opening_balance(
            partner_id=partner.id, company_ids=company_ids,
            date_from=date_from, posted_only=posted_only, options=options,
        )
        rows = self._fetch_statement_rows(
            partner_id=partner.id, company_ids=company_ids,
            date_from=date_from, date_to=date_to,
            posted_only=posted_only, statement_type=statement_type,
            options=options,
        )
        lines, total_due = self._build_lines(
            opening, rows, date_to, statement_type,
            options=options, company_ids=company_ids, currency=currency,
        )

        return {
            'columns': self._build_columns(),
            'lines': lines,
            'totals': {
                'amount_due': self._eh_round_monetary(
                    total_due, currency=currency,
                ),
            },
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': {
                'report_code': self.REPORT_CODE,
                'partner_id': partner.id,
                'partner_name': partner.display_name,
                'date_from': self._iso_date(date_from),
                'date_to': self._iso_date(date_to),
                'company_ids': sorted(int(c) for c in company_ids),
                'posted_only': posted_only,
                'statement_type': statement_type,
                'direction_label': self.DIRECTION_LABEL,
                'amount_due': self._eh_round_monetary(
                    total_due, currency=currency,
                ),
            },
        }

    @api.model
    def get_drilldown_action(self, options, line_id):
        if not line_id or not line_id.startswith('aml-'):
            return None
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

    # ---- internal helpers ----

    def _build_columns(self):
        return [
            {'expression_label': 'description', 'name': _("Description"),
             'figure_type': 'string'},
            {'expression_label': 'date', 'name': _("Date"),
             'figure_type': 'string'},
            {'expression_label': 'reference', 'name': _("Reference"),
             'figure_type': 'string'},
            {'expression_label': 'due_date', 'name': _("Due"),
             'figure_type': 'string'},
            {'expression_label': 'charge', 'name': _("Charge"),
             'figure_type': 'monetary'},
            {'expression_label': 'payment', 'name': _("Payment"),
             'figure_type': 'monetary'},
            {'expression_label': 'balance', 'name': _("Balance"),
             'figure_type': 'monetary'},
        ]

    def _fetch_opening_balance(
        self, partner_id, company_ids, date_from, posted_only, options=None,
    ):
        """Sum of partner's residual on the relevant account before date_from.

        Uses the same partial-reconcile rewind as the aged report so the
        opening balance reflects the historical state on (date_from - 1).
        """
        before = date_from - datetime.timedelta(days=1)
        query = MoveLineQuery(self.env, company_ids=company_ids)
        query.where_account_types((self.ACCOUNT_TYPE,))
        query.where_partners([partner_id])
        query.where_raw(SQL("aml.date <= %s", before))
        if posted_only:
            query.where_posted_only()
        self.apply_common_filters(query, options or {})
        residual_expr = SQL("""(
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
        )""", before, before)
        query.select(SQL("SUM(%s)", residual_expr), 'opening')
        rows = query.execute()
        if not rows:
            return 0.0
        return float(rows[0].get('opening') or 0.0) * self.SIGN

    def _fetch_statement_rows(
        self, partner_id, company_ids, date_from, date_to,
        posted_only, statement_type, options=None,
    ):
        query = MoveLineQuery(self.env, company_ids=company_ids)
        query.where_account_types((self.ACCOUNT_TYPE,))
        query.where_partners([partner_id])
        if posted_only:
            query.where_posted_only()
        self.apply_common_filters(query, options or {})

        residual_expr = SQL("""(
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

        if statement_type == 'open_item':
            # A true open-item statement lists every line still OPEN (nonzero
            # residual) as of date_to, at its RESIDUAL, regardless of when it
            # was raised: no date_from lower bound and no separate opening
            # carry. This is what fixes the overstatement - a period settlement
            # of a prior invoice drops that item to residual 0 and out of the
            # statement, instead of the old design which carried the prior
            # invoice in the opening but excluded its clearing payment.
            query.where_date_range(date_to=date_to)
            query.where_raw(SQL("%s <> 0", residual_expr))
            query.select(residual_expr, 'residual')
        else:
            query.where_date_range(date_from=date_from, date_to=date_to)

        query.select_field('id', alias='aml_id')
        query.select_field('date')
        query.select_field('date_maturity')
        query.select_field('debit')
        query.select_field('credit')
        query.select_field('balance')
        query.select_field('name', alias='line_label')
        query.select_field('ref')
        query.join_journal()
        query.select(SQL("aj.code"), 'journal_code')
        query.select(SQL("am.name"), 'move_name')
        query.order_by('date', 'ASC')
        query.order_by('id', 'ASC')
        return query.execute()

    def _build_lines(
        self, opening, rows, date_to, statement_type='open_item',
        options=None, company_ids=None, currency=None,
    ):
        """Compose the statement lines and return (lines, total_due).

        An open-item statement lists open items at their residual with no
        opening carry (each item shown once, at what is still outstanding). A
        full/activity statement opens with the prior balance and shows each
        line's gross charge/payment movement.
        """
        currency = currency or self._eh_monetary_currency(
            options=options, company_ids=company_ids,
        )
        lines = []
        is_open_item = statement_type == 'open_item'
        running = 0.0 if is_open_item else opening
        if not is_open_item:
            # Opening line (activity statement only).
            lines.append({
                'id': 'opening',
                'name': _("Opening Balance"),
                'level': 1,
                'columns': [
                    {'expression_label': 'date', 'value': ''},
                    {'expression_label': 'reference', 'value': ''},
                    {'expression_label': 'due_date', 'value': ''},
                    {'expression_label': 'charge', 'value': ''},
                    {'expression_label': 'payment', 'value': ''},
                    {'expression_label': 'balance',
                     'value': self._eh_round_monetary(
                         running, currency=currency,
                     )},
                ],
                'unfoldable': False,
                'meta': {'kind': 'opening'},
            })
        for row in rows:
            if is_open_item:
                balance = float(row.get('residual') or 0.0) * self.SIGN
            else:
                balance = float(row.get('balance') or 0.0) * self.SIGN
            if balance >= 0:
                charge_val = self._eh_round_monetary(
                    balance, currency=currency,
                )
                payment_val = None
            else:
                charge_val = None
                payment_val = self._eh_round_monetary(
                    -balance, currency=currency,
                )
            running = self._eh_round_monetary(
                running + balance, currency=currency,
            )
            label = (
                row.get('move_name')
                or row.get('ref')
                or row.get('line_label')
                or ''
            )
            lines.append({
                'id': "aml-%s" % row['aml_id'],
                'name': label,
                'level': 1,
                'columns': [
                    {'expression_label': 'date',
                     'value': self._iso_date(row['date']) if row.get('date') else None},
                    {'expression_label': 'reference',
                     'value': row.get('ref') or row.get('line_label') or ''},
                    {'expression_label': 'due_date',
                     'value': self._iso_date(row['date_maturity']) if row.get('date_maturity') else None},
                    {'expression_label': 'charge', 'value': charge_val},
                    {'expression_label': 'payment', 'value': payment_val},
                    {'expression_label': 'balance', 'value': running},
                ],
                'unfoldable': False,
                'meta': {
                    'kind': 'aml',
                    'aml_id': row['aml_id'],
                    'move_name': row.get('move_name'),
                },
            })
        # Closing line summarises the running balance as "amount due".
        # DIRECTION_LABEL is a lazy-translated class attribute; cast to
        # str() so the active language is resolved here, not at module
        # load time, before being interpolated.
        lines.append({
            'id': 'amount_due',
            'name': _("Amount %(direction)s as of %(date)s") % {
                'direction': str(self.DIRECTION_LABEL),
                'date': self._iso_date(date_to),
            },
            'level': 0,
            'columns': [
                {'expression_label': 'date', 'value': ''},
                {'expression_label': 'reference', 'value': ''},
                {'expression_label': 'due_date', 'value': ''},
                {'expression_label': 'charge', 'value': ''},
                {'expression_label': 'payment', 'value': ''},
                {
                    'expression_label': 'balance',
                    'value': self._eh_round_monetary(
                        running, currency=currency,
                    ),
                },
            ],
            'unfoldable': False,
            'meta': {'kind': 'amount_due'},
        })
        return lines, running


class EhVendorStatementHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.vendor_statement'
    _inherit = 'eh.account.dynamic.report.handler.customer_statement'
    _description = "Vendor statement report handler"

    REPORT_CODE = 'vendor_statement'
    REPORT_NAME = _lt("Vendor Statement")
    ACCOUNT_TYPE = 'liability_payable'
    SIGN = -1
    DIRECTION_LABEL = _lt("we owe vendor")
