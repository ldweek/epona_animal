# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Workflow-guard regression: state is a state machine, not a free field.

Both eh.accounting.change (IAS 8) and eh.subsequent.event (IAS 10) drive
their state only through actions that post a journal entry
(action_post_restatement / action_book_adjusting_entry) and unwind it
through action_reset_to_draft. The inherited eh.workflow.guard mixin blocks
a low-privilege user from RPC-writing state directly, which would otherwise
skip the action and its journal entry.

The default test environment runs as SUPERUSER (env.su is True), for which
the mixin deliberately abstains (trusted code); the bypass this closes is an
interactive, low-privilege user, so the guarded write must be attempted
with_user(a normal user) for the guard to fire.
"""

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_events', 'post_install', '-at_install')
class TestWorkflowGuard(EhAccountIntegrationTestCase):
    """A direct RPC write to a guarded state field is refused for a
    low-privilege user; the sanctioned sudo/action path is still allowed."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A normal internal user (EH user group grants read/write/create) but
        # NOT superuser, so the mixin guard is exercised rather than the ACL
        # layer. On Odoo 19 res.users uses group_ids (not groups_id).
        eh_user = cls.env.ref('eh_account_base.group_eh_user')
        try:
            cls.normal_user = cls.env['res.users'].with_context(
                mail_create_nosubscribe=True,
                no_reset_password=True,
            ).create({
                'name': 'Events Guard Tester',
                'login': 'events_guard_tester',
                'group_ids': [(6, 0, [eh_user.id])],
            })
        except Exception as exc:  # pragma: no cover - env cascade quirk
            cls.normal_user = None
            cls._user_error = exc

    def _require_user(self):
        if not self.normal_user:
            self.skipTest(
                "environment cannot create a test user: %s"
                % getattr(self, '_user_error', 'unknown'))

    def test_accounting_change_state_write_refused(self):
        self._require_user()
        rec = self.env['eh.accounting.change'].create({
            'change_type': 'error_correction'})
        self.assertEqual(rec.state, 'draft')
        # Direct RPC re-key of the state machine is blocked by the mixin: this
        # would otherwise skip action_post_restatement and its GL entry.
        with self.assertRaises(AccessError):
            rec.with_user(self.normal_user).write({'state': 'posted'})
        self.assertEqual(rec.state, 'draft',
                         "the blocked write must not have taken effect")
        # The sanctioned server path (sudo) still goes through.
        rec.sudo().write({'state': 'posted'})
        self.assertEqual(rec.state, 'posted',
                         "a sanctioned action (sudo) write must still go through")

    def test_subsequent_event_state_write_refused(self):
        self._require_user()
        doc = self.env['eh.subsequent.event'].create({
            'name': 'Court settlement',
            'reporting_date': '2026-12-31',
            'event_date': '2027-01-20',
            'is_adjusting': True,
            'category': 'litigation',
            'estimated_effect': 50000.0,
        })
        self.assertEqual(doc.state, 'draft')
        with self.assertRaises(AccessError):
            doc.with_user(self.normal_user).write({'state': 'posted'})
        self.assertEqual(doc.state, 'draft',
                         "the blocked write must not have taken effect")
