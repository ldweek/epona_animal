# -*- encoding: utf-8 -*-
"""Fail-closed upgrade regressions for IAS 8 / IAS 10 evidence."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_events', 'post_install', '-at_install')
class TestEventsMigration125(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        root = Path(__file__).resolve().parents[1]
        cls.pre = cls._load_migration(
            root / 'migrations' / '19.0.1.2.5' / 'pre-migration.py',
            'eh_events_pre_migration_125_test',
        )
        cls.post = cls._load_migration(
            root / 'migrations' / '19.0.1.2.5' / 'post-migration.py',
            'eh_events_post_migration_125_test',
        )
        cls.post_129 = cls._load_migration(
            root / 'migrations' / '19.0.1.2.9' / 'post-migration.py',
            'eh_events_post_migration_129_test',
        )

    @staticmethod
    def _load_migration(path, module_name):
        spec = spec_from_file_location(module_name, path)
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _posted_change(self, amount=-1500.0):
        change = self.env['eh.accounting.change'].create({
            'change_type': 'error_correction',
            'change_date': '2026-06-30',
            'retained_earnings_account_id': self.account_equity.id,
            'adjustment_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [(0, 0, {
                'name': 'Legacy correction',
                'adjustment': amount,
            })],
        })
        change.action_post_restatement()
        return change

    def _draft_event(self):
        return self.env['eh.subsequent.event'].create({
            'name': 'Legacy adjusting event',
            'reporting_date': '2026-06-30',
            'event_date': '2026-07-02',
            'authorized_for_issue_date': '2026-08-01',
            'is_adjusting': True,
            'estimated_effect': 100.0,
        })

    def _posted_event(self):
        event = self._draft_event()
        event.write({
            'journal_id': self.journal_misc.id,
            'debit_account_id': self.account_expense.id,
            'credit_account_id': self.account_payable.id,
        })
        event.action_book_adjusting_entry()
        return event

    def test_post_quarantines_fully_self_consistent_unstamped_graph(self):
        change = self._posted_change()
        original = change.move_id
        change.action_reset_to_draft()
        reversal = self.env['account.move'].search([
            ('reversed_entry_id', '=', original.id),
        ], limit=1)
        self.assertTrue(reversal)
        self.env.flush_all()
        # The original, exact reversal, source links, signatures and seals all
        # remain self-consistent. Only the new unforgeable-at-upgrade stamp is
        # removed, emulating a pre-control graph.
        self.env.cr.execute(
            "UPDATE account_move SET eh_event_provenance_version = 0 "
            "WHERE id IN %s",
            (tuple((original | reversal).ids),),
        )
        self.post.migrate(self.env.cr, '19.0.1.2.4')
        change.invalidate_recordset([
            'legacy_provenance_quarantined',
            'legacy_provenance_quarantine_note',
        ])
        original.invalidate_recordset(['eh_accounting_change_id'])
        reversal.invalidate_recordset(['eh_accounting_change_id'])
        self.assertTrue(change.legacy_provenance_quarantined)
        self.assertFalse(original.eh_accounting_change_id)
        self.assertFalse(reversal.eh_accounting_change_id)
        with self.assertRaises(UserError):
            change.action_post_restatement()

    def test_post_preserves_current_stamped_reset_history(self):
        change = self._posted_change()
        original = change.move_id
        change.action_reset_to_draft()
        reversal = self.env['account.move'].search([
            ('reversed_entry_id', '=', original.id),
        ], limit=1)
        self.assertEqual(change.state, 'draft')
        self.assertFalse(change.move_id)
        self.assertEqual(
            set((original | reversal).mapped('eh_event_provenance_version')),
            {1},
        )
        self.env.flush_all()
        self.post.migrate(self.env.cr, '19.0.1.2.4')
        change.invalidate_recordset(['legacy_provenance_quarantined'])
        original.invalidate_recordset(['eh_accounting_change_id'])
        reversal.invalidate_recordset(['eh_accounting_change_id'])
        self.assertFalse(change.legacy_provenance_quarantined)
        self.assertEqual(original.eh_accounting_change_id, change)
        self.assertEqual(reversal.eh_accounting_change_id, change)

    def test_post_accepts_change_repost_with_exact_historical_pair(self):
        change = self._posted_change()
        change.action_reset_to_draft()
        change.line_ids.write({'adjustment': -900.0})
        change.action_post_restatement()
        self.env.flush_all()
        self.post.migrate(self.env.cr, '19.0.1.2.4')
        change.invalidate_recordset(['legacy_provenance_quarantined'])
        linked = self.env['account.move'].search([
            ('eh_accounting_change_id', '=', change.id),
        ])
        self.assertFalse(change.legacy_provenance_quarantined)
        self.assertEqual(len(linked), 3)

    def test_post_accepts_event_repost_with_exact_historical_pair(self):
        event = self._posted_event()
        event.action_reset_to_draft()
        event.write({'estimated_effect': 150.0})
        event.action_book_adjusting_entry()
        self.env.flush_all()
        self.post.migrate(self.env.cr, '19.0.1.2.4')
        event.invalidate_recordset(['legacy_provenance_quarantined'])
        linked = self.env['account.move'].search([
            ('eh_subsequent_event_id', '=', event.id),
        ])
        self.assertFalse(event.legacy_provenance_quarantined)
        self.assertEqual(len(linked), 3)

    def test_post_quarantines_stamped_primary_with_invalid_topology(self):
        change = self._posted_change()
        move = change.move_id
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE account_move SET date = %s WHERE id = %s",
            ('2026-07-01', move.id),
        )
        self.post.migrate(self.env.cr, '19.0.1.2.4')
        change.invalidate_recordset(['legacy_provenance_quarantined'])
        self.assertTrue(change.legacy_provenance_quarantined)

    def test_manager_neutralises_quarantined_accounting_change(self):
        change = self._posted_change()
        primary = change.move_id
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE account_move SET eh_event_provenance_version = 0 "
            "WHERE id = %s", (primary.id,),
        )
        primary.invalidate_recordset(['eh_event_provenance_version'])
        self.post.migrate(self.env.cr, '19.0.1.2.4')
        change.invalidate_recordset([
            'legacy_provenance_quarantined',
            'legacy_provenance_quarantine_note',
        ])
        primary.invalidate_recordset(['eh_accounting_change_id'])
        self.assertTrue(change.legacy_provenance_quarantined)
        self.assertFalse(primary.eh_accounting_change_id)
        change.write({
            'legacy_remediation_reference': 'AUDIT-IAS8-001',
            'legacy_remediation_date': change.change_date,
        })
        change.action_remediate_legacy_provenance()
        reversal = change.legacy_remediation_move_id
        self.assertEqual(change.state, 'reversed')
        self.assertFalse(change.legacy_provenance_quarantined)
        self.assertEqual(primary.eh_event_provenance_version, 0)
        self.assertFalse(primary.eh_accounting_change_id)
        self.assertEqual(reversal.reversed_entry_id, primary)
        self.assertEqual(reversal.eh_accounting_change_id, change)
        self.assertEqual(reversal.eh_event_provenance_version, 1)
        self.assertEqual(
            change._eh_move_signature(reversal),
            sorted(
                (account_id, credit, debit)
                for account_id, debit, credit
                in change._eh_move_signature(primary)
            ),
        )
        self.assertTrue(change.legacy_remediated_by_id)
        self.assertTrue(change.legacy_remediated_at)
        with self.assertRaises(UserError):
            change.write({'legacy_remediation_reference': 'REWRITE-REF'})
        self.env.flush_all()
        self.post.migrate(self.env.cr, '19.0.1.2.4')
        change.invalidate_recordset(['legacy_provenance_quarantined'])
        self.assertFalse(change.legacy_provenance_quarantined)

    def test_manager_neutralises_quarantined_subsequent_event(self):
        event = self._posted_event()
        primary = event.move_id
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE account_move SET eh_event_provenance_version = 0 "
            "WHERE id = %s", (primary.id,),
        )
        primary.invalidate_recordset(['eh_event_provenance_version'])
        self.post.migrate(self.env.cr, '19.0.1.2.4')
        event.invalidate_recordset([
            'legacy_provenance_quarantined',
            'legacy_provenance_quarantine_note',
        ])
        primary.invalidate_recordset(['eh_subsequent_event_id'])
        self.assertTrue(event.legacy_provenance_quarantined)
        self.assertFalse(primary.eh_subsequent_event_id)
        event.write({
            'legacy_remediation_reference': 'AUDIT-IAS10-001',
            'legacy_remediation_date': event.reporting_date,
        })
        event.action_remediate_legacy_provenance()
        reversal = event.legacy_remediation_move_id
        self.assertEqual(event.state, 'reversed')
        self.assertFalse(event.legacy_provenance_quarantined)
        self.assertEqual(primary.eh_event_provenance_version, 0)
        self.assertFalse(primary.eh_subsequent_event_id)
        self.assertEqual(reversal.reversed_entry_id, primary)
        self.assertEqual(reversal.eh_subsequent_event_id, event)
        self.assertEqual(reversal.eh_event_provenance_version, 1)
        self.assertTrue(event.legacy_remediated_by_id)
        self.assertTrue(event.legacy_remediated_at)

    def test_pre_quarantines_header_and_child_output_drift(self):
        change = self.env['eh.accounting.change'].create({
            'change_type': 'error_correction',
            'change_date': '2026-06-30',
            'line_ids': [(0, 0, {
                'name': 'Legacy child',
                'as_previously_reported': 100.0,
                'adjustment': 20.0,
            })],
        })
        line = change.line_ids
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_accounting_change_line SET as_restated = 999 "
            "WHERE id = %s",
            (line.id,),
        )
        self.pre.migrate(self.env.cr, '19.0.1.2.4')
        change.invalidate_recordset(['legacy_provenance_quarantined'])
        self.assertTrue(change.legacy_provenance_quarantined)

    def test_pre_quarantines_subsequent_event_output_drift(self):
        event = self._draft_event()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_subsequent_event SET treatment = 'disclose' "
            "WHERE id = %s",
            (event.id,),
        )
        self.pre.migrate(self.env.cr, '19.0.1.2.4')
        event.invalidate_recordset(['legacy_provenance_quarantined'])
        self.assertTrue(event.legacy_provenance_quarantined)

    def test_pre_migration_sql_is_adaptive_to_missing_legacy_columns(self):
        minimal_change = self.pre._change_audit_sql(
            {'company_id', 'currency_id'}, set(),
        )
        self.assertIn('change.currency_id', minimal_change)
        self.assertNotIn('change.application', minimal_change)
        self.assertNotIn('line.as_restated', minimal_change)
        minimal_event = self.pre._event_audit_sql(
            {'company_id', 'currency_id'},
        )
        self.assertIn('event.currency_id', minimal_event)
        self.assertNotIn('event.next_period', minimal_event)
        self.assertNotIn('event.treatment', minimal_event)
        self.assertIsNone(self.pre._change_audit_sql(set(), set()))
        self.assertIsNone(self.pre._event_audit_sql(set()))

    def test_129_backfills_legacy_date_reference_and_company_sequences(self):
        change = self.env['eh.accounting.change'].create({
            'change_type': 'error_correction',
            'change_date': '2024-06-30',
            'restatement_date': '2024-01-01',
        })
        event = self.env['eh.subsequent.event'].create({
            'name': 'Legacy unnumbered event',
            'reporting_date': '2024-06-30',
            'event_date': '2024-07-15',
        })
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_accounting_change SET restatement_date = %s "
            "WHERE id = %s",
            ('2026-01-01', change.id),
        )
        self.env.cr.execute(
            "UPDATE eh_subsequent_event SET reference = '/' WHERE id = %s",
            (event.id,),
        )
        self.post_129.migrate(self.env.cr, '19.0.1.2.8')
        change.invalidate_recordset(['restatement_date'])
        event.invalidate_recordset(['reference'])
        self.assertEqual(str(change.restatement_date), '2024-06-30')
        self.assertRegex(event.reference, r'^IAS10/2024/\d{4}$')
        for code in ('eh.accounting.change', 'eh.subsequent.event'):
            self.assertEqual(self.env['ir.sequence'].search_count([
                ('code', '=', code),
                ('company_id', '=', self.company.id),
            ]), 1)
