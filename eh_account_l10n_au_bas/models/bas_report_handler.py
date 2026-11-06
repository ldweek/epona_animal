# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Dynamic-report adapter for immutable Australian BAS run snapshots."""

from datetime import date

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.translate import LazyTranslate


_lt = LazyTranslate(__name__)


class EhAustralianBasReportHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.au_bas'
    _inherit = 'eh.account.dynamic.report.handler.sectioned'
    _description = "Australian BAS dynamic report handler"

    REPORT_CODE = 'au_bas'
    REPORT_NAME = _lt("Australian BAS")
    _UNSUPPORTED_OPTION_KEYS = frozenset({
        'journal_ids', 'partner_ids', 'account_ids',
        'analytic_account_ids', 'analytic_plan_ids',
        'comparison', 'comparative',
    })

    @api.model
    def build_default_options(self):
        options = super().build_default_options()
        today = fields.Date.context_today(self)
        if 7 <= today.month <= 9:
            date_from, date_to = date(today.year, 7, 1), date(today.year, 9, 30)
        elif today.month >= 10:
            date_from, date_to = date(today.year, 10, 1), date(today.year, 12, 31)
        elif today.month <= 3:
            date_from, date_to = date(today.year, 1, 1), date(today.year, 3, 31)
        else:
            date_from, date_to = date(today.year, 4, 1), date(today.year, 6, 30)
        options['date'] = {
            'mode': 'range',
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
        }
        return options

    @api.model
    def compute(self, options):
        date_from = self._extract_date(options, 'date_from')
        date_to = self._extract_date(options, 'date_to')
        company_ids = options.get('company_ids') or [self.env.company.id]
        if len(set(company_ids)) != 1:
            raise UserError(_(
                "Australian BAS reports one legal entity at a time. Select "
                "exactly one company."
            ))
        company = self.env['res.company'].browse(company_ids).exists()
        if len(company) != 1 or company not in self.env.companies:
            raise UserError(_(
                "The selected BAS company is not enabled in the current "
                "company scope."
            ))
        run = self.env['eh.bas.run'].search([
            ('company_id', '=', company.id),
            ('date_from', '=', date_from),
            ('date_to', '=', date_to),
            ('state', 'in', ('computed', 'lodged')),
        ], order='amendment_no desc, id desc', limit=1)
        if not run:
            raise UserError(_(
                "No computed or lodged BAS snapshot exists for %(company)s "
                "from %(start)s to %(end)s.",
                company=company.display_name,
                start=date_from,
                end=date_to,
            ))

        snapshot_lines = run.line_ids.sorted(
            key=lambda line: (line.label_sequence, line.label_code or '')
        )
        if run.is_simpler_bas:
            snapshot_lines = snapshot_lines.filtered('is_lodgeable')
        currency = run.company_id.currency_id
        lines = []
        amounts = {}
        for line in snapshot_lines:
            amount = self._eh_round_monetary(
                line.amount or 0.0, currency=currency,
            )
            amounts[line.label_code] = amount
            lines.append({
                'id': 'au-bas-line-%s' % line.id,
                'name': '%s — %s' % (line.label_code, line.label_name),
                'level': 1,
                'columns': [{
                    'expression_label': 'amount',
                    'value': amount,
                }],
                'unfoldable': False,
                'meta': {
                    'kind': 'bas_label',
                    'run_id': run.id,
                    'label_code': line.label_code,
                    'aggregation': line.label_aggregation,
                    'is_lodgeable': line.is_lodgeable,
                },
            })
        gst_net = self._eh_round_monetary(
            amounts.get('1A', 0.0) - amounts.get('1B', 0.0),
            currency=currency,
        )
        lines.append(self._computed_line(
            'au-bas-gst-net', _("Net GST payable / (refundable)"),
            gst_net, kind='gst_net', currency=currency,
        ))
        return {
            'columns': self._build_two_column_layout(
                label_name=_("BAS label"), amount_name=_("Amount"),
            ),
            'lines': lines,
            'totals': {
                'g1': amounts.get('G1', 0.0),
                'gst_on_sales': amounts.get('1A', 0.0),
                'gst_on_purchases': amounts.get('1B', 0.0),
                'gst_net': gst_net,
                'amount': gst_net,
            },
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': {
                'report_code': self.REPORT_CODE,
                'run_id': run.id,
                'run_state': run.state,
                'amendment_no': run.amendment_no,
                'date_from': self._iso_date(date_from),
                'date_to': self._iso_date(date_to),
                'company_ids': [company.id],
                'reporting_basis': run.reporting_basis,
                'is_simpler_bas': run.is_simpler_bas,
                'currency_id': currency.id,
            },
        }
