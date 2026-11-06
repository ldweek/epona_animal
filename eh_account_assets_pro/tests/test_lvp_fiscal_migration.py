# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Upgrade regression for the LVP calendar-year allocation defect."""

import importlib.util
import json
from pathlib import Path

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestLvpFiscalMigration(EhAssetTestCase):

    def setUp(self):
        super().setUp()
        self.company.sudo().write({
            'fiscalyear_last_day': 30,
            'fiscalyear_last_month': '6',
        })
        self.account_pool = self._ensure_account(
            self.env, '1541', 'Migrated Low-Value Pool', 'asset_fixed',
        )
        self.pool = self.env['eh.asset.lvp.pool'].create({
            'name': 'Migrated LVP',
            'company_id': self.company.id,
            'threshold': 1000.0,
            'pool_account_id': self.account_pool.id,
            'accumulated_account_id': self.account_accum_dep.id,
            'expense_account_id': self.account_dep_expense.id,
            'journal_id': self.journal_misc.id,
        })

    @staticmethod
    def _migration_module():
        import odoo.addons.eh_account_assets_pro as assets_module

        path = (
            Path(assets_module.__file__).resolve().parent
            / 'migrations' / '19.0.1.5.7' / 'post-migration.py'
        )
        spec = importlib.util.spec_from_file_location(
            'eh_assets_lvp_fiscal_post_migration', str(path),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _run_real_migration(self):
        self.env.flush_all()
        migration = self._migration_module()
        migration.migrate(self.env.cr, '19.0.1.5.6')
        self.env.invalidate_all()
        return migration

    def _transfer_fy2026_asset(self, code):
        asset = self._make_asset(
            code=code,
            acquisition_cost=900.0,
            in_service_date='2025-09-15',
        )
        self.pool.action_transfer_asset(asset, transfer_date='2025-09-15')
        self.assertTrue(asset.lvp_allocation_date)
        self.assertTrue(asset.lvp_opening_value)
        self.assertTrue(asset.lvp_transferred_at)
        self.assertTrue(asset.lvp_transferred_by_id)
        return asset

    def test_unposted_legacy_row_is_quarantined_then_safely_recomputed(self):
        self._transfer_fy2026_asset('LV-MIGRATE-DRAFT')
        # Legacy calendar-year logic treated the September 2025 allocation as
        # an FY2026 opening balance and charged the 37.5% subsequent rate.
        line = self.env['eh.asset.lvp.pool.line'].sudo().create({
            'pool_id': self.pool.id,
            'year': 2026,
            'opening_balance': 900.0,
            'additions': 0.0,
            'amount': 337.5,
            'first_year_rate': 18.75,
            'subsequent_year_rate': 37.5,
            'pool_account_id': self.account_pool.id,
            'accumulated_account_id': self.account_accum_dep.id,
            'expense_account_id': self.account_dep_expense.id,
            'journal_id': self.journal_misc.id,
        })

        migration = self._run_real_migration()
        line.invalidate_recordset()
        snapshot = line.allocation_basis_original_values
        original = json.loads(snapshot)
        self.assertTrue(line.allocation_basis_quarantined)
        self.assertEqual(original['opening_balance'], 900.0)
        self.assertEqual(original['additions'], 0.0)
        self.assertEqual(original['amount'], 337.5)
        self.assertAlmostEqual(line.opening_balance, 900.0, places=2)
        self.assertAlmostEqual(line.additions, 0.0, places=2)
        self.assertAlmostEqual(line.amount, 337.5, places=2)
        self.assertFalse(line.move_id)
        with self.assertRaisesRegex(UserError, 'calendar-year allocation'):
            line.action_post()

        # Retrying the migration neither replaces the snapshot nor re-mutates
        # the row. This is the idempotency guarantee needed by failed upgrades.
        self.assertEqual(
            migration._quarantine_legacy_lvp_lines(self.env.cr), 0,
        )
        self.env.invalidate_all()
        self.assertEqual(line.allocation_basis_original_values, snapshot)

        line.action_recompute_allocation_basis()
        line.invalidate_recordset()
        self.assertFalse(line.allocation_basis_quarantined)
        self.assertEqual(line.allocation_basis_original_values, snapshot)
        self.assertAlmostEqual(line.opening_balance, 0.0, places=2)
        self.assertAlmostEqual(line.additions, 900.0, places=2)
        self.assertAlmostEqual(line.amount, 168.75, places=2)
        self.assertEqual(line.allocation_basis_reviewed_by_id, self.env.user)
        self.assertTrue(line.allocation_basis_reviewed_at)

        # A later helper retry must not re-quarantine a reviewed line because
        # its immutable legacy snapshot remains present.
        self.env.flush_all()
        self.assertEqual(
            migration._quarantine_legacy_lvp_lines(self.env.cr), 0,
        )
        self.env.invalidate_all()
        self.assertFalse(line.allocation_basis_quarantined)
        line.action_post()
        self.assertTrue(line.is_posted)
        self.assertEqual(line.move_id.state, 'posted')
        self.assertEqual(
            line.move_id.date, fields.Date.to_date('2026-06-30'),
        )

    def test_posted_legacy_row_review_never_rewrites_financial_evidence(self):
        self._transfer_fy2026_asset('LV-MIGRATE-POSTED')
        line = self.pool.action_compute_year(year=2026)
        line.action_post()
        move = line.move_id
        row_values = (
            line.year,
            line.opening_balance,
            line.additions,
            line.amount,
            line.first_year_rate,
            line.subsequent_year_rate,
            line.pool_account_id.id,
            line.accumulated_account_id.id,
            line.expense_account_id.id,
            line.journal_id.id,
            line.is_posted,
            line.move_id.id,
        )
        journal_values = (
            move.state,
            move.date,
            tuple(
                (entry.account_id.id, entry.debit, entry.credit)
                for entry in move.line_ids.sorted('id')
            ),
        )

        self._run_real_migration()
        line.invalidate_recordset()
        self.assertTrue(line.allocation_basis_quarantined)
        snapshot = line.allocation_basis_original_values
        with self.assertRaisesRegex(UserError, 'ledger evidence'):
            line.action_recompute_allocation_basis()

        line.action_acknowledge_allocation_basis_review()
        line.invalidate_recordset()
        move.invalidate_recordset()
        self.assertTrue(line.allocation_basis_quarantined)
        self.assertEqual(line.allocation_basis_original_values, snapshot)
        self.assertEqual(line.allocation_basis_reviewed_by_id, self.env.user)
        self.assertTrue(line.allocation_basis_reviewed_at)
        self.assertEqual(row_values, (
            line.year,
            line.opening_balance,
            line.additions,
            line.amount,
            line.first_year_rate,
            line.subsequent_year_rate,
            line.pool_account_id.id,
            line.accumulated_account_id.id,
            line.expense_account_id.id,
            line.journal_id.id,
            line.is_posted,
            line.move_id.id,
        ))
        self.assertEqual(journal_values, (
            move.state,
            move.date,
            tuple(
                (entry.account_id.id, entry.debit, entry.credit)
                for entry in move.line_ids.sorted('id')
            ),
        ))

    def test_incomplete_source_evidence_stays_quarantined(self):
        asset = self._transfer_fy2026_asset('LV-MIGRATE-UNPROVED')
        line = self.pool.action_compute_year(year=2026)
        # Simulate a pre-provenance legacy member. The remediation must not
        # fall back to its live NBV or guess the missing transfer evidence.
        asset.sudo().write({
            'lvp_opening_value': 0.0,
            'lvp_transferred_at': False,
            'lvp_transferred_by_id': False,
        })
        self._run_real_migration()
        line.invalidate_recordset()
        original = (
            line.opening_balance, line.additions, line.amount,
            line.allocation_basis_original_values,
        )

        with self.assertRaisesRegex(UserError, 'source provenance'):
            line.action_recompute_allocation_basis()

        line.invalidate_recordset()
        self.assertTrue(line.allocation_basis_quarantined)
        self.assertEqual(original, (
            line.opening_balance, line.additions, line.amount,
            line.allocation_basis_original_values,
        ))
        self.assertFalse(line.move_id)
