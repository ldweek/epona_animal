# -*- encoding: utf-8 -*-
"""Fail-closed upgrade regressions for recurring revenue bridge evidence."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_revenue_recurring', 'post_install', '-at_install')
class TestRevenueRecurringMigration103(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager'
        )
        cls.contract_asset = cls._ensure_account(
            cls.env, '1373', 'Bridge migration contract asset', 'asset_current',
        )
        cls.contract_liability = cls._ensure_account(
            cls.env, '2373', 'Bridge migration contract liability',
            'liability_current',
        )
        cls.sale_journal = cls.env['account.journal'].search([
            ('company_id', '=', cls.company.id),
            ('type', '=', 'sale'),
        ], limit=1)
        if not cls.sale_journal:
            cls.sale_journal = cls.env['account.journal'].create({
                'name': 'Bridge migration sales',
                'code': 'BMIG',
                'type': 'sale',
                'company_id': cls.company.id,
            })
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.0.3' / 'post-migration.py'
        )
        spec = spec_from_file_location(
            'eh_revenue_recurring_post_migration_103', path,
        )
        cls.migration = module_from_spec(spec)
        spec.loader.exec_module(cls.migration)

    def _contract(self):
        return self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1200.0,
            'revenue_account_id': self.account_revenue.id,
            'contract_asset_account_id': self.contract_asset.id,
            'contract_liability_account_id': self.contract_liability.id,
            'receivable_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'obligation_ids': [(0, 0, {
                'name': 'Legacy bridge service',
                'standalone_price': 1200.0,
                'satisfaction': 'over_time',
                'progress_method': 'output_milestones',
                'method_basis': 'Legacy monthly stand-ready service.',
            })],
        })

    def _template(self, code, contract):
        return self.env['eh.recurring.invoice.template'].create({
            'name': code,
            'code': code,
            'partner_id': self.partner_a.id,
            'journal_id': self.sale_journal.id,
            'start_date': fields.Date.from_string('2026-05-01'),
            'next_run_date': fields.Date.from_string('2026-05-01'),
            'auto_post': True,
            'revenue_contract_id': contract.id,
            'line_ids': [(0, 0, {
                'name': 'Legacy bridge billing',
                'account_id': self.account_revenue.id,
                'quantity': 1.0,
                'price_unit': 100.0,
            })],
        })

    def _migrate(self):
        self.env.flush_all()
        self.migration.migrate(self.env.cr, '19.0.1.0.2')
        self.env.invalidate_all()

    def test_valid_registered_bridge_shape_is_never_blessed(self):
        contract = self._contract()
        contract.action_activate()
        template = self._template('legacy_bridge_registered', contract)
        template.action_generate_now()
        move = template.last_generated_move_id
        billed_before = contract.amount_billed
        period_before = (
            move.eh_recurring_period_start,
            move.eh_recurring_period_end,
        )

        self._migrate()

        self.assertTrue(template.eh_legacy_integrity_unverified)
        self.assertTrue(contract.eh_integrity_review_required)
        self.assertEqual(contract.amount_billed, billed_before)
        self.assertEqual(template.revenue_contract_id, contract)
        self.assertEqual(template.last_generated_move_id, move)
        self.assertEqual(move.eh_revenue_contract_id, contract)
        self.assertTrue(move.eh_revenue_billing_registered)
        self.assertEqual(
            (move.eh_recurring_period_start, move.eh_recurring_period_end),
            period_before,
        )
        self.assertFalse(move.eh_sealed)
        self.assertTrue(move.eh_legacy_unverified_seal)
        self.assertTrue(move.eh_recurring_legacy_unverified)
        with self.assertRaisesRegex(UserError, 'quarantined'):
            template.action_generate_now()
        with self.assertRaises(UserError):
            contract.action_recognise()
        with self.assertRaises(UserError):
            move.invoice_line_ids.write({'price_unit': 1.0})

    def test_contract_link_without_move_is_preserved_and_quarantined(self):
        contract = self._contract()
        template = self._template('legacy_bridge_link_only', contract)
        self.assertFalse(template.generated_move_ids)

        self._migrate()

        self.assertTrue(template.eh_legacy_integrity_unverified)
        self.assertEqual(template.revenue_contract_id, contract)
        self.assertTrue(contract.eh_integrity_review_required)
        self.assertFalse(template.generated_move_ids)
        with self.assertRaisesRegex(UserError, 'quarantined'):
            template.write({'revenue_contract_id': False})

    def test_ordinary_pre_link_invoice_is_not_unsealed_or_frozen(self):
        contract = self._contract()
        template = self.env['eh.recurring.invoice.template'].create({
            'name': 'ordinary_before_bridge_link',
            'code': 'ordinary_before_bridge_link',
            'partner_id': self.partner_a.id,
            'journal_id': self.sale_journal.id,
            'start_date': fields.Date.from_string('2026-05-01'),
            'next_run_date': fields.Date.from_string('2026-05-01'),
            'auto_post': True,
            'line_ids': [(0, 0, {
                'name': 'Ordinary income billing',
                'account_id': self.account_revenue.id,
                'quantity': 1.0,
                'price_unit': 100.0,
            })],
        })
        template.action_generate_now()
        ordinary_move = template.last_generated_move_id
        self.assertTrue(ordinary_move.eh_sealed)
        self.assertFalse(ordinary_move.eh_revenue_contract_id)
        self.assertFalse(ordinary_move.eh_revenue_billing_registered)
        # Reproduce the legacy pre-freeze shape: the template was linked only
        # after this ordinary income invoice had already been generated.
        self.env.cr.execute(
            "UPDATE eh_recurring_invoice_template "
            "SET revenue_contract_id = %s WHERE id = %s",
            (contract.id, template.id),
        )
        template.invalidate_recordset(['revenue_contract_id'])

        self._migrate()

        self.assertTrue(template.eh_legacy_integrity_unverified)
        self.assertTrue(contract.eh_integrity_review_required)
        self.assertTrue(ordinary_move.eh_sealed)
        self.assertFalse(ordinary_move.eh_legacy_unverified_seal)
        self.assertFalse(ordinary_move.eh_recurring_legacy_unverified)

    def test_partial_dependency_schema_skips_foreign_contract_update(self):
        contract = self._contract()
        template = self._template('legacy_partial_dependency', contract)

        with patch.object(
            self.migration,
            '_table_has_columns',
            return_value=False,
        ):
            self._migrate()

        self.assertTrue(template.eh_legacy_integrity_unverified)
        self.assertFalse(contract.eh_integrity_review_required)
