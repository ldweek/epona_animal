# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression: the vendor-bill intake state machine is enforced by
eh.workflow.guard (sudo provenance), not merely by a readonly statusbar.

The intake advances received -> parsed -> matched -> posted through its own
actions (action_parse/action_match/action_post), which carry the manager
check, duplicate block and the sealed journal entry. A plain user must NOT be
able to skip that with a direct RPC ``write({'state': 'posted'})``. The guard
blocks any non-superuser write to 'state'; the sanctioned actions run under
sudo and are unaffected.

The test env runs as SUPERUSER, so the negative assertion is made through
``with_user(a plain user)`` - as the superuser the guard is (correctly) a
no-op.
"""

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged('eh_account_ap_automation', 'post_install', '-at_install')
class TestWorkflowGuard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A plain accounting user (group_eh_user): the model ACL grants
        # create/write on the intake and its lines, so a state/match_status
        # write reaching the ORM proves the WORKFLOW guard blocks it, not a
        # coarse access-rights denial, and the create-strip test can actually
        # create a record. It is NOT a manager and NOT superuser, so the guard
        # still fires. On Odoo 19 res.users uses group_ids (not groups_id).
        try:
            cls.user = cls.env['res.users'].create({
                'name': 'EH AP Plain User',
                'login': 'eh_ap_plain_user',
                'group_ids': [(6, 0, [
                    cls.env.ref('base.group_user').id,
                    cls.env.ref('eh_account_base.group_eh_user').id])],
            })
        except Exception:  # pragma: no cover - hardened env may forbid it
            cls.user = None

        cls.intake = cls.env['eh.ap.intake'].create({
            'raw_text': 'Invoice: INV-GUARD-001\nTotal: 100.00',
        })
        cls.line = cls.env['eh.ap.intake.line'].create({
            'intake_id': cls.intake.id,
            'invoice_qty': 1.0,
            'invoice_price': 10.0,
            'subtotal': 10.0,
        })

    def test_state_write_blocked_for_plain_user(self):
        """A non-superuser direct write of the guarded state must be refused."""
        if not self.user:
            self.skipTest("No plain user could be created in this environment.")
        self.assertEqual(self.intake.state, 'received')
        with self.assertRaises(AccessError):
            self.intake.with_user(self.user).write({'state': 'posted'})
        # Nothing moved: the intake is still received.
        self.intake.invalidate_recordset(['state'])
        self.assertEqual(self.intake.state, 'received')

    def test_line_match_status_write_blocked_for_plain_user(self):
        """A plain user cannot RPC-write a line's match_status past the
        manager-only override/reject gate."""
        if not self.user:
            self.skipTest("No plain user could be created in this environment.")
        self.assertEqual(self.line.match_status, 'pending')
        with self.assertRaises(AccessError):
            self.line.with_user(self.user).write({'match_status': 'overridden'})
        self.line.invalidate_recordset(['match_status'])
        self.assertEqual(self.line.match_status, 'pending')

    def test_create_in_guarded_state_stripped_for_plain_user(self):
        """A plain user cannot make an intake born in a posted state."""
        if not self.user:
            self.skipTest("No plain user could be created in this environment.")
        rec = self.env['eh.ap.intake'].with_user(self.user).create({
            'raw_text': 'Invoice: INV-GUARD-002\nTotal: 50.00',
            'state': 'posted',
        })
        # The guarded field is stripped; the model default applies.
        self.assertEqual(rec.state, 'received')

    def test_sanctioned_action_still_moves_state(self):
        """Positive path: the record's own action (run under sudo) advances
        state past the guard, proving the guard blocks only the RPC bypass."""
        self.intake.action_parse()
        self.assertEqual(self.intake.state, 'parsed')
