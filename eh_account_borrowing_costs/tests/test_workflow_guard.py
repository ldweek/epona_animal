# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Workflow-guard regression: state cannot be RPC-written past the action.

A state machine protected only by a readonly widget and a frozen-state write()
guard is not protected: a draft record's state is not frozen, so a plain user
could ``write({'state': 'capitalised'})`` straight past ``action_capitalise``
and its manager check and journal entry. The eh.workflow.guard mixin blocks
any direct write to a guarded field; only the record's own actions carry the
context flag that lets the write through.
"""

from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_borrowing_costs', 'post_install', '-at_install')
class TestBorrowingCostWorkflowGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A plain accounting user: read/write on the model, but NOT a manager
        # and NOT superuser. This is the RPC vector the guard closes.
        cls.plain_user = cls.env['res.users'].create({
            'name': 'Plain BC User',
            'login': 'plain_bc_user',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })

    def test_direct_state_write_is_blocked(self):
        rec = self.env['eh.borrowing.cost'].create({
            'name': '/', 'qualifying_asset': 'New plant',
            'specific_borrowing_cost': 1000.0,
        })
        self.assertEqual(rec.state, 'draft')
        # The test env is superuser, so the guard would (correctly) not fire;
        # exercise it as a genuine non-superuser interactive user instead.
        with self.assertRaises(AccessError):
            rec.with_user(self.plain_user).write({'state': 'capitalised'})
        # State is unchanged: the bypass did not take effect.
        self.assertEqual(rec.state, 'draft')

    def test_move_link_is_server_owned(self):
        rec = self.env['eh.borrowing.cost'].create({
            'name': '/', 'qualifying_asset': 'Evidence ownership test',
        })
        with self.assertRaises(AccessError):
            rec.with_user(self.plain_user).write({'move_id': 1})

    def test_child_edit_locks_parent_before_freeze_check(self):
        rec = self.env['eh.borrowing.cost'].create({
            'name': '/', 'qualifying_asset': 'Concurrent plant',
            'expenditure_line_ids': [(0, 0, {
                'date': '2026-01-01', 'amount': 100.0,
            })],
        })
        queries = []
        real_execute = self.env.cr.execute

        def _record_execute(*args, **kwargs):
            queries.append(str(args[0]))
            return real_execute(*args, **kwargs)

        with patch.object(self.env.cr, 'execute', _record_execute):
            rec.expenditure_line_ids.write({'amount': 125.0})
        self.assertTrue(any(
            'eh_borrowing_cost' in query and 'FOR UPDATE' in query
            for query in queries
        ))

    def test_non_manager_cannot_cancel_draft_period(self):
        rec = self.env['eh.borrowing.cost'].create({
            'name': '/', 'qualifying_asset': 'Cancel guard plant',
        })
        with self.assertRaises(AccessError):
            rec.with_user(self.plain_user).action_cancel()
        self.assertEqual(rec.state, 'draft')
