# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Workflow-guard regression tests.

The close run / task state machines are enforced by action_* methods that
carry side effects (segregation-of-duties, blocking-check rescan, manager
authorization). Without the eh.workflow.guard mixin a plain user could RPC
write({'state': 'closed'}) straight past those gates. These tests assert the
guard refuses a direct state write from a non-superuser and that the legitimate
action path still works.
"""

from datetime import date

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_close_workflow', 'post_install', '-at_install')
class TestWorkflowGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Checklist = cls.env['eh.close.checklist']

        # A plain accounting user: has read/write ACL on run + task (so the
        # write reaches the guard) but is NOT a manager and NOT superuser.
        cls.plain_user = cls.env['res.users'].create({
            'name': 'Plain Clerk',
            'login': 'plain_clerk_guard',
            'group_ids': [(6, 0, [
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })

        cls.checklist = cls.Checklist.create({
            'name': 'Guard Checklist',
            'code': 'guard_checklist',
            'ledger_lock_policy': 'signoff_only',
            'task_template_ids': [
                (0, 0, {
                    'sequence': 10, 'name': 'Reconcile bank',
                    'responsible_role': 'accountant',
                }),
            ],
        })
        cls.close_run = cls.checklist.action_create_run(
            name='Guard January 2024 Close',
            period_from=date(2024, 1, 1),
            period_to=date(2024, 1, 31),
        )
        cls.task = cls.close_run.task_ids[0]

    def test_direct_state_write_on_run_is_refused(self):
        """A non-superuser cannot skip the close workflow by RPC-writing the
        run straight to 'closed'; the guard raises AccessError."""
        self.assertEqual(self.close_run.state, 'open')
        with self.assertRaises(AccessError):
            self.close_run.with_user(self.plain_user).write({'state': 'closed'})
        # State unchanged: the direct write was rejected before it landed.
        self.assertEqual(self.close_run.state, 'open')

    def test_direct_state_write_on_task_is_refused(self):
        """A non-superuser cannot mark a task 'done' by a direct write,
        bypassing action_mark_done and its completed_by audit stamp."""
        self.assertEqual(self.task.state, 'pending')
        with self.assertRaises(AccessError):
            self.task.with_user(self.plain_user).write({'state': 'done'})
        self.assertEqual(self.task.state, 'pending')

    def test_plain_user_cannot_downgrade_required_task(self):
        with self.assertRaises(AccessError):
            self.task.with_user(self.plain_user).write({
                'is_required': False,
            })
        with self.assertRaises(UserError):
            self.task.with_user(
                self.plain_user,
            ).action_mark_not_applicable()
        self.assertTrue(self.task.is_required)
        self.assertEqual(self.task.state, 'pending')

    def test_legitimate_action_still_transitions_state(self):
        """The guard must not break the sanctioned path: the action method
        flags its own write and the transition succeeds."""
        self.close_run.with_user(self.plain_user).action_start()
        self.assertEqual(self.close_run.state, 'in_progress')

    def test_context_cannot_forge_run_signoff_fields(self):
        """Client context is not proof that a sign-off came from workflow."""
        forged = self.close_run.with_user(self.plain_user).with_context(
            eh_workflow_action=True,
        )
        with self.assertRaises(AccessError):
            forged.write({
                'prepared_by_id': self.plain_user.id,
                'prepared_at': fields.Datetime.now(),
            })
        self.assertFalse(self.close_run.prepared_by_id)

    def test_run_cannot_be_created_with_signoff_anchors(self):
        with self.assertRaises(AccessError):
            self.env['eh.close.run'].with_user(self.plain_user).create({
                'name': 'Forged signed run',
                'checklist_id': self.checklist.id,
                'company_id': self.env.company.id,
                'period_from': date(2024, 2, 1),
                'period_to': date(2024, 2, 29),
                'prepared_by_id': self.plain_user.id,
                'prepared_at': fields.Datetime.now(),
            })

    def test_task_completion_anchors_are_server_owned(self):
        task = self.task.with_user(self.plain_user)
        with self.assertRaises(AccessError):
            task.with_context(eh_workflow_action=True).write({
                'completed_by_id': self.plain_user.id,
                'completed_at': fields.Datetime.now(),
            })

    def test_signoff_events_are_append_only_and_server_owned(self):
        self.close_run.with_user(self.plain_user).action_start()
        event = self.close_run.signoff_event_ids
        self.assertEqual(event.event_type, 'prepare')
        with self.assertRaises(AccessError):
            self.env['eh.close.signoff'].with_user(self.plain_user).create({
                'run_id': self.close_run.id,
                'event_type': 'approve',
                'user_id': self.plain_user.id,
                'event_at': fields.Datetime.now(),
            })
        with self.assertRaises(AccessError):
            event.with_user(self.plain_user).write({'event_type': 'approve'})
        with self.assertRaises(AccessError):
            event.with_user(self.plain_user).unlink()
        with self.assertRaises(AccessError):
            self.env['eh.close.task'].with_user(self.plain_user).create({
                'run_id': self.close_run.id,
                'name': 'Forged completion',
                'completed_by_id': self.plain_user.id,
                'completed_at': fields.Datetime.now(),
            })
