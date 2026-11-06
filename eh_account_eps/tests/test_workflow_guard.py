# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Workflow-guard regression: state on eh.eps.run may only change through the
record's own actions, never a direct RPC/ORM write (eh.workflow.guard)."""

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged('eh_account_eps', 'post_install', '-at_install')
class TestEpsWorkflowGuard(TransactionCase):

    def test_direct_state_write_refused(self):
        """A non-superuser cannot RPC past action_compute to mark a draft run
        computed (which would skip the dilution compute and the input freeze).
        """
        user = self.env['res.users'].create({
            'name': 'EPS Plain User',
            'login': 'eps_plain_user',
            'email': 'eps_plain_user@example.com',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        run = self.env['eh.eps.run'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'net_profit': 100000.0,
        })
        self.assertEqual(run.state, 'draft')
        # Actor has model write ACL; failure therefore proves the workflow
        # guard, not a generic access denial.
        run.with_user(user)._eh_check_access('write')
        with self.assertRaises(AccessError):
            run.with_user(user).write({'state': 'computed'})
        # And the draft state is untouched.
        self.assertEqual(run.state, 'draft')
