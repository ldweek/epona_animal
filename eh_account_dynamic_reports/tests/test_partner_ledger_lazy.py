# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Partner Ledger lazy-expand tests.

Covers the lazy per-partner aml unfold that replaces the old eager dump of
every journal item in the initial payload:

* with options['lazy_expand'] the initial payload carries NO aml-kind lines;
  each partner_header is unfoldable + lazy + collapsed, and opening / total
  rows are present and non-foldable;
* expand_account_line('partner-N') returns that partner's aml rows as level-2
  children whose debit/credit reconcile to the partner_total cell
  (opening + sum(debit) - sum(credit) == closing);
* offset/limit paging is disjoint with correct has_more / total_count and a
  correctly-continued running balance across pages;
* a non-partner id falls through to super() and returns empty;
* the eager_expand (export) path still inlines all aml rows;
* the mono (no-flag) path is unchanged (still inlines aml).
"""

from unittest.mock import patch

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_dynamic_reports', 'integration',
        'post_install', '-at_install')
class TestPartnerLedgerLazy(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env[
            'eh.account.dynamic.report.handler.partner_ledger'
        ]
        cls.report = cls.env['eh.account.dynamic.report'].search(
            [('code', '=', 'partner_ledger')], limit=1)

    def setUp(self):
        super().setUp()
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
        }
        self.lazy_options = dict(self.options, lazy_expand=True)

    def _post(self, lines, date_str='2026-06-15'):
        return self.post_balanced_move(
            lines, date=fields.Date.from_string(date_str))

    @staticmethod
    def _lines_for_partner(result, partner_id):
        return [
            line for line in result['lines']
            if (line.get('meta') or {}).get('partner_id') == partner_id
        ]

    @staticmethod
    def _by_kind(lines, kind):
        for line in lines:
            if (line.get('meta') or {}).get('kind') == kind:
                return line
        return None

    @staticmethod
    def _col(line, label):
        for col in line['columns']:
            if col['expression_label'] == label:
                return col['value']
        return None

    # ---- initial lazy payload shape ----

    def test_initial_payload_has_no_aml_lines(self):
        self._post([
            {'account': self.account_receivable, 'debit': 1000.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 1000.0},
        ])
        result = self.handler.compute(self.lazy_options)
        kinds = [(l.get('meta') or {}).get('kind') for l in result['lines']]
        self.assertNotIn('aml', kinds,
                         "lazy initial payload must not materialise aml rows")
        self.assertIn('partner_header', kinds)
        self.assertIn('opening_balance', kinds)
        self.assertIn('partner_total', kinds)

    def test_partner_header_is_lazy_unfoldable_collapsed(self):
        self._post([
            {'account': self.account_receivable, 'debit': 1000.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 1000.0},
        ])
        result = self.handler.compute(self.lazy_options)
        header = self._by_kind(
            self._lines_for_partner(result, self.partner_a.id),
            'partner_header')
        self.assertTrue(header.get('unfoldable'))
        self.assertTrue(header.get('lazy'))
        self.assertFalse(header.get('unfolded'))

    def test_mono_path_still_inlines_aml(self):
        # No lazy_expand flag -> legacy inlined shape (existing suite relies
        # on this; the partner header stays non-foldable).
        self._post([
            {'account': self.account_receivable, 'debit': 1000.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 1000.0},
        ])
        result = self.handler.compute(self.options)
        kinds = [(l.get('meta') or {}).get('kind') for l in result['lines']]
        self.assertIn('aml', kinds)
        header = self._by_kind(
            self._lines_for_partner(result, self.partner_a.id),
            'partner_header')
        self.assertFalse(header.get('unfoldable'))

    def test_eager_export_path_inlines_aml(self):
        # eager_expand (PDF/XLSX) forces the inlined path even with
        # lazy_expand set, so paper output is unchanged.
        self._post([
            {'account': self.account_receivable, 'debit': 1000.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 1000.0},
        ])
        opts = dict(self.lazy_options, eager_expand=True)
        result = self.handler.compute(opts)
        kinds = [(l.get('meta') or {}).get('kind') for l in result['lines']]
        self.assertIn('aml', kinds)

    # ---- expand reconciles to the partner total ----

    def test_expand_children_reconcile_to_total(self):
        # Opening (prior period) + three in-period entries.
        self._post([
            {'account': self.account_receivable, 'debit': 400.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 400.0},
        ], date_str='2025-12-10')
        self._post([
            {'account': self.account_receivable, 'debit': 100.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 100.0},
        ], date_str='2026-03-01')
        self._post([
            {'account': self.account_receivable, 'debit': 250.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 250.0},
        ], date_str='2026-05-01')
        self._post([
            {'account': self.account_cash, 'debit': 50.0,
             'partner': self.partner_a},
            {'account': self.account_receivable, 'credit': 50.0,
             'partner': self.partner_a},
        ], date_str='2026-07-01')

        result = self.handler.compute(self.lazy_options)
        partner_lines = self._lines_for_partner(result, self.partner_a.id)
        opening = self._col(
            self._by_kind(partner_lines, 'opening_balance'), 'balance')
        total = self._col(
            self._by_kind(partner_lines, 'partner_total'), 'balance')

        res = self.handler.expand_account_line(
            self.lazy_options, "partner-%s" % self.partner_a.id,
            offset=0, limit=80)
        children = res['child_lines']
        self.assertEqual(res['total_count'], 3)
        self.assertEqual(len(children), 3)
        # Every child is a level-2 aml row parented to the partner header.
        for child in children:
            self.assertEqual(child['level'], 2)
            self.assertEqual(
                child['parent_id'], "partner-%s" % self.partner_a.id)
            self.assertEqual((child.get('meta') or {}).get('kind'), 'aml')
            self.assertFalse(child.get('lazy'))

        # opening + sum(debit) - sum(credit) == closing total.
        sum_debit = round(sum(self._col(c, 'debit') for c in children), 2)
        sum_credit = round(sum(self._col(c, 'credit') for c in children), 2)
        self.assertAlmostEqual(
            round(opening + sum_debit - sum_credit, 2), total, places=2)
        # The last child's running balance equals the partner total.
        self.assertAlmostEqual(
            self._col(children[-1], 'balance'), total, places=2)

    # ---- paging ----

    def test_paging_is_disjoint_and_balance_continues(self):
        # Opening then 5 in-period debits.
        self._post([
            {'account': self.account_receivable, 'debit': 100.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 100.0},
        ], date_str='2025-12-10')
        for i in range(5):
            self._post([
                {'account': self.account_receivable, 'debit': 10.0 + i,
                 'partner': self.partner_a},
                {'account': self.account_revenue, 'credit': 10.0 + i},
            ], date_str='2026-06-%02d' % (i + 1))

        line_id = "partner-%s" % self.partner_a.id
        result = self.handler.compute(self.lazy_options)
        total = self._col(self._by_kind(
            self._lines_for_partner(result, self.partner_a.id),
            'partner_total'), 'balance')

        page1 = self.handler.expand_account_line(
            self.lazy_options, line_id, offset=0, limit=2)
        self.assertEqual(page1['total_count'], 5)
        self.assertTrue(page1['has_more'])
        self.assertEqual(len(page1['child_lines']), 2)
        self.assertEqual(page1['next_offset'], 2)

        page2 = self.handler.expand_account_line(
            self.lazy_options, line_id, offset=2, limit=2)
        self.assertTrue(page2['has_more'])

        page3 = self.handler.expand_account_line(
            self.lazy_options, line_id, offset=4, limit=2)
        self.assertFalse(page3['has_more'])
        self.assertEqual(len(page3['child_lines']), 1)

        ids = set()
        for page in (page1, page2, page3):
            ids |= {c['id'] for c in page['child_lines']}
        self.assertEqual(len(ids), 5, "pages must be disjoint")

        # The running balance continues across the page boundary: the last
        # row of the full set equals the partner total.
        self.assertAlmostEqual(
            self._col(page3['child_lines'][-1], 'balance'), total, places=2)

    def test_lazy_page_query_is_bounded_and_stably_ordered(self):
        query = self.handler._build_line_entries_page_query(
            [self.company.id],
            fields.Date.from_string('2026-01-01'),
            fields.Date.from_string('2026-12-31'),
            True,
            self.lazy_options,
            [self.partner_a.id],
            offset=7,
            limit=3,
        )
        sql = query.build()
        self.assertIn('ORDER BY aml.date ASC, aml.id ASC', sql.code)
        self.assertIn('LIMIT', sql.code)
        self.assertIn('OFFSET', sql.code)
        self.assertEqual(tuple(sql.params[-2:]), (4, 7))
        self.assertIn(
            ('asset_receivable', 'liability_payable'), sql.params,
        )

    def test_lazy_expand_never_calls_eager_row_fetch(self):
        self._post([
            {'account': self.account_receivable, 'debit': 25.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 25.0},
        ])
        HandlerClass = type(self.handler)
        with patch.object(
            HandlerClass,
            '_fetch_line_entries',
            side_effect=AssertionError('lazy expand materialised all rows'),
        ):
            result = self.handler.expand_account_line(
                self.lazy_options,
                "partner-%s" % self.partner_a.id,
                offset=0,
                limit=1,
            )
        self.assertEqual(result['total_count'], 1)
        self.assertEqual(len(result['child_lines']), 1)

    # ---- fallbacks ----

    def test_non_partner_id_falls_through_to_empty(self):
        res = self.handler.expand_account_line(
            self.lazy_options, 'totally-bogus')
        self.assertEqual(res['child_lines'], [])
        self.assertEqual(res['total_count'], 0)

    def test_compound_partner_id_not_expandable(self):
        # 'partner-N-opening' / 'partner-N-total' must NOT expand as a
        # partner header (only the bare 'partner-N' id does).
        res = self.handler.expand_account_line(
            self.lazy_options,
            "partner-%s-opening" % self.partner_a.id)
        self.assertEqual(res['child_lines'], [])

    def test_partner_filter_scopes_expand(self):
        self._post([
            {'account': self.account_receivable, 'debit': 100.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 100.0},
        ])
        self._post([
            {'account': self.account_receivable, 'debit': 200.0,
             'partner': self.partner_b},
            {'account': self.account_revenue, 'credit': 200.0},
        ])
        res_a = self.handler.expand_account_line(
            self.lazy_options, "partner-%s" % self.partner_a.id)
        for child in res_a['child_lines']:
            self.assertEqual(
                (child.get('meta') or {}).get('partner_id'),
                self.partner_a.id)
