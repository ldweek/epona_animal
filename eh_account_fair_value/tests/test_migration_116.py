# -*- encoding: utf-8 -*-
"""Upgrade regression for explicit fair-value derecognition dates."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_fair_value', 'post_install', '-at_install')
class TestFairValueMigration116(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.1.6' / 'post-migration.py'
        )
        spec = spec_from_file_location('eh_fair_value_migration_116', path)
        cls.migration = module_from_spec(spec)
        spec.loader.exec_module(cls.migration)

    def _item(self, name):
        return self.env['eh.fair.value.item'].create({
            'name': name,
            'measurement_date': '2026-04-30',
            'prior_carrying': 1000.0,
            'fair_value': 1000.0,
            'routing': 'pl',
        })

    def test_backfills_only_missing_closed_date_from_existing_evidence(self):
        legacy = self._item('Legacy derecognition')
        preserved = self._item('Existing explicit disposal date')
        open_item = self._item('Open item with anomalous result date')
        self.env.flush_all()
        self.env.cr.execute(
            """
            UPDATE eh_fair_value_item
               SET state = 'derecognised',
                   derecognition_date = '2026-04-30',
                   disposal_date = NULL
             WHERE id = %s
            """,
            (legacy.id,),
        )
        self.env.cr.execute(
            """
            UPDATE eh_fair_value_item
               SET state = 'derecognised',
                   derecognition_date = '2026-04-30',
                   disposal_date = '2026-04-29'
             WHERE id = %s
            """,
            (preserved.id,),
        )
        self.env.cr.execute(
            """
            UPDATE eh_fair_value_item
               SET state = 'measured',
                   derecognition_date = '2026-04-30',
                   disposal_date = NULL
             WHERE id = %s
            """,
            (open_item.id,),
        )

        self.migration.migrate(self.env.cr, '19.0.1.1.5')
        self.env.invalidate_all()

        self.assertEqual(
            legacy.disposal_date, fields.Date.to_date('2026-04-30'),
        )
        # Existing bytes are reviewer evidence, even if inconsistent: the
        # upgrade must not silently correct or overwrite them.
        self.assertEqual(
            preserved.disposal_date, fields.Date.to_date('2026-04-29'),
        )
        self.assertFalse(open_item.disposal_date)
        self.assertEqual(self.migration._backfill_disposal_dates(self.env.cr),
                         0)
