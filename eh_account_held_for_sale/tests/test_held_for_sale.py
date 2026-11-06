# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IFRS 5 held-for-sale tests."""

import importlib.util
from pathlib import Path

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_held_for_sale', 'integration', 'post_install', '-at_install')
class TestHeldForSale(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.asset = cls._ensure_account(
            cls.env, '1700', 'Assets Held for Sale', 'asset_current')
        cls.impairment = cls._ensure_account(
            cls.env, '5170', 'Held-for-sale Impairment', 'expense')
        cls.gain_loss = cls._ensure_account(
            cls.env, '4700', 'Disposal Gain/Loss', 'income_other')

    def _item(self, carrying=1000.0, fvlcts=800.0, **vals):
        base = {
            'name': '/', 'carrying_amount': carrying,
            'fair_value_less_costs': fvlcts,
            'classification_date': '2026-01-31',
            'asset_account_id': self.asset.id,
            'impairment_account_id': self.impairment.id,
            'proceeds_account_id': self.account_cash.id,
            'gain_loss_account_id': self.gain_loss.id,
            'journal_id': self.journal_misc.id,
        }
        base.update(vals)
        return self.env['eh.held.for.sale'].create(base)

    def _bal(self, account):
        lines = self.env['account.move.line'].search([
            ('account_id', '=', account.id), ('parent_state', '=', 'posted')])
        return sum(lines.mapped('debit')) - sum(lines.mapped('credit'))

    def _running_asset(self, cost=36000.0, post_first_line=True):
        """A running fixed asset with its schedule generated and, by
        default, its first depreciation line posted so net book value has
        moved below cost.
        """
        accum = self._ensure_account(
            self.env, '1510', 'Accumulated Depreciation', 'asset_fixed')
        dep_exp = self._ensure_account(
            self.env, '5100', 'Depreciation Expense', 'expense_depreciation')
        fixed = self._ensure_account(
            self.env, '1500', 'Fixed Assets', 'asset_fixed')
        category = self.env['eh.asset.category'].create({
            'name': 'HFS IT Hardware', 'code': 'HFSITHW',
            'method': 'straight_line', 'useful_life_months': 36,
            'salvage_rate': 0.0, 'prorate_first_period': False,
            'asset_account_id': fixed.id,
            'depreciation_account_id': dep_exp.id,
            'accumulated_depreciation_account_id': accum.id,
            'disposal_gain_account_id': self.gain_loss.id,
            'disposal_loss_account_id': self.gain_loss.id,
            'journal_id': self.journal_misc.id,
        })
        asset = self.env['eh.asset'].create({
            'name': '/', 'category_id': category.id,
            'acquisition_date': '2026-01-01', 'in_service_date': '2026-01-31',
            'acquisition_cost': cost, 'salvage_value': 0.0,
            'method': 'straight_line', 'useful_life_months': 36,
            'prorate_first_period': False,
            'asset_account_id': fixed.id,
            'depreciation_account_id': dep_exp.id,
            'accumulated_depreciation_account_id': accum.id,
            'disposal_gain_account_id': self.gain_loss.id,
            'disposal_loss_account_id': self.gain_loss.id,
            'journal_id': self.journal_misc.id,
        })
        asset.action_activate()
        if post_first_line:
            first = asset.depreciation_line_ids.sorted('depreciation_date')[0]
            first.action_post()
        return asset

    def test_writedown_lower_of(self):
        item = self._item(carrying=1000.0, fvlcts=800.0)
        self.assertAlmostEqual(item.writedown, 200.0, places=2)

    def test_classify_posts_writedown(self):
        item = self._item(carrying=1000.0, fvlcts=800.0)
        item.action_classify()
        self.assertEqual(item.state, 'held')
        self.assertTrue(item.depreciation_ceased)
        self.assertAlmostEqual(item.carrying_amount, 800.0, places=2)
        self.assertAlmostEqual(self._bal(self.impairment), 200.0, places=2)
        self.assertAlmostEqual(self._bal(self.asset), -200.0, places=2)

    def test_classify_no_writedown_when_fvlcts_above(self):
        item = self._item(carrying=1000.0, fvlcts=1200.0)
        item.action_classify()
        self.assertAlmostEqual(item.carrying_amount, 1000.0, places=2)
        self.assertFalse(self._bal(self.impairment))

    def test_sale_with_gain(self):
        item = self._item(carrying=1000.0, fvlcts=800.0)
        item.action_classify()  # carrying now 800
        item.proceeds = 900.0
        item.action_sell()
        self.assertEqual(item.state, 'sold')
        self.assertAlmostEqual(self._bal(self.account_cash), 900.0, places=2)
        # Gain = 900 - 800 = 100.
        self.assertAlmostEqual(self._bal(self.gain_loss), -100.0, places=2)

    def test_sale_with_loss(self):
        item = self._item(carrying=1000.0, fvlcts=800.0)
        item.action_classify()
        item.proceeds = 700.0
        item.action_sell()
        self.assertAlmostEqual(self._bal(self.gain_loss), 100.0, places=2)

    def test_sale_of_linked_asset_disposes_it(self):
        """Selling an item linked to a fixed asset must derecognise the
        underlying eh.asset (reverse cost + accumulated depreciation, move it
        to 'disposed'), not leave it paused with a live net book value."""
        asset = self._running_asset(cost=36000.0)
        item = self._item(carrying=35000.0, fvlcts=40000.0, asset_id=asset.id)
        item.action_classify()  # fvlcts above NBV: no writedown; asset paused
        self.assertEqual(asset.state, 'paused')
        self.assertTrue(item.asset_paused_by_hfs)
        item.proceeds = 40000.0
        item.action_sell()
        self.assertEqual(item.state, 'sold')
        # The whole point of the fix: the underlying asset is actually
        # disposed and no longer resumable, not stranded 'paused'.
        self.assertEqual(asset.state, 'disposed')
        self.assertFalse(item.asset_paused_by_hfs)
        self.assertEqual(asset.disposal_move_id.eh_held_for_sale_id, item)
        self.assertIn(asset.disposal_move_id, item.move_ids)
        disposal = asset.disposal_move_id
        by_account = {
            account: (
                sum(disposal.line_ids.filtered(
                    lambda line: line.account_id == account).mapped('debit')),
                sum(disposal.line_ids.filtered(
                    lambda line: line.account_id == account).mapped('credit')),
            )
            for account in (
                self.account_cash, asset.asset_account_id,
                asset.accumulated_depreciation_account_id, self.gain_loss,
            )
        }
        self.assertEqual(by_account[self.account_cash], (40000.0, 0.0))
        self.assertEqual(by_account[asset.asset_account_id], (0.0, 36000.0))
        self.assertEqual(
            by_account[asset.accumulated_depreciation_account_id],
            (1000.0, 0.0),
        )
        self.assertEqual(by_account[self.gain_loss], (0.0, 5000.0))

    def test_entries_balance(self):
        item = self._item(carrying=1000.0, fvlcts=800.0)
        item.action_classify()
        item.proceeds = 850.0
        item.action_sell()
        for move in item.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_cannot_sell_before_classify(self):
        item = self._item()
        with self.assertRaises(UserError):
            item.action_sell()

    def test_carrying_amount_locked_after_classify(self):
        item = self._item(carrying=1000.0, fvlcts=800.0)
        item.action_classify()
        self.assertAlmostEqual(item.carrying_amount, 800.0, places=2)
        # A hand edit of the ledger-derived carrying amount is blocked.
        with self.assertRaises(UserError):
            item.carrying_amount = 500.0
        # The figure remains in sync with the posted remeasurement.
        self.assertAlmostEqual(item.carrying_amount, 800.0, places=2)
        self.assertAlmostEqual(self._bal(self.asset), -200.0, places=2)

    def test_carrying_amount_locked_after_sale(self):
        item = self._item(carrying=1000.0, fvlcts=800.0)
        item.action_classify()
        item.proceeds = 900.0
        item.action_sell()
        with self.assertRaises(UserError):
            item.carrying_amount = 123.0

    def test_remeasure_further_writedown(self):
        item = self._item(carrying=1000.0, fvlcts=800.0)
        item.action_classify()  # carrying now 800
        item.fair_value_less_costs = 650.0
        item.action_remeasure()
        self.assertAlmostEqual(item.carrying_amount, 650.0, places=2)
        # Original 200 write-down plus a further 150 = 350 in impairment.
        self.assertAlmostEqual(self._bal(self.impairment), 350.0, places=2)
        self.assertAlmostEqual(self._bal(self.asset), -350.0, places=2)

    def test_remeasure_reversal(self):
        item = self._item(carrying=1000.0, fvlcts=800.0)
        item.action_classify()  # carrying now 800, impairment 200
        item.fair_value_less_costs = 950.0
        item.action_remeasure()
        self.assertAlmostEqual(item.carrying_amount, 950.0, places=2)
        # 200 write-down partly reversed by 150 => 50 net in impairment.
        self.assertAlmostEqual(self._bal(self.impairment), 50.0, places=2)
        self.assertAlmostEqual(self._bal(self.asset), -50.0, places=2)

    def test_remeasure_reversal_within_cap_posts_fully(self):
        # Classify 1000 -> 800 (write-down 200). A rise back to exactly 1000
        # is fully within the cumulative write-down of 200, so it posts in
        # full and returns the carrying amount to its pre-classification level.
        item = self._item(carrying=1000.0, fvlcts=800.0)
        item.action_classify()  # carrying now 800, impairment 200
        item.fair_value_less_costs = 1000.0
        item.action_remeasure()
        self.assertAlmostEqual(item.carrying_amount, 1000.0, places=2)
        # 200 write-down fully reversed => nil net in impairment / asset.
        self.assertAlmostEqual(self._bal(self.impairment), 0.0, places=2)
        self.assertAlmostEqual(self._bal(self.asset), 0.0, places=2)

    def test_remeasure_reversal_capped_at_cumulative_writedown(self):
        # Classify 1000 -> 800 (write-down 200). A later FVLCTS of 1500 would
        # imply a 700 reversal, but IFRS 5.22 caps the gain at the cumulative
        # 200 write-down: only 200 is reversed and carrying stops at 1000, the
        # pre-classification amount, not 1500.
        item = self._item(carrying=1000.0, fvlcts=800.0)
        item.action_classify()  # carrying now 800, impairment 200
        item.fair_value_less_costs = 1500.0
        item.action_remeasure()
        self.assertAlmostEqual(item.carrying_amount, 1000.0, places=2)
        # Reversal limited to 200 => impairment and asset net to nil, never
        # a net credit that would carry the asset above its original cost.
        self.assertAlmostEqual(self._bal(self.impairment), 0.0, places=2)
        self.assertAlmostEqual(self._bal(self.asset), 0.0, places=2)
        # The posted reversal leg is 200, not the uncapped 700.
        reversal = item.move_ids.filtered(
            lambda m: any('reversal' in (line.name or '')
                          for line in m.line_ids))
        asset_leg = reversal.line_ids.filtered(
            lambda line: line.account_id == self.asset)
        self.assertAlmostEqual(sum(asset_leg.mapped('debit')), 200.0, places=2)

    def test_remeasure_entries_balance(self):
        item = self._item(carrying=1000.0, fvlcts=800.0)
        item.action_classify()
        item.fair_value_less_costs = 700.0
        item.action_remeasure()
        for move in item.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_remeasure_requires_manager(self):
        item = self._item(carrying=1000.0, fvlcts=800.0)
        item.action_classify()
        item.fair_value_less_costs = 700.0
        user = self.env['res.users'].create({
            'name': 'p2', 'login': 'hfs_plain2@test',
            'email': 'hfs_plain2@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            item.with_user(user).action_remeasure()

    def test_classify_requires_manager(self):
        item = self._item()
        user = self.env['res.users'].create({
            'name': 'p', 'login': 'hfs_plain@test', 'email': 'hfs_plain@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            item.with_user(user).action_classify()

    # ---- linked-asset integration (IFRS 5.15, 5.25) ----

    def test_classify_with_asset_pauses_and_seeds_nbv(self):
        # 36,000 over 36 months, no proration => 1,000 first charge posted,
        # so net book value is 35,000. Classifying an item linked to this
        # asset must seed the carrying amount from that ledger figure and
        # pause the asset (its depreciation ceases, IFRS 5.25).
        asset = self._running_asset(cost=36000.0)
        self.assertEqual(asset.state, 'running')
        self.assertAlmostEqual(asset.net_book_value, 35000.0, places=2)
        # Hand-key a wrong carrying amount to prove classify overrides it
        # from the asset's NBV rather than trusting the input.
        item = self._item(carrying=1.0, fvlcts=40000.0, asset_id=asset.id)
        item.action_classify()
        self.assertEqual(item.state, 'held')
        self.assertTrue(item.depreciation_ceased)
        self.assertTrue(item.asset_paused_by_hfs)
        # Carrying seeded from NBV (35,000), not the hand-keyed 1.0. FVLCTS
        # is above NBV so there is no write-down and it holds at 35,000.
        self.assertAlmostEqual(item.carrying_amount, 35000.0, places=2)
        # The asset is paused so the monthly cron will skip it.
        self.assertEqual(asset.state, 'paused')

    def test_classification_posts_full_due_rows_and_mid_period_stub(self):
        """IFRS 5.25 stops depreciation on the classification date, not at
        the previous schedule boundary."""
        asset = self._running_asset(cost=36000.0, post_first_line=False)
        item = self._item(
            carrying=1.0,
            fvlcts=40000.0,
            asset_id=asset.id,
            classification_date='2026-02-15',
        )

        item.action_classify()

        posted = asset.depreciation_line_ids.filtered('is_posted')
        stub = posted.filtered('is_event_accrual')
        self.assertEqual(len(stub), 1)
        self.assertEqual(str(stub.depreciation_date), '2026-02-15')
        self.assertAlmostEqual(stub.amount, 535.71, places=2)
        self.assertTrue(asset.depreciation_line_ids.filtered(
            lambda line: line.is_posted
            and str(line.depreciation_date) == '2026-01-31'))
        self.assertAlmostEqual(item.carrying_amount, 34464.29, places=2)
        self.assertAlmostEqual(item.carrying_amount, asset.net_book_value, 2)

    def test_cancel_rebuilds_future_schedule_from_resume_date(self):
        asset = self._running_asset(cost=36000.0, post_first_line=False)
        item = self._item(
            carrying=1.0,
            fvlcts=40000.0,
            asset_id=asset.id,
            classification_date='2026-02-15',
        )
        item.action_classify()
        remaining_periods = len(asset.depreciation_line_ids.filtered(
            lambda line: not line.is_posted))
        item.write({
            'declassification_date': '2026-05-15',
            'declassification_adjusted_carrying_amount': item.carrying_amount,
            'declassification_recoverable_amount': item.carrying_amount + 1.0,
            'declassification_measurement_completed': True,
            'declassification_basis': 'Prospective resume workpaper HFS-27-R',
        })

        item.action_cancel()

        future = asset.depreciation_line_ids.filtered(
            lambda line: not line.is_posted).sorted('depreciation_date')
        self.assertEqual(len(future), remaining_periods)
        self.assertTrue(future)
        self.assertGreater(str(future[0].depreciation_date), '2026-05-15')
        self.assertFalse(future.filtered(
            lambda line: str(line.depreciation_date) <= '2026-05-15'))

    def test_classification_respects_accounting_lock_date(self):
        asset = self._running_asset(cost=36000.0, post_first_line=False)
        item = self._item(
            carrying=1.0,
            fvlcts=40000.0,
            asset_id=asset.id,
            classification_date='2026-02-15',
        )
        previous_lock = self.company.fiscalyear_lock_date
        self.company.fiscalyear_lock_date = '2026-02-15'
        try:
            with self.assertRaises(UserError):
                item.action_classify()
        finally:
            self.company.fiscalyear_lock_date = previous_lock
        self.assertEqual(item.state, 'draft')
        self.assertEqual(asset.state, 'running')

    def test_linked_classification_reclasses_gross_ppe_under_ifrs_5_38(self):
        asset = self._running_asset(cost=36000.0)
        ppe_account = asset.asset_account_id
        accumulated_account = asset.accumulated_depreciation_account_id
        item = self._item(
            carrying=1.0, fvlcts=40000.0, asset_id=asset.id)

        item.action_classify()

        move = item.asset_reclassification_move_id
        self.assertTrue(move)
        self.assertEqual(move.state, 'posted')
        self.assertEqual(move.eh_held_for_sale_id, item)
        by_account = {
            account: (
                sum(move.line_ids.filtered(
                    lambda line: line.account_id == account).mapped('debit')),
                sum(move.line_ids.filtered(
                    lambda line: line.account_id == account).mapped('credit')),
            )
            for account in (self.asset, ppe_account, accumulated_account)
        }
        self.assertEqual(by_account[self.asset], (35000.0, 0.0))
        self.assertEqual(by_account[accumulated_account], (1000.0, 0.0))
        self.assertEqual(by_account[ppe_account], (0.0, 36000.0))
        self.assertAlmostEqual(
            sum(move.line_ids.mapped('debit')),
            sum(move.line_ids.mapped('credit')), places=2,
        )

    def test_cease_classification_restores_ppe_and_transfers_impairment(self):
        asset = self._running_asset(cost=36000.0)
        item = self._item(
            carrying=1.0, fvlcts=30000.0, asset_id=asset.id)
        item.action_classify()
        self.assertAlmostEqual(asset.net_book_value, 30000.0, places=2)
        item.write({
            'declassification_adjusted_carrying_amount': 30000.0,
            'declassification_recoverable_amount': 31000.0,
            'declassification_measurement_completed': True,
            'declassification_basis': 'Current IAS 36 workpaper HFS-27',
        })

        item.action_cancel()

        self.assertEqual(item.state, 'cancelled')
        self.assertTrue(item.asset_reclassification_reversal_move_id)
        self.assertEqual(asset.state, 'running')
        hfs_net = sum(
            -event.amount if event.is_reversal else event.amount
            for event in asset.impairment_ids.filtered(
                lambda event: event.state == 'posted'
                and event.accumulated_account_id == self.asset)
        )
        normal_net = sum(
            -event.amount if event.is_reversal else event.amount
            for event in asset.impairment_ids.filtered(
                lambda event: event.state == 'posted'
                and event.accumulated_account_id
                    == asset.accumulated_depreciation_account_id)
        )
        self.assertAlmostEqual(hfs_net, 0.0, places=2)
        self.assertAlmostEqual(normal_net, 5000.0, places=2)
        self.assertAlmostEqual(asset.net_book_value, 30000.0, places=2)

    def test_legacy_linked_classification_is_blocked_until_remediated(self):
        asset = self._running_asset(cost=36000.0)
        item = self._item(
            carrying=asset.net_book_value, fvlcts=34000.0,
            asset_id=asset.id,
        )
        asset.action_pause()
        self.env.cr.execute(
            "UPDATE eh_held_for_sale "
            "SET state = 'held', depreciation_ceased = TRUE, "
            "asset_paused_by_hfs = TRUE, "
            "asset_reclassification_quarantined = TRUE "
            "WHERE id = %s",
            (item.id,),
        )
        item.invalidate_recordset()
        item.fair_value_less_costs = 33000.0
        with self.assertRaisesRegex(UserError, 'no reviewed IFRS 5.38'):
            item.action_remeasure()

        item.write({
            'asset_reclassification_correction_date': fields.Date.today(),
            'asset_reclassification_correction_basis':
                'Upgrade remediation workpaper IC-HFS-001',
        })
        item.action_remediate_asset_reclassification()
        self.assertTrue(item.asset_reclassification_move_id)
        self.assertFalse(item.asset_reclassification_quarantined)
        item.action_remeasure()
        self.assertAlmostEqual(item.carrying_amount, 33000.0, places=2)

    def test_upgrade_quarantines_legacy_linked_classification(self):
        asset = self._running_asset(cost=36000.0)
        item = self._item(
            carrying=asset.net_book_value, fvlcts=asset.net_book_value,
            asset_id=asset.id,
        )
        self.env.cr.execute(
            "UPDATE eh_held_for_sale SET state = 'held' WHERE id = %s",
            (item.id,),
        )
        group_asset = asset
        group = self.env['eh.disposal.group'].create({
            'name': '/',
            'classification_date': '2026-01-31',
            'fair_value_less_costs': group_asset.net_book_value,
            'asset_account_id': self.asset.id,
            'impairment_account_id': self.impairment.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [(0, 0, {
                'name': 'Legacy linked member',
                'carrying_amount': group_asset.net_book_value,
                'asset_id': group_asset.id,
                'account_id': self.asset.id,
            })],
        })
        self.env.cr.execute(
            "UPDATE eh_disposal_group SET state = 'held' WHERE id = %s",
            (group.id,),
        )
        migration_path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.2.1.0' / 'post-migration.py'
        )
        spec = importlib.util.spec_from_file_location(
            'eh_hfs_migration_2_1_0', str(migration_path))
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        migration.migrate(self.env.cr, '19.0.2.0.6')

        item.invalidate_recordset()
        self.assertTrue(item.asset_reclassification_quarantined)
        self.assertIn(
            'Historical entries were not fabricated',
            item.asset_reclassification_quarantine_note,
        )
        group.invalidate_recordset()
        self.assertTrue(group.asset_reclassification_quarantined)
        self.assertIn(
            'Historical entries were not fabricated',
            group.asset_reclassification_quarantine_note,
        )

    def test_upgrade_retires_loose_group_alias(self):
        item = self._item(carrying=1000.0, fvlcts=1000.0)
        group = self.env['eh.disposal.group'].create({
            'name': 'Legacy alias group',
            'classification_date': '2026-01-31',
            'fair_value_less_costs': 1000.0,
            'asset_account_id': self.asset.id,
            'impairment_account_id': self.impairment.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [(0, 0, {
                'name': 'A', 'carrying_amount': 1000.0,
                'account_id': self.asset.id,
            })],
        })

        self.env.cr.execute(
            "ALTER TABLE eh_held_for_sale "
            "ADD COLUMN IF NOT EXISTS group_id INTEGER")
        self.env.cr.execute(
            "UPDATE eh_held_for_sale SET group_id = %s WHERE id = %s",
            (group.id, item.id),
        )
        migration_path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.2.1.1' / 'post-migration.py'
        )
        spec = importlib.util.spec_from_file_location(
            'eh_hfs_migration_2_1_1', str(migration_path))
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        migration.migrate(self.env.cr, '19.0.2.1.0')

        item.invalidate_recordset(['legacy_group_reference'])
        self.assertIn('Legacy alias group', item.legacy_group_reference)

    def test_cron_skips_paused_hfs_asset(self):
        # A held-for-sale asset is paused; the depreciation cron only posts
        # running assets, so no further depreciation line is posted for it.
        asset = self._running_asset(cost=36000.0)
        item = self._item(carrying=1.0, fvlcts=40000.0, asset_id=asset.id)
        item.action_classify()
        self.assertEqual(asset.state, 'paused')
        posted_before = len(
            asset.depreciation_line_ids.filtered(lambda l: l.is_posted))
        # Run the cron with a cut-off well past every scheduled line.
        self.env['eh.asset'].with_context(
            allowed_company_ids=self.company.ids
        )._cron_post_due()
        posted_after = len(
            asset.depreciation_line_ids.filtered(lambda l: l.is_posted))
        self.assertEqual(
            posted_after, posted_before,
            "The cron must not post depreciation for a paused held-for-sale "
            "asset (IFRS 5.25).")

    def test_cancel_resumes_paused_asset(self):
        # A cease-to-be-classified event resumes the asset this record
        # paused (IFRS 5.26).
        asset = self._running_asset(cost=36000.0)
        item = self._item(carrying=1.0, fvlcts=40000.0, asset_id=asset.id)
        item.action_classify()
        self.assertEqual(asset.state, 'paused')
        self.assertTrue(item.asset_paused_by_hfs)
        item.write({
            'declassification_adjusted_carrying_amount': item.carrying_amount,
            'declassification_recoverable_amount': item.carrying_amount + 1.0,
            'declassification_measurement_completed': True,
            'declassification_basis': 'No-HFS depreciation and recoverable amount review',
        })
        item.action_cancel()
        self.assertEqual(item.state, 'cancelled')
        self.assertEqual(asset.state, 'running')
        self.assertFalse(item.asset_paused_by_hfs)

    def test_cancel_requires_explicit_zero_capable_measurement_attestation(self):
        item = self._item(carrying=1000.0, fvlcts=800.0)
        item.action_classify()
        carrying_before = item.carrying_amount
        item.declassification_basis = 'Measurement workpaper retained'

        with self.assertRaisesRegex(UserError, 'Complete and attest'):
            item.action_cancel()

        self.assertEqual(item.state, 'held')
        self.assertEqual(item.carrying_amount, carrying_before)

    def test_cancel_posts_nonzero_ifrs5_27_adjustment(self):
        item = self._item(carrying=1000.0, fvlcts=800.0)
        item.action_classify()
        item.write({
            'declassification_date': '2026-06-30',
            'declassification_adjusted_carrying_amount': 950.0,
            'declassification_recoverable_amount': 900.0,
            'declassification_measurement_completed': True,
            'declassification_basis': 'Non-zero IFRS 5.27 workpaper HFS-27-NZ',
        })

        item.action_cancel()

        self.assertEqual(item.state, 'cancelled')
        self.assertAlmostEqual(item.carrying_amount, 900.0, places=2)
        move = item.declassification_move_id
        self.assertTrue(move)
        asset_line = move.line_ids.filtered(
            lambda line: line.account_id == self.asset)
        impairment_line = move.line_ids.filtered(
            lambda line: line.account_id == self.impairment)
        self.assertAlmostEqual(sum(asset_line.mapped('debit')), 100.0, 2)
        self.assertAlmostEqual(sum(impairment_line.mapped('credit')), 100.0, 2)

    def test_classify_writedown_reduces_asset_nbv_in_lockstep(self):
        # A linked-asset write-down must reduce the eh.asset net book value by
        # the same amount so the asset subledger and the held-for-sale carrying
        # amount agree (IFRS 5.15). Without routing the write-down through the
        # asset's impairment engine the asset NBV would stay at 35,000 while
        # the carrying amount fell to the FVLCTS, desyncing the two subledgers.
        asset = self._running_asset(cost=36000.0)
        self.assertAlmostEqual(asset.net_book_value, 35000.0, places=2)
        # FVLCTS below the seeded NBV of 35,000 forces a 5,000 write-down.
        item = self._item(carrying=1.0, fvlcts=30000.0, asset_id=asset.id)
        item.action_classify()
        self.assertEqual(item.state, 'held')
        self.assertAlmostEqual(item.carrying_amount, 30000.0, places=2)
        # The asset subledger fell by the same 5,000: NBV now equals the
        # held-for-sale carrying amount, the two subledgers reconcile.
        self.assertAlmostEqual(asset.net_book_value, 30000.0, places=2)
        self.assertAlmostEqual(
            asset.net_book_value, item.carrying_amount, places=2)
        # The write-down posted exactly one balanced impairment charge on the
        # asset, sharing the held-for-sale accounts.
        self.assertAlmostEqual(asset.accumulated_impairment, 5000.0, places=2)
        for move in item.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_remeasure_writedown_reduces_asset_nbv_in_lockstep(self):
        # A subsequent remeasurement write-down on a linked asset must also
        # flow through the asset's impairment engine so the subledgers do not
        # re-diverge after classification (IFRS 5.15).
        asset = self._running_asset(cost=36000.0)
        item = self._item(carrying=1.0, fvlcts=30000.0, asset_id=asset.id)
        item.action_classify()  # NBV and carrying both 30,000, impairment 5,000
        self.assertAlmostEqual(asset.net_book_value, 30000.0, places=2)
        item.fair_value_less_costs = 28000.0
        item.action_remeasure()
        self.assertAlmostEqual(item.carrying_amount, 28000.0, places=2)
        self.assertAlmostEqual(asset.net_book_value, 28000.0, places=2)
        self.assertAlmostEqual(
            asset.net_book_value, item.carrying_amount, places=2)
        self.assertAlmostEqual(asset.accumulated_impairment, 7000.0, places=2)

    def test_remeasure_reversal_restores_asset_nbv_in_lockstep(self):
        # A reversal on a linked asset restores the asset NBV by the same
        # amount, capped at the cumulative write-down (IFRS 5.22 aligned with
        # IAS 36.117), keeping the subledgers reconciled.
        asset = self._running_asset(cost=36000.0)
        item = self._item(carrying=1.0, fvlcts=30000.0, asset_id=asset.id)
        item.action_classify()  # NBV and carrying both 30,000, impairment 5,000
        item.fair_value_less_costs = 33000.0
        item.action_remeasure()
        self.assertAlmostEqual(item.carrying_amount, 33000.0, places=2)
        self.assertAlmostEqual(asset.net_book_value, 33000.0, places=2)
        self.assertAlmostEqual(
            asset.net_book_value, item.carrying_amount, places=2)
        # 5,000 charge partly reversed by 3,000 => 2,000 net impairment.
        self.assertAlmostEqual(asset.accumulated_impairment, 2000.0, places=2)

    def test_active_owner_blocks_second_claim_resume_and_depreciation(self):
        asset = self._running_asset(cost=36000.0)
        owner = self._item(
            carrying=1.0, fvlcts=40000.0, asset_id=asset.id)
        owner.action_classify()
        duplicate = self._item(
            carrying=1.0, fvlcts=40000.0, asset_id=asset.id)
        with self.assertRaises(UserError):
            duplicate.action_classify()
        group = self.env['eh.disposal.group'].create({
            'name': '/', 'fair_value_less_costs': 40000.0,
            'asset_account_id': self.asset.id,
            'impairment_account_id': self.impairment.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [(0, 0, {
                'name': 'Duplicate claim', 'asset_id': asset.id,
                'carrying_amount': 1.0, 'account_id': self.asset.id,
            })],
        })
        with self.assertRaises(UserError):
            group.action_classify()
        with self.assertRaises(UserError):
            asset.action_resume()
        next_line = asset.depreciation_line_ids.filtered(
            lambda line: not line.is_posted).sorted('depreciation_date')[:1]
        with self.assertRaises(UserError):
            next_line.action_post()

    def test_asset_cannot_cross_claim_hfs_and_investment_property(self):
        if 'eh.investment.property' not in self.env:
            self.skipTest('investment-property module not installed')
        asset = self._running_asset(cost=36000.0)
        item = self._item(
            carrying=1.0, fvlcts=40000.0, asset_id=asset.id)
        item.action_classify()
        prop = self.env['eh.investment.property'].create({
            'name': '/', 'model_basis': 'fair_value', 'initial_cost': 0.0,
            'property_account_id': self.asset.id,
            'fv_gain_loss_account_id': self.impairment.id,
            'journal_id': self.journal_misc.id,
            'transfer_in_asset_id': asset.id,
            'transfer_fair_value': item.carrying_amount,
        })
        with self.assertRaises(UserError):
            prop.action_transfer_in()
        item.write({
            'declassification_adjusted_carrying_amount': item.carrying_amount,
            'declassification_recoverable_amount': item.carrying_amount + 1.0,
            'declassification_measurement_completed': True,
            'declassification_basis': (
                'No-HFS depreciation and recoverable amount review'
            ),
        })
        item.action_cancel()
        prop.action_transfer_in()
        self.assertEqual(asset.state, 'disposed')
        duplicate = self._item(
            carrying=1.0, fvlcts=40000.0, asset_id=asset.id)
        with self.assertRaises(UserError):
            duplicate.action_classify()

    def test_hfs_owned_impairment_cannot_cancel_from_asset_register(self):
        asset = self._running_asset(cost=36000.0)
        item = self._item(
            carrying=1.0, fvlcts=30000.0, asset_id=asset.id)
        item.action_classify()
        impairment = asset.impairment_ids.filtered(
            lambda event: event.move_id.eh_held_for_sale_id == item)
        self.assertEqual(len(impairment), 1)
        with self.assertRaises(UserError):
            impairment.action_cancel()
        self.assertEqual(impairment.state, 'posted')
        self.assertEqual(impairment.move_id.state, 'posted')

    def test_low_value_pool_asset_cannot_be_claimed(self):
        asset = self._running_asset(cost=36000.0)
        pool = self.env['eh.asset.lvp.pool'].create({
            'name': 'HFS rejection pool',
            'company_id': self.company.id,
        })
        asset._eh_workflow_write({'lvp_pool_id': pool.id})
        item = self._item(
            carrying=1.0, fvlcts=30000.0, asset_id=asset.id)
        with self.assertRaises(UserError):
            item.action_classify()

    def test_remeasurement_and_sale_use_their_effective_dates(self):
        item = self._item(
            carrying=1000.0, fvlcts=800.0,
            classification_date='2026-01-31')
        item.action_classify()
        item.write({
            'fair_value_less_costs': 700.0,
            'remeasurement_date': '2026-02-28',
        })
        item.action_remeasure()
        item.write({'proceeds': 750.0, 'sale_date': '2026-03-31'})
        item.action_sell()
        self.assertEqual(
            item.move_ids.sorted('id').mapped('date'),
            [
                fields.Date.to_date('2026-01-31'),
                fields.Date.to_date('2026-02-28'),
                fields.Date.to_date('2026-03-31'),
            ],
        )

    # ---- posted-figure integrity controls (freeze / unlink / state gate) ----

    def _plain_user(self, login='hfs_ctrl_plain@test'):
        return self.env['res.users'].create({
            'name': 'ctrl-plain', 'login': login, 'email': login,
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})

    def test_posted_item_input_frozen_at_write(self):
        # A classified item's ledger-derived carrying amount is frozen at the
        # ORM write layer; a hand edit is blocked while it is held.
        item = self._item(carrying=1000.0, fvlcts=800.0)
        item.action_classify()
        self.assertEqual(item.state, 'held')
        with self.assertRaises(UserError):
            item.write({'carrying_amount': 500.0})
        self.assertAlmostEqual(item.carrying_amount, 800.0, places=2)

    def test_posted_item_cannot_be_unlinked(self):
        # A classified item carries a posted GL movement; deleting it would
        # orphan the move, so unlink is blocked even for a manager.
        item = self._item(carrying=1000.0, fvlcts=800.0)
        item.action_classify()
        with self.assertRaises(UserError):
            item.unlink()
        self.assertTrue(item.exists())
        # A draft item, with no GL movement, still deletes normally.
        draft = self._item(carrying=1000.0, fvlcts=800.0)
        draft.unlink()
        self.assertFalse(draft.exists())

    def test_plain_user_cannot_raw_reset_state(self):
        # A plain user cannot raw-reset a held item's state to draft to lift
        # the carrying-amount freeze; the state-reset gate manager-blocks it.
        item = self._item(carrying=1000.0, fvlcts=800.0)
        item.action_classify()
        user = self._plain_user()
        with self.assertRaises(UserError):
            item.with_user(user).write({'state': 'draft'})
        self.assertEqual(item.state, 'held')

    def test_sold_item_move_ondelete_restrict(self):
        # The posting move is ondelete='restrict', so the linked move cannot
        # be deleted out from under a posted item.
        item = self._item(carrying=1000.0, fvlcts=800.0)
        item.action_classify()
        move = item.move_ids[:1]
        self.assertTrue(move)
        with self.assertRaises(Exception):
            move.unlink()

    def test_normal_classify_sell_flow_still_works(self):
        # The sanctioned action flow is unaffected by the new state gate: a
        # manager classifies, then sells, and both raw state writes (which
        # move out of / between frozen states) pass via the context flag.
        item = self._item(carrying=1000.0, fvlcts=800.0)
        item.action_classify()
        self.assertEqual(item.state, 'held')
        item.proceeds = 900.0
        item.action_sell()
        self.assertEqual(item.state, 'sold')
        self.assertAlmostEqual(self._bal(self.gain_loss), -100.0, places=2)
        # Cancel from a fresh held item is also a sanctioned exit.
        other = self._item(carrying=1000.0, fvlcts=800.0)
        other.action_classify()
        other.write({
            'declassification_adjusted_carrying_amount': 1000.0,
            'declassification_recoverable_amount': 800.0,
            'declassification_measurement_completed': True,
            'declassification_basis': 'No-HFS adjusted carrying and recoverable amount review',
        })
        other.action_cancel()
        self.assertEqual(other.state, 'cancelled')

    def test_classify_no_asset_unchanged(self):
        # With no linked asset, behaviour is exactly as before: the
        # hand-keyed carrying amount stands and nothing is paused.
        item = self._item(carrying=1000.0, fvlcts=800.0)
        item.action_classify()
        self.assertFalse(item.asset_id)
        self.assertFalse(item.asset_paused_by_hfs)
        self.assertAlmostEqual(item.carrying_amount, 800.0, places=2)
