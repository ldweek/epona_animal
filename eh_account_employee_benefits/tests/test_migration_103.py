# -*- encoding: utf-8 -*-
"""Migration regressions for employee-benefit provenance quarantine."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_account_employee_benefits', 'post_install', '-at_install')
class TestBenefitMigration104(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        root = Path(__file__).resolve().parents[1]
        cls.pre = cls._load(
            root / 'migrations' / '19.0.1.0.4' / 'pre-migration.py',
            'eh_benefit_migration_104_pre_test',
        )
        cls.post = cls._load(
            root / 'migrations' / '19.0.1.0.4' / 'post-migration.py',
            'eh_benefit_migration_104_post_test',
        )
        cls.expense = cls._ensure_account(
            cls.env, '6298', 'Benefit Migration Expense', 'expense')
        cls.payable = cls._ensure_account(
            cls.env, '2198', 'Benefit Migration Payable',
            'liability_current')

    @staticmethod
    def _load(path, name):
        spec = spec_from_file_location(name, path)
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _posted_dc(self):
        accrual = self.env['eh.benefit.dc.accrual'].create({
            'period_date': '2026-12-31',
            'amount': 1000.0,
            'expense_account_id': self.expense.id,
            'payable_account_id': self.payable.id,
            'journal_id': self.journal_misc.id,
        })
        accrual.action_post()
        return accrual

    def test_pre_migration_quarantines_forged_company_anchor(self):
        accrual = self.env['eh.benefit.dc.accrual'].create({
            'period_date': '2026-12-31', 'amount': 1.0,
        })
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_benefit_dc_accrual SET currency_id = NULL WHERE id = %s",
            (accrual.id,),
        )
        self.pre.migrate(self.env.cr, '19.0.1.0.2')
        accrual.invalidate_recordset([
            'provenance_quarantined', 'provenance_quarantine_note',
        ])
        self.assertTrue(accrual.provenance_quarantined)

    def test_pre_migration_accepts_schema_without_supersedes_column(self):
        """Published 1.0.0 schema must reach ORM setup without SQL error."""
        self.env.flush_all()
        self.env.cr.execute(
            "ALTER TABLE eh_benefit_valuation "
            "DROP COLUMN supersedes_id CASCADE"
        )
        self.pre.migrate(self.env.cr, '19.0.1.0.0')
        self.env.cr.execute(
            "SELECT EXISTS ("
            "SELECT 1 FROM pg_attribute "
            "WHERE attrelid = 'eh_benefit_valuation'::regclass "
            "AND attname = 'supersedes_id' "
            "AND attnum > 0 AND NOT attisdropped)"
        )
        self.assertTrue(self.env.cr.fetchone()[0])

    def test_pre_preserves_valid_correction_for_stamped_history_audit(self):
        plan = self.env['eh.benefit.plan'].create({
            'name': 'Legacy lineage plan',
        })
        original = self.env['eh.benefit.valuation'].create({
            'plan_id': plan.id,
            'period_end': '2026-12-31',
        })
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_benefit_valuation SET state = 'reversed' "
            "WHERE id = %s",
            (original.id,),
        )
        original.invalidate_recordset(['state'])
        correction = self.env['eh.benefit.valuation'].create({
            'plan_id': plan.id,
            'period_end': '2026-12-31',
        })
        self.assertEqual(correction.supersedes_id, original)
        self.env.flush_all()
        self.pre.migrate(self.env.cr, '19.0.1.0.3')
        original.invalidate_recordset(['provenance_quarantined'])
        correction.invalidate_recordset(['provenance_quarantined'])
        self.assertFalse(original.provenance_quarantined)
        self.assertFalse(correction.provenance_quarantined)
        # Anchor capture does not blanket-quarantine valid correction shape.
        # Stamped-history audit still quarantines unproved reversed evidence,
        # and posting its correction must follow that decision.
        self.post.migrate(self.env.cr, '19.0.1.0.3')
        original.invalidate_recordset(['provenance_quarantined'])
        self.assertTrue(original.provenance_quarantined)
        with self.assertRaises(UserError):
            correction.action_post()

    def test_post_migration_quarantines_reversal_without_exact_edge(self):
        accrual = self._posted_dc()
        accrual.action_reverse()
        reversal = accrual.move_ids - accrual.move_id
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE account_move SET reversed_entry_id = NULL WHERE id = %s",
            (reversal.id,),
        )
        reversal.invalidate_recordset(['reversed_entry_id'])
        self.post.migrate(self.env.cr, '19.0.1.0.2')
        accrual.invalidate_recordset([
            'provenance_quarantined', 'provenance_quarantine_note',
        ])
        self.assertTrue(accrual.provenance_quarantined)

    def test_post_quarantines_self_consistent_unstamped_forged_graph(self):
        accrual = self._posted_dc()
        accrual.action_reverse()
        evidence = accrual.move_ids
        self.assertEqual(len(evidence), 2)
        self.env.flush_all()
        # Keep the exact original/reversal edge, source pointers, signatures,
        # state and seals. Removing only the new stamp emulates the fully
        # self-consistent graph an attacker could forge before this control.
        self.env.cr.execute(
            "UPDATE account_move SET eh_benefit_provenance_version = 0 "
            "WHERE id IN %s",
            (tuple(evidence.ids),),
        )
        evidence.invalidate_recordset(['eh_benefit_provenance_version'])
        self.post.migrate(self.env.cr, '19.0.1.0.3')
        accrual.invalidate_recordset([
            'provenance_quarantined', 'provenance_quarantine_note',
        ])
        self.assertTrue(accrual.provenance_quarantined)

    def test_post_preserves_current_stamped_graph(self):
        accrual = self._posted_dc()
        accrual.action_reverse()
        self.env.flush_all()
        self.post.migrate(self.env.cr, '19.0.1.0.3')
        accrual.invalidate_recordset(['provenance_quarantined'])
        self.assertFalse(accrual.provenance_quarantined)

    def test_manager_neutralises_quarantined_legacy_evidence(self):
        accrual = self._posted_dc()
        primary = accrual.move_id
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE account_move SET eh_benefit_provenance_version = 0 "
            "WHERE id = %s", (primary.id,),
        )
        primary.invalidate_recordset(['eh_benefit_provenance_version'])
        self.post.migrate(self.env.cr, '19.0.1.0.3')
        accrual.invalidate_recordset([
            'provenance_quarantined', 'provenance_quarantine_note',
        ])
        self.assertTrue(accrual.provenance_quarantined)
        accrual.provenance_remediation_reference = 'AUDIT-EB-001'
        accrual.reversal_date = accrual.period_date
        accrual.action_remediate_provenance()
        self.assertEqual(accrual.state, 'reversed')
        self.assertFalse(accrual.provenance_quarantined)
        self.assertEqual(primary.eh_benefit_provenance_version, 0)
        reversal = accrual.move_ids - primary
        self.assertEqual(len(reversal), 1)
        self.assertEqual(reversal.reversed_entry_id, primary)
        self.assertEqual(reversal.eh_benefit_provenance_version, 1)
        self.assertTrue(accrual.provenance_remediated_by_id)
        self.assertTrue(accrual.provenance_remediated_at)
