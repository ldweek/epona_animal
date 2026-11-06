# -*- encoding: utf-8 -*-
"""Executable rehearsal for the 1.6.5 computed-run quarantine."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_fx_revaluation', 'post_install', '-at_install')
class TestFxMigration165(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.gain = cls._ensure_account(
            cls.env, '4925', 'Migration 165 FX Gain', 'income_other',
        )
        cls.loss = cls._ensure_account(
            cls.env, '5935', 'Migration 165 FX Loss', 'expense',
        )
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.6.5' / 'post-migration.py'
        )
        spec = spec_from_file_location('eh_fx_post_migration_165', path)
        cls.migration = module_from_spec(spec)
        spec.loader.exec_module(cls.migration)

    def _legacy_computed_run(self, date):
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': date,
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain.id,
            'loss_account_id': self.loss.id,
            'auto_reverse': False,
        })
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_fx_revaluation_run "
            "SET state = 'computed', source_move_version = 0 WHERE id = %s",
            (run.id,),
        )
        run.invalidate_recordset()
        return run

    def test_clean_database_version_none_is_a_noop(self):
        run = self._legacy_computed_run('2026-10-31')
        with patch.object(self.migration, 'migrate_fx_provenance') as validator:
            self.migration.migrate(self.env.cr, None)
        run.invalidate_recordset(['source_move_version'])
        self.assertFalse(validator.called)
        self.assertEqual(run.source_move_version, 0)

    def test_old_computed_snapshot_is_quarantined(self):
        run = self._legacy_computed_run('2026-11-30')
        with patch.object(self.migration, 'migrate_fx_provenance') as validator:
            self.migration.migrate(self.env.cr, '19.0.1.6.4')
        run.invalidate_recordset(['source_move_version'])
        validator.assert_called_once_with(self.env.cr)
        self.assertEqual(run.source_move_version, -1)
