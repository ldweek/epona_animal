# -*- encoding: utf-8 -*-
"""Upgrade regression for GL-derived contract positions."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_revenue', 'post_install', '-at_install')
class TestRevenueMigration124(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.asset = cls._ensure_account(
            cls.env, '1358', 'Migration Contract Asset', 'asset_current')
        cls.liability = cls._ensure_account(
            cls.env, '2358', 'Migration Contract Liability',
            'liability_current')
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.2.4' / 'post-migration.py'
        )
        spec = spec_from_file_location('eh_revenue_post_migration_124', path)
        cls.migration = module_from_spec(spec)
        spec.loader.exec_module(cls.migration)

    def test_terminal_legacy_position_is_rebuilt_and_quarantined(self):
        contract = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'revenue_account_id': self.account_revenue.id,
            'contract_asset_account_id': self.asset.id,
            'contract_liability_account_id': self.liability.id,
            'receivable_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'obligation_ids': [(0, 0, {
                'name': 'Delivered legacy item',
                'standalone_price': 1000.0,
                'satisfied': True,
            })],
        })
        contract.action_activate()
        contract.action_recognise()
        self.env.cr.execute(
            "UPDATE eh_revenue_contract SET state = 'done', "
            "contract_asset = 999999.0, contract_liability = 999999.0 "
            "WHERE id = %s",
            [contract.id],
        )

        self.migration.migrate(self.env.cr, '19.0.1.2.3')
        contract.invalidate_recordset()
        self.assertEqual(contract.state, 'done')
        self.assertAlmostEqual(contract.contract_asset, 1000.0, places=2)
        self.assertAlmostEqual(contract.contract_liability, 0.0, places=2)
        self.assertTrue(contract.eh_integrity_review_required)
        self.assertTrue(contract.eh_integrity_review_reason)

    def test_financial_anchor_without_linked_gl_is_quarantined(self):
        contract = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'contract_asset_account_id': self.asset.id,
            'contract_liability_account_id': self.liability.id,
            'obligation_ids': [(0, 0, {
                'name': 'Missing provenance item',
                'standalone_price': 1000.0,
            })],
        })
        self.env.cr.execute(
            "UPDATE eh_revenue_contract SET amount_billed = 250.0 "
            "WHERE id = %s",
            [contract.id],
        )

        self.migration.migrate(self.env.cr, '19.0.1.2.3')
        contract.invalidate_recordset()
        self.assertTrue(contract.eh_integrity_review_required)
        self.assertAlmostEqual(contract.amount_billed, 250.0, places=2)
        self.assertFalse(contract.move_ids)

    def test_active_legacy_link_is_quarantined_without_inferred_provenance(self):
        contract = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'revenue_account_id': self.account_revenue.id,
            'contract_asset_account_id': self.asset.id,
            'contract_liability_account_id': self.liability.id,
            'receivable_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'obligation_ids': [(0, 0, {
                'name': 'Legacy delivered item',
                'standalone_price': 1000.0,
                'satisfied': True,
            })],
        })
        contract.action_activate()
        contract.action_recognise()

        self.migration.migrate(self.env.cr, '19.0.1.2.3')
        contract.invalidate_recordset()
        self.assertTrue(contract.eh_integrity_review_required)
        self.assertIn('provenance', contract.eh_integrity_review_reason)
