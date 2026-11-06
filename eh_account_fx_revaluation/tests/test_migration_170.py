# -*- encoding: utf-8 -*-
"""Executable rehearsal for the 1.7.0 measurement/date backfill."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_fx_revaluation', 'post_install', '-at_install')
class TestFxMigration170(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.oci = cls._ensure_account(
            cls.env, '3197', 'Migration 170 OCI', 'equity',
        )
        cls.instrument = cls._ensure_account(
            cls.env, '1197', 'Migration 170 instrument', 'asset_current',
        )
        cls.gain = cls._ensure_account(
            cls.env, '4927', 'Migration 170 gain', 'income_other',
        )
        cls.loss = cls._ensure_account(
            cls.env, '5937', 'Migration 170 loss', 'expense',
        )
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.7.0' / 'post-migration.py'
        )
        spec = spec_from_file_location('eh_fx_post_migration_170', path)
        cls.migration = module_from_spec(spec)
        spec.loader.exec_module(cls.migration)

    def _legacy_records(self):
        hedge = self.env['eh.fx.hedge'].create({
            'name': 'Migration 170 hedge',
            'hedge_type': 'cash_flow',
            'designation_date': '2026-11-01',
            'hedged_item_description': 'Legacy forecast sale',
            'hedging_instrument_description': 'Legacy forward',
            'hedged_currency_id': self.company.currency_id.id,
            'oci_account_id': self.oci.id,
            'pl_account_id': self.gain.id,
            'instrument_account_id': self.instrument.id,
            'journal_id': self.journal_misc.id,
        })
        movement = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': hedge.id,
            'movement_date': '2026-11-30',
            'total_change': 40.0,
            'effective_portion': 30.0,
            'hedged_item_change': -30.0,
        })
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-11-30',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain.id,
            'loss_account_id': self.loss.id,
            'auto_reverse': False,
        })
        position = self.env['eh.fx.cta.position'].create({
            'name': 'Migration 170 CTA',
            'operation_type': 'subsidiary',
            'cta_account_id': self.oci.id,
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain.id,
            'loss_account_id': self.loss.id,
            'disposal_pct': 50.0,
            'disposal_date': '2026-12-01',
        })
        linked_move = self.env['account.move'].create({
            'move_type': 'entry',
            'company_id': self.company.id,
            'journal_id': self.journal_misc.id,
            'date': '2026-12-15',
            'ref': 'Migration 170 historical link',
        })
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_fx_hedge_movement "
            "SET hedged_item_change = -7.0, ineffective_portion = 99.0, "
            "reclassification_date = '2026-12-31' WHERE id = %s",
            (movement.id,),
        )
        self.env.cr.execute(
            "UPDATE eh_fx_revaluation_run SET reversal_move_id = %s, "
            "reversal_date = '2026-12-01' WHERE id = %s",
            (linked_move.id, run.id),
        )
        self.env.cr.execute(
            "UPDATE eh_fx_cta_position SET disposal_move_id = %s "
            "WHERE id = %s",
            (linked_move.id, position.id),
        )
        self.env.invalidate_all()
        return movement, run, position

    def test_upgrade_backfills_exact_measurements_and_linked_dates(self):
        movement, run, position = self._legacy_records()

        with patch.object(self.migration, 'migrate_fx_provenance') as validator:
            self.migration.migrate(self.env.cr, '19.0.1.6.9')

        self.env.invalidate_all()
        validator.assert_called_once_with(self.env.cr)
        self.assertEqual(movement.hedged_item_change, -30.0)
        self.assertEqual(movement.ineffective_portion, 10.0)
        self.assertEqual(str(movement.reclassification_date), '2026-11-30')
        self.assertEqual(str(run.reversal_date), '2026-12-15')
        self.assertEqual(position.operation_type, 'associate_joint_venture')
        self.assertEqual(str(position.disposal_date), '2026-12-15')

    def test_clean_install_version_none_is_a_noop(self):
        movement, run, position = self._legacy_records()

        with patch.object(self.migration, 'migrate_fx_provenance') as validator:
            self.migration.migrate(self.env.cr, None)

        self.env.invalidate_all()
        self.assertFalse(validator.called)
        self.assertEqual(movement.hedged_item_change, -7.0)
        self.assertEqual(str(run.reversal_date), '2026-12-01')
        self.assertEqual(position.operation_type, 'subsidiary')
