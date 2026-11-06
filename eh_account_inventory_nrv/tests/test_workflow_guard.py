# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""eh.workflow.guard regression: a plain user cannot RPC-write the run's
state straight past action_post and its journal entry."""

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_inventory_nrv', 'post_install', '-at_install')
class TestNrvWorkflowGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.writedown_expense = cls._ensure_account(
            cls.env, '5150', 'Inventory Write-down', 'expense')
        cls.allowance = cls._ensure_account(
            cls.env, '1490', 'Inventory Write-down Allowance', 'asset_current')
        cls.plain_user = cls.env['res.users'].create({
            'name': 'NRV plain', 'login': 'nrv_guard@test',
            'email': 'nrv_guard@test',
            'group_ids': [(6, 0, [
                cls.env.ref('eh_account_base.group_eh_user').id])]})

    def _draft_run(self):
        return self.env['eh.nrv.run'].create({
            'reporting_date': '2026-06-30',
            'writedown_expense_account_id': self.writedown_expense.id,
            'allowance_account_id': self.allowance.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [(0, 0, {
                'name': 'A', 'cost': 1000.0,
                'net_realisable_value': 700.0})],
        })

    def test_plain_user_cannot_jump_state_to_posted(self):
        """A draft run's state is not frozen by the input-freeze, so the old
        write() guard let it through. The workflow guard blocks a direct write
        to state from a non-superuser: the only way to reach 'posted' is
        action_post, which enforces the manager gate, account validation and
        the journal entry."""
        run = self._draft_run()
        self.assertEqual(run.state, 'draft')
        with self.assertRaises(AccessError):
            run.with_user(self.plain_user).write({'state': 'posted'})
        with self.assertRaises(AccessError):
            run.with_user(self.plain_user).with_context(
                eh_nrv_state_change=True,
            ).write({'state': 'posted'})
        # State did not move and no move was booked.
        run.invalidate_recordset(['state', 'move_id'])
        self.assertEqual(run.state, 'draft')
        self.assertFalse(run.move_id)

    def test_sanctioned_action_still_flags_the_write(self):
        """The guard must not break the legitimate flow: action_compute /
        action_post carry the workflow-action flag, so the state advances."""
        run = self._draft_run()
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        run.action_post()
        self.assertEqual(run.state, 'posted')
        self.assertTrue(run.move_id)
