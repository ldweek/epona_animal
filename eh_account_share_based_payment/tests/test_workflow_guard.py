# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Workflow-guard regression: state is a state machine, not a free field.

Both eh.sbp.plan and eh.sbp.period.run drive their state only through
actions that post journal entries (Activate/Settle/Cancel; Compute/Post).
The eh.workflow.guard mixin blocks a low-privilege user from RPC-writing
state directly, which would otherwise skip the action and its entry.

The default test environment runs as SUPERUSER (env.su is True), for which
the mixin deliberately abstains (trusted code); the bypass this closes is
an interactive, low-privilege user, so the guarded write must be attempted
with_user(a normal user) for the guard to fire.
"""

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_share_based_payment', 'post_install', '-at_install')
class TestWorkflowGuard(EhAccountIntegrationTestCase):
    """A direct RPC write to a guarded state field is refused for a
    low-privilege user; the sanctioned action path (context-flagged) is
    still allowed."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A normal internal user with write access to the models (EH user
        # group grants read/write/create) but NOT superuser, so the mixin
        # guard is exercised rather than the ACL layer.
        eh_user = cls.env.ref('eh_account_base.group_eh_user')
        try:
            cls.normal_user = cls.env['res.users'].with_context(
                mail_create_nosubscribe=True,
                no_reset_password=True,
            ).create({
                'name': 'SBP Guard Tester',
                'login': 'sbp_guard_tester',
                'group_ids': [(6, 0, [eh_user.id])],
            })
        except Exception as exc:  # pragma: no cover - env cascade quirk
            cls.normal_user = None
            cls._user_error = exc

    def _require_user(self):
        if not self.normal_user:
            self.skipTest(
                "environment cannot create a test user: %s"
                % getattr(self, '_user_error', 'unknown'))

    def test_plan_state_write_refused_for_low_privilege_user(self):
        self._require_user()
        plan = self.env['eh.sbp.plan'].create({'name': '/'})
        self.assertEqual(plan.state, 'draft')
        # Direct RPC re-key of the state machine is blocked by the mixin.
        with self.assertRaises(AccessError):
            plan.with_user(self.normal_user).write({'state': 'active'})
        # A server-authorized sudo transition is still allowed; there is no
        # forgeable context flag in the workflow provenance contract.
        plan.with_user(self.normal_user).sudo().write({'state': 'active'})
        self.assertEqual(plan.state, 'active',
                         "a server-authorized action write must still work")

    def test_run_state_write_refused_for_low_privilege_user(self):
        self._require_user()
        plan = self.env['eh.sbp.plan'].create({'name': '/'})
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id,
            'period_end': '2027-01-01',
        })
        self.assertEqual(run.state, 'draft')
        with self.assertRaises(AccessError):
            run.with_user(self.normal_user).write({'state': 'posted'})
