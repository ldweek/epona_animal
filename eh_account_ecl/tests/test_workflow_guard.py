# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression: state is a guarded field, not writable by a direct RPC.

A draft ECL run / write-off must not be advanced to a posted state by a plain
user writing ``{'state': 'posted'}`` straight past the action and its journal
entry. eh.workflow.guard refuses any non-superuser write to a guarded field;
only the record's own actions (which run as su) may move state.
"""

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_ecl', 'post_install', '-at_install')
class TestWorkflowGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.impairment = cls._ensure_account(
            cls.env, '5290', 'Impairment Loss', 'expense')
        cls.allowance = cls._ensure_account(
            cls.env, '1290', 'Loss Allowance', 'asset_current')
        cls.plain_user = cls.env['res.users'].create({
            'name': 'ECL Plain User',
            'login': 'ecl_plain_user',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_manager').id,
            ])],
        })

    def _run(self):
        return self.env['eh.ecl.run'].create({
            'reporting_date': '2026-06-30',
            'opening_allowance': 0.0,
            'impairment_expense_account_id': self.impairment.id,
            'loss_allowance_account_id': self.allowance.id,
            'journal_id': self.journal_misc.id,
            'bucket_ids': [(0, 0, {
                'name': '90+', 'days_from': 91, 'days_to': 0,
                'loss_rate': 25.0, 'stage': '3'})],
        })

    def test_run_state_rpc_write_blocked(self):
        """A non-superuser cannot RPC a draft run straight to posted."""
        if not self.plain_user:
            self.skipTest("No non-superuser user available in this env.")
        rec = self._run()
        self.assertEqual(rec.state, 'draft')
        with self.assertRaises(AccessError):
            rec.with_user(self.plain_user).write({'state': 'posted'})
        with self.assertRaises(AccessError):
            rec.with_user(self.plain_user).write({
                'zero_movement_posted': True,
            })
        # The sanctioned action path (which runs as su) still works.
        rec.bucket_ids.gross_carrying = 1000.0
        rec.action_compute()
        self.assertEqual(rec.state, 'computed')

    def test_writeoff_state_rpc_write_blocked(self):
        """A non-superuser cannot RPC a draft write-off straight to posted."""
        if not self.plain_user:
            self.skipTest("No non-superuser user available in this env.")
        rec = self._run()
        rec.bucket_ids.gross_carrying = 1000.0
        rec.action_compute()
        rec.action_post()
        line = self.env['account.move.line'].search([
            ('account_id.account_type', '=', 'asset_receivable'),
            ('parent_state', '=', 'posted'),
            ('reconciled', '=', False),
        ], limit=1)
        if not line:
            self.skipTest("No open receivable line to write off in this env.")
        writeoff = self.env['eh.ecl.writeoff'].create({
            'run_id': rec.id,
            'move_line_id': line.id,
            'amount': 10.0,
            'stage': '3',
        })
        self.assertEqual(writeoff.state, 'draft')
        with self.assertRaises(AccessError):
            writeoff.with_user(self.plain_user).write({'state': 'posted'})
