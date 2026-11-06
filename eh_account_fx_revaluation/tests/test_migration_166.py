# -*- encoding: utf-8 -*-
"""Regression tests for reciprocal-rate upgrade quarantine."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase

from .compat import add_user_groups


@tagged('eh_account_fx_revaluation', 'post_install', '-at_install')
class TestFxMigration166(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        add_user_groups(
            cls.env.user, cls.env.ref('eh_account_base.group_eh_manager'),
        )
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.6.6' / 'post-migration.py'
        )
        spec = spec_from_file_location('eh_fx_post_migration_166', path)
        cls.migration = module_from_spec(spec)
        spec.loader.exec_module(cls.migration)

    def _config(self):
        Config = self.env['eh.fx.rate.config']
        Config.search([('company_id', '=', self.company.id)]).unlink()
        return Config.create({
            'company_id': self.company.id,
            'provider': 'ecb',
            'enabled': True,
        })

    def _legacy_rate(self):
        foreign = self.env['res.currency'].with_context(
            active_test=False,
        ).search([('id', '!=', self.company.currency_id.id)], limit=1)
        foreign.sudo().write({'active': True})
        rate_company = (
            self.company.root_id
            if 'root_id' in self.env['res.company']._fields
            else self.company
        )
        Rate = self.env['res.currency.rate'].sudo()
        Rate.search([
            ('currency_id', '=', foreign.id),
            ('name', '=', '2026-12-29'),
            ('company_id', '=', rate_company.id),
        ]).unlink()
        return Rate.create({
            'currency_id': foreign.id,
            'name': '2026-12-29',
            'company_id': rate_company.id,
            'rate': 1.23456789,
        })

    def _migrate(self, version='19.0.1.6.5'):
        self.env.flush_all()
        with patch.object(self.migration, 'migrate_fx_provenance'):
            self.migration.migrate(self.env.cr, version)
        self.env.invalidate_all()

    def test_upgrade_disables_config_but_never_mutates_unproven_rate(self):
        config = self._config()
        rate = self._legacy_rate()
        original_rate = rate.rate

        self._migrate()

        config.invalidate_recordset()
        rate.invalidate_recordset()
        self.assertFalse(config.enabled)
        self.assertTrue(config.rate_direction_review_required)
        self.assertFalse(config.last_fetched_at)
        self.assertIn('not changed', config.last_error)
        self.assertEqual(rate.rate, original_rate)

    def test_clean_install_does_not_create_safety_hold(self):
        config = self._config()

        self._migrate(version=None)

        config.invalidate_recordset()
        self.assertTrue(config.enabled)
        self.assertFalse(config.rate_direction_review_required)

    def test_manager_acknowledgement_releases_hold_but_keeps_fetch_disabled(self):
        config = self._config()
        config.sudo().write({
            'enabled': False,
            'rate_direction_review_required': True,
        })

        config.action_acknowledge_rate_direction_review()

        config.invalidate_recordset()
        self.assertFalse(config.rate_direction_review_required)
        self.assertFalse(config.enabled)
        self.assertTrue(config.rate_direction_reviewed_at)
        self.assertEqual(config.rate_direction_reviewed_by_id, self.env.user)

    def test_fetch_is_blocked_until_review_acknowledged(self):
        config = self._config()
        config.sudo().write({'rate_direction_review_required': True})

        with self.assertRaises(UserError):
            config.fetch_rates(currency_codes=['EUR'])
