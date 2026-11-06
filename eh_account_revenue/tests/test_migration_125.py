# -*- encoding: utf-8 -*-
"""Remediation regressions for legacy revenue move links."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_revenue', 'post_install', '-at_install')
class TestRevenueMigration125(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.asset = cls._ensure_account(
            cls.env, '1359', 'Migration 125 Contract Asset', 'asset_current')
        cls.liability = cls._ensure_account(
            cls.env, '2359', 'Migration 125 Contract Liability',
            'liability_current')
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.2.5' / 'post-migration.py'
        )
        spec = spec_from_file_location('eh_revenue_post_migration_125', path)
        cls.migration = module_from_spec(spec)
        spec.loader.exec_module(cls.migration)

    def _recognised_contract(self):
        contract = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'revenue_account_id': self.account_revenue.id,
            'contract_asset_account_id': self.asset.id,
            'contract_liability_account_id': self.liability.id,
            'receivable_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'obligation_ids': [(0, 0, {
                'name': 'Delivered item',
                'standalone_price': 1000.0,
                'satisfied': True,
            })],
        })
        contract.action_activate()
        contract.action_recognise()
        return contract

    def test_indistinguishable_124_links_are_preserved_and_quarantined(self):
        contract = self._recognised_contract()
        move = contract.move_ids

        self.migration.migrate(self.env.cr, '19.0.1.2.4')
        contract.invalidate_recordset()
        move.invalidate_recordset()
        self.assertTrue(contract.eh_integrity_review_required)
        self.assertEqual(contract.move_ids, move)
        self.assertEqual(move.state, 'posted')
        self.assertAlmostEqual(contract.contract_asset, 1000.0, places=2)

    def test_foreign_legacy_link_cannot_feed_contract_position(self):
        contract = self._recognised_contract()
        move = contract.move_ids
        other = self.env['res.company'].create({
            'name': 'Foreign Legacy Revenue Company',
        })
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE account_move SET company_id = %s WHERE id = %s",
            (other.id, move.id),
        )
        move.invalidate_recordset()

        self.migration.migrate(self.env.cr, '19.0.1.2.4')
        contract.invalidate_recordset()
        move.invalidate_recordset()
        self.assertTrue(contract.eh_integrity_review_required)
        self.assertEqual(move.eh_revenue_contract_id, contract)
        self.assertAlmostEqual(contract.contract_asset, 0.0, places=2)
        self.assertAlmostEqual(contract.contract_liability, 0.0, places=2)
