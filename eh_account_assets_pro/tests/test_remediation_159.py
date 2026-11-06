# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression pins for the 1.5.9 audit remediation surface."""

import importlib.util
from pathlib import Path

from lxml import etree

from odoo.tests import new_test_user, tagged

from .common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestAssetsProRemediation159(EhAssetTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.auditor = new_test_user(
            cls.env,
            login='eh_assets_pro_readonly_auditor_159',
            groups='eh_account_base.group_eh_auditor',
        )

    @staticmethod
    def _arch(view):
        return etree.fromstring(str(view.arch_db).encode('utf-8'))

    def test_auditor_has_read_only_acl_on_every_persistent_asset_model(self):
        model_names = (
            'eh.asset.category',
            'eh.asset',
            'eh.asset.depreciation.line',
            'eh.lease.contract',
            'eh.lease.schedule.line',
            'eh.asset.book',
            'eh.asset.book.line',
            'eh.asset.impairment',
            'eh.asset.lvp.pool',
            'eh.asset.lvp.pool.line',
            'eh.asset.cgu',
            'eh.asset.cgu.cashflow',
            'eh.asset.cgu.test.event',
            'eh.lease.option',
        )
        for model_name in model_names:
            with self.subTest(model=model_name):
                model = self.env[model_name].with_user(self.auditor)
                self.assertTrue(
                    model.check_access_rights('read', raise_exception=False),
                )
                for operation in ('write', 'create', 'unlink'):
                    self.assertFalse(model.check_access_rights(
                        operation, raise_exception=False,
                    ))
                # Also exercise the global company rule, not only the ACL row.
                model.search([], limit=1)

    def test_asset_and_category_forms_expose_required_configuration(self):
        asset_arch = self._arch(
            self.env.ref('eh_account_assets_pro.view_eh_asset_form'),
        )
        components = asset_arch.xpath("//page[@name='components']")
        self.assertEqual(len(components), 1)
        self.assertNotIn('invisible', components[0].attrib)
        self.assertEqual(
            len(components[0].xpath(".//field[@name='parent_asset_id']")),
            1,
        )
        self.assertEqual(len(asset_arch.xpath(
            "//field[@name='prorata_mode']",
        )), 1)
        for field_name in (
            'asset_account_id', 'depreciation_account_id',
            'accumulated_depreciation_account_id', 'journal_id',
        ):
            node = asset_arch.xpath(
                "//field[@name='%s']" % field_name,
            )[0]
            serialized = etree.tostring(node, encoding='unicode')
            self.assertIn('readonly', serialized)
            self.assertIn('state', serialized)
            self.assertIn('draft', serialized)

        category_arch = self._arch(
            self.env.ref('eh_account_assets_pro.view_eh_asset_category_form'),
        )
        self.assertEqual(len(category_arch.xpath(
            "//field[@name='prorata_mode']",
        )), 1)

    def test_lease_measurements_and_accounts_render_frozen_after_activation(self):
        arch = self._arch(
            self.env.ref('eh_account_assets_pro.view_eh_lease_contract_form'),
        )
        for field_name in (
            'lessor_id', 'commencement_date', 'term_months', 'cadence',
            'payment_timing', 'payment_amount',
            'incremental_borrowing_rate', 'rou_asset_account_id',
            'lease_liability_account_id', 'interest_expense_account_id',
            'rou_depreciation_account_id',
            'rou_accumulated_depreciation_account_id', 'cash_account_id',
            'journal_id',
        ):
            node = arch.xpath("//field[@name='%s']" % field_name)[0]
            serialized = etree.tostring(node, encoding='unicode')
            self.assertIn('readonly', serialized)
            self.assertIn('state', serialized)
            self.assertIn('draft', serialized)
        current_term = arch.xpath(
            "//field[@name='current_measurement_term_months']",
        )
        self.assertEqual(len(current_term), 1)

    def test_posted_status_widgets_are_display_only(self):
        for view_xmlid in (
            'eh_account_assets_pro.view_eh_asset_depreciation_line_tree',
            'eh_account_assets_pro.view_eh_lease_schedule_line_tree',
        ):
            arch = self._arch(self.env.ref(view_xmlid))
            node = arch.xpath("//field[@name='is_posted']")[0]
            self.assertEqual(node.get('readonly'), '1')
            self.assertNotEqual(node.get('widget'), 'boolean_toggle')

    def test_asset_register_presents_reconciling_columns_and_real_states(self):
        report_arch = str(self.env.ref(
            'eh_account_assets_pro.report_asset_register',
        ).arch_db)
        for token in (
            'total_cost', 'total_revaluation', 'total_depr',
            'total_impairment', 'total_nbv', 'Revaluation',
            'Accum. Impairment', "'running':",
        ):
            self.assertIn(token, report_arch)
        self.assertNotIn("'active':", report_arch)
        card_arch = str(self.env.ref(
            'eh_account_assets_pro.report_asset_card',
        ).arch_db)
        self.assertIn('Asset Card:', card_arch)
        self.assertNotIn('Asset Card —', card_arch)

    @staticmethod
    def _migration_module():
        import odoo.addons.eh_account_assets_pro as assets_module

        path = (
            Path(assets_module.__file__).resolve().parent
            / 'migrations' / '19.0.1.5.9' / 'post-migration.py'
        )
        spec = importlib.util.spec_from_file_location(
            'eh_assets_pro_post_migration_159', str(path),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_159_migration_retires_uop_and_seeds_measurement_term_idempotently(self):
        asset = self._make_asset(code='MIG-159-UOP')
        category = self.env['eh.asset.category'].create({
            'name': 'Legacy UOP category',
            'code': 'M159',
            'method': 'manual',
        })
        lease = self._make_lease(
            reference='MIG-159-TERM', term_months=12,
        )
        self.env['eh.lease.option'].create({
            'lease_id': lease.id,
            'option_type': 'extension',
            'extension_months': 6,
            'reasonably_certain': True,
        })
        lease.action_activate()
        opening_move = lease.opening_move_id
        opening_bytes = (
            opening_move.state,
            tuple(
                (line.account_id.id, line.debit, line.credit)
                for line in opening_move.line_ids.sorted('id')
            ),
        )

        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_asset SET method = 'units_of_production' WHERE id = %s",
            (asset.id,),
        )
        self.env.cr.execute(
            "UPDATE eh_asset_category SET method = 'units_of_production' "
            "WHERE id = %s",
            (category.id,),
        )
        self.env.cr.execute(
            "UPDATE eh_lease_contract "
            "SET current_measurement_term_months = 0 WHERE id = %s",
            (lease.id,),
        )
        move_count = self.env['account.move'].search_count([])

        migration = self._migration_module()
        migration.migrate(self.env.cr, '19.0.1.5.8')
        self.env.invalidate_all()

        self.assertEqual(asset.method, 'manual')
        self.assertEqual(category.method, 'manual')
        self.assertEqual(lease.term_months, 12)
        self.assertEqual(lease.current_measurement_term_months, 18)
        self.assertEqual(self.env['account.move'].search_count([]), move_count)
        self.assertEqual(opening_bytes, (
            opening_move.state,
            tuple(
                (line.account_id.id, line.debit, line.credit)
                for line in opening_move.line_ids.sorted('id')
            ),
        ))

        # Upgrade retries must be a no-op on already migrated bytes.
        migration.migrate(self.env.cr, '19.0.1.5.8')
        self.env.invalidate_all()
        self.assertEqual(asset.method, 'manual')
        self.assertEqual(category.method, 'manual')
        self.assertEqual(lease.current_measurement_term_months, 18)
        self.assertEqual(self.env['account.move'].search_count([]), move_count)
