# -*- encoding: utf-8 -*-
"""Fail-closed upgrade regressions for legacy revenue evidence."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_revenue', 'post_install', '-at_install')
class TestRevenueMigration126(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.asset = cls._ensure_account(
            cls.env, '1360', 'Migration 126 Contract Asset', 'asset_current')
        cls.liability = cls._ensure_account(
            cls.env, '2360', 'Migration 126 Contract Liability',
            'liability_current')
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.2.6' / 'pre-migration.py'
        )
        spec = spec_from_file_location('eh_revenue_pre_migration_126', path)
        cls.migration = module_from_spec(spec)
        spec.loader.exec_module(cls.migration)
        post_path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.2.7' / 'post-migration.py'
        )
        post_spec = spec_from_file_location(
            'eh_revenue_post_migration_127', post_path,
        )
        cls.post_migration = module_from_spec(post_spec)
        post_spec.loader.exec_module(cls.post_migration)

    def test_pre_migration_defers_when_old_schema_lacks_columns(self):
        class OldSchemaCursor:
            def __init__(self):
                self.calls = []

            def execute(self, query, params=None):
                self.calls.append((query, params))

            @staticmethod
            def fetchall():
                return []

        cursor = OldSchemaCursor()
        self.migration.migrate(cursor, '19.0.1.2.3')
        self.assertEqual(len(cursor.calls), 1)
        self.assertIn('information_schema.columns', cursor.calls[0][0])

    def test_post_migration_runs_deferred_audit_idempotently(self):
        contract = self._contract('deferred-post')
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_revenue_contract SET amount_billed = 250.0 "
            "WHERE id = %s",
            (contract.id,),
        )
        self.post_migration.migrate(self.env.cr, '19.0.1.2.6')
        self.post_migration.migrate(self.env.cr, '19.0.1.2.6')
        contract.invalidate_recordset()
        self.assertTrue(contract.eh_integrity_review_required)
        self.assertAlmostEqual(contract.amount_billed, 250.0, places=2)

    def _contract(self, suffix):
        return self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'revenue_account_id': self.account_revenue.id,
            'contract_asset_account_id': self.asset.id,
            'contract_liability_account_id': self.liability.id,
            'receivable_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'obligation_ids': [(0, 0, {
                'name': f'Obligation {suffix}',
                'standalone_price': 1000.0,
                'variable_consideration': True,
                'variable_estimate': 100.0,
                'variable_constraint': 80.0,
            })],
        })

    def test_anchor_only_contract_from_early_125_is_quarantined(self):
        contract = self._contract('anchor')
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_revenue_contract SET amount_billed = 250.0 "
            "WHERE id = %s",
            (contract.id,),
        )
        self.migration.migrate(self.env.cr, '19.0.1.2.5')
        contract.invalidate_recordset()
        self.assertTrue(contract.eh_integrity_review_required)
        self.assertAlmostEqual(contract.amount_billed, 250.0, places=2)

    def test_unproved_child_evidence_and_scope_are_preserved_quarantined(self):
        modified = self._contract('modification')
        reviewed = self._contract('review')
        frozen = self._contract('allocation')
        mismatched = self._contract('scope')
        review = self.env['eh.revenue.constraint.review'].create({
            'obligation_id': reviewed.obligation_ids.id,
            'review_date': '2026-06-30',
            'new_estimate': 120.0,
            'new_constraint': 90.0,
            'rationale': 'Legacy row',
        })
        other = self.env['res.company'].create({
            'name': 'Revenue Legacy Scope Company',
        })
        self.env.flush_all()
        self.env.cr.execute("""
            INSERT INTO eh_revenue_modification
                (contract_id, company_id, currency_id, method, description,
                 price_before, price_after, create_uid, write_uid,
                 create_date, write_date)
            VALUES (%s, %s, %s, 'prospective', 'Legacy raw evidence',
                    1000.0, 1200.0, %s, %s, NOW(), NOW())
        """, (
            modified.id, modified.company_id.id, modified.currency_id.id,
            self.env.uid, self.env.uid,
        ))
        self.env.cr.execute(
            "UPDATE eh_revenue_constraint_review SET state = 'applied' "
            "WHERE id = %s",
            (review.id,),
        )
        self.env.cr.execute(
            "UPDATE eh_revenue_obligation "
            "SET allocation_frozen = TRUE, frozen_allocation = 750.0 "
            "WHERE id = %s",
            (frozen.obligation_ids.id,),
        )
        self.env.cr.execute(
            "UPDATE eh_revenue_obligation SET company_id = %s WHERE id = %s",
            (other.id, mismatched.obligation_ids.id),
        )

        self.migration.migrate(self.env.cr, '19.0.1.2.5')
        contracts = modified | reviewed | frozen | mismatched
        contracts.invalidate_recordset()
        self.assertTrue(all(contracts.mapped('eh_integrity_review_required')))
        self.env.cr.execute(
            "SELECT count(*) FROM eh_revenue_modification "
            "WHERE contract_id = %s",
            (modified.id,),
        )
        self.assertEqual(self.env.cr.fetchone(), (1,))
        self.env.cr.execute(
            "SELECT state FROM eh_revenue_constraint_review WHERE id = %s",
            (review.id,),
        )
        self.assertEqual(self.env.cr.fetchone(), ('applied',))

    def test_child_recognition_anchor_cannot_hide_behind_zero_parent(self):
        contract = self._contract('child-anchor')
        obligation = contract.obligation_ids
        self.env.flush_all()
        # Both columns were client-writable before the hardening.  A forged
        # child cumulative anchor plus a zeroed parent aggregate must not
        # survive merely because the header looks clean.
        self.env.cr.execute(
            "UPDATE eh_revenue_obligation SET recognised_amount = 100.0 "
            "WHERE id = %s",
            (obligation.id,),
        )
        self.env.cr.execute(
            "UPDATE eh_revenue_contract SET amount_recognised = 0.0 "
            "WHERE id = %s",
            (contract.id,),
        )
        self.migration.migrate(self.env.cr, '19.0.1.2.5')
        contract.invalidate_recordset()
        obligation.invalidate_recordset()
        self.assertTrue(contract.eh_integrity_review_required)
        self.assertAlmostEqual(obligation.recognised_amount, 100.0, places=2)
