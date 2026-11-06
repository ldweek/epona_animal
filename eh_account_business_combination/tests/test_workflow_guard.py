# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Workflow-guard regression: a plain user cannot RPC-write state past the
posting actions and their journal entries.

The state machines here are enforced by eh.workflow.guard, whose write() guard
refuses a direct write to a guarded field unless it originates from a
server-initiated (sudo) action. The bypass being closed: a draft record's state
is not frozen, so without the guard any user could write({'state': 'posted'})
straight past action_* and its sealed journal entry. Provenance is proven by
env.su, not a forgeable context key, so even a privileged (manager) user who is
not the superuser is blocked from writing state directly; only the action's own
sudo transition succeeds.
"""

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_business_combination', 'post_install', '-at_install')
class TestWorkflowGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A plain (non-superuser) user, granted the manager group so it clears
        # model access and record rules and the assertion isolates the
        # workflow guard rather than an ACL denial. On Odoo 19 res.users uses
        # group_ids (not groups_id).
        try:
            groups = cls.env.ref('base.group_user')
            manager = cls.env.ref('eh_account_base.group_eh_manager')
            cls.user = cls.env['res.users'].create({
                'name': 'EH Plain User',
                'login': 'eh_bc_wf_guard_user',
                'group_ids': [(6, 0, (groups | manager).ids)],
                'company_id': cls.company.id,
                'company_ids': [(6, 0, cls.company.ids)],
            })
        except Exception:  # noqa: BLE001 - environment may forbid user create
            cls.user = None

    def _skip_if_no_user(self):
        if not self.user:
            self.skipTest("Could not create a non-superuser test user.")

    def test_combination_state_write_blocked(self):
        self._skip_if_no_user()
        rec = self.env['eh.business.combination'].create({
            'acquiree_name': 'Target Co',
            'company_id': self.company.id,
        })
        self.assertEqual(rec.state, 'draft')
        with self.assertRaises(AccessError):
            rec.with_user(self.user).write({'state': 'recognised'})
        # The server-initiated (sudo) path is what the actions use; it passes.
        rec.sudo().write({'state': 'recognised'})
        self.assertEqual(rec.state, 'recognised')

    def test_combination_measurement_close_flag_blocked(self):
        self._skip_if_no_user()
        rec = self.env['eh.business.combination'].create({
            'acquiree_name': 'Target Co 2',
            'company_id': self.company.id,
        })
        with self.assertRaises(AccessError):
            rec.with_user(self.user).write(
                {'measurement_period_closed': True})

    def test_equity_investment_state_write_blocked(self):
        self._skip_if_no_user()
        rec = self.env['eh.equity.investment'].create({
            'investee_name': 'Associate Co',
            'ownership_pct': 25.0,
            'company_id': self.company.id,
        })
        self.assertEqual(rec.state, 'draft')
        with self.assertRaises(AccessError):
            rec.with_user(self.user).write({'state': 'active'})

    def test_adjustment_state_write_blocked(self):
        self._skip_if_no_user()
        combo = self.env['eh.business.combination'].create({
            'acquiree_name': 'Target Co 3',
            'company_id': self.company.id,
        })
        adj = self.env['eh.bizcombo.adjustment'].create({
            'combination_id': combo.id,
            'name': 'New information',
        })
        self.assertEqual(adj.state, 'draft')
        with self.assertRaises(AccessError):
            adj.with_user(self.user).write({'state': 'applied'})

    def test_contingent_remeasure_state_write_blocked(self):
        self._skip_if_no_user()
        combo = self.env['eh.business.combination'].create({
            'acquiree_name': 'Target Co 4',
            'company_id': self.company.id,
            'contingent_classification': 'liability',
        })
        rem = self.env['eh.bizcombo.contingent.remeasure'].create({
            'combination_id': combo.id,
            'new_fair_value': 1000.0,
        })
        self.assertEqual(rem.state, 'draft')
        with self.assertRaises(AccessError):
            rem.with_user(self.user).write({'state': 'applied'})

    def test_shared_lock_discards_stale_subsequent_state(self):
        """A parent-lock wait must not leave a cached draft child actionable.

        Raw SQL emulates the committed state another worker would expose after
        this worker acquired the shared combination lock.
        """
        combo = self.env['eh.business.combination'].create({
            'acquiree_name': 'Locked Target',
            'company_id': self.company.id,
            'contingent_classification': 'liability',
        })
        adjustment = self.env['eh.bizcombo.adjustment'].create({
            'combination_id': combo.id,
            'name': 'Already applied by another worker',
        })
        remeasure = self.env['eh.bizcombo.contingent.remeasure'].create({
            'combination_id': combo.id,
            'new_fair_value': 1000.0,
        })
        # Prime both ORM caches with draft before simulating the other worker.
        self.assertEqual(adjustment.state, 'draft')
        self.assertEqual(remeasure.state, 'draft')
        self.env.cr.execute(
            "UPDATE eh_bizcombo_adjustment SET state = 'applied' WHERE id = %s",
            [adjustment.id],
        )
        self.env.cr.execute(
            "UPDATE eh_bizcombo_contingent_remeasure SET state = 'applied' "
            "WHERE id = %s",
            [remeasure.id],
        )
        with self.assertRaises(UserError):
            adjustment.action_cancel()
        with self.assertRaises(UserError):
            remeasure.action_cancel()

    def test_create_in_guarded_state_stripped(self):
        """A non-superuser create cannot make a record born recognised: the
        guarded state field is stripped so the model default applies."""
        self._skip_if_no_user()
        rec = self.env['eh.business.combination'].with_user(self.user).create({
            'acquiree_name': 'Target Co 5',
            'company_id': self.company.id,
            'state': 'recognised',
        })
        self.assertEqual(rec.state, 'draft')
