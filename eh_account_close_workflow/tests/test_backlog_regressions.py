# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Focused regressions for the Close Workflow audit remediation tranche."""

import ast
from datetime import date
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

from odoo import fields, release
from odoo.exceptions import ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


MODULE_ROOT = Path(__file__).resolve().parents[1]


def _load_migration(filename):
    path = MODULE_ROOT / 'migrations' / '19.0.1.1.7' / filename
    spec = spec_from_file_location(
        'eh_close_117_' + filename.replace('-', '_').replace('.', '_'),
        path,
    )
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@tagged('eh_account_close_workflow', 'integration', 'post_install',
        '-at_install')
class TestCloseWorkflowBacklogRegressions(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Checklist = cls.env['eh.close.checklist']
        cls.Run = cls.env['eh.close.run']
        cls.checklist = cls.Checklist.create({
            'name': 'Backlog Regression Checklist',
            'code': 'backlog_regression_close',
            'ledger_lock_policy': 'signoff_only',
            'task_template_ids': [(0, 0, {
                'name': 'Prove close evidence',
                'responsible_role': 'accountant',
            })],
        })

    def _run(self, **overrides):
        values = {
            'name': 'Backlog February 2025 Close',
            'period_from': date(2025, 2, 1),
            'period_to': date(2025, 2, 28),
        }
        values.update(overrides)
        return self.checklist.action_create_run(**values)

    def test_defaulted_api_run_is_deterministically_bounded(self):
        with patch.object(
            fields.Date, 'context_today', return_value=date(2026, 5, 17),
        ):
            run = self.checklist.action_create_run(name='Defaulted May Close')
        self.assertEqual(run.period_from, date(2026, 5, 1))
        self.assertEqual(run.period_to, date(2026, 5, 31))
        self.assertTrue(self.Run._fields['period_from'].required)
        self.assertTrue(self.Run._fields['period_to'].required)

    def test_company_and_branch_scope_isolated_by_allowed_companies(self):
        company_vals = {'name': 'Close Workflow Branch Company'}
        if 'parent_id' in self.env['res.company']._fields:
            company_vals['parent_id'] = self.company.id
        other = self.env['res.company'].sudo().create(company_vals)
        other_checklist = self.Checklist.sudo().create({
            'name': 'Other Company Close',
            'code': 'other_company_close',
            'company_id': other.id,
            'ledger_lock_policy': 'signoff_only',
            'task_template_ids': [(0, 0, {
                'name': 'Other company evidence',
                'responsible_role': 'accountant',
            })],
        })
        other_run = self.Run.sudo().create({
            'name': 'Other Company March Close',
            'checklist_id': other_checklist.id,
            'company_id': other.id,
            'period_from': date(2025, 3, 1),
            'period_to': date(2025, 3, 31),
        })
        scoped_user = self.env['res.users'].create({
            'name': 'Close Scope User',
            'login': 'close_scope_user',
            'company_id': self.company.id,
            'company_ids': [(6, 0, [self.company.id, other.id])],
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        current_only = self.Run.with_user(scoped_user).with_context(
            allowed_company_ids=[self.company.id],
        )
        self.assertFalse(current_only.search([('id', '=', other_run.id)]))
        both = self.Run.with_user(scoped_user).with_context(
            allowed_company_ids=[self.company.id, other.id],
        )
        self.assertEqual(both.search([('id', '=', other_run.id)]), other_run)

    def test_archived_shared_code_and_database_index_are_unique(self):
        shared = self.Checklist.create({
            'name': 'Archived Shared Close',
            'code': 'archived_shared_close',
            'company_id': False,
        })
        shared.active = False
        with self.assertRaises(ValidationError):
            self.Checklist.create({
                'name': 'Duplicate Archived Shared Close',
                'code': 'archived_shared_close',
                'company_id': False,
            })
        self.env.cr.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'eh_close_checklist_shared_code_uniq'"
        )
        indexdef = (self.env.cr.fetchone() or [''])[0].lower()
        self.assertIn('unique index', indexdef)
        self.assertIn('company_id is null', indexdef.replace('(', '').replace(')', ''))

    def test_views_expose_safe_picker_block_dialog_and_static_kanban(self):
        run_arch = self.env.ref(
            'eh_account_close_workflow.view_eh_close_run_form',
        ).arch_db
        kanban_arch = self.env.ref(
            'eh_account_close_workflow.view_eh_close_run_kanban',
        ).arch_db
        task_arch = self.env.ref(
            'eh_account_close_workflow.view_eh_close_task_form',
        ).arch_db
        self.assertIn("('company_id', '=', company_id)", run_arch)
        self.assertIn('action_open_block_wizard', run_arch)
        self.assertIn('action_open_block_wizard', task_arch)
        self.assertNotIn('name="action_block"', run_arch + task_arch)
        self.assertIn('records_draggable="false"', kanban_arch)

    def test_legacy_period_and_block_reason_migration_is_durable(self):
        run = self._run()
        task = run.task_ids
        run.flush_recordset()
        task.flush_recordset()
        self.env.cr.execute(
            "ALTER TABLE eh_close_run "
            "ALTER COLUMN period_from DROP NOT NULL, "
            "ALTER COLUMN period_to DROP NOT NULL"
        )
        self.env.cr.execute(
            "UPDATE eh_close_run SET period_from = NULL, period_to = NULL "
            "WHERE id = %s",
            [run.id],
        )
        self.env.cr.execute(
            "UPDATE eh_close_task SET state = 'blocked', "
            "blocked_reason = NULL WHERE id = %s",
            [task.id],
        )
        _load_migration('pre-migration.py').migrate(
            self.env.cr, '19.0.1.1.6',
        )
        self.env.cr.execute(
            "ALTER TABLE eh_close_run "
            "ALTER COLUMN period_from SET NOT NULL, "
            "ALTER COLUMN period_to SET NOT NULL"
        )
        run.invalidate_recordset(['period_from', 'period_to'])
        task.invalidate_recordset(['blocked_reason'])
        self.assertTrue(run.period_from)
        self.assertTrue(run.period_to)
        self.assertLessEqual(run.period_from, run.period_to)
        self.assertIn('Legacy blocked task', task.blocked_reason)

    def test_reopened_migration_posts_idempotent_user_provenance(self):
        run = self._run()
        self.env.cr.execute(
            "UPDATE eh_close_run SET state = 'reopened', reopened_by_id = %s "
            "WHERE id = %s",
            [self.env.user.id, run.id],
        )
        run.invalidate_recordset(['state', 'reopened_by_id'])
        migration = _load_migration('post-migration.py')
        migration.migrate(self.env.cr, '19.0.1.1.6')
        migration.migrate(self.env.cr, '19.0.1.1.6')
        messages = run.message_ids.filtered(
            lambda message: 'EH_CLOSE_MIGRATION_1_1_7' in str(message.body),
        )
        self.assertEqual(len(messages), 1)
        self.assertIn('fresh preparer', str(messages.body))

    def test_manifest_listing_and_targeted_sql_are_current(self):
        manifest = ast.literal_eval(
            (MODULE_ROOT / '__manifest__.py').read_text(),
        )
        expected = '%s.0.1.1.7' % release.version_info[0]
        listing = (
            MODULE_ROOT / 'static' / 'description' / 'index.html'
        ).read_text()
        run_source = (MODULE_ROOT / 'models' / 'run.py').read_text()
        self.assertEqual(manifest['version'], expected)
        self.assertLessEqual(len(manifest['summary']), 200)
        self.assertGreaterEqual(listing.count('v' + expected), 2)
        self.assertNotIn('There is no append-only audit log model', listing)
        self.assertNotIn('UI-readonly rather than write-locked', listing)
        self.assertIn('company-wide Global Lock Date', listing)
        self.assertIn('strict: preparer, reviewer', listing)
        self.assertIn('exceptional_unbalanced_moves', run_source)
        self.assertNotIn("move.line_ids.mapped('balance')", run_source)
