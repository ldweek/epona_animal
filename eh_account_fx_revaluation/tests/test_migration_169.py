# -*- encoding: utf-8 -*-
"""Executable rehearsal for the corrected-run uniqueness migration."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from psycopg2 import IntegrityError

from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger


@tagged('eh_account_fx_revaluation', 'post_install', '-at_install')
class TestFxMigration169(TransactionCase):

    def test_migration_replaces_date_wide_constraint_with_live_index(self):
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.6.9' / 'pre-migration.py'
        )
        spec = spec_from_file_location('eh_fx_pre_migration_169', path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        schema = 'eh_fx_169_%d' % self.env.company.id

        class RollbackProbe(Exception):
            pass

        try:
            with self.env.cr.savepoint():
                self.env.cr.execute('CREATE SCHEMA %s' % schema)
                self.env.cr.execute('SET LOCAL search_path TO %s' % schema)
                self.env.cr.execute(
                    "CREATE TABLE eh_fx_revaluation_run ("
                    "id integer PRIMARY KEY, company_id integer NOT NULL, "
                    "revaluation_date date NOT NULL, state varchar NOT NULL, "
                    "CONSTRAINT legacy_fx_date_company_uniq "
                    "UNIQUE (revaluation_date, company_id))"
                )
                self.env.cr.execute(
                    "INSERT INTO eh_fx_revaluation_run VALUES "
                    "(1, 7, '2026-06-30', 'posted')"
                )
                migration.migrate(self.env.cr, '19.0.1.6.8')
                # Terminal evidence does not consume the date.
                self.env.cr.execute(
                    "INSERT INTO eh_fx_revaluation_run VALUES "
                    "(2, 7, '2026-06-30', 'reversed')"
                )
                # A second live run still fails under concurrent SQL writes.
                with self.assertRaises(IntegrityError), \
                        mute_logger('odoo.sql_db'), self.env.cr.savepoint():
                    self.env.cr.execute(
                        "INSERT INTO eh_fx_revaluation_run VALUES "
                        "(3, 7, '2026-06-30', 'draft')"
                    )
                self.env.cr.execute(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname = current_schema() "
                    "AND indexname = "
                    "'eh_fx_revaluation_run_live_date_uniq'"
                )
                indexdef = self.env.cr.fetchone()[0]
                for live_state in ('draft', 'computed', 'posted'):
                    self.assertIn(live_state, indexdef)
                self.assertNotIn('reversed', indexdef)
                self.assertNotIn('cancelled', indexdef)
                raise RollbackProbe()
        except RollbackProbe:
            pass
