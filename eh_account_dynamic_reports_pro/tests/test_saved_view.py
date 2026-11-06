# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Integration proof for Pro metadata on viewer's saved-view model."""

import json
import importlib.util
from datetime import date
from pathlib import Path
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged(
    'eh_account_dynamic_reports_pro', 'integration',
    'saved_view_unify', 'post_install', '-at_install',
)
class TestSavedView(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        DynRep = cls.env['eh.account.dynamic.report']
        cls.report = DynRep.search([('code', '=', 'trial_balance')], limit=1)
        if not cls.report:
            cls.report = DynRep.create({
                'code': 'trial_balance',
                'name': 'Trial Balance',
                'handler_model':
                    'eh.account.dynamic.report.handler.trial_balance',
            })
        cls.SavedView = cls.env['eh.account.report.saved_view']

    def setUp(self):
        super().setUp()
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
        }

    def _save(self, name, model=None, **kwargs):
        saved_view_model = self.SavedView if model is None else model
        view_id = saved_view_model.save_view(
            name, self.report.code, self.options, **kwargs,
        )
        return saved_view_model.browse(view_id)

    def _make_account_user(self, name, login):
        return self.env['res.users'].create({
            'name': name,
            'login': login,
            'group_ids': [
                (4, self.env.ref('account.group_account_user').id),
                (4, self.env.ref('eh_account_base.group_eh_user').id),
            ],
        })

    def test_viewer_model_is_only_registered_saved_view_model(self):
        view = self._save(
            'Canonical Pro view', pinned=True, shared=True, sequence=4,
        )
        self.assertTrue(view.exists())
        self.assertEqual(view._name, 'eh.account.report.saved_view')
        self.assertNotIn('eh.report.saved.view', self.env.registry.models)
        self.assertEqual(view.report_code, self.report.code)
        self.assertTrue(view.pinned)
        self.assertTrue(view.shared)
        self.assertEqual(view.sequence, 4)
        self.assertTrue(view.created_on)

    def test_viewer_list_exposes_and_orders_pro_metadata(self):
        unpinned = self._save('Unpinned canonical', sequence=1)
        pinned = self._save(
            'Pinned canonical', pinned=True, shared=True, sequence=50,
        )

        listing = self.SavedView.list_for(self.report.code)
        selected = [row for row in listing if row['id'] in {
            pinned.id, unpinned.id,
        }]
        self.assertEqual(
            [row['id'] for row in selected],
            [pinned.id, unpinned.id],
        )
        pinned_row = selected[0]
        self.assertTrue(pinned_row['pinned'])
        self.assertTrue(pinned_row['shared'])
        self.assertTrue(pinned_row['owned'])
        self.assertEqual(pinned_row['sequence'], 50)
        self.assertEqual(pinned_row['use_count'], 0)
        self.assertIsNone(pinned_row['last_used_at'])

    def test_viewer_load_updates_telemetry_atomically(self):
        view = self._save('Viewer telemetry')
        self.assertEqual(view.load_options(), self.options)
        self.assertEqual(view.load_options(), self.options)
        view.invalidate_recordset(['use_count', 'last_used_at'])
        self.assertEqual(view.use_count, 2)
        self.assertTrue(view.last_used_at)

        row = next(
            item for item in self.SavedView.list_for(self.report.code)
            if item['id'] == view.id
        )
        self.assertEqual(row['use_count'], 2)
        self.assertTrue(row['last_used_at'])

    def test_shared_view_lists_and_loads_for_reader_with_telemetry(self):
        owner = self._make_account_user(
            'Saved View Sharer', 'canonical_saved_view_sharer',
        )
        reader = self._make_account_user(
            'Saved View Reader', 'canonical_saved_view_reader',
        )
        shared = self._save(
            'Team baseline', model=self.SavedView.with_user(owner),
            shared=True, pinned=True,
        )

        reader_model = self.SavedView.with_user(reader)
        row = next(
            item for item in reader_model.list_for(self.report.code)
            if item['id'] == shared.id
        )
        self.assertTrue(row['shared'])
        self.assertTrue(row['pinned'])
        self.assertFalse(row['owned'])
        self.assertEqual(
            shared.with_user(reader).load_options(),
            self.options,
        )
        shared.invalidate_recordset(['use_count'])
        self.assertEqual(shared.use_count, 1)

        with self.assertRaises(AccessError):
            shared.with_user(reader).action_toggle_pinned()
        with self.assertRaises(AccessError):
            shared.with_user(reader).write({'name': 'Hijacked'})
        with self.assertRaises(AccessError):
            shared.with_user(reader).unlink()

    def test_private_view_cannot_be_listed_or_loaded_by_other_user(self):
        owner = self._make_account_user(
            'Private View Owner', 'canonical_private_view_owner',
        )
        reader = self._make_account_user(
            'Private View Reader', 'canonical_private_view_reader',
        )
        private = self._save(
            'Private baseline', model=self.SavedView.with_user(owner),
        )
        reader_model = self.SavedView.with_user(reader)
        self.assertNotIn(
            private.id,
            [row['id'] for row in reader_model.list_for(self.report.code)],
        )
        with self.assertRaises(AccessError):
            private.with_user(reader).load_options()
        private.invalidate_recordset(['use_count'])
        self.assertEqual(private.use_count, 0)

    def test_malformed_options_fail_before_telemetry(self):
        view = self._save('Malformed legacy options')
        for raw_options in (
            '{bad', '[]', 'null', '"text"', '1', '{"value": NaN}',
        ):
            self.env.cr.execute(
                "UPDATE eh_account_report_saved_view "
                "SET options_json = %s WHERE id = %s",
                (raw_options, view.id),
            )
            view.invalidate_recordset(['options_json', 'use_count'])
            with self.subTest(raw_options=raw_options):
                with self.assertRaises(UserError):
                    view.load_options()
                self.assertEqual(view.use_count, 0)
        self.env.cr.execute(
            "UPDATE eh_account_report_saved_view "
            "SET options_json = '{}' WHERE id = %s",
            (view.id,),
        )
        view.invalidate_recordset(['options_json'])

    def test_legacy_migration_is_idempotent_and_keeps_source(self):
        self.env.cr.execute("SELECT to_regclass('eh_report_saved_view')")
        if not self.env.cr.fetchone()[0]:
            self.env.cr.execute(
                "CREATE TABLE eh_report_saved_view ("
                " id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, "
                " report_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
                " company_id INTEGER, options_json TEXT NOT NULL, "
                " is_shared BOOLEAN, notes TEXT, sequence INTEGER, "
                " pinned BOOLEAN, created_on TIMESTAMP, "
                " last_used_at TIMESTAMP, use_count INTEGER, "
                " create_uid INTEGER, create_date TIMESTAMP, "
                " write_uid INTEGER, write_date TIMESTAMP"
                ")"
            )
        self.env.cr.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM eh_report_saved_view"
        )
        legacy_id = self.env.cr.fetchone()[0]
        legacy_name = 'Legacy migration proof %s' % legacy_id
        self.env.cr.execute(
            "INSERT INTO eh_report_saved_view "
            " (id, name, report_id, user_id, company_id, options_json, "
            "  is_shared, notes, sequence, pinned, use_count, create_uid, "
            "  create_date, write_uid, write_date) "
            "VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s, 3, TRUE, 7, "
            "        %s, NOW(), %s, NOW())",
            (
                legacy_id, legacy_name, self.report.id, self.env.uid,
                self.company.id, json.dumps(self.options),
                'Preserved migration note', self.env.uid, self.env.uid,
            ),
        )
        external_id = self.env['ir.model.data'].create({
            'module': 'eh_account_dynamic_reports_pro',
            'name': 'test_legacy_saved_view_%s' % legacy_id,
            'model': 'eh.report.saved.view',
            'res_id': legacy_id,
            'noupdate': True,
        })

        migration_path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.1.8' / 'post-migration.py'
        )
        spec = importlib.util.spec_from_file_location(
            'eh_saved_view_migration_118', str(migration_path),
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migrate(self.env.cr, '19.0.1.1.7')
        migration.migrate(self.env.cr, '19.0.1.1.7')

        canonical = self.SavedView.search([
            ('user_id', '=', self.env.uid),
            ('report_code', '=', self.report.code),
            ('name', '=', legacy_name),
        ])
        self.assertEqual(len(canonical), 1)
        self.assertTrue(canonical.pinned)
        self.assertTrue(canonical.shared)
        self.assertEqual(canonical.sequence, 3)
        self.assertEqual(canonical.use_count, 7)
        self.assertEqual(canonical.notes, 'Preserved migration note')
        self.env.cr.execute(
            "SELECT COUNT(*) FROM eh_report_saved_view WHERE id = %s",
            (legacy_id,),
        )
        self.assertEqual(self.env.cr.fetchone()[0], 1)
        external_id.invalidate_recordset(['model', 'res_id'])
        self.assertEqual(
            external_id.model,
            'eh.account.report.saved_view',
        )
        self.assertEqual(external_id.res_id, canonical.id)

    def test_relative_dates_resolve_when_viewer_loads(self):
        options = {
            'date': {
                'mode': 'range',
                'date_from': 'auto_qtd',
                'date_to': 'today',
            },
        }
        view_id = self.SavedView.save_view(
            'Current quarter', self.report.code, options,
        )
        with patch.object(
            fields.Date, 'context_today', return_value=date(2026, 8, 24),
        ):
            loaded = self.SavedView.browse(view_id).load_options()
        self.assertEqual(loaded['date']['date_from'], '2026-07-01')
        self.assertEqual(loaded['date']['date_to'], '2026-08-24')

    def test_partial_non_date_value_is_rejected_when_saved(self):
        options = {
            'date': {'date_from': ['legacy', 'partial']},
            'posted_only': False,
        }
        with self.assertRaises(ValidationError):
            self.SavedView.save_view(
                'Partial legacy options', self.report.code, options,
            )

    def test_unknown_relative_date_fails_when_saved(self):
        with self.assertRaisesRegex(ValidationError, 'unsupported date token'):
            self.SavedView.save_view(
                'Unknown relative date', self.report.code,
                {'date': {'date_from': 'auto_unknown'}},
            )

    def test_structured_period_overrides_only_saved_date_block(self):
        view = self._save('Structured prior month')
        view.write({'period_preset': 'previous_month'})
        with patch.object(
            fields.Date, 'context_today', return_value=date(2026, 8, 24),
        ):
            loaded = view.load_options()
        self.assertEqual(loaded['date'], {
            'mode': 'range',
            'date_from': '2026-07-01',
            'date_to': '2026-07-31',
        })
        self.assertEqual(loaded['company_ids'], self.options['company_ids'])
        self.assertEqual(loaded['posted_only'], self.options['posted_only'])

    def test_custom_period_requires_ordered_bounds(self):
        view = self._save('Custom bounds')
        with self.assertRaises(ValidationError):
            view.write({'period_preset': 'custom'})
        with self.assertRaises(ValidationError):
            view.write({
                'period_preset': 'custom',
                'period_date_from': '2026-06-30',
                'period_date_to': '2026-01-01',
            })
        view.write({
            'period_preset': 'custom',
            'period_date_from': '2026-01-01',
            'period_date_to': '2026-06-30',
        })
        self.assertEqual(view.load_options()['date']['date_to'], '2026-06-30')

    def test_viewer_save_upserts_without_losing_pin(self):
        first = self._save('Reusable view', pinned=True)
        changed = dict(self.options, show_zero=True)
        second_id = self.SavedView.save_view(
            'Reusable view', self.report.code, changed,
        )
        self.assertEqual(second_id, first.id)
        first.invalidate_recordset(['options_json', 'pinned'])
        self.assertTrue(first.pinned)
        self.assertTrue(json.loads(first.options_json)['show_zero'])

    def test_owner_identity_is_immutable(self):
        owner = self._make_account_user(
            'Immutable View Owner', 'canonical_saved_identity_owner',
        )
        other = self._make_account_user(
            'Immutable View Other', 'canonical_saved_identity_other',
        )
        with self.assertRaises(AccessError):
            self.SavedView.with_user(owner).create({
                'name': 'Forged owner',
                'report_code': self.report.code,
                'options_json': '{}',
                'user_id': other.id,
            })
        view = self._save(
            'Immutable canonical identity',
            model=self.SavedView.with_user(owner),
        )
        with self.assertRaises(AccessError):
            view.with_user(owner).write({'user_id': other.id})
        with self.assertRaises(AccessError):
            view.with_user(owner).write({'company_id': False})

    def test_owner_can_toggle_pin(self):
        view = self._save('Toggle pin')
        self.assertFalse(view.pinned)
        view.action_toggle_pinned()
        self.assertTrue(view.pinned)
        view.action_toggle_pinned()
        self.assertFalse(view.pinned)

    def test_pro_menu_opens_viewer_saved_view_model(self):
        action = self.env.ref(
            'eh_account_dynamic_reports_pro.action_eh_report_saved_view',
        )
        self.assertEqual(
            action.res_model,
            'eh.account.report.saved_view',
        )
