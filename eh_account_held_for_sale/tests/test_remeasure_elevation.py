# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression: subsequent group remeasurement must work for a REAL manager.

action_remeasure() stamps the linked asset's own eh.asset.impairment.state
through _eh_attach_asset_impairment. eh.asset.impairment guards ``state`` via
eh.workflow.guard, so the write only passes when the acting environment is
elevated (env.su True). action_classify / action_sell elevate with
``self = self._eh_workflow_action()``; action_remeasure previously did not,
so a real (non-superuser) EH Accounting Manager hit AccessError and the whole
IFRS 5.21-22 remeasurement rolled back for any asset-backed disposal group.

The test env normally runs as superuser, for which the guard is deliberately
inert, so the asset-linked remeasure must be driven through with_user() a
non-superuser manager to exercise the guard in both directions (further
write-down and reversal).
"""

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_account_held_for_sale', 'post_install', '-at_install')
class TestRemeasureElevation(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The env user is a superuser in tests; give it the manager group so
        # the fixture setup (classify) runs cleanly. The guard is exercised
        # separately through with_user(cls.manager) below.
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.imp = cls._ensure_account(
            cls.env, '5175', 'HFS Group Impairment', 'expense')
        cls.acc_a = cls._ensure_account(
            cls.env, '1791', 'HFS Member A', 'asset_current')
        cls.acc_fallback = cls._ensure_account(
            cls.env, '1789', 'Disposal Group Assets', 'asset_current')
        # Asset ledger accounts.
        cls.acc_fixed = cls._ensure_account(
            cls.env, '1500', 'Fixed Assets', 'asset_fixed')
        cls.acc_accum = cls._ensure_account(
            cls.env, '1510', 'Accumulated Depreciation', 'asset_fixed')
        cls.acc_dep = cls._ensure_account(
            cls.env, '5100', 'Depreciation Expense', 'expense_depreciation')
        # A real, non-superuser EH Accounting Manager. group_eh_manager
        # implies group_eh_user + account.group_account_manager, so this user
        # has every access right the remeasure path touches; only env.su is
        # False, which is exactly what makes the workflow guard live.
        cls.manager = cls.env['res.users'].create({
            'name': 'HFS Group Manager',
            'login': 'eh_hfs_group_manager',
            'email': 'eh_hfs_group_manager@example.com',
            'company_id': cls.company.id,
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_manager').id,
            ])],
        })

    def _running_asset(self, cost=36000.0):
        """A running fixed asset (straight line, 36 months, no proration)
        with its first depreciation line posted, so its ledger net book value
        is cost - cost / 36 (35,000 on a 36,000 cost)."""
        category = self.env['eh.asset.category'].search([
            ('code', '=', 'DGITHW'),
            ('company_id', '=', self.company.id)], limit=1)
        if not category:
            category = self.env['eh.asset.category'].create({
                'name': 'DG IT Hardware', 'code': 'DGITHW',
                'method': 'straight_line', 'useful_life_months': 36,
                'salvage_rate': 0.0, 'prorate_first_period': False,
                'asset_account_id': self.acc_fixed.id,
                'depreciation_account_id': self.acc_dep.id,
                'accumulated_depreciation_account_id': self.acc_accum.id,
                'journal_id': self.journal_misc.id,
            })
        asset = self.env['eh.asset'].create({
            'name': '/', 'category_id': category.id,
            'acquisition_date': '2026-01-01', 'in_service_date': '2026-01-31',
            'acquisition_cost': cost, 'salvage_value': 0.0,
            'method': 'straight_line', 'useful_life_months': 36,
            'prorate_first_period': False,
            'asset_account_id': self.acc_fixed.id,
            'depreciation_account_id': self.acc_dep.id,
            'accumulated_depreciation_account_id': self.acc_accum.id,
            'journal_id': self.journal_misc.id,
        })
        asset.action_activate()
        first = asset.depreciation_line_ids.sorted('depreciation_date')[0]
        first.action_post()
        return asset

    def _classified_asset_group(self):
        """A held disposal group whose sole member is a linked running asset
        (seeded NBV 35,000), classified with no write-down (FVLCTS = NBV)."""
        asset = self._running_asset(cost=36000.0)
        self.assertAlmostEqual(asset.net_book_value, 35000.0, places=2)
        group = self.env['eh.disposal.group'].create({
            'name': '/',
            'classification_date': '2026-01-31',
            'fair_value_less_costs': 35000.0,
            'asset_account_id': self.acc_fallback.id,
            'impairment_account_id': self.imp.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [(0, 0, {
                'name': 'A1', 'carrying_amount': 1.0,
                'asset_id': asset.id, 'account_id': self.acc_a.id,
            })],
        })
        group.action_classify()
        self.assertEqual(group.state, 'held')
        self.assertEqual(
            group.move_ids,
            group.asset_reclassification_move_id,
            'classification posts presentation only; no write-down',
        )
        self.assertAlmostEqual(group.carrying_amount, 35000.0, places=2)
        return group, asset

    def test_remeasure_writedown_asset_member_as_real_manager(self):
        """A further write-down on an asset-backed group must post for a
        non-superuser manager: the sanctioned action stamps the asset's
        eh.asset.impairment.state, which is guarded, so the action must run
        elevated. Before the elevation fix this raised AccessError and the
        whole entry rolled back."""
        group, asset = self._classified_asset_group()
        # FVLCTS falls to 30,000 -> a 5,000 further write-down.
        group.fair_value_less_costs = 30000.0
        group.with_user(self.manager).action_remeasure()
        self.assertEqual(group.state, 'held')
        line = group.line_ids
        self.assertAlmostEqual(line.carrying_amount, 30000.0, places=2)
        self.assertAlmostEqual(line.cumulative_writedown, 5000.0, places=2)
        self.assertAlmostEqual(group.carrying_amount, 30000.0, places=2)
        # The asset subledger moved in lockstep and the impairment posted.
        asset.invalidate_recordset()
        self.assertAlmostEqual(asset.accumulated_impairment, 5000.0, places=2)
        self.assertAlmostEqual(asset.net_book_value, 30000.0, places=2)
        self.assertEqual(len(asset.impairment_ids), 1)
        self.assertEqual(asset.impairment_ids.state, 'posted')

    def test_remeasure_reversal_asset_member_as_real_manager(self):
        """A subsequent reversal on an asset-backed group must also post for
        a non-superuser manager (the reversal branch of action_remeasure
        stamps the same guarded eh.asset.impairment.state)."""
        group, asset = self._classified_asset_group()
        # Establish a 5,000 write-down first (as the superuser env; the guard
        # is inert there, so this is not the path under test).
        group.fair_value_less_costs = 30000.0
        group.action_remeasure()
        self.assertAlmostEqual(group.line_ids.cumulative_writedown, 5000.0,
                               places=2)
        # Now FVLCTS recovers to 35,000: the manager reverses the write-down,
        # capped at the cumulative 5,000 (IFRS 5.22).
        group.fair_value_less_costs = 35000.0
        group.with_user(self.manager).action_remeasure()
        self.assertEqual(group.state, 'held')
        line = group.line_ids
        self.assertAlmostEqual(line.carrying_amount, 35000.0, places=2)
        self.assertAlmostEqual(line.cumulative_writedown, 0.0, places=2)
        self.assertAlmostEqual(group.carrying_amount, 35000.0, places=2)
        asset.invalidate_recordset()
        self.assertAlmostEqual(asset.accumulated_impairment, 0.0, places=2)
        self.assertAlmostEqual(asset.net_book_value, 35000.0, places=2)
        # Both the write-down and the reversal posted their own impairment
        # rows on the asset (one draft-then-posted per event).
        self.assertEqual(len(asset.impairment_ids), 2)
        self.assertTrue(all(
            i.state == 'posted' for i in asset.impairment_ids))
