# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression guard: the reconciliation session state machine cannot be
skipped by a direct RPC/ORM write.

eh.reconciliation.session (open -> closed) inherits eh.workflow.guard, so
'state' may only move through action_close (which runs as su); a direct
non-superuser ``write({'state': 'closed'})`` is refused. The test runner
env is SUPERUSER, which would sail past the guard, so the negative path
runs as a non-superuser manager (with_user) whose model ACL grants write,
proving the failure is the workflow guard and not an access-rights denial.
"""

from odoo.exceptions import AccessError
from odoo.tests import tagged

from .common import EhReconcileIntegrationTestCase


@tagged('eh_account_reconcile_pro', 'post_install', '-at_install')
class TestWorkflowGuard(EhReconcileIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.session = cls.env['eh.reconciliation.session'].open_or_create(
            cls.bank_journal.id)
        try:
            cls.user = cls.env['res.users'].create({
                'name': 'Guard Manager',
                'login': 'eh_reconcile_guard_manager',
                'company_id': cls.company.id,
                'company_ids': [(6, 0, cls.company.ids)],
                'group_ids': [(4, cls.env.ref('base.group_user').id),
                              (4, cls.env.ref(
                                  'eh_account_base.group_eh_manager').id)],
            })
        except Exception:  # noqa: BLE001
            cls.user = False

    def test_direct_state_write_blocked_for_normal_user(self):
        """A non-superuser cannot RPC past action_close into 'closed'."""
        if not self.user:
            self.skipTest("No non-superuser manager could be provisioned.")
        self.assertEqual(self.session.state, 'open')
        with self.assertRaises(AccessError):
            self.session.with_user(self.user).write({'state': 'closed'})
        self.assertEqual(self.session.state, 'open')

    def test_sudo_state_write_passes(self):
        """The sanctioned server path (su) writes state normally."""
        self.assertEqual(self.session.state, 'open')
        self.session.sudo().write({'state': 'closed'})
        self.assertEqual(self.session.state, 'closed')
