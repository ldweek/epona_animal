# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Asset lifecycle: create, compute schedule, activate, post lines.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestAssetLifecycle(EhAssetTestCase):

    def test_create_asset_assigns_sequence(self):
        asset = self._make_asset(code='SEQ-001')
        self.assertNotEqual(asset.name, '/')
        self.assertTrue(asset.name.startswith('FA/'))

    def test_compute_schedule_straight_line(self):
        asset = self._make_asset(
            acquisition_cost=36000.0,
            useful_life_months=36,
            method='straight_line',
            prorate_first_period=False,
        )
        asset.action_compute_schedule()
        self.assertEqual(len(asset.depreciation_line_ids), 36)
        # 36 months at 1000 each.
        self.assertEqual(
            asset.depreciation_line_ids[0].amount, 1000.0,
        )
        # All lines sum to depreciable (cost - salvage).
        total = sum(asset.depreciation_line_ids.mapped('amount'))
        self.assertEqual(total, 36000.0)

    def test_compute_schedule_with_salvage(self):
        asset = self._make_asset(
            acquisition_cost=10000.0,
            salvage_value=1000.0,
            useful_life_months=36,
            method='straight_line',
            prorate_first_period=False,
        )
        asset.action_compute_schedule()
        # Total depreciable = 9000, last NBV should be salvage.
        total = sum(asset.depreciation_line_ids.mapped('amount'))
        self.assertAlmostEqual(total, 9000.0, places=2)
        self.assertAlmostEqual(
            asset.depreciation_line_ids[-1].remaining_value, 1000.0, places=2,
        )

    def test_compute_schedule_reducing_balance(self):
        asset = self._make_asset(
            acquisition_cost=10000.0,
            useful_life_months=24,
            method='reducing_balance',
            declining_factor=2.0,
            prorate_first_period=False,
        )
        asset.action_compute_schedule()
        self.assertGreater(len(asset.depreciation_line_ids), 0)
        # Total must equal cost minus salvage (0).
        total = sum(asset.depreciation_line_ids.mapped('amount'))
        self.assertAlmostEqual(total, 10000.0, places=1)

    def test_supported_method_selections_are_honest_and_pin_uop_refusal(self):
        asset_methods = dict(
            self.env['eh.asset']._fields['method'].selection,
        )
        category_methods = dict(
            self.env['eh.asset.category']._fields['method'].selection,
        )
        expected = {
            'straight_line', 'reducing_balance', 'prime_cost',
            'diminishing_value', 'manual',
        }
        self.assertTrue(expected.issubset(set(asset_methods)))
        self.assertTrue(expected.issubset(set(category_methods)))
        self.assertNotIn('units_of_production', asset_methods)
        self.assertNotIn('units_of_production', category_methods)
        with self.assertRaises(ValueError):
            self._make_asset(
                code='UOP-REFUSED-ASSET', method='units_of_production',
            )
        with self.assertRaises(ValueError):
            self.env['eh.asset.category'].create({
                'name': 'Unsupported UOP category',
                'code': 'UOPX',
                'method': 'units_of_production',
            })

    def test_activate_requires_posting_setup(self):
        asset = self._make_asset(
            code='IT-NS-1',
            depreciation_account_id=False,
        )
        with self.assertRaises(UserError):
            asset.action_activate()

    def test_activate_generates_schedule_if_missing(self):
        asset = self._make_asset(code='IT-AUTO-1')
        # No explicit compute call; activation should build the schedule.
        asset.action_activate()
        self.assertEqual(asset.state, 'running')
        self.assertEqual(len(asset.depreciation_line_ids), 36)

    def test_pause_and_resume(self):
        asset = self._make_asset(code='IT-PR-1')
        asset.action_activate()
        asset.action_pause()
        self.assertEqual(asset.state, 'paused')
        asset.action_resume()
        self.assertEqual(asset.state, 'running')

    def test_post_due_lines_creates_moves(self):
        asset = self._make_asset(
            code='IT-POST-1',
            in_service_date='2025-01-31',
            acquisition_cost=12000.0,
            useful_life_months=12,
        )
        asset.action_activate()
        asset.action_post_due_lines()
        posted = asset.depreciation_line_ids.filtered(lambda l: l.is_posted)
        self.assertEqual(len(posted), 12)
        self.assertAlmostEqual(sum(posted.mapped('amount')), 12_000.0, places=2)
        for line in posted:
            self.assertTrue(line.move_id)
            self.assertEqual(line.move_id.state, 'posted')
            expense = line.move_id.line_ids.filtered(
                lambda move_line: (
                    move_line.account_id == self.account_dep_expense
                ),
            )
            accumulated = line.move_id.line_ids.filtered(
                lambda move_line: (
                    move_line.account_id == self.account_accum_dep
                ),
            )
            self.assertAlmostEqual(expense.debit, line.amount, places=2)
            self.assertAlmostEqual(accumulated.credit, line.amount, places=2)
            self.assertAlmostEqual(
                sum(line.move_id.line_ids.mapped('debit')),
                sum(line.move_id.line_ids.mapped('credit')),
                places=2,
            )
        self.assertAlmostEqual(asset.total_depreciated, 12_000.0, places=2)
        self.assertAlmostEqual(asset.net_book_value, 0.0, places=2)
        self.assertEqual(asset.state, 'fully_depreciated')

    def test_locked_schedule_date_fails_instead_of_silent_redate(self):
        asset = self._make_asset(
            code='IT-LOCK-DATE',
            in_service_date='2026-01-31',
            acquisition_cost=12_000.0,
            useful_life_months=12,
        )
        asset.action_activate()
        line = asset.depreciation_line_ids.sorted('sequence')[0]
        self.company.fiscalyear_lock_date = line.depreciation_date
        company_checker = getattr(
            self.company, '_get_violated_lock_dates', None,
        )
        if company_checker:
            try:
                lock_dates = company_checker(
                    line.depreciation_date, False, asset.journal_id,
                )
            except TypeError:
                lock_dates = company_checker(
                    line.depreciation_date, False,
                )
            self.assertTrue(lock_dates)
        else:
            # Odoo 16-17 keep the same check on account.move.
            probe = self.env['account.move'].new({
                'company_id': self.company.id,
                'journal_id': asset.journal_id.id,
                'date': line.depreciation_date,
            })
            self.assertTrue(probe._get_violated_lock_dates(
                line.depreciation_date, False,
            ))
        move_count = self.env['account.move'].search_count([])

        with self.assertRaisesRegex(UserError, 'loss of provenance'):
            line.action_post()

        self.assertFalse(line.is_posted)
        self.assertFalse(line.move_id)
        self.assertEqual(
            self.env['account.move'].search_count([]), move_count,
        )

    def test_set_to_draft_blocked_after_post(self):
        asset = self._make_asset(
            code='IT-D-1',
            in_service_date='2025-01-31',
            acquisition_cost=12000.0,
            useful_life_months=12,
        )
        asset.action_activate()
        asset.action_post_due_lines()
        with self.assertRaises(UserError):
            asset.action_set_to_draft()

    def test_set_to_draft_when_no_post(self):
        asset = self._make_asset(code='IT-D-2')
        asset.action_activate()
        asset.action_set_to_draft()
        self.assertEqual(asset.state, 'draft')

    def test_recompute_schedule_in_draft_only(self):
        asset = self._make_asset(code='IT-RC-1')
        asset.action_compute_schedule()
        asset.action_activate()
        with self.assertRaises(UserError):
            asset.action_compute_schedule()
