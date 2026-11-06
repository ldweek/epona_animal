# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
WS2 aged unfold tests.

Covers the lazy per-partner open-item unfold added to the aged
receivable / payable reports:

* a partner row is lazy-unfoldable only when options['lazy_expand'] is set,
  and the mono (no-lazy) path is byte-identical to the legacy line shape;
* expand_account_line('partner-N') returns the partner's invoice lines as
  level-2 'aml-N' children whose residuals, placed in their matching bucket
  column, sum exactly to the parent bucket cells (sign-correct for AR and
  AP SIGN=-1);
* the reconcile-state filter ('open' vs 'all') changes the child set;
* the aging interval / bucket-count config changes the bucket a line lands
  in and reconciles parent and child placement;
* offset/limit paging is disjoint, with correct has_more / total_count;
* the 'aml-N' drilldown opens the single journal entry.
"""

from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_dynamic_reports', 'integration',
        'post_install', '-at_install')
class TestAgedUnfold(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env[
            'eh.account.dynamic.report.handler.aged_receivable'
        ]
        cls.payable_handler = cls.env[
            'eh.account.dynamic.report.handler.aged_payable'
        ]
        cls.report = cls.env['eh.account.dynamic.report'].search(
            [('code', '=', 'aged_receivable')], limit=1,
        )
        if not cls.report:
            cls.report = cls.env['eh.account.dynamic.report'].create({
                'code': 'aged_receivable',
                'name': 'Aged Receivable',
                'handler_model':
                    'eh.account.dynamic.report.handler.aged_receivable',
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

    # ---- fixtures ----

    def _post_receivable(self, partner, amount, days_overdue):
        date_maturity = self.date_to - timedelta(days=days_overdue)
        return self.post_balanced_move(
            [
                {'account': self.account_receivable, 'debit': amount,
                 'partner': partner, 'date_maturity': date_maturity},
                {'account': self.account_revenue, 'credit': amount},
            ],
            date=fields.Date.from_string('2026-06-15'),
        )

    def _post_payable(self, partner, amount, days_overdue):
        date_maturity = self.date_to - timedelta(days=days_overdue)
        return self.post_balanced_move(
            [
                {'account': self.account_payable, 'credit': amount,
                 'partner': partner, 'date_maturity': date_maturity},
                {'account': self.account_expense, 'debit': amount},
            ],
            date=fields.Date.from_string('2026-06-15'),
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

    # ---- lazy gating + mono path ----

    def test_mono_path_partner_not_unfoldable(self):
        # Without lazy_expand the partner row keeps the legacy shape so the
        # existing suite and export see byte-identical lines.
        self._post_receivable(self.partner_a, 100.0, days_overdue=10)
        result = self.handler.compute(self.options)
        line = self._line_for_partner(result, self.partner_a.id)
        self.assertIsNotNone(line)
        self.assertFalse(line.get('unfoldable'))
        self.assertNotIn('lazy', line)

    def test_lazy_flag_makes_partner_unfoldable(self):
        self._post_receivable(self.partner_a, 100.0, days_overdue=10)
        opts = dict(self.options, lazy_expand=True)
        result = self.handler.compute(opts)
        line = self._line_for_partner(result, self.partner_a.id)
        self.assertTrue(line.get('unfoldable'))
        self.assertTrue(line.get('lazy'))
        self.assertFalse(line.get('unfolded'))

    def test_eager_export_path_stays_non_lazy(self):
        # PDF/XLSX render with eager_expand: partner rows must NOT become
        # lazy (export inlines nothing for aged; paper output unchanged).
        self._post_receivable(self.partner_a, 100.0, days_overdue=10)
        opts = dict(self.options, lazy_expand=True, eager_expand=True)
        result = self.handler.compute(opts)
        line = self._line_for_partner(result, self.partner_a.id)
        self.assertFalse(line.get('unfoldable'))

    # ---- expand reconciles to the bucket cell ----

    def test_expand_children_sum_to_bucket_cells(self):
        # Three receivables for one partner in three different buckets.
        self._post_receivable(self.partner_a, 50.0, days_overdue=-10)
        self._post_receivable(self.partner_a, 75.0, days_overdue=20)
        self._post_receivable(self.partner_a, 100.0, days_overdue=120)
        opts = dict(self.options, lazy_expand=True)
        parent_result = self.handler.compute(opts)
        parent = self._line_for_partner(parent_result, self.partner_a.id)

        res = self.handler.expand_account_line(
            opts, "partner-%s" % self.partner_a.id, offset=0, limit=80,
        )
        children = res['child_lines']
        self.assertEqual(res['total_count'], 3)
        self.assertEqual(len(children), 3)

        # For every bucket column, the children's contributions sum to the
        # parent's bucket cell (cell-to-children reconciliation).
        bucket_keys = ['not_due', 'bucket_30', 'bucket_60',
                       'bucket_90', 'bucket_older']
        for key in bucket_keys:
            child_sum = round(sum(
                self._bucket(c, key) for c in children
            ), 2)
            self.assertAlmostEqual(
                child_sum, self._bucket(parent, key), places=2,
                msg="children must reconcile to parent bucket %s" % key,
            )
        # And the residuals land in the right buckets.
        self.assertAlmostEqual(self._bucket(parent, 'not_due'), 50.0, 2)
        self.assertAlmostEqual(self._bucket(parent, 'bucket_30'), 75.0, 2)
        self.assertAlmostEqual(self._bucket(parent, 'bucket_older'), 100.0, 2)

    def test_payable_children_sign_matches_parent(self):
        # AP SIGN=-1: child residuals must carry the same sign as the
        # parent bucket so they reconcile.
        self._post_payable(self.partner_a, 200.0, days_overdue=45)
        opts = dict(self.options, lazy_expand=True)
        parent_result = self.payable_handler.compute(opts)
        parent = self._line_for_partner(parent_result, self.partner_a.id)
        self.assertAlmostEqual(self._bucket(parent, 'bucket_60'), 200.0, 2)

        res = self.payable_handler.expand_account_line(
            opts, "partner-%s" % self.partner_a.id,
        )
        children = res['child_lines']
        self.assertEqual(len(children), 1)
        self.assertAlmostEqual(
            self._bucket(children[0], 'bucket_60'),
            self._bucket(parent, 'bucket_60'), places=2,
        )

    # ---- paging ----

    def test_paging_is_disjoint(self):
        for i in range(5):
            self._post_receivable(self.partner_a, 10.0 + i, days_overdue=20)
        opts = dict(self.options, lazy_expand=True)
        line_id = "partner-%s" % self.partner_a.id

        page1 = self.handler.expand_account_line(
            opts, line_id, offset=0, limit=2)
        self.assertEqual(page1['total_count'], 5)
        self.assertTrue(page1['has_more'])
        self.assertEqual(len(page1['child_lines']), 2)
        self.assertEqual(page1['next_offset'], 2)

        page2 = self.handler.expand_account_line(
            opts, line_id, offset=page1['next_offset'], limit=2)
        self.assertTrue(page2['has_more'])

        page3 = self.handler.expand_account_line(
            opts, line_id, offset=4, limit=2)
        self.assertFalse(page3['has_more'])
        self.assertEqual(len(page3['child_lines']), 1)

        ids = {c['id'] for c in page1['child_lines']}
        ids |= {c['id'] for c in page2['child_lines']}
        ids |= {c['id'] for c in page3['child_lines']}
        self.assertEqual(len(ids), 5)

    def test_lazy_page_query_is_bounded_and_stably_ordered(self):
        query = self.handler._build_open_lines_page_query(
            [self.company.id],
            self.date_to,
            True,
            self.options,
            'open',
            [self.partner_a.id],
            offset=9,
            limit=4,
        )
        sql = query.build()
        self.assertIn('ORDER BY aml.date ASC, aml.id ASC', sql.code)
        self.assertIn('LIMIT', sql.code)
        self.assertIn('OFFSET', sql.code)
        self.assertEqual(tuple(sql.params[-2:]), (5, 9))
        self.assertIn('apr.max_date <=', sql.code)

    def test_lazy_expand_never_calls_eager_row_fetch(self):
        self._post_receivable(self.partner_a, 35.0, days_overdue=20)
        HandlerClass = type(self.handler)
        with patch.object(
            HandlerClass,
            '_fetch_open_lines',
            side_effect=AssertionError('lazy expand materialised all rows'),
        ):
            result = self.handler.expand_account_line(
                dict(self.options, lazy_expand=True),
                "partner-%s" % self.partner_a.id,
                offset=0,
                limit=1,
            )
        self.assertEqual(result['total_count'], 1)
        self.assertEqual(len(result['child_lines']), 1)

    # ---- reconcile-state filter changes the set ----

    def test_reconcile_state_changes_child_set(self):
        # One open receivable + one fully reconciled receivable for the same
        # partner. 'open' sees only the open one; 'all' sees both.
        self._post_receivable(self.partner_a, 100.0, days_overdue=20)
        inv = self._post_receivable(self.partner_a, 60.0, days_overdue=20)
        # Reconcile the second invoice against an offsetting credit so its
        # residual is zero as of date_to.
        credit = self.post_balanced_move(
            [
                {'account': self.account_receivable, 'credit': 60.0,
                 'partner': self.partner_a},
                {'account': self.account_revenue, 'debit': 60.0},
            ],
            date=fields.Date.from_string('2026-06-20'),
        )
        recv_lines = (inv.line_ids | credit.line_ids).filtered(
            lambda l: l.account_id == self.account_receivable)
        recv_lines.reconcile()

        opts_open = dict(self.options, lazy_expand=True,
                         reconcile_state='open')
        opts_all = dict(self.options, lazy_expand=True,
                        reconcile_state='all')
        line_id = "partner-%s" % self.partner_a.id

        open_res = self.handler.expand_account_line(opts_open, line_id)
        all_res = self.handler.expand_account_line(opts_all, line_id)

        self.assertLess(
            open_res['total_count'], all_res['total_count'],
            "reconcile_state='all' must surface more lines than 'open'",
        )
        self.assertEqual(open_res['total_count'], 1)

    # ---- aging config changes bucket boundaries ----

    def test_aging_config_changes_bucket(self):
        # 40 days overdue: default 30-day grid -> bucket_60 (31-60).
        # With a 15-day interval the same line lands in bucket_45 (31-45)
        # instead, proving the config moves the bucket boundary.
        self._post_receivable(self.partner_a, 100.0, days_overdue=40)

        # Default grid: lands in bucket_60.
        default_result = self.handler.compute(
            dict(self.options, lazy_expand=True))
        default_line = self._line_for_partner(
            default_result, self.partner_a.id)
        self.assertAlmostEqual(
            self._bucket(default_line, 'bucket_60'), 100.0, 2)

        # 15-day grid, 6 buckets -> columns not_due + 6 buckets (15/30/45/
        # 60/75/older). A 40-day line moves to bucket_45 (31-45).
        opts = dict(self.options, lazy_expand=True,
                    aging_interval=15, aging_bucket_count=6)
        parent_result = self.handler.compute(opts)
        parent = self._line_for_partner(parent_result, self.partner_a.id)
        col_keys = [c['expression_label']
                    for c in parent_result['columns']]
        self.assertIn('bucket_15', col_keys)
        self.assertIn('bucket_45', col_keys)
        # not_due + 6 buckets = 7 monetary buckets.
        bucket_cols = [k for k in col_keys
                       if k.startswith('bucket_') or k == 'not_due']
        self.assertEqual(len(bucket_cols), 7)
        self.assertAlmostEqual(self._bucket(parent, 'bucket_45'), 100.0, 2)
        # Under the 15-day grid the 40-day line is NOT in bucket_30 (16-30).
        self.assertAlmostEqual(self._bucket(parent, 'bucket_30'), 0.0, 2)

        res = self.handler.expand_account_line(
            opts, "partner-%s" % self.partner_a.id)
        child = res['child_lines'][0]
        self.assertAlmostEqual(self._bucket(child, 'bucket_45'), 100.0, 2)
        self.assertEqual(child['meta']['bucket'], 'bucket_45')

    # ---- drilldown ----

    def test_aml_drilldown_opens_move(self):
        move = self._post_receivable(self.partner_a, 100.0, days_overdue=10)
        recv = move.line_ids.filtered(
            lambda l: l.account_id == self.account_receivable)
        action = self.handler.get_drilldown_action(
            self.options, "aml-%s" % recv.id)
        self.assertIsNotNone(action)
        self.assertEqual(action['res_model'], 'account.move')
        self.assertEqual(action['res_id'], move.id)

    def test_partner_drilldown_still_works(self):
        # The legacy partner-N full drilldown must remain intact.
        action = self.handler.get_drilldown_action(
            self.options, "partner-%s" % self.partner_a.id)
        self.assertIsNotNone(action)
        self.assertEqual(action['res_model'], 'account.move.line')

    def test_expand_non_partner_id_is_empty(self):
        res = self.handler.expand_account_line(self.options, 'totally-bogus')
        self.assertEqual(res['child_lines'], [])
        self.assertEqual(res['total_count'], 0)

    # ---- cache hash separates aged config / unfold sets ----

    def test_cache_hash_separates_aging_config(self):
        import json
        Execution = self.env['eh.account.report.execution']

        def hash_of(opts):
            canonical = Execution._canonicalise_options(opts)
            return Execution._hash_string(
                json.dumps(canonical, sort_keys=True, default=str))

        base = dict(self.options, lazy_expand=True)
        h1 = hash_of(base)
        h2 = hash_of(dict(base, aging_interval=15))
        h3 = hash_of(dict(base, reconcile_state='all'))
        # unfolded_lines must be order-insensitive (same set, two orders,
        # one hash) so expanding partners in any order re-serves the cache.
        h4 = hash_of(dict(base, unfolded_lines=['partner-1', 'partner-2']))
        h5 = hash_of(dict(base, unfolded_lines=['partner-2', 'partner-1']))
        self.assertNotEqual(h1, h2)
        self.assertNotEqual(h1, h3)
        self.assertEqual(h4, h5)
