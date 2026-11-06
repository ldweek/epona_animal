# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.workflow.guard retrofit probe for the year-end closing run.

The state machine is enforced in the UI (readonly statusbar + header buttons)
and the write() figure-freeze, but a *draft* run's state is not frozen. Without
the workflow guard a plain user could RPC ``write({'state': 'posted'})`` straight
past ``action_post`` and its journal entry, marking the year closed with no GL
closing move behind it. These probes assert eh.workflow.guard blocks that:

* A plain (non-superuser) user cannot direct-write a draft run's state to
  'posted' (or 'computed'); it raises AccessError.
* The sanctioned action path still transitions state normally (proving the
  guard flags legitimate action writes rather than blocking everything).
"""

from datetime import date

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_year_end', 'post_install', '-at_install')
class TestYearEndWorkflowGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Run = cls.env['eh.year.end.run']
        cls.retained_earnings = cls._ensure_account(
            cls.env, '3100', 'Retained Earnings', 'equity',
        )
        # A plain accountant: enough to read/write the model through the normal
        # record rules, but NOT a manager and NOT superuser, so the guard's
        # "not env.su and no action flag" branch is the one under test.
        cls.plain_user = cls.env['res.users'].create({
            'name': 'Plain Accountant',
            'login': 'plain_ye_guard_user',
            'email': 'plain_ye_guard@example.com',
            'group_ids': [(6, 0, [
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })

    def _draft_run(self):
        return self.Run.create({
            'fiscal_year_start': date(2026, 1, 1),
            'fiscal_year_end': date(2026, 12, 31),
            'company_id': self.env.company.id,
            'journal_id': self.journal_misc.id,
            'retained_earnings_account_id': self.retained_earnings.id,
        })

    def test_plain_user_cannot_rpc_state_to_posted(self):
        """The core bypass: skip action_post + its journal entry by writing
        state directly. eh.workflow.guard must refuse it."""
        run = self._draft_run()
        self.assertEqual(run.state, 'draft')
        with self.assertRaises(AccessError):
            run.with_user(self.plain_user).write({'state': 'posted'})
        with self.assertRaises(AccessError):
            run.with_user(self.plain_user).with_context(
                eh_year_end_state_change=True,
            ).write({'state': 'posted'})
        # State unchanged; no closing move was ever built.
        self.assertEqual(run.state, 'draft')
        self.assertFalse(run.move_id)

    def test_plain_user_cannot_rpc_state_to_computed(self):
        """Even the intermediate transition cannot be forged: it would let a
        user reach 'Post' without the breakdown lines action_compute builds."""
        run = self._draft_run()
        with self.assertRaises(AccessError):
            run.with_user(self.plain_user).write({'state': 'computed'})
        self.assertEqual(run.state, 'draft')

    def test_sanctioned_action_still_transitions_state(self):
        """The guard flags legitimate action writes, so the normal button path
        still moves state (guard is a gate, not a wall)."""
        run = self._draft_run()
        run.action_compute()
        self.assertEqual(run.state, 'computed')
