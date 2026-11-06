# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Wave 0 lazy + virtualized engine contract tests.

Covers the Part A engine contract:

* expand_account_line returns paged children that SUM to the parent cell
  (the reconciliation invariant) for Trial Balance, P&L and Balance Sheet.
* Paging: a limit yields has_more, next_offset advances, and the union of
  pages equals the full child set with no overlap or gap.
* General Ledger running balance is continuous across pages and the final
  page's closing equals the account total cell.
* GL lazy payload contains NO aml rows (initial render is O(accounts));
  the eager (export) path still inlines them.
* Cache hash is order-insensitive for unfolded_lines and differs when the
  unfold set differs (no stale-payload cross-serve).
* expand_line is a pure function of its inputs: calling twice with the same
  arguments returns identical children (so client collapse/re-expand can
  reuse cached children without refetching).
* A malformed line id degrades to an empty collapsed page, never raising.
"""

from unittest.mock import patch

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)
from odoo.addons.eh_account_base.models.report_execution import (
    EhAccountReportExecution,
)


@tagged('eh_account_dynamic_reports', 'integration', 'post_install',
        '-at_install')
class TestLazyExpandEngine(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tb_handler = cls.env[
            'eh.account.dynamic.report.handler.trial_balance']
        cls.gl_handler = cls.env[
            'eh.account.dynamic.report.handler.general_ledger']
        cls.pl_handler = cls.env[
            'eh.account.dynamic.report.handler.profit_and_loss']
        cls.bs_handler = cls.env[
            'eh.account.dynamic.report.handler.balance_sheet']
        cls.gl_report = cls._ensure_report(
            cls, 'general_ledger', 'General Ledger',
            'eh.account.dynamic.report.handler.general_ledger')
        cls.tb_report = cls._ensure_report(
            cls, 'trial_balance', 'Trial Balance',
            'eh.account.dynamic.report.handler.trial_balance')

    def _ensure_report(self, code, name, handler_model):
        report = self.env['eh.account.dynamic.report'].search(
            [('code', '=', code)], limit=1)
        if not report:
            report = self.env['eh.account.dynamic.report'].create({
                'code': code, 'name': name, 'handler_model': handler_model,
            })
        return report

    def setUp(self):
        super().setUp()
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
        }

    def _post(self, lines, date_str='2026-06-15'):
        return self.post_balanced_move(
            lines, date=fields.Date.from_string(date_str))

    @staticmethod
    def _col(line, label):
        for c in line['columns']:
            if c['expression_label'] == label:
                return c['value']
        return None

    @staticmethod
    def _leaf_for(payload, account_id):
        for line in payload['lines']:
            if (line.get('meta') or {}).get('account_id') == account_id \
                    and not (line.get('meta') or {}).get('kind') == 'aml':
                if line['id'] == 'account-%s' % account_id:
                    return line
        return None

    # ---- reconciliation: Trial Balance ----

    def test_tb_children_sum_to_cell(self):
        # Three debits and one credit on the cash account.
        self._post([
            {'account': self.account_cash, 'debit': 100.0},
            {'account': self.account_revenue, 'credit': 100.0},
        ], '2026-03-01')
        self._post([
            {'account': self.account_cash, 'debit': 40.0},
            {'account': self.account_revenue, 'credit': 40.0},
        ], '2026-04-01')
        self._post([
            {'account': self.account_cash, 'credit': 30.0},
            {'account': self.account_expense, 'debit': 30.0},
        ], '2026-05-01')

        opts = dict(self.options, hierarchical_groups=False, lazy_expand=True)
        payload = self.tb_handler.compute(opts)
        leaf = self._leaf_for(payload, self.account_cash.id)
        self.assertIsNotNone(leaf)
        # Leaf must be a lazy-expandable leaf (no eager children).
        self.assertTrue(leaf.get('lazy'))
        self.assertTrue(leaf.get('unfoldable'))
        self.assertFalse(leaf.get('unfolded'))
        cell_debit = self._col(leaf, 'period_debit')
        cell_credit = self._col(leaf, 'period_credit')

        res = self.tb_handler.expand_account_line(opts, leaf['id'])
        children = res['child_lines']
        self.assertEqual(res['total_count'], 3)
        self.assertEqual(len(children), 3)
        child_debit = sum(self._col(c, 'period_debit') or 0.0 for c in children)
        child_credit = sum(
            self._col(c, 'period_credit') or 0.0 for c in children)
        self.assertAlmostEqual(child_debit, cell_debit, places=2)
        self.assertAlmostEqual(child_credit, cell_credit, places=2)
        # A single aml has no opening/closing of its own.
        for c in children:
            self.assertEqual(self._col(c, 'opening_debit'), '')
            self.assertEqual(self._col(c, 'closing_credit'), '')

    def test_tb_paging_union_equals_full_set(self):
        for i in range(5):
            self._post([
                {'account': self.account_cash, 'debit': 10.0},
                {'account': self.account_revenue, 'credit': 10.0},
            ], '2026-06-%02d' % (i + 1))
        opts = dict(self.options, hierarchical_groups=False)
        payload = self.tb_handler.compute(opts)
        leaf = self._leaf_for(payload, self.account_cash.id)

        page1 = self.tb_handler.expand_account_line(
            opts, leaf['id'], offset=0, limit=2)
        self.assertEqual(len(page1['child_lines']), 2)
        self.assertTrue(page1['has_more'])
        self.assertEqual(page1['next_offset'], 2)
        self.assertEqual(page1['total_count'], 5)

        page2 = self.tb_handler.expand_account_line(
            opts, leaf['id'], offset=page1['next_offset'], limit=2)
        page3 = self.tb_handler.expand_account_line(
            opts, leaf['id'], offset=page2['next_offset'], limit=2)
        self.assertFalse(page3['has_more'])

        ids = [c['meta']['aml_id'] for c in (
            page1['child_lines'] + page2['child_lines']
            + page3['child_lines'])]
        self.assertEqual(len(ids), 5)
        self.assertEqual(len(set(ids)), 5, "pages overlapped")

    # ---- reconciliation: GL running balance continuity ----

    def test_gl_running_balance_continuous_across_pages(self):
        # Opening from a prior period.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 200.0},
                {'account': self.account_equity, 'credit': 200.0},
            ], date=fields.Date.from_string('2025-12-31'),
        )
        for i in range(4):
            self._post([
                {'account': self.account_cash, 'debit': 25.0},
                {'account': self.account_revenue, 'credit': 25.0},
            ], '2026-06-%02d' % (i + 1))

        opts = dict(self.options, lazy_expand=True)
        payload = self.gl_handler.compute(opts)
        # No aml rows in the lazy payload.
        kinds = [(l.get('meta') or {}).get('kind') for l in payload['lines']]
        self.assertNotIn('aml', kinds)
        header = self._leaf_for(payload, self.account_cash.id)
        self.assertTrue(header.get('lazy'))
        total_line = next(
            l for l in payload['lines']
            if l['id'] == 'account-%s-total' % self.account_cash.id)
        closing_cell = self._col(total_line, 'balance')

        # Page through two-at-a-time and check continuity.
        p1 = self.gl_handler.expand_account_line(
            opts, header['id'], offset=0, limit=2)
        p2 = self.gl_handler.expand_account_line(
            opts, header['id'], offset=2, limit=2)
        bals = [self._col(c, 'balance') for c in
                (p1['child_lines'] + p2['child_lines'])]
        # Opening 200, then +25 four times => 225, 250, 275, 300.
        self.assertAlmostEqual(bals[0], 225.0, places=2)
        self.assertAlmostEqual(bals[1], 250.0, places=2)
        # Page boundary: p2's first running continues from p1's last + delta.
        self.assertAlmostEqual(bals[2], 275.0, places=2)
        self.assertAlmostEqual(bals[3], 300.0, places=2)
        # Final page closing equals the account total cell.
        self.assertAlmostEqual(bals[-1], closing_cell, places=2)

    def test_gl_eager_path_still_inlines_aml(self):
        self._post([
            {'account': self.account_cash, 'debit': 50.0},
            {'account': self.account_revenue, 'credit': 50.0},
        ])
        # Default compute() (no lazy_expand) is eager: aml rows present.
        eager = self.gl_handler.compute(self.options)
        eager_kinds = [
            (l.get('meta') or {}).get('kind') for l in eager['lines']]
        self.assertIn('aml', eager_kinds)
        # eager_expand overrides lazy_expand (export over a screen snapshot).
        forced = self.gl_handler.compute(
            dict(self.options, lazy_expand=True, eager_expand=True))
        self.assertIn(
            'aml', [(l.get('meta') or {}).get('kind') for l in forced['lines']])

    # ---- reconciliation: P&L and Balance Sheet ----

    def test_pl_children_sum_to_cell(self):
        self._post([
            {'account': self.account_revenue, 'credit': 300.0},
            {'account': self.account_cash, 'debit': 300.0},
        ], '2026-02-01')
        self._post([
            {'account': self.account_revenue, 'credit': 700.0},
            {'account': self.account_cash, 'debit': 700.0},
        ], '2026-03-01')
        opts = dict(self.options, hierarchical_groups=False, lazy_expand=True)
        payload = self.pl_handler.compute(opts)
        leaf = self._leaf_for(payload, self.account_revenue.id)
        self.assertIsNotNone(leaf)
        self.assertTrue(leaf.get('lazy'))
        cell = self._col(leaf, 'amount')

        res = self.pl_handler.expand_account_line(opts, leaf['id'])
        child_sum = sum(self._col(c, 'amount') for c in res['child_lines'])
        self.assertAlmostEqual(child_sum, cell, places=2)
        # Income presents positive in the report; children too.
        self.assertGreater(child_sum, 0.0)

    def test_bs_children_sum_to_as_of_cell(self):
        # Activity before and within: the as-of cell is cumulative.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 500.0},
                {'account': self.account_equity, 'credit': 500.0},
            ], date=fields.Date.from_string('2025-11-30'),
        )
        self._post([
            {'account': self.account_cash, 'debit': 120.0},
            {'account': self.account_equity, 'credit': 120.0},
        ], '2026-06-10')
        opts = {
            'date': {'date_from': '0001-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True, 'show_zero': False,
            'hierarchical_groups': False,
        }
        payload = self.bs_handler.compute(opts)
        leaf = self._leaf_for(payload, self.account_cash.id)
        self.assertIsNotNone(leaf)
        cell = self._col(leaf, 'amount')

        res = self.bs_handler.expand_account_line(opts, leaf['id'])
        child_sum = sum(self._col(c, 'amount') for c in res['child_lines'])
        # The as-of cell includes the pre-period opening; children span the
        # full [epoch, date_to] window so they reconcile to the cumulative.
        self.assertAlmostEqual(child_sum, cell, places=2)
        self.assertAlmostEqual(child_sum, 620.0, places=2)

    # ---- purity / no-refetch ----

    def test_expand_is_pure_function(self):
        self._post([
            {'account': self.account_cash, 'debit': 10.0},
            {'account': self.account_revenue, 'credit': 10.0},
        ])
        opts = dict(self.options, hierarchical_groups=False)
        payload = self.tb_handler.compute(opts)
        leaf = self._leaf_for(payload, self.account_cash.id)
        first = self.tb_handler.expand_account_line(opts, leaf['id'])
        second = self.tb_handler.expand_account_line(opts, leaf['id'])
        # Identical inputs => identical children. The client relies on this
        # so collapse/re-expand can reuse cached children with no refetch.
        self.assertEqual(
            [c['meta']['aml_id'] for c in first['child_lines']],
            [c['meta']['aml_id'] for c in second['child_lines']],
        )

    # ---- fallback: malformed input never raises ----

    def test_expand_malformed_line_id_returns_empty(self):
        for bad in ('section-income-header', 'account-not-int', '', None,
                    'aml-5'):
            res = self.tb_handler.expand_account_line(self.options, bad)
            self.assertEqual(res['child_lines'], [])
            self.assertEqual(res['total_count'], 0)
            self.assertFalse(res['has_more'])

    def test_malformed_offset_is_normalized_before_empty_response(self):
        for handler in (self.tb_handler, self.gl_handler):
            for bad_offset in ('not-an-offset', object(), float('inf')):
                result = handler.expand_account_line(
                    self.options,
                    'account-not-int',
                    offset=bad_offset,
                )
                self.assertEqual(result['child_lines'], [])
                self.assertEqual(result['next_offset'], 0)
                self.assertEqual(result['total_count'], 0)
                self.assertFalse(result['has_more'])

    def test_shared_expand_direct_huge_limit_is_capped(self):
        class QueryProbe:

            def __init__(self, count=False):
                self.count = count
                self.limit_value = None

            def select_count(self, **kwargs):
                self.count = True
                return self

            def select_field(self, *args):
                return self

            def order_by(self, *args):
                return self

            def offset(self, *args):
                return self

            def limit(self, value):
                self.limit_value = value
                return self

            def execute(self):
                return [{'row_count': 0}] if self.count else []

        count_query = QueryProbe()
        page_query = QueryProbe()
        HandlerClass = type(self.tb_handler)
        with patch.object(
            HandlerClass,
            '_expand_build_page_query',
            side_effect=[count_query, page_query],
        ), patch.object(
            HandlerClass,
            '_expand_select_columns',
            return_value=page_query,
        ):
            result = self.tb_handler.expand_account_line(
                self.options,
                'account-%s' % self.account_cash.id,
                limit=10_000_000,
            )

        self.assertEqual(result['child_lines'], [])
        # limit + one probe row, capped to RPC's existing 500-row ceiling.
        self.assertEqual(page_query.limit_value, 501)

    def test_gl_expand_direct_huge_limit_is_capped(self):
        class QueryProbe:

            def __init__(self, count=False):
                self.count = count
                self.limit_value = None

            def select_count(self, **kwargs):
                self.count = True
                return self

            def select_field(self, *args):
                return self

            def order_by(self, *args):
                return self

            def offset(self, *args):
                return self

            def limit(self, value):
                self.limit_value = value
                return self

            def execute(self):
                return [{'row_count': 0}] if self.count else []

        count_query = QueryProbe()
        page_query = QueryProbe()
        HandlerClass = type(self.gl_handler)
        with patch.object(
            HandlerClass,
            '_fetch_opening_balances',
            return_value={},
        ), patch.object(
            HandlerClass,
            '_expand_prefix_balance',
            return_value=0.0,
        ), patch.object(
            HandlerClass,
            '_expand_build_page_query',
            side_effect=[count_query, page_query],
        ), patch.object(
            HandlerClass,
            '_expand_select_columns',
            return_value=page_query,
        ):
            result = self.gl_handler.expand_account_line(
                self.options,
                'account-%s' % self.account_cash.id,
                limit=10_000_000,
            )

        self.assertEqual(result['child_lines'], [])
        self.assertEqual(page_query.limit_value, 501)

    def test_orchestrator_expand_line_fallback_on_error(self):
        # A missing account id must leave the row collapsed, not raise:
        # expand_line wraps the invalid leaf defensively.
        res = self.gl_report.expand_line(
            self.options, 'account-999999999', 0, None)
        self.assertEqual(res['child_lines'], [])

    # ---- cache hash correctness (Part A §7) ----

    def test_unfolded_lines_hash_order_insensitive(self):
        a = EhAccountReportExecution._canonicalise_options(
            {'unfolded_lines': ['account-1', 'account-2', 'account-3']})
        b = EhAccountReportExecution._canonicalise_options(
            {'unfolded_lines': ['account-3', 'account-1', 'account-2']})
        self.assertEqual(a, b, "unfolded_lines must hash order-insensitively")

    def test_unfolded_lines_hash_differs_on_different_set(self):
        a = EhAccountReportExecution._canonicalise_options(
            {'unfolded_lines': ['account-1', 'account-2']})
        b = EhAccountReportExecution._canonicalise_options(
            {'unfolded_lines': ['account-1', 'account-3']})
        self.assertNotEqual(
            a, b, "different unfold sets must produce different hashes")

    def test_plain_lists_still_order_sensitive(self):
        # A non-id-list key keeps order (the existing contract).
        r = EhAccountReportExecution._canonicalise_options([3, 1, 2])
        self.assertEqual(r, [3, 1, 2])
