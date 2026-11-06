# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Upgrade regressions for noupdate Collections configuration."""

import importlib.util
from pathlib import Path

from odoo.tests import TransactionCase, tagged


@tagged('eh_account_collections', 'post_install', '-at_install')
class TestCollectionsMigrations(TransactionCase):

    @classmethod
    def _template_migration(cls):
        migrations = Path(__file__).parents[1] / 'migrations'
        path = next(
            candidate
            for candidate in migrations.glob('*/post-migrate.py')
            if candidate.parent.name.endswith('.1.2.8')
        )
        spec = importlib.util.spec_from_file_location(
            'eh_collections_template_migration_test', path,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_branded_wiring_upgrades_defaults_but_preserves_custom_data(self):
        migration = self._template_migration()
        level = self.env.ref('eh_account_collections.followup_level_1')
        legacy = self.env.ref(
            'eh_account_collections.mail_template_followup_reminder_1',
        )
        branded = self.env.ref(
            'eh_account_collections.email_template_eh_dunning_friendly',
        )
        custom = self.env['mail.template'].create({
            'name': 'Customer collections template',
            'model_id': self.env['ir.model']._get_id(
                'eh.collections.case',
            ),
            'subject': 'Customer subject',
        })

        # A user-selected template is noupdate business data. The migration
        # must not reset it merely because the default ladder XML changed.
        level.mail_template_id = custom
        self.env.flush_all()
        migration.migrate(self.env.cr, '19.0.1.2.7')
        self.env.invalidate_all()
        self.assertEqual(level.mail_template_id, custom)

        # The exact legacy shipped reference is metadata and is upgraded.
        level.mail_template_id = legacy
        self.env.flush_all()
        migration.migrate(self.env.cr, '19.0.1.2.7')
        self.env.invalidate_all()
        self.assertEqual(level.mail_template_id, branded)

        # Nor may the migration overwrite customer-authored content on the
        # branded noupdate record.
        branded.write({
            'name': 'Customer — Preserve This',
            'email_from': 'billing@example.com',
        })
        self.env.flush_all()
        migration.migrate(self.env.cr, '19.0.1.2.7')
        self.env.invalidate_all()
        self.assertEqual(branded.name, 'Customer — Preserve This')
        self.assertEqual(branded.email_from, 'billing@example.com')
