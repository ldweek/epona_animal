# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Partner Ledger handler tests.

Covers structure (header, opening, entries, total per partner), running
balance math, opening from prior period, posted only, cancelled exclusion,
filter narrowing, error handling, orchestrator wiring, drill down, XLSX.
"""

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_dynamic_reports', 'integration', 'post_install', '-at_install')
class TestPartnerLedgerHandler(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env[
            'eh.account.dynamic.report.handler.partner_ledger'
        ]
        cls.report = cls.env['eh.account.dynamic.report'].search(
            [('code', '=', 'partner_ledger')], limit=1,
        )
        if not cls.report:
            cls.report = cls.env['eh.account.dynamic.report'].create({
                'code': 'partner_ledger',
                'name': 'Partner Ledger',
                'handler_model':
                    'eh.account.dynamic.report.handler.partner_ledger',
            })

    def setUp(self):
        super().setUp()
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
        }

    def _post_in_period(self, lines, date_str='2026-06-15'):
        return self.post_balanced_move(
            lines, date=fields.Date.from_string(date_str),
        )

    @staticmethod
    def _lines_for_partner(result, partner_id):
        return [
            line for line in result['lines']
            if (line.get('meta') or {}).get('partner_id') == partner_id
        ]

    @staticmethod
    def _line_by_kind(lines, kind):
        for line in lines:
            if (line.get('meta') or {}).get('kind') == kind:
                return line
        return None

    @staticmethod
    def _column_value(line, label):
        for col in line['columns']:
            if col['expression_label'] == label:
                return col['value']
        return None

    def test_foreign_currency_amount_on_line(self):
        currency = self.env['res.currency'].create({
            'name': 'ZZP', 'symbol': 'P', 'rounding': 0.001})
        self.env['res.currency.rate'].create({
            'currency_id': currency.id, 'name': '2026-01-01',
            'rate': 0.5, 'company_id': self.company.id})
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': fields.Date.from_string('2026-06-15'),
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {'account_id': self.account_receivable.id,
                        'partner_id': self.partner_a.id,
                        'debit': 110.0, 'credit': 0.0,
                        'currency_id': currency.id,
                        'amount_currency': 220.001}),
                (0, 0, {'account_id': self.account_revenue.id,
                        'debit': 0.0, 'credit': 110.0}),
            ],
        })
        move.action_post()

        result = self.handler.compute(self.options)
        aml = self._line_by_kind(
            self._lines_for_partner(result, self.partner_a.id), 'aml')
        self.assertEqual(
            self._column_value(aml, 'foreign'), '220.001 ZZP')

    def test_kwd_precision_keeps_one_fils_ledger_balance(self):
        kwd = self.env['res.currency'].with_context(active_test=False).search(
            [('name', '=', 'KWD')], limit=1,
        )
        self.assertTrue(kwd)
        lines = self.handler._build_lines(
            {self.partner_a.id: 0.001}, [], False, currency=kwd,
        )
        opening = self._line_by_kind(lines, 'opening_balance')
        total = self._line_by_kind(lines, 'partner_total')
        self.assertAlmostEqual(
            self._column_value(opening, 'balance'), 0.001, places=3,
        )
        self.assertAlmostEqual(
            self._column_value(total, 'balance'), 0.001, places=3,
        )

    # ---- core rendering ----

    def test_partner_header_opening_entry_total_present(self):
        self._post_in_period([
            {'account': self.account_receivable, 'debit': 1000.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 1000.0},
        ])
        result = self.handler.compute(self.options)
        partner_lines = self._lines_for_partner(result, self.partner_a.id)
        kinds = [(l.get('meta') or {}).get('kind') for l in partner_lines]
        self.assertIn('partner_header', kinds)
        self.assertIn('opening_balance', kinds)
        self.assertIn('aml', kinds)
        self.assertIn('partner_total', kinds)

    def test_running_balance_per_partner(self):
        self._post_in_period([
            {'account': self.account_receivable, 'debit': 100.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 100.0},
        ], date_str='2026-06-15')
        self._post_in_period([
            {'account': self.account_receivable, 'credit': 30.0,
             'partner': self.partner_a},
            {'account': self.account_cash, 'debit': 30.0,
             'partner': self.partner_a},
        ], date_str='2026-06-20')
        result = self.handler.compute(self.options)
        partner_lines = self._lines_for_partner(result, self.partner_a.id)
        aml_lines = [
            l for l in partner_lines
            if (l.get('meta') or {}).get('kind') == 'aml'
        ]
        balances = [self._column_value(l, 'balance') for l in aml_lines]
        # Enterprise-style default scope includes only receivable/payable:
        # receivable +100 and receivable -30. Partner-tagged cash is omitted.
        self.assertEqual(len(balances), 2)
        # Last entry's running balance should equal the partner total.
        total_line = self._line_by_kind(partner_lines, 'partner_total')
        self.assertAlmostEqual(
            balances[-1],
            self._column_value(total_line, 'balance'),
            places=2,
        )

    def test_opening_balance_from_prior_period(self):
        self.post_balanced_move(
            [
                {'account': self.account_receivable, 'debit': 500.0,
                 'partner': self.partner_a},
                {'account': self.account_revenue, 'credit': 500.0},
            ],
            date=fields.Date.from_string('2025-12-15'),
        )
        result = self.handler.compute(self.options)
        partner_lines = self._lines_for_partner(result, self.partner_a.id)
        opening_line = self._line_by_kind(partner_lines, 'opening_balance')
        self.assertIsNotNone(opening_line)
        self.assertAlmostEqual(
            self._column_value(opening_line, 'balance'), 500.0, places=2,
        )

    def test_lines_without_partner_excluded(self):
        # Pure cash entry with no partner attached.
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 50.0},
            {'account': self.account_cash, 'debit': 50.0},
        ])
        result = self.handler.compute(self.options)
        # No partner row should appear because no aml had a partner_id.
        self.assertEqual(len(result['lines']), 0)

    # ---- filter narrowing ----

    def test_partner_filter(self):
        self._post_in_period([
            {'account': self.account_receivable, 'debit': 100.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 100.0},
        ])
        self._post_in_period([
            {'account': self.account_receivable, 'debit': 200.0,
             'partner': self.partner_b},
            {'account': self.account_revenue, 'credit': 200.0},
        ])
        opts = dict(self.options)
        opts['partner_ids'] = [self.partner_a.id]
        result = self.handler.compute(opts)
        a_lines = self._lines_for_partner(result, self.partner_a.id)
        b_lines = self._lines_for_partner(result, self.partner_b.id)
        self.assertGreater(len(a_lines), 0)
        self.assertEqual(len(b_lines), 0)

    def test_default_scope_is_receivable_and_payable_only(self):
        self._post_in_period([
            {'account': self.account_cash, 'debit': 90.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 90.0},
        ])
        result = self.handler.compute(self.options)
        self.assertEqual(
            self._lines_for_partner(result, self.partner_a.id), [],
        )

    def test_explicit_account_filter_replaces_default_scope(self):
        self._post_in_period([
            {'account': self.account_cash, 'debit': 90.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 90.0},
        ])
        options = dict(self.options, account_ids=[self.account_cash.id])
        result = self.handler.compute(options)
        aml = self._line_by_kind(
            self._lines_for_partner(result, self.partner_a.id), 'aml',
        )
        self.assertIsNotNone(aml)
        self.assertEqual(aml['meta']['account_id'], self.account_cash.id)

    # ---- state filtering ----

    def test_posted_only_excludes_draft(self):
        self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': '2026-06-15',
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_receivable.id,
                    'partner_id': self.partner_a.id, 'debit': 999.0,
                }),
                (0, 0, {
                    'account_id': self.account_revenue.id, 'credit': 999.0,
                }),
            ],
        })
        result = self.handler.compute(self.options)
        a_lines = self._lines_for_partner(result, self.partner_a.id)
        self.assertEqual(len(a_lines), 0)

    def test_cancelled_excluded(self):
        move = self._post_in_period([
            {'account': self.account_receivable, 'debit': 444.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 444.0},
        ])
        move.button_cancel()
        result = self.handler.compute(self.options)
        a_lines = self._lines_for_partner(result, self.partner_a.id)
        self.assertEqual(len(a_lines), 0)

    # ---- error handling ----

    def test_missing_date_raises(self):
        bad = dict(self.options)
        bad.pop('date')
        with self.assertRaises(UserError):
            self.handler.compute(bad)

    # ---- orchestrator wiring ----

    def test_orchestrator_renders(self):
        self._post_in_period([
            {'account': self.account_receivable, 'debit': 100.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 100.0},
        ])
        result = self.report.render(self.options)
        self.assertFalse(result['from_cache'])
        self.assertGreater(len(result['lines']), 0)

    def test_orchestrator_cache_hit_on_second_render(self):
        self._post_in_period([
            {'account': self.account_receivable, 'debit': 100.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 100.0},
        ])
        first = self.report.render(self.options)
        second = self.report.render(self.options)
        self.assertFalse(first['from_cache'])
        self.assertTrue(second['from_cache'])

    # ---- drill down ----

    def test_drilldown_aml_opens_move_form(self):
        move = self._post_in_period([
            {'account': self.account_receivable, 'debit': 75.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 75.0},
        ])
        result = self.handler.compute(self.options)
        partner_lines = self._lines_for_partner(result, self.partner_a.id)
        aml_line = self._line_by_kind(partner_lines, 'aml')
        action = self.handler.get_drilldown_action(self.options, aml_line['id'])
        self.assertIsNotNone(action)
        self.assertEqual(action['res_model'], 'account.move')
        self.assertEqual(action['res_id'], move.id)

    def test_drilldown_partner_header_returns_filtered_aml_action(self):
        options = dict(
            self.options,
            account_type_ids=['asset_receivable'],
        )
        action = self.handler.get_drilldown_action(
            options, "partner-%s" % self.partner_a.id,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action['res_model'], 'account.move.line')
        domain_pairs = [tuple(d[:2]) for d in action['domain']
                        if isinstance(d, (list, tuple)) and len(d) >= 2]
        self.assertIn(('partner_id', '='), domain_pairs)
        self.assertIn(
            (
                'account_id.account_type',
                'in',
                ['asset_receivable'],
            ),
            action['domain'],
        )

    def test_drilldown_partner_header_uses_default_account_types(self):
        action = self.handler.get_drilldown_action(
            self.options, "partner-%s" % self.partner_a.id,
        )
        self.assertIn(
            (
                'account_id.account_type',
                'in',
                ['asset_receivable', 'liability_payable'],
            ),
            action['domain'],
        )

    def test_drilldown_unknown_id_returns_none(self):
        self.assertIsNone(self.handler.get_drilldown_action(
            self.options, 'partner-%s-opening' % self.partner_a.id,
        ))
        self.assertIsNone(self.handler.get_drilldown_action(
            self.options, 'totally-bogus',
        ))

    # ---- XLSX export ----

    def test_xlsx_export_renders_workbook(self):
        self._post_in_period([
            {'account': self.account_receivable, 'debit': 100.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 100.0},
        ])
        content = self.report.render_xlsx(self.options)
        self.assertEqual(content[:2], b'PK')
        self.assertGreater(len(content), 1000)
