# -*- encoding: utf-8 -*-
"""Upgrade regressions for AP provenance quarantine scope and recovery."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from odoo.tests import tagged

from .common import EhApTestCase


@tagged('eh_account_ap_automation', 'post_install', '-at_install')
class TestApMigration145(EhApTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        migration_root = Path(__file__).parents[1] / 'migrations'
        cls.migration_142 = cls._load_migration(
            migration_root / '19.0.1.4.2' / 'pre-migration.py',
            'eh_ap_pre_migration_142',
        )
        cls.migration_143 = cls._load_migration(
            migration_root / '19.0.1.4.3' / 'post-migration.py',
            'eh_ap_post_migration_143',
        )
        cls.migration_145 = cls._load_migration(
            migration_root / '19.0.1.4.5' / 'post-migration.py',
            'eh_ap_post_migration_145',
        )
        cls.migration_148 = cls._load_migration(
            migration_root / '19.0.1.4.8' / 'post-migration.py',
            'eh_ap_post_migration_148',
        )

    @staticmethod
    def _load_migration(path, name):
        spec = spec_from_file_location(name, path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        return migration

    def _migrate(self, migration, version):
        self.env.flush_all()
        migration.migrate(self.env.cr, version)
        self.env.invalidate_all()

    def test_142_drops_legacy_many2one_fk_before_jsonb_conversion(self):
        self.env.cr.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_attribute "
            "WHERE attrelid = 'res_partner'::regclass "
            "AND attname = 'eh_ap_tolerance_profile_id' "
            "AND attnum > 0 AND NOT attisdropped)"
        )
        if not self.env.cr.fetchone()[0]:
            self.skipTest(
                'company-dependent partner values use ir.property on this '
                'core series; no legacy JSONB column can be migrated',
            )
        owned = self.env['res.partner'].create({
            'name': 'AP owned legacy vendor',
            'company_id': self.company.id,
        })
        shared = self.env['res.partner'].create({
            'name': 'AP shared legacy vendor',
            'company_id': False,
        })
        self.env.flush_all()
        self.env.cr.execute(
            "ALTER TABLE res_partner ALTER COLUMN "
            "eh_ap_tolerance_profile_id TYPE int4 USING NULL"
        )
        self.env.cr.execute(
            "ALTER TABLE res_partner ALTER COLUMN "
            "eh_ap_invoice_ref_regex TYPE varchar USING NULL"
        )
        self.env.cr.execute(
            "ALTER TABLE res_partner ALTER COLUMN "
            "eh_ap_total_regex TYPE varchar USING NULL"
        )
        legacy_fk = 'eh_ap_test_legacy_tolerance_profile_fkey'
        self.env.cr.execute(
            "ALTER TABLE res_partner ADD CONSTRAINT " + legacy_fk + " "
            "FOREIGN KEY (eh_ap_tolerance_profile_id) "
            "REFERENCES eh_ap_tolerance_profile(id)"
        )
        self.env.cr.execute(
            """
            UPDATE res_partner
               SET eh_ap_tolerance_profile_id = %s,
                   eh_ap_invoice_ref_regex = 'OWN-([0-9]+)',
                   eh_ap_total_regex = 'TOTAL: ([0-9.]+)'
             WHERE id = %s
            """,
            [self.default_profile.id, owned.id],
        )
        self.env.cr.execute(
            """
            UPDATE res_partner
               SET eh_ap_tolerance_profile_id = %s,
                   eh_ap_invoice_ref_regex = 'SHARED',
                   eh_ap_total_regex = 'SHARED-TOTAL'
             WHERE id = %s
            """,
            [self.default_profile.id, shared.id],
        )

        self._migrate(self.migration_142, '19.0.1.4.1')

        self.env.cr.execute(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid = 'res_partner'::regclass AND conname = %s",
            [legacy_fk],
        )
        self.assertEqual(self.env.cr.fetchone()[0], 0)
        self.env.cr.execute(
            "SELECT eh_ap_tolerance_profile_id, eh_ap_invoice_ref_regex, "
            "eh_ap_total_regex, eh_ap_legacy_config_quarantine "
            "FROM res_partner WHERE id = %s",
            [owned.id],
        )
        profile, invoice_regex, total_regex, quarantine = self.env.cr.fetchone()
        company_key = str(self.company.id)
        self.assertEqual(profile[company_key], self.default_profile.id)
        self.assertEqual(invoice_regex[company_key], 'OWN-([0-9]+)')
        self.assertEqual(total_regex[company_key], 'TOTAL: ([0-9.]+)')
        self.assertFalse(quarantine)
        self.env.cr.execute(
            "SELECT eh_ap_tolerance_profile_id, "
            "eh_ap_legacy_config_quarantine FROM res_partner WHERE id = %s",
            [shared.id],
        )
        shared_profile, shared_quarantine = self.env.cr.fetchone()
        self.assertIsNone(shared_profile)
        self.assertIn(
            'shared-partner-company-ambiguous', shared_quarantine,
        )

    def test_143_preserves_live_inbox_state_matrix(self):
        intakes = self.env['eh.ap.intake'].create([
            {'partner_id': self.partner_a.id}
            for _state in ('received', 'parsed', 'matched', 'exception',
                           'rejected')
        ])
        for intake, state in zip(
                intakes,
                ('received', 'parsed', 'matched', 'exception', 'rejected')):
            self.env.cr.execute(
                "UPDATE eh_ap_intake SET state = %s WHERE id = %s",
                (state, intake.id),
            )

        self._migrate(self.migration_143, '19.0.1.4.2')

        self.assertFalse(any(intakes.mapped('provenance_quarantined')))

    def test_143_quarantines_only_claimed_invalid_posting(self):
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
        })
        self.env.flush_all()
        self.env.cr.execute(
            """
            UPDATE eh_ap_intake
               SET state = 'posted',
                   posted_at = NOW(),
                   posted_by_id = %s
             WHERE id = %s
            """,
            (self.env.user.id, intake.id),
        )

        self._migrate(self.migration_143, '19.0.1.4.2')

        self.assertTrue(intake.provenance_quarantined)
        self.assertFalse(intake.move_id)

    def test_145_releases_only_false_inbox_quarantine(self):
        live, claimed = self.env['eh.ap.intake'].create([
            {'partner_id': self.partner_a.id},
            {'partner_id': self.partner_a.id},
        ])
        old_note = (
            '19.0.1.4.3 upgrade audit: the legacy journal-entry link could '
            'not be proven from mutual posted-link.'
        )
        self.env.flush_all()
        self.env.cr.execute(
            """
            UPDATE eh_ap_intake
               SET provenance_quarantined = TRUE,
                   provenance_quarantine_note = %s
             WHERE id IN %s
            """,
            (old_note, tuple((live | claimed).ids)),
        )
        self.env.cr.execute(
            """
            UPDATE eh_ap_intake
               SET state = 'posted',
                   posted_at = NOW(),
                   posted_by_id = %s
             WHERE id = %s
            """,
            (self.env.user.id, claimed.id),
        )

        self._migrate(self.migration_145, '19.0.1.4.4')

        self.assertFalse(live.provenance_quarantined)
        self.assertIn('false inbox quarantine',
                      live.provenance_quarantine_note)
        self.assertTrue(claimed.provenance_quarantined)
        self.assertEqual(claimed.provenance_quarantine_note, old_note)

    def test_148_preserves_legacy_reviewed_taxes_as_manual(self):
        tax_values = {
            'name': 'Legacy AP reviewed tax',
            'amount': 6.543,
            'amount_type': 'percent',
            'type_tax_use': 'purchase',
            'company_id': self.company.id,
        }
        if 'country_id' in self.env['account.tax']._fields:
            country = (
                getattr(self.company, 'account_fiscal_country_id', False)
                or self.company.country_id
                or self.env.ref('base.us')
            )
            tax_values['country_id'] = country.id
        tax = self.env['account.tax'].create(tax_values)
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
        })
        taxed, untaxed = self.env['eh.ap.intake.line'].create([
            {
                'intake_id': intake.id,
                'description': 'Legacy taxed line',
                'tax_ids': [(6, 0, tax.ids)],
            },
            {
                'intake_id': intake.id,
                'description': 'Legacy untaxed line',
            },
        ])
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_ap_intake_line SET tax_provenance = NULL "
            "WHERE id IN %s",
            [tuple((taxed | untaxed).ids)],
        )

        self._migrate(self.migration_148, '19.0.1.4.7')

        self.assertEqual(taxed.tax_provenance, 'manual')
        self.assertEqual(untaxed.tax_provenance, 'none')
