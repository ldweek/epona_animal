# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Hierarchical group emission tests.

Seeds an account.group hierarchy (Current Assets -> Cash & Equivalents)
on top of the standard test chart and asserts that:

* Group lines are emitted with unfoldable=True and a parent_id chain.
* Account lines parent to the deepest group whose code prefix matches.
* Group totals equal the sum of child accounts.
* Flat mode (hierarchical_groups=False) preserves the historical shape.
* Ungrouped accounts attach to the section header.
"""

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_dynamic_reports', 'integration', 'post_install', '-at_install')
class TestHierarchicalGroups(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env[
            'eh.account.dynamic.report.handler.balance_sheet'
        ]
        # Seed a two-level account.group hierarchy: 1 (Assets) ->
        # 11 (Current Assets). Attach the standard cash account to
        # the inner group so we can assert the nested rendering.
        Group = cls.env['account.group']
        cls.parent_group = Group.create({
            'name': 'Assets group',
            'code_prefix_start': '1',
            'code_prefix_end': '1',
            'company_id': cls.company.id,
        })
        cls.child_group = Group.create({
            'name': 'Current Assets',
            'code_prefix_start': '10',
            'code_prefix_end': '10',
            'parent_id': cls.parent_group.id,
            'company_id': cls.company.id,
        })
        # Re-link cash to the inner group; the receivable stays
        # ungrouped to exercise the section-direct attachment path.
        cls.account_cash.group_id = cls.child_group.id

    def setUp(self):
        super().setUp()
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
            'hierarchical_groups': True,
        }
        # Seed one balanced move so assets are non-zero in the period.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 500.0},
                {'account': self.account_equity, 'credit': 500.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )

    def _line_ids(self, result):
        return [l['id'] for l in result['lines']]

    def _line_by_id(self, result, line_id):
        for l in result['lines']:
            if l['id'] == line_id:
                return l
        return None

    def test_group_lines_emitted_with_parent_chain(self):
        result = self.handler.compute(self.options)
        ids = self._line_ids(result)
        # Outer group line
        # Cash is a current asset, so its group tree hangs off the
        # Current Assets subsection (IAS 1.60/66 presentation).
        outer_id = "section-assets_current-group-%s" % self.parent_group.id
        # Inner group line
        inner_id = "section-assets_current-group-%s_%s" % (
            self.parent_group.id, self.child_group.id,
        )
        self.assertIn(outer_id, ids)
        self.assertIn(inner_id, ids)
        outer = self._line_by_id(result, outer_id)
        inner = self._line_by_id(result, inner_id)
        cash_line = self._line_by_id(
            result, "account-%s" % self.account_cash.id,
        )
        # Hierarchy parent_id chain: cash -> inner -> outer -> section
        self.assertEqual(cash_line['parent_id'], inner_id)
        self.assertEqual(inner['parent_id'], outer_id)
        self.assertEqual(
            outer['parent_id'], "section-assets_current-header")
        self.assertTrue(outer['unfoldable'])
        self.assertTrue(inner['unfoldable'])
        self.assertFalse(cash_line['unfoldable'])

    def test_group_totals_equal_child_account_sum(self):
        result = self.handler.compute(self.options)
        outer = self._line_by_id(
            result, "section-assets_current-group-%s" % self.parent_group.id,
        )
        cash_line = self._line_by_id(
            result, "account-%s" % self.account_cash.id,
        )
        # Cash is the only account in the group; the group total
        # equals the cash amount.
        self.assertAlmostEqual(
            outer['columns'][0]['value'],
            cash_line['columns'][0]['value'],
            places=2,
        )

    def test_flat_mode_preserves_historical_shape(self):
        flat_options = dict(self.options, hierarchical_groups=False)
        result = self.handler.compute(flat_options)
        ids = self._line_ids(result)
        # No hierarchy-group ids of any section shape.
        self.assertFalse(
            any('-group-' in i for i in ids),
            "flat mode should not emit group lines",
        )
        # The flat account line is still present.
        self.assertIn("account-%s" % self.account_cash.id, ids)

        hierarchical_ids = self._line_ids(self.handler.compute(self.options))
        self.assertTrue(
            any('-group-' in line_id for line_id in hierarchical_ids),
            "fixture must exercise at least one real hierarchy group",
        )

    def test_levels_increase_with_depth(self):
        result = self.handler.compute(self.options)
        outer = self._line_by_id(
            result, "section-assets_current-group-%s" % self.parent_group.id,
        )
        inner = self._line_by_id(
            result, "section-assets_current-group-%s_%s" % (
                self.parent_group.id, self.child_group.id,
            ),
        )
        cash_line = self._line_by_id(
            result, "account-%s" % self.account_cash.id,
        )
        self.assertEqual(outer['level'], 1)
        self.assertEqual(inner['level'], 2)
        self.assertEqual(cash_line['level'], 3)
