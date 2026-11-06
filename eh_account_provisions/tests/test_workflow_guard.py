# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Workflow-guard regression: the provision state machine cannot be driven
by a direct RPC/ORM write.

The systemic defect this closes: a state machine enforced only in the UI (a
readonly statusbar) plus a write() guard that blocks LEAVING a frozen state
is not protected, because a draft's state is not frozen. A low-privilege user
could RPC ``write({'state': 'recognised'})`` straight past ``action_recognise``
and the journal entry it posts. The shared ``eh.workflow.guard`` blocks every
write to the guarded ``state`` field unless it originates from one of the
record's own actions.
"""

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_provisions', 'post_install', '-at_install')
class TestProvisionWorkflowGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.provision_liab = cls._ensure_account(
            cls.env, '2900', 'Provisions', 'liability_current')
        # A non-manager operational user: has read/write ACL on eh.provision
        # (group_eh_user) but is NOT the superuser, so the guard fires.
        cls.plain_user = cls.env['res.users'].create({
            'name': 'Provision Operator',
            'login': 'eh_provision_operator',
            'company_id': cls.env.company.id,
            'company_ids': [(6, 0, cls.env.company.ids)],
            'group_ids': [(6, 0, [
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })

    def _draft_provision(self):
        return self.env['eh.provision'].create({
            'name': '/', 'classification': 'provision',
            'best_estimate': 1000.0,
            'provision_account_id': self.provision_liab.id,
            'expense_account_id': self.account_expense.id,
            'journal_id': self.journal_misc.id,
            'action_accounting_date': '2026-01-01',
        })

    def test_direct_state_write_refused_for_plain_user(self):
        """A plain user cannot RPC the draft straight to 'recognised',
        skipping action_recognise and its journal entry."""
        provision = self._draft_provision()
        with self.assertRaises(AccessError):
            provision.with_user(self.plain_user).write({'state': 'recognised'})
        # State is untouched; no posting was skipped.
        self.assertEqual(provision.state, 'draft')

    def test_direct_state_write_to_settled_refused(self):
        """Relabelling to a terminal state directly is refused too."""
        provision = self._draft_provision()
        with self.assertRaises(AccessError):
            provision.with_user(self.plain_user).write({'state': 'settled'})
        self.assertEqual(provision.state, 'draft')

    def test_action_path_still_works(self):
        """The sanctioned action still moves the state (guard is scoped to
        direct writes, not the record's own methods)."""
        provision = self._draft_provision()
        provision.action_recognise()
        self.assertEqual(provision.state, 'recognised')
