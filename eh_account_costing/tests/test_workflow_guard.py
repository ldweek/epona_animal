# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression for the eh.workflow.guard retrofit on eh_account_costing.

The state machine of each costing document (cost card, variance run,
contribution report) was enforced only in the UI (readonly statusbar) plus a
write() guard that froze records once they LEFT draft. That left the
bypass open on a DRAFT record: a plain user could RPC
``write({'state': 'posted'})`` straight past the record's own action and its
sealed journal entry. The eh.workflow.guard mixin blocks any direct write to
a guarded field; only the record's own flagged actions may change state.

The test env runs as SUPERUSER, for which the guard deliberately does not
fire, so every guarded write here is attempted through ``with_user`` a plain
internal user.
"""

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged('eh_account_costing', 'post_install', '-at_install')
class TestWorkflowGuard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # group_eh_user grants legitimate read/write/create on the costing
        # models, so the AccessError below comes from the workflow guard
        # (a blocked state write), not from a missing model ACL.
        cls.user = cls.env['res.users'].create({
            'name': 'Costing Clerk',
            'login': 'eh_costing_clerk',
            'email': 'clerk@example.com',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })

    def test_direct_state_write_on_cost_card_refused(self):
        """A non-superuser cannot RPC a draft card straight to 'active'."""
        card = self.env['eh.cost.card'].create({
            'item_name': 'Guarded Widget',
            'line_ids': [(0, 0, {
                'element': 'material', 'std_qty': 1.0, 'std_price': 1.0})],
        })
        self.assertEqual(card.state, 'draft')
        with self.assertRaises(AccessError):
            card.with_user(self.user).write({'state': 'active'})
        # The record's own action still works (carries the flag).
        card.action_activate()
        self.assertEqual(card.state, 'active')

    def test_direct_state_write_on_variance_run_refused(self):
        """A non-superuser cannot RPC a draft run straight to 'posted',
        skipping action_post and its sealed journal entry."""
        run = self.env['eh.cost.variance.run'].create({
            'period_start': '2026-01-01',
            'period_end': '2026-01-31',
        })
        self.assertEqual(run.state, 'draft')
        with self.assertRaises(AccessError):
            run.with_user(self.user).write({'state': 'posted'})

    def test_direct_state_write_on_contribution_report_refused(self):
        """A non-superuser cannot RPC a draft report straight to 'done'."""
        report = self.env['eh.contribution.report'].create({
            'period_start': '2026-01-01',
            'period_end': '2026-01-31',
        })
        self.assertEqual(report.state, 'draft')
        with self.assertRaises(AccessError):
            report.with_user(self.user).write({'state': 'done'})
