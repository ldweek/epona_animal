# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression: the deferred tax run state machine is enforced at the ORM
write layer, not merely in the UI.

Without eh.workflow.guard a plain user (group_eh_user has write access) could
RPC ``write({'state': 'posted'})`` straight past ``action_post`` and its
manager check, account validation and GL movement, leaving a run flagged
posted with no journal entry. The guard blocks any unflagged write to a
guarded field for a non-superuser; only the model's own action_* methods,
which carry the context flag, may move the state.
"""

from odoo.exceptions import AccessError
from odoo.tests import new_test_user, tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_deferred_tax', 'post_install', '-at_install')
class TestWorkflowGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A plain sub-ledger user: read/write/create on the run but NOT a
        # manager. This is the low-privilege interactive user the guard
        # protects against.
        cls.user = new_test_user(
            cls.env, login='eh_dtax_user',
            groups='eh_account_base.group_eh_user',
        )

    def _draft_run(self):
        return self.env['eh.deferred.tax.run'].create({
            'statutory_rate': 25.0,
            'period_end': '2026-12-31',
            'journal_id': self.journal_misc.id,
        })

    def test_direct_state_write_is_blocked(self):
        """A non-superuser cannot jump the state past the action methods."""
        run = self._draft_run()
        # The test env itself runs as superuser, for whom the guard is a
        # trusted no-op; the vector only exists for a real interactive user,
        # so the write MUST go through with_user.
        with self.assertRaises(AccessError):
            run.with_user(self.user).write({'state': 'posted'})
        # State is untouched: no phantom-posted run.
        self.assertEqual(run.state, 'draft')

    def test_action_transition_still_works(self):
        """The sanctioned action carries the flag and moves state normally."""
        run = self._draft_run()
        self.env['eh.deferred.tax.line'].create({
            'run_id': run.id, 'name': 'Plant', 'nature': 'asset',
            'carrying_amount': 1000.0, 'tax_base': 600.0,
        })
        run.action_compute()
        self.assertEqual(run.state, 'computed')
