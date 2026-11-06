# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Workflow-guard regression: a plain user cannot RPC-write the pack state.

The audit-pack period close (draft -> checks_run -> signed_off) may advance
only through action_run_checks / action_sign_off, which run their guarded
writes under sudo. A direct write of 'state' by a non-superuser must be
refused by the inherited eh.workflow.guard, so a user cannot jump straight to
'signed_off' and skip the integrity scan, segregation-of-duties check and
lock-date advance.
"""

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged('eh_account_audit_pack', 'post_install', '-at_install')
class TestWorkflowGuard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.doc = cls.env['eh.audit.pack'].create({
            'period_from': '2026-01-01', 'period_to': '2026-12-31'})

    def _plain_user(self):
        """A non-superuser internal user with write access to the pack.

        Uses the module's own user group so ACL grants write; the guard, not
        the ACL, is what must block the state write. Skips gracefully if the
        user cannot be created in this environment.
        """
        try:
            group = self.env.ref('eh_account_base.group_eh_user')
            return self.env['res.users'].create({
                'name': 'Plain Guard User',
                'login': 'audit_guard_plain@test',
                'email': 'audit_guard_plain@test',
                'group_ids': [(6, 0, [
                    group.id,
                    self.env.ref('base.group_user').id])]})
        except Exception:  # noqa: BLE001 - environment cannot host the user
            return None

    def test_plain_user_cannot_write_state(self):
        user = self._plain_user()
        if user is None:
            self.skipTest("cannot create a non-superuser in this environment")
        self.assertEqual(self.doc.state, 'draft')
        # env.su is False for this user, so the inherited guard refuses the
        # direct write of the guarded 'state' field.
        with self.assertRaises(AccessError):
            self.doc.with_user(user).write({'state': 'signed_off'})
        self.doc.invalidate_recordset(['state'])
        self.assertEqual(self.doc.state, 'draft')

    def test_action_still_advances_state(self):
        # Positive path: the sanctioned action (which runs under sudo) still
        # advances the state normally.
        pack = self.env['eh.audit.pack'].create({
            'period_from': '2025-01-01', 'period_to': '2025-12-31'})
        pack.action_run_checks()
        self.assertEqual(pack.state, 'checks_run')
