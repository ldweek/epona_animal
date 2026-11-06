# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Aged Payable handler tests.

The handler is a thin sibling of Aged Receivable: same bucket logic, same
sectioning, different ACCOUNT_TYPES and SIGN. These tests focus on the
differences (sign convention, scope to liability_payable accounts) and
defer broader bucket coverage to the AR test suite.
"""

from datetime import timedelta

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_dynamic_reports', 'integration', 'post_install', '-at_install')
class TestAgedPayableHandler(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env[
            'eh.account.dynamic.report.handler.aged_payable'
        ]
        cls.report = cls.env['eh.account.dynamic.report'].search(
            [('code', '=', 'aged_payable')], limit=1,
        )
        if not cls.report:
            cls.report = cls.env['eh.account.dynamic.report'].create({
                'code': 'aged_payable',
                'name': 'Aged Payable',
                'handler_model':
                    'eh.account.dynamic.report.handler.aged_payable',
            })

    def setUp(self):
        super().setUp()
        self.date_to = fields.Date.from_string('2026-12-31')
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
        }

    def _post_payable(self, partner, amount, days_overdue):
        """Post a vendor bill style entry: payable credited, expense debited.
        Sets date_maturity at (date_to - days_overdue).
        """
        date_maturity = self.date_to - timedelta(days=days_overdue)
        post_date = fields.Date.from_string('2026-06-15')
        return self.post_balanced_move(
            [
                {'account': self.account_payable, 'credit': amount,
                 'partner': partner, 'date_maturity': date_maturity},
                {'account': self.account_expense, 'debit': amount},
            ],
            date=post_date,
        )

    @staticmethod
    def _line_for_partner(result, partner_id):
        for line in result['lines']:
            if (line.get('meta') or {}).get('partner_id') == partner_id:
                return line
        return None

    @staticmethod
    def _bucket(line, key):
        for col in line['columns']:
            if col['expression_label'] == key:
                return col['value']
        return None

    # ---- core ----

    def test_payable_amounts_are_positive_after_sign_flip(self):
        self._post_payable(self.partner_a, 100.0, days_overdue=20)
        result = self.handler.compute(self.options)
        line = self._line_for_partner(result, self.partner_a.id)
        self.assertIsNotNone(line)
        self.assertAlmostEqual(self._bucket(line, 'bucket_30'), 100.0, places=2)
        # Total is positive.
        self.assertGreaterEqual(self._bucket(line, 'total'), 0.0)

    def test_partnerless_payable_is_included(self):
        maturity = self.date_to - timedelta(days=20)
        self.post_balanced_move([
            {'account': self.account_payable, 'credit': 175.0,
             'date_maturity': maturity},
            {'account': self.account_expense, 'debit': 175.0},
        ], date=fields.Date.from_string('2026-06-15'))
        result = self.handler.compute(self.options)
        line = next(
            item for item in result['lines']
            if item['id'] == 'partner-none')
        self.assertAlmostEqual(self._bucket(line, 'bucket_30'), 175.0, places=2)
        self.assertAlmostEqual(result['totals']['total'], 175.0, places=2)

    def test_partial_vendor_payment_reports_remaining_residual(self):
        bill = self._post_payable(self.partner_a, 100.0, days_overdue=20)
        payment = self.post_balanced_move([
            {'account': self.account_payable, 'debit': 35.0,
             'partner': self.partner_a},
            {'account': self.account_cash, 'credit': 35.0},
        ], date=fields.Date.from_string('2026-07-15'))
        (bill.line_ids | payment.line_ids).filtered(
            lambda line: line.account_id == self.account_payable
        ).reconcile()

        line = self._line_for_partner(
            self.handler.compute(self.options), self.partner_a.id)
        self.assertAlmostEqual(self._bucket(line, 'total'), 65.0, places=2)

    def test_scope_excludes_receivable_accounts(self):
        # A receivable line should not appear in Aged Payable.
        date_maturity = self.date_to - timedelta(days=30)
        self.post_balanced_move(
            [
                {'account': self.account_receivable, 'debit': 200.0,
                 'partner': self.partner_a, 'date_maturity': date_maturity},
                {'account': self.account_revenue, 'credit': 200.0},
            ],
            date=fields.Date.from_string('2026-06-15'),
        )
        # Plus a real payable.
        self._post_payable(self.partner_b, 50.0, days_overdue=10)

        result = self.handler.compute(self.options)
        # partner_a (receivable only) should not appear.
        self.assertIsNone(self._line_for_partner(result, self.partner_a.id))
        # partner_b (payable) should.
        self.assertIsNotNone(self._line_for_partner(result, self.partner_b.id))

    def test_multiple_buckets_for_one_partner(self):
        self._post_payable(self.partner_a, 30.0, days_overdue=-5)
        self._post_payable(self.partner_a, 60.0, days_overdue=20)
        self._post_payable(self.partner_a, 90.0, days_overdue=200)
        result = self.handler.compute(self.options)
        line = self._line_for_partner(result, self.partner_a.id)
        self.assertAlmostEqual(self._bucket(line, 'not_due'), 30.0, places=2)
        self.assertAlmostEqual(self._bucket(line, 'bucket_30'), 60.0, places=2)
        self.assertAlmostEqual(
            self._bucket(line, 'bucket_older'), 90.0, places=2,
        )
        self.assertAlmostEqual(self._bucket(line, 'total'), 180.0, places=2)

    def test_totals_row(self):
        self._post_payable(self.partner_a, 100.0, days_overdue=10)
        self._post_payable(self.partner_b, 50.0, days_overdue=70)
        result = self.handler.compute(self.options)
        totals = result['totals']
        self.assertAlmostEqual(totals['bucket_30'], 100.0, places=2)
        self.assertAlmostEqual(totals['bucket_90'], 50.0, places=2)
        self.assertAlmostEqual(totals['total'], 150.0, places=2)

    # ---- orchestrator wiring ----

    def test_orchestrator_renders(self):
        self._post_payable(self.partner_a, 100.0, days_overdue=10)
        result = self.report.render(self.options)
        self.assertFalse(result['from_cache'])

    def test_orchestrator_cache_hit_on_second_render(self):
        self._post_payable(self.partner_a, 100.0, days_overdue=10)
        first = self.report.render(self.options)
        second = self.report.render(self.options)
        self.assertFalse(first['from_cache'])
        self.assertTrue(second['from_cache'])

    # ---- drill down ----

    def test_drilldown_partner_returns_filtered_aml_action(self):
        move = self._post_payable(
            self.partner_a, 100.0, days_overdue=10,
        )
        payable = move.line_ids.filtered(
            lambda line: line.account_id == self.account_payable,
        ).ensure_one()
        action = self.handler.get_drilldown_action(
            self.options, "partner-%s" % self.partner_a.id,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action['res_model'], 'account.move.line')
        self.assertEqual(action['domain'], [('id', 'in', [payable.id])])

    def test_xlsx_export_renders_workbook(self):
        self._post_payable(self.partner_a, 100.0, days_overdue=10)
        content = self.report.render_xlsx(self.options)
        self.assertEqual(content[:2], b'PK')
        self.assertGreater(len(content), 1000)
