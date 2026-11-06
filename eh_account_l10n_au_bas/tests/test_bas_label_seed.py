# -*- encoding: utf-8 -*-
##############################################################################
# ERP Heritage  -  Copyright (C) 2026 (https://www.erpheritage.com.au/)
##############################################################################
"""
The seed data ships the canonical BAS label set. Verify the rows landed.
"""

import importlib.util
import json
from pathlib import Path

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


class BasLabelSeedTest(EhAccountIntegrationTestCase):

    def test_g1_through_g3_present(self):
        for code in ('G1', 'G2', 'G3'):
            self.assertTrue(
                self.env['eh.bas.label'].search_count([('code', '=', code)]),
                "BAS label %s missing from seed" % code,
            )

    def test_gst_collected_paid_present(self):
        self.assertTrue(
            self.env['eh.bas.label'].search_count([('code', '=', '1A')]),
            "BAS label 1A (GST collected) missing",
        )
        self.assertTrue(
            self.env['eh.bas.label'].search_count([('code', '=', '1B')]),
            "BAS label 1B (GST paid) missing",
        )

    def test_payg_w1_w2_present(self):
        for code in ('W1', 'W2'):
            self.assertTrue(
                self.env['eh.bas.label'].search_count([('code', '=', code)]),
                "BAS label %s missing" % code,
            )

    def test_unique_codes(self):
        all_codes = self.env['eh.bas.label'].search([]).mapped('code')
        self.assertEqual(
            len(all_codes), len(set(all_codes)),
            "Duplicate BAS label codes in the seed data: %r" % (all_codes,),
        )

    def test_aggregation_values_are_valid(self):
        valid = {'manual', 'base_credit', 'base_debit',
                 'tax_credit', 'tax_debit',
                 'gst_on_sales', 'gst_on_purchases', 'total_sales_incl',
                 'payg_withheld_total'}
        for label in self.env['eh.bas.label'].search([]):
            self.assertIn(
                label.aggregation, valid,
                "Label %s has invalid aggregation %s" % (
                    label.code, label.aggregation,
                ),
            )

    def test_base_and_tax_modes_are_distinct_and_refund_aware(self):
        Run = self.env['eh.bas.run']
        self.assertEqual(
            Run._aggregation_to_sql('base_credit'),
            ('aml.tax_line_id IS NULL', '-SUM(aml.balance)'),
        )
        self.assertEqual(
            Run._aggregation_to_sql('base_debit'),
            ('aml.tax_line_id IS NULL', 'SUM(aml.balance)'),
        )
        self.assertEqual(
            Run._aggregation_to_sql('tax_credit'),
            ('aml.tax_line_id IS NOT NULL', '-SUM(aml.balance)'),
        )
        self.assertEqual(
            Run._aggregation_to_sql('tax_debit'),
            ('aml.tax_line_id IS NOT NULL', 'SUM(aml.balance)'),
        )

    def test_w5_is_a_computed_definition(self):
        w5 = self.env['eh.bas.label'].search([('code', '=', 'W5')], limit=1)
        self.assertEqual(w5.aggregation, 'payg_withheld_total')

    def test_run_help_describes_current_full_worksheet(self):
        help_text = self.env['eh.bas.run']._fields['line_ids'].help
        self.assertIn('33-label', help_text)
        self.assertNotIn('G1-G20', help_text)

    def test_120_migration_preserves_report_translations_idempotently(self):
        report = self.env.ref('eh_account_l10n_au_bas.report_au_bas')
        translations = {
            'en_US': 'Legacy BAS description',
            'fr_FR': 'Description BAS conservee',
        }
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_account_dynamic_report "
            "SET description = %s::jsonb WHERE id = %s",
            (json.dumps(translations), report.id),
        )

        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.2.0' / 'post-migration.py'
        )
        spec = importlib.util.spec_from_file_location(
            'eh_au_bas_post_migration_120', path,
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        migration.migrate(self.env.cr, '19.0.1.1.8')
        migration.migrate(self.env.cr, '19.0.1.1.8')
        self.env.cr.execute(
            "SELECT description::text FROM eh_account_dynamic_report "
            "WHERE id = %s",
            (report.id,),
        )
        migrated = json.loads(self.env.cr.fetchone()[0])

        self.assertEqual(
            migrated['en_US'], migration._BAS_REPORT_DESCRIPTION,
        )
        self.assertEqual(migrated['fr_FR'], translations['fr_FR'])
        self.assertEqual(set(migrated), {'en_US', 'fr_FR'})
