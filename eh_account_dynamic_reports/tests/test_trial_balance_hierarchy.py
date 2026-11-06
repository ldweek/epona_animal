# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Trial Balance hierarchical-grouping tests.
"""

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_dynamic_reports', 'integration', 'post_install', '-at_install')
class TestTrialBalanceHierarchy(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env[
            'eh.account.dynamic.report.handler.trial_balance'
        ]
        # Two-level group hierarchy on the test chart.
        Group = cls.env['account.group']
        cls.outer = Group.create({
            'name': 'Assets group',
            'code_prefix_start': '1',
            'code_prefix_end': '1',
            'company_id': cls.company.id,
        })
        cls.inner = Group.create({
            'name': 'Current Assets',
            'code_prefix_start': '10',
            'code_prefix_end': '10',
            'parent_id': cls.outer.id,
            'company_id': cls.company.id,
        })
        cls.account_cash.group_id = cls.inner.id

    def setUp(self):
        super().setUp()
        # Post a balanced move so cash carries a non-zero TB row.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 500.0},
                {'account': self.account_equity, 'credit': 500.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
            'hierarchical_groups': True,
        }

    def _line_by_id(self, result, line_id):
        for l in result['lines']:
            if l['id'] == line_id:
                return l
        return None

    def test_groups_emitted_with_parent_chain(self):
        result = self.handler.compute(self.options)
        outer_id = "section-trial_balance-group-%s" % self.outer.id
        inner_id = "section-trial_balance-group-%s_%s" % (
            self.outer.id, self.inner.id,
        )
        outer = self._line_by_id(result, outer_id)
        inner = self._line_by_id(result, inner_id)
        cash = self._line_by_id(
            result, "account-%s" % self.account_cash.id,
        )
        self.assertTrue(outer)
        self.assertTrue(inner)
        self.assertTrue(cash)
        self.assertEqual(cash['parent_id'], inner_id)
        self.assertEqual(inner['parent_id'], outer_id)
        self.assertTrue(outer['unfoldable'])
        self.assertFalse(cash['unfoldable'])

    def test_six_columns_emitted_per_line(self):
        result = self.handler.compute(self.options)
        cash = self._line_by_id(
            result, "account-%s" % self.account_cash.id,
        )
        self.assertEqual(len(cash['columns']), 6)
        labels = [c['expression_label'] for c in cash['columns']]
        self.assertEqual(labels, [
            'opening_debit', 'opening_credit',
            'period_debit', 'period_credit',
            'closing_debit', 'closing_credit',
        ])

    def test_group_total_equals_account_sum(self):
        result = self.handler.compute(self.options)
        outer = self._line_by_id(
            result, "section-trial_balance-group-%s" % self.outer.id,
        )
        cash = self._line_by_id(
            result, "account-%s" % self.account_cash.id,
        )
        # Cash is the only account in this group; the outer group
        # row equals the cash row column-by-column.
        for cash_col, group_col in zip(
            cash['columns'], outer['columns'],
        ):
            self.assertAlmostEqual(
                cash_col['value'], group_col['value'], places=2,
            )

    def test_flat_mode_preserves_historical_shape(self):
        flat = dict(self.options, hierarchical_groups=False)
        result = self.handler.compute(flat)
        ids = [l['id'] for l in result['lines']]
        self.assertFalse(
            any(i.startswith('section-trial_balance-group-') for i in ids),
            "flat mode should not emit group lines",
        )
        self.assertIn(
            "account-%s" % self.account_cash.id, ids,
        )
