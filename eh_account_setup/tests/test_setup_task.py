# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Smoke tests for the accounting setup guide."""

import ast
import inspect
from pathlib import Path

from lxml import etree
from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger


@tagged('eh_account_setup', 'unit', 'post_install', '-at_install')
class TestSetupTaskLine(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Task = cls.env['eh.account.setup.task']
        cls.Line = cls.env['eh.account.setup.task.line']
        cls.plain_user = cls.env['res.users'].create({
            'name': 'Setup Guide Plain User',
            'login': 'setup_guide_plain_user',
            'email': 'setup_guide_plain_user@example.com',
            'company_id': cls.env.company.id,
            'company_ids': [(6, 0, cls.env.company.ids)],
            'group_ids': [(6, 0, [
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        cls.manager_user = cls.env['res.users'].create({
            'name': 'Setup Guide Manager',
            'login': 'setup_guide_manager',
            'email': 'setup_guide_manager@example.com',
            'company_id': cls.env.company.id,
            'company_ids': [(6, 0, cls.env.company.ids)],
            'group_ids': [(6, 0, [
                cls.env.ref('eh_account_base.group_eh_manager').id,
            ])],
        })

    def test_post_install_seeds_one_line_per_task_per_company(self):
        tasks = self.Task.search([])
        if not tasks:
            self.skipTest("no task definitions seeded")
        for company in self.env['res.company'].search([]):
            with self.subTest(company=company.name):
                lines = self.Line.search([('company_id', '=', company.id)])
                self.assertEqual(
                    len(lines), len(tasks),
                    f"company {company.name} expected {len(tasks)} lines, got {len(lines)}",
                )

    def test_post_init_hook_is_idempotent(self):
        from odoo.addons.eh_account_setup.hooks import post_init_hook
        before = len(self.Line.search([]))
        post_init_hook(self.env)
        # Keep the Odoo 16 cursor/registry contract executable without a dead
        # version branch in the Odoo 19-authored test suite.
        post_init_hook(self.env.cr, self.env.registry)
        after = len(self.Line.search([]))
        self.assertEqual(before, after, "post_init_hook is not idempotent")

    def test_unique_per_company_constraint(self):
        line = self.Line.search([], limit=1)
        if not line:
            self.skipTest("no lines to test")
        with self.assertRaises(IntegrityError), \
                mute_logger('odoo.sql_db'), self.env.cr.savepoint():
            self.Line.create({
                'task_id': line.task_id.id,
                'company_id': line.company_id.id,
                'state': 'todo',
            })

    def test_every_relevant_seeded_action_resolves_and_opens(self):
        lines = self.Line.search([('is_relevant', '=', True)])
        if not lines:
            self.skipTest("no relevant setup lines to test")
        for line in lines:
            with self.subTest(task=line.task_id.key, xmlid=line.action_xmlid):
                action = self.env.ref(
                    line.action_xmlid, raise_if_not_found=False,
                )
                self.assertTrue(action, line.action_xmlid)
                self.assertTrue(action._name.startswith('ir.actions.'))
                opened = line.action_open()
                self.assertEqual(opened.get('id'), action.id)
                expected = self.env['ir.actions.actions']._for_xml_id(
                    line.action_xmlid,
                )
                self.assertEqual(opened, expected)
                self.assertNotIn('create_uid', opened)

    def test_action_open_rejects_non_action_xmlid(self):
        task = self.Task.create({
            'key': 'invalid_non_action_xmlid',
            'name': 'Invalid non action XML ID',
            'category': 'foundations',
            'action_xmlid': 'base.main_company',
        })
        line = self.Line.search([
            ('task_id', '=', task.id),
            ('company_id', '=', self.env.company.id),
        ], limit=1)
        with self.assertRaisesRegex(UserError, 'does not point to an action'):
            line.action_open()

    def test_definition_views_use_effective_order_and_float_widget(self):
        list_arch = etree.fromstring(self.env.ref(
            'eh_account_setup.view_eh_account_setup_task_list',
        ).arch_db.encode())
        sequence = list_arch.xpath(
            "//list/field[@name='sequence'] | "
            "//tree/field[@name='sequence']",
        )[0]
        self.assertNotEqual(sequence.get('widget'), 'handle')

        form_arch = etree.fromstring(self.env.ref(
            'eh_account_setup.view_eh_account_setup_task_form',
        ).arch_db.encode())
        completion = form_arch.xpath(
            "//field[@name='completion_ratio'][@widget='percentage']",
        )
        self.assertTrue(completion)
        self.assertEqual(completion[0].get('digits'), '[16, 1]')

    def test_completed_today_filter_is_timezone_aware_bounded_day(self):
        arch = etree.fromstring(self.env.ref(
            'eh_account_setup.view_eh_account_setup_line_search',
        ).arch_db.encode())
        today = arch.xpath("//filter[@name='filter_completed_today']")[0]
        domain = today.get('domain')
        self.assertIn("('completed_at', '>=', 'today')", domain)
        self.assertIn("('completed_at', '<', 'today +1d')", domain)
        self.assertNotIn('strftime', domain)

    def test_setup_guide_menu_is_visible_to_setup_user(self):
        menu = self.env.ref('eh_account_setup.menu_setup_guide')
        self.assertEqual(
            menu.parent_id,
            self.env.ref('eh_account_base.menu_eh_operations'),
        )
        visible_ids = self.env['ir.ui.menu'].with_user(
            self.plain_user,
        )._visible_menu_ids()
        self.assertIn(menu.id, visible_ids)

    def test_definition_action_has_nocontent_help(self):
        action = self.env.ref('eh_account_setup.action_eh_account_setup_task')
        self.assertIn('No setup task definitions yet', action.help)
        self.assertIn('tracked setup step for every company', action.help)

    def test_computed_many2many_fields_declare_no_phantom_tables(self):
        from odoo.addons.eh_account_setup.models import setup_task_line
        source = inspect.getsource(
            setup_task_line.EhAccountSetupTaskLine,
        )
        self.assertNotIn('eh_setup_line_sibling_rel', source)
        self.assertNotIn('eh_setup_line_peer_rel', source)
        self.assertIsNone(self.Line._fields['sibling_line_ids'].relation)
        self.assertIsNone(self.Line._fields['peer_line_ids'].relation)

    def test_listing_matches_release_and_runtime_contract(self):
        from odoo.addons import eh_account_setup
        module_root = Path(eh_account_setup.__file__).resolve().parent
        manifest = ast.literal_eval(
            (module_root / '__manifest__.py').read_text(encoding='utf-8'),
        )
        listing = (
            module_root / 'static' / 'description' / 'index.html'
        ).read_text(encoding='utf-8')
        self.assertGreaterEqual(
            listing.count('v%s' % manifest['version']), 2,
        )
        self.assertIn("web-safe action dictionary", listing)
        self.assertIn("excluded from the completion-ratio denominator", listing)
        self.assertNotIn("counts skipped tasks as not done", listing)

    def test_mark_done_records_audit(self):
        line = self.Line.search([('state', '=', 'todo')], limit=1)
        if not line:
            self.skipTest("no todo lines to test")
        line.action_mark_done()
        self.assertEqual(line.state, 'done')
        self.assertTrue(line.completed_at)
        self.assertEqual(line.completed_by_id, self.env.user)

    def test_progress_state_and_audit_fields_require_actions(self):
        line = self.Line.search([('state', '=', 'todo')], limit=1)
        if not line:
            self.skipTest("no todo lines to test")
        actor_line = line.with_user(self.plain_user)
        with self.assertRaises(AccessError):
            actor_line.write({
                'state': 'done',
                'completed_at': '2020-01-01 00:00:00',
                'completed_by_id': self.manager_user.id,
            })
        with self.assertRaises(AccessError):
            actor_line.with_context(eh_setup_action=True).write({
                'state': 'done',
            })
        actor_line.action_mark_done()
        line.invalidate_recordset()
        self.assertEqual(line.state, 'done')
        self.assertEqual(line.completed_by_id, self.plain_user)

    def test_completion_notes_freeze_until_controlled_reset(self):
        line = self.Line.search([('state', '=', 'todo')], limit=1)
        if not line:
            self.skipTest("no todo lines to test")
        actor_line = line.with_user(self.plain_user)
        actor_line.write({'notes': 'Preparer evidence before completion'})
        actor_line.action_mark_done()
        with self.assertRaisesRegex(UserError, 'notes are frozen'):
            actor_line.write({'notes': 'silently rewritten after completion'})
        actor_line.action_reset()
        actor_line.write({'notes': 'Reviewed correction after reset'})
        line.invalidate_recordset()
        self.assertEqual(line.notes, 'Reviewed correction after reset')

    def test_completion_outcome_cannot_be_restamped_without_reset(self):
        line = self.Line.search([('state', '=', 'todo')], limit=1)
        if not line:
            self.skipTest("no todo lines to test")
        actor_line = line.with_user(self.plain_user)
        actor_line.action_mark_done()
        line.invalidate_recordset()
        completed_at = line.completed_at
        completed_by = line.completed_by_id
        with self.assertRaisesRegex(UserError, 'Only pending'):
            actor_line.action_mark_done()
        with self.assertRaisesRegex(UserError, 'Only pending'):
            actor_line.action_mark_skipped()
        line.invalidate_recordset()
        self.assertEqual(line.state, 'done')
        self.assertEqual(line.completed_at, completed_at)
        self.assertEqual(line.completed_by_id, completed_by)

        actor_line.action_reset()
        actor_line.action_mark_skipped()
        line.invalidate_recordset()
        self.assertEqual(line.state, 'skipped')
        self.assertEqual(line.completed_by_id, self.plain_user)

    def test_progress_identity_and_rows_are_immutable(self):
        line = self.Line.search([], limit=1)
        if not line:
            self.skipTest("no lines to test")
        other_company = self.env['res.company'].create({
            'name': 'Setup Guide Other Company',
        })
        manager_line = line.with_user(self.manager_user)
        with self.assertRaises(AccessError):
            manager_line.write({'company_id': other_company.id})
        with self.assertRaises(AccessError):
            manager_line.unlink()

    def test_task_unlink_sees_hidden_company_progress(self):
        task = self.Task.search([], limit=1)
        hidden = self.env['res.company'].create({
            'name': 'Hidden setup progress company',
        })
        hidden_line = self.Line.sudo().search([
            ('task_id', '=', task.id),
            ('company_id', '=', hidden.id),
        ], limit=1)
        self.assertTrue(hidden_line)
        with self.assertRaisesRegex(UserError, 'audit history'):
            task.with_user(self.manager_user).unlink()
        self.assertTrue(hidden_line.exists())

    def test_reset_keeps_chatter_transition_history(self):
        line = self.Line.search([('state', '=', 'todo')], limit=1)
        before = len(line.message_ids)
        actor_line = line.with_user(self.plain_user)
        actor_line.action_mark_done()
        actor_line.action_reset()
        line.invalidate_recordset(['message_ids'])
        self.assertGreaterEqual(len(line.message_ids), before + 2)

    def test_task_stored_totals_are_engine_owned(self):
        task = self.Task.search([], limit=1)
        if not task:
            self.skipTest("no task definitions seeded")
        with self.assertRaises(AccessError):
            task.with_user(self.manager_user).write({'line_count': 999})

    def test_skip_then_reset(self):
        line = self.Line.search([('state', '=', 'todo')], limit=1)
        if not line:
            self.skipTest("no todo lines to test")
        line.action_mark_skipped()
        self.assertEqual(line.state, 'skipped')
        self.assertTrue(line.completed_at)
        line.action_reset()
        self.assertEqual(line.state, 'todo')
        self.assertFalse(line.completed_at)
        self.assertFalse(line.completed_by_id)

    def test_is_relevant_matches_installed_modules(self):
        installed = set(self.env['ir.module.module'].sudo().search([
            ('state', '=', 'installed'),
        ]).mapped('name'))
        for line in self.Line.search([], limit=20):
            with self.subTest(task=line.task_id.key):
                required = (line.required_modules or '').split()
                expected = all(m in installed for m in required)
                self.assertEqual(line.is_relevant, expected)

    def test_search_is_relevant_returns_only_matching(self):
        true_ids = set(self.Line.search([('is_relevant', '=', True)]).ids)
        all_ids = set(self.Line.search([]).ids)
        false_ids = set(self.Line.search([('is_relevant', '=', False)]).ids)
        self.assertEqual(true_ids | false_ids, all_ids)
        self.assertFalse(true_ids & false_ids)

    def test_new_company_seeds_lines(self):
        tasks_count = len(self.Task.search([]))
        if not tasks_count:
            self.skipTest("no task definitions seeded")
        new_company = self.env['res.company'].create({
            'name': 'eh_account_setup test company',
        })
        seeded = self.Line.search([('company_id', '=', new_company.id)])
        self.assertEqual(
            len(seeded), tasks_count,
            "creating a company should seed one line per task definition",
        )

    def test_new_task_seeds_every_existing_company_without_rewriting_progress(self):
        company = self.env['res.company'].create({
            'name': 'Setup task seeding second company',
        })
        existing = self.Line.search([
            ('company_id', '=', self.env.company.id),
        ], limit=1)
        existing.action_mark_done()
        task = self.Task.create({
            'key': 'runtime_seed_%s' % company.id,
            'name': 'Runtime seeded setup task',
            'category': 'foundations',
            'action_xmlid': 'base.action_res_company_form',
        })

        seeded = self.Line.search([('task_id', '=', task.id)])
        self.assertEqual(set(seeded.company_id.ids), set(
            self.env['res.company'].sudo().search([]).ids
        ))
        self.assertEqual(existing.state, 'done')
        before = len(seeded)
        task._eh_seed_company_lines(self.env['res.company'].sudo().search([]))
        self.assertEqual(len(self.Line.search([('task_id', '=', task.id)])), before)
        self.assertIn(company, seeded.company_id)

    def test_task_line_counts_track_state_changes(self):
        task = self.Task.search([], limit=1)
        if not task:
            self.skipTest("no task definitions seeded")
        before_done = task.line_done_count
        line = self.Line.search([
            ('task_id', '=', task.id),
            ('state', '=', 'todo'),
        ], limit=1)
        if not line:
            self.skipTest("no todo lines for the picked task")
        line.action_mark_done()
        task.invalidate_recordset()
        self.assertEqual(task.line_done_count, before_done + 1)
        self.assertGreaterEqual(task.line_count, task.line_done_count)
        active_count = task.line_done_count + task.line_todo_count
        if active_count:
            expected_ratio = 100.0 * task.line_done_count / active_count
            self.assertAlmostEqual(task.completion_ratio, expected_ratio, places=2)

    def test_skipped_companies_are_excluded_from_completion_ratio(self):
        other = self.env['res.company'].create({
            'name': 'Setup ratio skipped company',
        })
        allowed = [self.env.company.id, other.id]
        task = self.Task.with_context(
            allowed_company_ids=allowed,
        ).search([], limit=1)
        lines = self.Line.with_context(
            allowed_company_ids=allowed,
        ).search([
            ('task_id', '=', task.id),
            ('state', '=', 'todo'),
        ], limit=2)
        self.assertEqual(len(lines), 2)
        lines[0].action_mark_done()
        lines[1].action_mark_skipped()
        task.invalidate_recordset([
            'line_done_count', 'line_todo_count', 'line_skipped_count',
            'completion_ratio',
        ])
        self.assertEqual(task.line_done_count, 1)
        self.assertEqual(task.line_skipped_count, 1)
        self.assertAlmostEqual(task.completion_ratio, 100.0, places=2)

    def test_task_totals_do_not_leak_other_company_progress(self):
        task = self.Task.search([], limit=1)
        if not task:
            self.skipTest("no task definitions seeded")
        other_company = self.env['res.company'].create({
            'name': 'Setup Totals Hidden Company',
        })
        other_line = self.Line.search([
            ('task_id', '=', task.id),
            ('company_id', '=', other_company.id),
        ], limit=1)
        other_line.action_mark_done()
        restricted = task.with_user(self.plain_user).with_context(
            allowed_company_ids=[self.env.company.id],
        )
        visible_lines = self.Line.with_user(self.plain_user).with_context(
            allowed_company_ids=[self.env.company.id],
        ).search([('task_id', '=', task.id)])
        restricted.invalidate_recordset([
            'line_count', 'line_done_count', 'line_todo_count',
            'line_skipped_count', 'completion_ratio',
        ])
        self.assertEqual(restricted.line_count, len(visible_lines))
        self.assertEqual(
            restricted.line_done_count,
            len(visible_lines.filtered(lambda line: line.state == 'done')),
        )
        self.assertNotIn(other_line, visible_lines)

    def test_sibling_line_ids_excludes_self_and_other_tasks(self):
        line = self.Line.search([], limit=1)
        if not line:
            self.skipTest("no lines to test")
        siblings = line.sibling_line_ids
        self.assertNotIn(line, siblings)
        for sibling in siblings:
            self.assertEqual(sibling.task_id, line.task_id)
            self.assertNotEqual(sibling.id, line.id)

    def test_peer_line_ids_excludes_self_other_companies_other_categories(self):
        line = self.Line.search([], limit=1)
        if not line:
            self.skipTest("no lines to test")
        peers = line.peer_line_ids
        self.assertNotIn(line, peers)
        for peer in peers:
            self.assertEqual(peer.company_id, line.company_id)
            self.assertEqual(peer.category, line.category)
            self.assertNotEqual(peer.id, line.id)

    def test_company_progress_counts_consistent(self):
        line = self.Line.search([], limit=1)
        if not line:
            self.skipTest("no lines to test")
        actual_total = self.Line.search_count([
            ('company_id', '=', line.company_id.id),
            ('is_relevant', '=', True),
        ])
        actual_done = self.Line.search_count([
            ('company_id', '=', line.company_id.id),
            ('is_relevant', '=', True),
            ('state', '=', 'done'),
        ])
        self.assertEqual(line.company_total_count, actual_total)
        self.assertEqual(line.company_done_count, actual_done)

    def test_task_progress_counts_consistent(self):
        line = self.Line.search([], limit=1)
        if not line:
            self.skipTest("no lines to test")
        actual_total = self.Line.search_count([
            ('task_id', '=', line.task_id.id),
        ])
        actual_done = self.Line.search_count([
            ('task_id', '=', line.task_id.id),
            ('state', '=', 'done'),
        ])
        self.assertEqual(line.task_total_count, actual_total)
        self.assertEqual(line.task_done_count, actual_done)

    def test_action_view_company_lines_returns_filtered_action(self):
        line = self.Line.search([], limit=1)
        if not line:
            self.skipTest("no lines to test")
        action = line.action_view_company_lines()
        self.assertEqual(action['res_model'], 'eh.account.setup.task.line')
        self.assertIn(('company_id', '=', line.company_id.id), action['domain'])

    def test_action_view_task_lines_returns_filtered_action(self):
        line = self.Line.search([], limit=1)
        if not line:
            self.skipTest("no lines to test")
        action = line.action_view_task_lines()
        self.assertEqual(action['res_model'], 'eh.account.setup.task.line')
        self.assertIn(('task_id', '=', line.task_id.id), action['domain'])

    def test_action_view_lines_filters_by_state_when_requested(self):
        task = self.Task.search([], limit=1)
        if not task:
            self.skipTest("no task definitions seeded")
        action_done = task.action_view_lines_done()
        self.assertIn(('state', '=', 'done'), action_done['domain'])
        action_todo = task.action_view_lines_todo()
        self.assertIn(('state', '=', 'todo'), action_todo['domain'])
        action_skipped = task.action_view_lines_skipped()
        self.assertIn(('state', '=', 'skipped'), action_skipped['domain'])
        action_all = task.action_view_lines()
        self.assertNotIn(('state', '=', 'done'), action_all['domain'])
        self.assertNotIn(('state', '=', 'todo'), action_all['domain'])
