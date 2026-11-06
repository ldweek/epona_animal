# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Tests for component accounting, impairment, and AU low-value pool.
"""

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from .common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestComponentAccounting(EhAssetTestCase):

    def test_component_links_to_parent(self):
        parent = self._make_asset(code='BLDG-001', acquisition_cost=1_000_000.0)
        component = self._make_asset(
            code='BLDG-001-HVAC',
            acquisition_cost=80_000.0,
            parent_asset_id=parent.id,
        )
        self.assertEqual(component.parent_asset_id, parent)
        self.assertEqual(parent.component_count, 1)
        self.assertEqual(parent.component_ids, component)

    def test_rolled_up_cost_includes_components(self):
        parent = self._make_asset(code='BLDG-002', acquisition_cost=900_000.0)
        self._make_asset(
            code='BLDG-002-HVAC',
            acquisition_cost=70_000.0,
            parent_asset_id=parent.id,
        )
        self._make_asset(
            code='BLDG-002-LIFT',
            acquisition_cost=30_000.0,
            parent_asset_id=parent.id,
        )
        parent.invalidate_recordset(['rolled_up_cost'])
        self.assertAlmostEqual(parent.rolled_up_cost, 1_000_000.0, places=2)
        self.assertEqual(parent.component_count, 2)

    def test_leaf_asset_rolled_up_equals_own(self):
        a = self._make_asset(code='LEAF-1', acquisition_cost=12000.0)
        self.assertEqual(a.rolled_up_cost, a.acquisition_cost)
        self.assertEqual(a.rolled_up_nbv, a.net_book_value)


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestImpairment(EhAssetTestCase):

    def setUp(self):
        super().setUp()
        self.asset = self._make_asset(
            acquisition_cost=50_000.0,
            method='straight_line',
            useful_life_months=60,
            prorate_first_period=False,
        )
        self.asset.action_compute_schedule()
        self.asset.action_activate()

    def test_impairment_charge_reduces_nbv(self):
        before = self.asset.net_book_value
        impairment = self.env['eh.asset.impairment'].create({
            'asset_id': self.asset.id,
            'impairment_date': '2026-06-30',
            'amount': 5_000.0,
            'is_reversal': False,
            'reason': 'Recoverable amount fell below carrying amount',
        })
        impairment.action_post()
        self.asset.invalidate_recordset(
            ['net_book_value', 'accumulated_impairment'],
        )
        self.assertAlmostEqual(
            self.asset.net_book_value, before - 5_000.0, places=2,
        )
        self.assertAlmostEqual(
            self.asset.accumulated_impairment, 5_000.0, places=2,
        )

    def test_draft_impairment_does_not_change_nbv(self):
        """Reported net book value must reflect only posted impairments.
        A draft (unposted) impairment must not reduce NBV or accumulated
        impairment; posting it applies the reduction. This keeps the
        displayed carrying amount in sync with the posted ledger."""
        before_nbv = self.asset.net_book_value
        before_accum = self.asset.accumulated_impairment
        impairment = self.env['eh.asset.impairment'].create({
            'asset_id': self.asset.id,
            'impairment_date': '2026-06-30',
            'amount': 5_000.0,
            'is_reversal': False,
            'reason': 'Draft impairment pending review',
        })
        self.assertEqual(impairment.state, 'draft')
        self.asset.invalidate_recordset(
            ['net_book_value', 'accumulated_impairment'],
        )
        # Draft impairment leaves reported figures untouched.
        self.assertAlmostEqual(
            self.asset.net_book_value, before_nbv, places=2,
        )
        self.assertAlmostEqual(
            self.asset.accumulated_impairment, before_accum, places=2,
        )
        # Posting the impairment applies the reduction.
        impairment.action_post()
        self.asset.invalidate_recordset(
            ['net_book_value', 'accumulated_impairment'],
        )
        self.assertAlmostEqual(
            self.asset.net_book_value, before_nbv - 5_000.0, places=2,
        )
        self.assertAlmostEqual(
            self.asset.accumulated_impairment, before_accum + 5_000.0,
            places=2,
        )

    def test_impairment_reversal_restores_nbv(self):
        # Charge first, then reverse (both posted to the ledger).
        charge = self.env['eh.asset.impairment'].create({
            'asset_id': self.asset.id,
            'impairment_date': '2026-06-30',
            'amount': 5_000.0,
            'reason': 'Initial loss',
        }).action_post()
        self.asset.invalidate_recordset(
            ['net_book_value', 'accumulated_impairment'],
        )
        baseline = self.asset.net_book_value
        self.env['eh.asset.impairment'].create({
            'asset_id': self.asset.id,
            'impairment_date': '2026-12-31',
            'amount': 2_000.0,
            'is_reversal': True,
            'reason': 'Conditions reversed',
        }).action_post()
        self.asset.invalidate_recordset(
            ['net_book_value', 'accumulated_impairment'],
        )
        self.assertAlmostEqual(
            self.asset.net_book_value, baseline + 2_000.0, places=2,
        )
        self.assertAlmostEqual(
            self.asset.accumulated_impairment, 3_000.0, places=2,
        )

    def test_impairment_cancellation_must_be_reverse_chronological(self):
        charge = self.env['eh.asset.impairment'].create({
            'asset_id': self.asset.id,
            'impairment_date': '2026-06-30',
            'amount': 5_000.0,
            'reason': 'Initial loss',
        })
        charge.action_post()
        reversal = self.env['eh.asset.impairment'].create({
            'asset_id': self.asset.id,
            'impairment_date': '2026-07-31',
            'amount': 2_000.0,
            'is_reversal': True,
            'reason': 'Partial recovery',
        })
        reversal.action_post()
        move_count = self.env['account.move'].search_count([])

        with self.assertRaisesRegex(UserError, 'reverse chronological'):
            charge.action_cancel()

        self.assertEqual(charge.state, 'posted')
        self.assertEqual(reversal.state, 'posted')
        self.assertFalse(charge.reversal_move_id)
        self.assertEqual(
            self.env['account.move'].search_count([]), move_count,
        )
        self.asset.invalidate_recordset(['accumulated_impairment'])
        self.assertAlmostEqual(
            self.asset.accumulated_impairment, 3_000.0, places=2,
        )

    def test_reversal_above_charges_blocked(self):
        charge = self.env['eh.asset.impairment'].create({
            'asset_id': self.asset.id,
            'impairment_date': '2026-06-30',
            'amount': 1_000.0,
            'reason': 'Small loss',
        })
        charge.action_post()
        with self.assertRaisesRegex(
                ValidationError, 'available impairment balance of 1000'):
            self.env['eh.asset.impairment'].create({
                'asset_id': self.asset.id,
                'impairment_date': '2026-12-31',
                'amount': 2_000.0,
                'is_reversal': True,
                'reason': 'Reversal exceeds charges',
            })

    def test_impairment_post_creates_move(self):
        impairment = self.env['eh.asset.impairment'].create({
            'asset_id': self.asset.id,
            'impairment_date': '2026-06-30',
            'amount': 5_000.0,
            'reason': 'Test charge',
        })
        impairment.action_post()
        self.assertEqual(impairment.state, 'posted')
        self.assertTrue(impairment.move_id)
        self.assertEqual(impairment.move_id.state, 'posted')

    def test_non_manager_cannot_post_impairment(self):
        """SoD: a non-manager (group_eh_user only) is blocked from posting
        an impairment charge to the GL; a manager succeeds and the move
        balances."""
        impairment = self.env['eh.asset.impairment'].create({
            'asset_id': self.asset.id,
            'impairment_date': '2026-06-30',
            'amount': 5_000.0,
            'reason': 'SoD test charge',
        })
        clerk = self._make_non_manager_user()
        with self.assertRaises(UserError):
            impairment.with_user(clerk).action_post()
        self.assertEqual(impairment.state, 'draft')
        self.assertFalse(impairment.move_id)
        # Manager path succeeds and the move balances.
        impairment.action_post()
        self.assertEqual(impairment.state, 'posted')
        move = impairment.move_id
        self.assertEqual(move.state, 'posted')
        self.assertAlmostEqual(
            sum(move.line_ids.mapped('debit')),
            sum(move.line_ids.mapped('credit')),
            places=2,
        )

    def test_non_manager_cannot_post_depreciation(self):
        """SoD: a non-manager cannot post a depreciation line to the GL;
        a manager succeeds and the move balances."""
        line = self.asset.depreciation_line_ids.sorted('sequence')[0]
        clerk = self._make_non_manager_user()
        with self.assertRaises(UserError):
            line.with_user(clerk).action_post()
        self.assertFalse(line.is_posted)
        self.assertFalse(line.move_id)
        # Manager path succeeds and the move balances.
        line.action_post()
        self.assertTrue(line.is_posted)
        move = line.move_id
        self.assertEqual(move.state, 'posted')
        self.assertAlmostEqual(
            sum(move.line_ids.mapped('debit')),
            sum(move.line_ids.mapped('credit')),
            places=2,
        )

    def _impair_asset(self, **overrides):
        """Build, schedule, and activate a clean straight-line asset for
        the IAS 36 schedule tests. 60,000 over 60 months, no salvage, no
        proration: original depreciation is a flat 1,000 per period."""
        vals = {
            'acquisition_cost': 60_000.0,
            'method': 'straight_line',
            'useful_life_months': 60,
            'salvage_value': 0.0,
            'prorate_first_period': False,
        }
        vals.update(overrides)
        asset = self._make_asset(**vals)
        asset.action_compute_schedule()
        asset.action_activate()
        return asset

    def _post_first_lines(self, asset, count):
        for line in asset.depreciation_line_ids.sorted('sequence')[:count]:
            line.action_post()

    def test_impairment_rebuilds_remaining_schedule(self):
        """IAS 36.63: after an impairment charge is posted, future
        depreciation is re-amortised on the impaired carrying amount over
        the remaining life, so total depreciation plus impairment never
        exceeds cost less salvage (no over-depreciation)."""
        asset = self._impair_asset(code='IMP-REBUILD')
        self._post_first_lines(asset, 5)
        asset.invalidate_recordset(['net_book_value', 'total_depreciated'])
        self.assertAlmostEqual(asset.net_book_value, 55_000.0, places=2)

        self.env['eh.asset.impairment'].create({
            'asset_id': asset.id,
            'impairment_date': '2026-06-30',
            'amount': 11_000.0,
            'reason': 'Recoverable amount below carrying amount',
            'impairment_account_id': self.account_impairment.id,
            'accumulated_account_id': self.account_accum_dep.id,
        }).action_post()
        asset.invalidate_recordset(
            ['net_book_value', 'accumulated_impairment'],
        )

        self.assertAlmostEqual(asset.net_book_value, 44_000.0, places=2)
        unposted = asset.depreciation_line_ids.filtered(
            lambda l: not l.is_posted,
        )
        # Remaining schedule now re-amortises only the impaired NBV.
        self.assertAlmostEqual(
            sum(unposted.mapped('amount')), 44_000.0, places=2,
        )
        # Total scheduled depreciation plus impairment equals cost.
        total = sum(asset.depreciation_line_ids.mapped('amount'))
        self.assertAlmostEqual(
            total + asset.accumulated_impairment, 60_000.0, places=2,
        )
        self.assertAlmostEqual(
            unposted.sorted('sequence')[-1].remaining_value, 0.0, places=2,
        )

    def test_negative_recoverable_amount_blocked(self):
        with self.assertRaises(ValidationError):
            self.env['eh.asset.impairment'].create({
                'asset_id': self.asset.id,
                'impairment_date': '2026-06-30',
                'amount': 1_000.0,
                'recoverable_amount': -1.0,
                'reason': 'Invalid negative measurement',
            })

    def test_backdated_impairment_after_later_posting_blocked(self):
        self.asset.depreciation_line_ids.sorted('sequence')[5].action_post()
        impairment = self.env['eh.asset.impairment'].create({
            'asset_id': self.asset.id,
            'impairment_date': '2026-03-31',
            'amount': 1_000.0,
            'reason': 'Backdated after a later sealed depreciation entry',
        })
        with self.assertRaises(UserError):
            impairment.action_post()

    def test_pooled_asset_cannot_be_impaired_individually(self):
        pool = self.env['eh.asset.lvp.pool'].create({
            'name': 'Impairment exclusion pool',
            'company_id': self.company.id,
            'threshold': 100_000.0,
        })
        pool.action_transfer_asset(self.asset, transfer_date='2026-01-31')
        impairment = self.env['eh.asset.impairment'].create({
            'asset_id': self.asset.id,
            'impairment_date': '2026-06-30',
            'amount': 1_000.0,
            'reason': 'Would double-count pool carrying value',
        })
        with self.assertRaises(UserError):
            impairment.action_post()

    def test_reversal_capped_at_depreciated_historical_cost(self):
        """IAS 36.117: a reversal cannot lift the carrying amount above the
        depreciated historical cost. After an impairment plus continued
        depreciation on the lower base, reversing the full charge would
        breach that ceiling and is blocked even though it does not exceed
        the cumulative charge."""
        asset = self._impair_asset(code='IMP-CEIL')
        self._post_first_lines(asset, 2)
        self.env['eh.asset.impairment'].create({
            'asset_id': asset.id,
            'impairment_date': '2026-03-31',
            'amount': 12_000.0,
            'reason': 'Loss',
            'impairment_account_id': self.account_impairment.id,
            'accumulated_account_id': self.account_accum_dep.id,
        }).action_post()
        # Post three of the rebuilt (lower) periods so depreciation has run
        # on the impaired base.
        for line in (
            asset.depreciation_line_ids
            .filtered(lambda l: not l.is_posted)
            .sorted('sequence')[:3]
        ):
            line.action_post()
        with self.assertRaises(ValidationError):
            self.env['eh.asset.impairment'].create({
                'asset_id': asset.id,
                'impairment_date': '2026-06-30',
                'amount': 12_000.0,
                'is_reversal': True,
                'reason': 'Full reversal exceeds the IAS 36.117 ceiling',
            })

    def test_reversal_within_ceiling_allowed(self):
        """A reversal that stays under both the cumulative charge and the
        depreciated historical cost ceiling is permitted."""
        asset = self._impair_asset(code='IMP-OK')
        self._post_first_lines(asset, 2)
        self.env['eh.asset.impairment'].create({
            'asset_id': asset.id,
            'impairment_date': '2026-03-31',
            'amount': 12_000.0,
            'reason': 'Loss',
            'impairment_account_id': self.account_impairment.id,
            'accumulated_account_id': self.account_accum_dep.id,
        }).action_post()
        for line in (
            asset.depreciation_line_ids
            .filtered(lambda l: not l.is_posted)
            .sorted('sequence')[:3]
        ):
            line.action_post()
        reversal = self.env['eh.asset.impairment'].create({
            'asset_id': asset.id,
            'impairment_date': '2026-06-30',
            'amount': 3_000.0,
            'is_reversal': True,
            'reason': 'Partial recovery within the ceiling',
        })
        self.assertTrue(reversal)


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestLowValuePool(EhAssetTestCase):

    def setUp(self):
        super().setUp()
        # Distinct pool asset account so the transfer reclassification move
        # moves carrying value between different GL accounts (not onto the
        # asset's own account).
        self.account_pool = self._ensure_account(
            self.env, '1540', 'Low-Value Pool', 'asset_fixed',
        )
        self.pool = self.env['eh.asset.lvp.pool'].create({
            'name': 'LVP 2026',
            'company_id': self.company.id,
            'threshold': 1000.0,
            'pool_account_id': self.account_pool.id,
            'accumulated_account_id': self.account_accum_dep.id,
            'expense_account_id': self.account_dep_expense.id,
            'journal_id': self.journal_misc.id,
        })

    def test_transfer_eligible_asset(self):
        small_asset = self._make_asset(
            code='LV-1', acquisition_cost=800.0,
        )
        self.pool.action_transfer_asset(small_asset)
        self.assertEqual(small_asset.lvp_pool_id, self.pool)
        self.assertEqual(self.pool.asset_count, 1)
        self.assertAlmostEqual(self.pool.transferred_in_total, 800.0, places=2)

    def test_transfer_and_compute_are_reachable_through_safe_ui_actions(self):
        small_asset = self._make_asset(
            code='LV-WIZARD', acquisition_cost=800.0,
            in_service_date='2026-03-15',
        )
        asset_action = small_asset.action_open_lvp_transfer_wizard()
        pool_action = self.pool.action_open_transfer_wizard()
        self.assertEqual(
            asset_action['res_model'], 'eh.asset.lvp.transfer.wizard',
        )
        self.assertEqual(
            pool_action['res_model'], 'eh.asset.lvp.transfer.wizard',
        )
        wizard = self.env['eh.asset.lvp.transfer.wizard'].create({
            'company_id': self.company.id,
            'pool_id': self.pool.id,
            'asset_id': small_asset.id,
            'transfer_date': '2026-03-15',
        })
        wizard.action_transfer()
        self.assertTrue(wizard.processed)
        self.assertEqual(small_asset.lvp_pool_id, self.pool)
        with self.assertRaises(UserError):
            wizard.action_transfer()

        current_year = self.pool.action_compute_current_year()
        self.assertEqual(current_year['tag'], 'reload')
        self.assertTrue(self.pool.line_ids.filtered(
            lambda line: line.year == fields.Date.context_today(
                self.pool,
            ).year,
        ))

    def test_non_manager_cannot_transfer_or_compute_pool(self):
        small_asset = self._make_asset(
            code='LV-MANAGER-GATE', acquisition_cost=800.0,
        )
        clerk = self._make_non_manager_user()
        with self.assertRaises(UserError):
            self.pool.with_user(clerk).action_transfer_asset(small_asset)
        with self.assertRaises(UserError):
            self.pool.with_user(clerk).action_compute_year(year=2026)
        self.assertFalse(small_asset.lvp_pool_id)

    def test_transfer_above_threshold_blocked(self):
        big_asset = self._make_asset(
            code='LV-2', acquisition_cost=5000.0,
        )
        with self.assertRaises(UserError):
            self.pool.action_transfer_asset(big_asset)

    def test_transfer_rejects_non_asset_and_auc_schedules(self):
        deferred = self._make_asset(
            code='LV-DEFERRED', acquisition_cost=800.0,
            deferred_type='deferred_expense',
        )
        auc = self._make_asset(
            code='LV-AUC', acquisition_cost=800.0,
            is_under_construction=True,
        )
        with self.assertRaises(UserError):
            self.pool.action_transfer_asset(deferred)
        with self.assertRaises(UserError):
            self.pool.action_transfer_asset(auc)

    def test_transfer_cannot_precede_acquisition_or_posted_depreciation(self):
        asset = self._make_asset(code='LV-BACKDATE', acquisition_cost=900.0)
        with self.assertRaises(UserError):
            self.pool.action_transfer_asset(asset, transfer_date='2025-12-31')
        asset.action_activate()
        asset.depreciation_line_ids.sorted('sequence')[2].action_post()
        with self.assertRaises(UserError):
            self.pool.action_transfer_asset(asset, transfer_date='2026-02-28')

    def test_compute_year_first_year_rate(self):
        # Asset allocated to the pool in 2026 -> first-year rate (18.75%)
        # applies to additions; opening balance is zero in year 1.
        small_asset = self._make_asset(
            code='LV-3',
            acquisition_cost=1000.0,
            in_service_date='2026-03-15',
        )
        self.pool.action_transfer_asset(small_asset, transfer_date='2026-03-15')
        line = self.pool.action_compute_year(year=2026)
        # additions = 1000, opening = 0
        # depreciation = 0 + 0.1875 * 1000 = 187.5
        self.assertAlmostEqual(line.amount, 187.5, places=2)

    def test_compute_year_subsequent_year_rate(self):
        # Asset allocated to the pool in 2025 -> appears in opening balance
        # for 2026.
        small_asset = self._make_asset(
            code='LV-4',
            acquisition_cost=1000.0,
            in_service_date='2025-06-01',
        )
        self.pool.action_transfer_asset(small_asset, transfer_date='2025-06-01')
        # Year 1 (2025): additions=1000, opening=0 -> 187.5
        line_2025 = self.pool.action_compute_year(year=2025)
        # Year 2 (2026): the same asset is now in opening_balance
        # (post prior-year dep). opening = 1000 - 187.5 = 812.5
        # depreciation = 0.375 * 812.5 = 304.6875 -> rounded
        line_2026 = self.pool.action_compute_year(year=2026)
        self.assertAlmostEqual(line_2025.amount, 187.5, places=2)
        self.assertAlmostEqual(line_2026.amount, 304.69, places=2)

    def test_compute_year_idempotent(self):
        small_asset = self._make_asset(
            code='LV-5', acquisition_cost=800.0,
            in_service_date='2026-01-01',
        )
        self.pool.action_transfer_asset(small_asset, transfer_date='2026-01-01')
        self.pool.action_compute_year(year=2026)
        with self.assertRaises(UserError):
            self.pool.action_compute_year(year=2026)

    def test_transfer_posts_balanced_reclass_move(self):
        """When the pool carries its own GL accounts, transferring an asset
        posts the balanced reclassification move claimed in the docstring:
        the asset's net book value lands in the pool asset account and the
        entry balances by construction."""
        small_asset = self._make_asset(
            code='LV-RECLASS', acquisition_cost=900.0,
        )
        nbv = small_asset.net_book_value
        self.pool.action_transfer_asset(small_asset)
        move = self.env['account.move'].search([
            ('ref', 'like', 'LVP transfer%'),
            ('company_id', '=', self.company.id),
        ], limit=1)
        self.assertTrue(move, "A reclassification move should be posted.")
        self.assertEqual(move.state, 'posted')
        total_debit = sum(move.line_ids.mapped('debit'))
        total_credit = sum(move.line_ids.mapped('credit'))
        self.assertAlmostEqual(total_debit, total_credit, places=2)
        # Net book value lands in the pool asset account on the debit side.
        pool_line = move.line_ids.filtered(
            lambda l: l.account_id == self.pool.pool_account_id,
        )
        self.assertAlmostEqual(sum(pool_line.mapped('debit')), nbv, places=2)

    def test_tax_only_pool_transfer_posts_no_move(self):
        """A pool with no GL accounts is a tax-only pool: the transfer
        records the operational link but posts no journal entry, matching
        the corrected docstring."""
        tax_pool = self.env['eh.asset.lvp.pool'].create({
            'name': 'LVP tax-only',
            'company_id': self.company.id,
            'threshold': 1000.0,
        })
        small_asset = self._make_asset(
            code='LV-TAXONLY', acquisition_cost=700.0,
        )
        before = self.env['account.move'].search_count([
            ('company_id', '=', self.company.id),
        ])
        tax_pool.action_transfer_asset(small_asset)
        after = self.env['account.move'].search_count([
            ('company_id', '=', self.company.id),
        ])
        self.assertEqual(before, after)
        self.assertEqual(small_asset.lvp_pool_id, tax_pool)

    def test_non_manager_cannot_post_pool_depreciation(self):
        """SoD: a non-manager cannot post the pool's annual depreciation to
        the GL; a manager succeeds and the move balances."""
        small_asset = self._make_asset(
            code='LV-SOD', acquisition_cost=1000.0,
            in_service_date='2026-03-15',
        )
        self.pool.action_transfer_asset(small_asset, transfer_date='2026-03-15')
        line = self.pool.action_compute_year(year=2026)
        clerk = self._make_non_manager_user()
        with self.assertRaises(UserError):
            line.with_user(clerk).action_post()
        self.assertFalse(line.is_posted)
        self.assertFalse(line.move_id)
        # Manager path succeeds and the move balances.
        line.action_post()
        self.assertTrue(line.is_posted)
        move = line.move_id
        self.assertEqual(move.state, 'posted')
        self.assertAlmostEqual(
            sum(move.line_ids.mapped('debit')),
            sum(move.line_ids.mapped('credit')),
            places=2,
        )

    def test_pool_depreciates_on_transferred_adjustable_value(self):
        """Regression: an asset transferred in partly depreciated must be
        pooled, reported and depreciated on its opening adjustable value (net
        book value at transfer), not the gross acquisition cost. Otherwise the
        pool over-depreciates and the subledger diverges from the GL that only
        carries the reclassified net book value.
        """
        # $900 asset depreciated individually down to a $400 net book value.
        asset = self._make_asset(
            code='LV-ADJ', acquisition_cost=900.0,
            in_service_date='2023-01-31', useful_life_months=36,
        )
        asset.action_activate()
        # per period = 900/36 = 25; post 20 lines -> 500 depreciated -> nbv 400.
        asset.depreciation_line_ids.sorted('sequence')[:20].action_post()
        asset.invalidate_recordset(['net_book_value', 'total_depreciated'])
        self.assertAlmostEqual(asset.net_book_value, 400.0, places=2)
        self.assertLess(asset.net_book_value, asset.acquisition_cost)

        self.pool.action_transfer_asset(asset, transfer_date='2025-03-15')
        # The opening adjustable value (NBV at transfer), not gross cost, is
        # captured and drives the pool's reported balance.
        self.assertAlmostEqual(asset.lvp_opening_value, 400.0, places=2)
        self.assertAlmostEqual(self.pool.transferred_in_total, 400.0, places=2)
        self.assertAlmostEqual(self.pool.pool_balance, 400.0, places=2)

        # First pool year: 18.75% of the $400 adjustable value = 75.00,
        # NOT 18.75% of the $900 gross cost (168.75).
        line = self.pool.action_compute_year(year=2025)
        self.assertAlmostEqual(line.amount, 75.0, places=2)
        self.assertNotAlmostEqual(line.amount, 168.75, places=2)
        self.assertAlmostEqual(line.additions, 400.0, places=2)

    def test_pool_rate_keyed_on_allocation_year_not_in_service(self):
        """Regression: the ATO first-year (18.75%) vs subsequent-year (37.5%)
        rate is keyed on the year the asset is ALLOCATED into the pool, not its
        original in-service year; and an asset is not depreciated in the pool
        for a year before it was transferred in.
        """
        # In service in 2023, but allocated into the pool during 2025.
        asset = self._make_asset(
            code='LV-ALLOC', acquisition_cost=900.0,
            in_service_date='2023-06-15',
        )
        self.pool.action_transfer_asset(asset, transfer_date='2025-04-01')
        self.assertEqual(asset.lvp_allocation_date.year, 2025)

        # 2024 is before allocation: no charge at all (not depreciated in the
        # pool before it was ever transferred in).
        line_2024 = self.pool.action_compute_year(year=2024)
        self.assertAlmostEqual(line_2024.amount, 0.0, places=2)

        # 2025 is the allocation year: first-year 18.75% of 900 = 168.75,
        # NOT the subsequent-year 37.5% (337.50) that the in-service-year
        # mis-key would have charged.
        line_2025 = self.pool.action_compute_year(year=2025)
        self.assertAlmostEqual(line_2025.amount, 168.75, places=2)
        self.assertNotAlmostEqual(line_2025.amount, 337.5, places=2)

    def test_pool_rate_uses_financial_not_calendar_allocation_year(self):
        """A July-June pool treats September 2025 as an FY2026 addition."""
        original_calendar = {
            'fiscalyear_last_day': self.company.fiscalyear_last_day,
            'fiscalyear_last_month': self.company.fiscalyear_last_month,
        }
        try:
            self.company.sudo().write({
                'fiscalyear_last_day': 30,
                'fiscalyear_last_month': '6',
            })
            asset = self._make_asset(
                code='LV-FY-ALLOC', acquisition_cost=900.0,
                in_service_date='2025-09-15',
            )
            self.pool.action_transfer_asset(
                asset, transfer_date='2025-09-15',
            )

            line_2026 = self.pool.action_compute_year(year=2026)

            self.assertAlmostEqual(line_2026.opening_balance, 0.0, places=2)
            self.assertAlmostEqual(line_2026.additions, 900.0, places=2)
            self.assertAlmostEqual(line_2026.amount, 168.75, places=2)
            self.assertEqual(
                line_2026._posting_date(), fields.Date.to_date('2026-06-30'),
            )
        finally:
            self.company.sudo().write(original_calendar)
