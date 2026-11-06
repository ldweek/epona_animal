# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Deferred revenue and deferred expense recognition tests.

These exercise the deferred_type extension on eh.asset: schedule
generation reuses the asset depreciation engine, but action_post on
each schedule line emits a different journal entry shape depending on
the deferred_type. The two flavours are:

* deferred_revenue: DR Deferred Revenue (liability decreases),
  CR Revenue (income recognised).
* deferred_expense: DR Expense (P/L), CR Prepaid Asset (asset decreases).

Both must be balanced and use the configured holding / recognition
accounts. Total recognised across the schedule must equal the original
acquisition cost.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestDeferredRecognition(EhAssetTestCase):

    def _make_deferred(self, deferred_type, **overrides):
        if deferred_type == 'deferred_revenue':
            holding = self._ensure_account(
                self.env, '2400', 'Deferred Revenue', 'liability_current',
            )
            recognition = self._ensure_account(
                self.env, '4001', 'Recognised Revenue', 'income',
            )
        else:
            holding = self._ensure_account(
                self.env, '1410', 'Prepaid Expense Asset', 'asset_current',
            )
            recognition = self.account_expense
        vals = {
            'name': '/',
            'category_id': self.asset_category.id,
            'deferred_type': deferred_type,
            'partner_id': self.partner_a.id,
            'acquisition_date': '2026-01-01',
            'in_service_date': '2026-01-31',
            'acquisition_cost': 12000.0,
            'salvage_value': 0.0,
            'method': 'straight_line',
            'useful_life_months': 12,
            'prorate_first_period': False,
            'asset_account_id': holding.id,
            'depreciation_account_id': recognition.id,
            # accumulated_depreciation is unused on deferred but the
            # field is shared with the asset case; keep it set so the
            # form does not feel half-empty.
            'accumulated_depreciation_account_id': self.account_accum_dep.id,
            'journal_id': self.journal_misc.id,
        }
        vals.update(overrides)
        return self.env['eh.asset'].create(vals)

    def test_deferred_revenue_post_balanced_entry(self):
        deferred = self._make_deferred('deferred_revenue')
        deferred.action_compute_schedule()
        deferred.action_activate()
        first_line = deferred.depreciation_line_ids.sorted('sequence')[0]
        first_line.action_post()
        move = first_line.move_id
        self.assertTrue(move and move.state == 'posted')
        debit = sum(move.line_ids.mapped('debit'))
        credit = sum(move.line_ids.mapped('credit'))
        self.assertAlmostEqual(debit, credit, places=2)
        holding_leg = move.line_ids.filtered(
            lambda l: l.account_id == deferred.asset_account_id,
        )
        self.assertEqual(len(holding_leg), 1)
        self.assertGreater(holding_leg.debit, 0)
        self.assertEqual(holding_leg.credit, 0)
        recog_leg = move.line_ids.filtered(
            lambda l: l.account_id == deferred.depreciation_account_id,
        )
        self.assertEqual(len(recog_leg), 1)
        self.assertGreater(recog_leg.credit, 0)
        self.assertEqual(recog_leg.debit, 0)

    def test_deferred_expense_post_balanced_entry(self):
        deferred = self._make_deferred('deferred_expense')
        deferred.action_compute_schedule()
        deferred.action_activate()
        first_line = deferred.depreciation_line_ids.sorted('sequence')[0]
        first_line.action_post()
        move = first_line.move_id
        debit = sum(move.line_ids.mapped('debit'))
        credit = sum(move.line_ids.mapped('credit'))
        self.assertAlmostEqual(debit, credit, places=2)
        recog_leg = move.line_ids.filtered(
            lambda l: l.account_id == deferred.depreciation_account_id,
        )
        self.assertEqual(len(recog_leg), 1)
        self.assertGreater(recog_leg.debit, 0)
        holding_leg = move.line_ids.filtered(
            lambda l: l.account_id == deferred.asset_account_id,
        )
        self.assertEqual(len(holding_leg), 1)
        self.assertGreater(holding_leg.credit, 0)

    def test_deferred_total_recognised_equals_initial(self):
        deferred = self._make_deferred('deferred_expense')
        deferred.action_compute_schedule()
        deferred.action_activate()
        for line in deferred.depreciation_line_ids.sorted('sequence'):
            line.action_post()
        recognised = 0.0
        for line in deferred.depreciation_line_ids:
            recog_legs = line.move_id.line_ids.filtered(
                lambda l: l.account_id == deferred.depreciation_account_id,
            )
            recognised += sum(recog_legs.mapped('debit'))
        self.assertAlmostEqual(recognised, 12000.0, places=2)

    def test_deferred_schedules_reject_fixed_asset_disposal(self):
        for deferred_type in ('deferred_revenue', 'deferred_expense'):
            deferred = self._make_deferred(
                deferred_type,
                code='DISPOSE-%s' % deferred_type,
            )
            deferred.action_activate()
            move_count = self.env['account.move'].search_count([])
            line_count = len(deferred.depreciation_line_ids)
            with self.subTest(kind=deferred_type, entry='asset_button'):
                with self.assertRaises(UserError):
                    deferred.action_open_dispose_wizard()
            wizard = self.env['eh.asset.dispose.wizard'].create({
                'asset_id': deferred.id,
                'disposal_date': '2026-06-30',
            })
            with self.subTest(kind=deferred_type, entry='direct_rpc'):
                with self.assertRaises(UserError):
                    wizard.action_dispose()
            self.assertEqual(deferred.state, 'running')
            self.assertEqual(len(deferred.depreciation_line_ids), line_count)
            self.assertEqual(
                self.env['account.move'].search_count([]), move_count,
            )
