# -*- encoding: utf-8 -*-
"""Upgrade regressions for IAS 23 provenance."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_borrowing_costs', 'post_install', '-at_install')
class TestBorrowingMigration103(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.asset = cls._ensure_account(
            cls.env, '1681', 'Migration Asset Under Construction',
            'asset_non_current')
        cls.interest = cls._ensure_account(
            cls.env, '5681', 'Migration Interest Expense', 'expense')
        proof_path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.0.3' / 'post-migration.py'
        )
        proof_spec = spec_from_file_location(
            'eh_borrowing_post_migration_103', proof_path,
        )
        cls.proof_migration = module_from_spec(proof_spec)
        proof_spec.loader.exec_module(cls.proof_migration)
        pre_path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.0.5' / 'pre-migration.py'
        )
        pre_spec = spec_from_file_location(
            'eh_borrowing_pre_migration_105', pre_path,
        )
        cls.pre_migration = module_from_spec(pre_spec)
        pre_spec.loader.exec_module(cls.pre_migration)
        post_path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.0.5' / 'post-migration.py'
        )
        post_spec = spec_from_file_location(
            'eh_borrowing_post_migration_105', post_path,
        )
        cls.post_migration = module_from_spec(post_spec)
        post_spec.loader.exec_module(cls.post_migration)
        rate_path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.0.7' / 'pre-migration.py'
        )
        rate_spec = spec_from_file_location(
            'eh_borrowing_pre_migration_107', rate_path,
        )
        cls.rate_migration = module_from_spec(rate_spec)
        rate_spec.loader.exec_module(cls.rate_migration)
        cap_path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.0.9' / 'pre-migration.py'
        )
        cap_spec = spec_from_file_location(
            'eh_borrowing_pre_migration_109', cap_path,
        )
        cls.cap_migration = module_from_spec(cap_spec)
        cap_spec.loader.exec_module(cls.cap_migration)

    def _migrate(self):
        self.pre_migration.migrate(self.env.cr, '19.0.1.0.4')
        self.post_migration.migrate(self.env.cr, '19.0.1.0.4')
        self.env.invalidate_all()

    def _capitalised(self):
        record = self.env['eh.borrowing.cost'].create({
            'qualifying_asset': 'Legacy plant',
            'period_end': '2026-12-31',
            'specific_borrowing_cost': 850.0,
            'asset_account_id': self.asset.id,
            'borrowing_cost_account_id': self.interest.id,
            'journal_id': self.journal_misc.id,
        })
        record.action_capitalise()
        return record

    def test_103_proof_first_backfill_links_only_exact_evidence(self):
        record = self._capitalised()
        move = record.move_id
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE account_move SET eh_borrowing_cost_id = NULL "
            "WHERE id = %s",
            (move.id,),
        )
        self.proof_migration.migrate(self.env.cr, '19.0.1.0.2')
        self.env.invalidate_all()
        self.assertEqual(move.eh_borrowing_cost_id, record)
        self.assertFalse(record.eh_integrity_review_required)

    def test_103_ambiguous_candidates_are_all_quarantined(self):
        first = self._capitalised()
        second = self._capitalised()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_borrowing_cost SET move_id = %s WHERE id = %s",
            (first.move_id.id, second.id),
        )
        self.proof_migration.migrate(self.env.cr, '19.0.1.0.2')
        self.env.invalidate_all()
        self.assertTrue(first.eh_integrity_review_required)
        self.assertTrue(second.eh_integrity_review_required)

    def test_109_preserves_cap_intent_and_reversal_date(self):
        capped = self.env['eh.borrowing.cost'].create({
            'qualifying_asset': 'Legacy capped project',
            'actual_borrowing_cost': 125.0,
        })
        uncapped = self.env['eh.borrowing.cost'].create({
            'qualifying_asset': 'Legacy uncapped project',
        })
        reversed_record = self._capitalised()
        reversed_record.reversal_date = reversed_record.move_id.date
        reversed_record.action_reverse()
        expected_date = reversed_record.reversal_move_id.date
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_borrowing_cost "
            "SET apply_actual_borrowing_cost_cap = FALSE"
        )
        self.env.cr.execute(
            "UPDATE eh_borrowing_cost SET posted_reversal_date = NULL "
            "WHERE id = %s",
            (reversed_record.id,),
        )
        self.cap_migration.migrate(self.env.cr, '19.0.1.0.8')
        self.env.invalidate_all()
        self.assertTrue(capped.apply_actual_borrowing_cost_cap)
        self.assertFalse(uncapped.apply_actual_borrowing_cost_cap)
        self.assertEqual(reversed_record.posted_reversal_date, expected_date)

    def test_exact_legacy_move_is_never_blessed_or_backfilled(self):
        record = self._capitalised()
        move = record.move_id
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE account_move SET eh_borrowing_cost_id = NULL "
            "WHERE id = %s",
            (move.id,),
        )
        move.invalidate_recordset()

        self._migrate()
        move.invalidate_recordset()
        record.invalidate_recordset()
        self.assertFalse(move.eh_borrowing_cost_id)
        self.assertEqual(record.move_id, move)
        self.assertTrue(record.eh_integrity_review_required)

    def test_manager_can_release_only_reproved_legacy_evidence(self):
        record = self._capitalised()
        move = record.move_id
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE account_move SET eh_borrowing_cost_id = NULL "
            "WHERE id = %s",
            (move.id,),
        )
        self._migrate()
        record.invalidate_recordset()
        move.invalidate_recordset()
        self.assertTrue(record.eh_integrity_review_required)
        self.assertFalse(move.eh_borrowing_cost_id)
        record.integrity_review_note = (
            "Compared source period, sealed entry, accounts, date and amount."
        )
        record.action_release_integrity_review()
        self.assertFalse(record.eh_integrity_review_required)
        self.assertEqual(record.integrity_reviewed_by_id, self.env.user)
        self.assertTrue(record.integrity_reviewed_at)
        self.assertEqual(move.eh_borrowing_cost_id, record)
        with self.assertRaises(UserError):
            record.integrity_review_note = "rewrite attestation"

    def test_unsealed_legacy_evidence_cannot_be_released(self):
        record = self._capitalised()
        move = record.move_id
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE account_move SET eh_borrowing_cost_id = NULL, "
            "eh_sealed = FALSE WHERE id = %s",
            (move.id,),
        )
        self._migrate()
        record.invalidate_recordset()
        move.invalidate_recordset()
        record.integrity_review_note = "Unsealed evidence reviewed."
        with self.assertRaises(UserError):
            record.action_release_integrity_review()
        self.assertTrue(record.eh_integrity_review_required)

    def test_rate_basis_migration_preserves_legacy_period_semantics(self):
        record = self.env['eh.borrowing.cost'].create({
            'qualifying_asset': 'Legacy period-rate project',
            'period_end': '2026-03-31',
            'general_expenditure': 100000.0,
            'capitalisation_rate': 3.0,
        })
        self.env.flush_all()
        self.env.cr.execute("""
            ALTER TABLE eh_borrowing_cost
            ALTER COLUMN capitalisation_rate_basis DROP NOT NULL
        """)
        self.env.cr.execute("""
            UPDATE eh_borrowing_cost
               SET capitalisation_rate_basis = NULL
             WHERE id = %s
        """, (record.id,))
        self.rate_migration.migrate(self.env.cr, '19.0.1.0.6')
        record.invalidate_recordset(['capitalisation_rate_basis'])
        self.assertEqual(record.capitalisation_rate_basis, 'period')

    def test_unsealed_legacy_move_is_preserved_but_quarantined(self):
        record = self._capitalised()
        move = record.move_id
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE account_move SET eh_borrowing_cost_id = NULL, "
            "eh_sealed = FALSE WHERE id = %s",
            (move.id,),
        )
        move.invalidate_recordset()

        self._migrate()
        move.invalidate_recordset()
        record.invalidate_recordset()
        self.assertFalse(move.eh_borrowing_cost_id)
        self.assertEqual(record.move_id, move)
        self.assertTrue(record.eh_integrity_review_required)

    def test_legacy_child_scope_mismatch_is_preserved_and_quarantined(self):
        record = self.env['eh.borrowing.cost'].create({
            'qualifying_asset': 'Legacy scoped plant',
            'expenditure_line_ids': [(0, 0, {
                'date': '2026-01-01', 'amount': 100.0,
            })],
        })
        line = record.expenditure_line_ids
        other = self.env['res.company'].create({
            'name': 'Legacy Borrowing Scope Company',
        })
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_borrowing_cost_line SET company_id = %s WHERE id = %s",
            (other.id, line.id),
        )
        self._migrate()
        record.invalidate_recordset()
        self.assertTrue(record.eh_integrity_review_required)
        self.env.cr.execute(
            "SELECT company_id FROM eh_borrowing_cost_line WHERE id = %s",
            (line.id,),
        )
        self.assertEqual(self.env.cr.fetchone(), (other.id,))
