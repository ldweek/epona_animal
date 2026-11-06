# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Historical residuals follow accounting dates, never ORM create time."""

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_dynamic_reports', 'integration', 'post_install',
        '-at_install')
class TestHistoricalResidualAccountingDate(EhAccountIntegrationTestCase):

    CUTOFF = fields.Date.to_date('2026-06-30')

    def setUp(self):
        super().setUp()
        for account in (self.account_receivable, self.account_payable):
            if not account.reconcile:
                account.sudo().write({'reconcile': True})

    def _reconcile_pair(
        self, account, source_date, settlement_date, artificial_create_date,
    ):
        receivable = account == self.account_receivable
        source = self.post_balanced_move(
            [
                {
                    'account': account,
                    'debit': 100.0 if receivable else 0.0,
                    'credit': 0.0 if receivable else 100.0,
                    'partner': self.partner_a,
                    'date_maturity': source_date,
                },
                {
                    'account': (
                        self.account_revenue if receivable
                        else self.account_expense
                    ),
                    'debit': 0.0 if receivable else 100.0,
                    'credit': 100.0 if receivable else 0.0,
                },
            ],
            date=source_date,
        )
        settlement = self.post_balanced_move(
            [
                {
                    'account': self.account_cash,
                    'debit': 100.0 if receivable else 0.0,
                    'credit': 0.0 if receivable else 100.0,
                },
                {
                    'account': account,
                    'debit': 0.0 if receivable else 100.0,
                    'credit': 100.0 if receivable else 0.0,
                    'partner': self.partner_a,
                    'date_maturity': settlement_date,
                },
            ],
            date=settlement_date,
        )
        source_line = source.line_ids.filtered(
            lambda line: line.account_id == account,
        )
        settlement_line = settlement.line_ids.filtered(
            lambda line: line.account_id == account,
        )
        (source_line | settlement_line).reconcile()
        partial = (
            source_line.matched_debit_ids
            | source_line.matched_credit_ids
        )
        self.assertEqual(len(partial), 1)
        self.assertEqual(
            fields.Date.to_date(partial.max_date),
            max(source_date, settlement_date),
        )

        # Make create time deliberately disagree with accounting max_date.
        # Production SQL must remain correct regardless of when ORM inserted
        # the reconciliation evidence.
        partial.flush_recordset()
        self.env.cr.execute(
            "UPDATE account_partial_reconcile SET create_date = %s "
            "WHERE id = %s",
            [artificial_create_date, partial.id],
        )
        partial.invalidate_recordset(['create_date'])
        return source_line.id

    def _aged_total(self, code):
        result = self.env[
            'eh.account.dynamic.report.handler.%s' % code
        ].compute({
            'date': {
                'date_from': '2026-01-01',
                'date_to': fields.Date.to_string(self.CUTOFF),
            },
            'company_ids': self.company.ids,
            'posted_only': True,
            'show_zero': False,
        })
        for line in result.get('lines') or []:
            if (line.get('meta') or {}).get('partner_id') == self.partner_a.id:
                for column in line.get('columns') or []:
                    if column.get('expression_label') == 'total':
                        return abs(float(column.get('value') or 0.0))
        return 0.0

    def _statement_due(self, code):
        result = self.env[
            'eh.account.dynamic.report.handler.%s' % code
        ].compute({
            'partner_id': self.partner_a.id,
            'date': {
                'date_from': '2026-01-01',
                'date_to': fields.Date.to_string(self.CUTOFF),
            },
            'company_ids': self.company.ids,
            'posted_only': True,
            'statement_type': 'open_item',
        })
        return abs(float((result.get('totals') or {}).get('amount_due') or 0.0))

    def _aged_drilldown_ids(self, code):
        handler = self.env[
            'eh.account.dynamic.report.handler.%s' % code
        ]
        action = handler.get_drilldown_action({
            'date': {
                'date_from': '2026-01-01',
                'date_to': fields.Date.to_string(self.CUTOFF),
            },
            'company_ids': self.company.ids,
            'posted_only': True,
            'show_zero': False,
        }, 'partner-%d' % self.partner_a.id)
        self.assertIsNotNone(action)
        return set(self.env['account.move.line'].search(action['domain']).ids)

    def test_backdated_reconciliation_created_late_is_closed(self):
        for account in (self.account_receivable, self.account_payable):
            self._reconcile_pair(
                account,
                fields.Date.to_date('2026-05-01'),
                fields.Date.to_date('2026-05-15'),
                '2026-07-15 12:00:00',
            )
        self.assertAlmostEqual(self._aged_total('aged_receivable'), 0.0)
        self.assertAlmostEqual(self._aged_total('aged_payable'), 0.0)
        self.assertAlmostEqual(self._statement_due('customer_statement'), 0.0)
        self.assertAlmostEqual(self._statement_due('vendor_statement'), 0.0)

    def test_future_accounting_reconciliation_created_early_is_open(self):
        source_ids = {}
        for account in (self.account_receivable, self.account_payable):
            source_ids[account.account_type] = self._reconcile_pair(
                account,
                fields.Date.to_date('2026-05-01'),
                fields.Date.to_date('2026-07-15'),
                '2026-06-15 12:00:00',
            )
        self.assertAlmostEqual(self._aged_total('aged_receivable'), 100.0)
        self.assertAlmostEqual(self._aged_total('aged_payable'), 100.0)
        self.assertAlmostEqual(self._statement_due('customer_statement'), 100.0)
        self.assertAlmostEqual(self._statement_due('vendor_statement'), 100.0)
        # Both source lines are fully reconciled today (stored residual zero),
        # but were open at the cutoff. The partner click must resolve the same
        # max_date-backed line ids that produced the historical totals.
        self.assertIn(
            source_ids['asset_receivable'],
            self._aged_drilldown_ids('aged_receivable'),
        )
        self.assertIn(
            source_ids['liability_payable'],
            self._aged_drilldown_ids('aged_payable'),
        )
