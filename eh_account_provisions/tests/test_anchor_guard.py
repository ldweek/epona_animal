# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Anchor-guard regression: the posted subledger figures of a provision
cannot be restated by a direct RPC/ORM write.

The systemic defect this closes: ``carrying_amount``, ``utilised_amount`` and
``reimbursement_recognised`` are posted IAS 37 anchors that only the record's
own actions may move (each posts the GL entry first). They were merely
``readonly`` on the view, which the ORM/RPC ``write()`` does not enforce, so a
``group_eh_user`` holder (perm_write on the model) could RPC
``write({'carrying_amount': 100000})`` and silently overstate the IAS 37.84
carrying amount, then a manager clicking Reverse would post an inflated
writeback. The shared ``eh.workflow.guard`` now guards these fields: every
write is refused unless it originates from an action (provenance proven by
env.su, not a forgeable context flag).

The second half proves the guard-retrofit did NOT strand the sanctioned
actions: a real (non-superuser) EH Accounting Manager can still run the full
lifecycle (recognise / unwind / remeasure / recognise-reimbursement /
utilise), because each action elevates through ``_eh_workflow_action``.
"""

from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_provisions', 'post_install', '-at_install')
class TestProvisionAnchorGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.provision_liab = cls._ensure_account(
            cls.env, '2900', 'Provisions', 'liability_current')
        cls.finance_cost = cls._ensure_account(
            cls.env, '5700', 'Finance Cost', 'expense')
        cls.reimb_asset = cls._ensure_account(
            cls.env, '1450', 'Reimbursement Receivable', 'asset_current')

        # A plain operational user: has read/write ACL on eh.provision
        # (group_eh_user) but is NOT the superuser, so the guard fires on a
        # direct write to a guarded anchor.
        cls.plain_user = cls.env['res.users'].create({
            'name': 'Provision Operator',
            'login': 'eh_provision_anchor_operator',
            'company_id': cls.env.company.id,
            'company_ids': [(6, 0, cls.env.company.ids)],
            'group_ids': [(6, 0, [
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        # A real (non-superuser) EH Accounting Manager, used to prove the
        # sanctioned actions still work after the anchors were guarded.
        cls.manager_user = cls.env['res.users'].create({
            'name': 'Provision Manager',
            'login': 'eh_provision_anchor_manager',
            'company_id': cls.env.company.id,
            'company_ids': [(6, 0, cls.env.company.ids)],
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_manager').id,
            ])],
        })

    def _recognised_provision(self, **vals):
        base = {
            'name': '/', 'classification': 'provision',
            'best_estimate': 1000.0,
            'discount_rate': 10.0, 'periods_to_settlement': 1,
            'provision_account_id': self.provision_liab.id,
            'expense_account_id': self.account_expense.id,
            'finance_cost_account_id': self.finance_cost.id,
            'settlement_account_id': self.account_cash.id,
            'reimbursement_account_id': self.reimb_asset.id,
            'journal_id': self.journal_misc.id,
            'action_accounting_date': '2026-01-01',
        }
        base.update(vals)
        provision = self.env['eh.provision'].create(base)
        provision.action_recognise()
        return provision

    # ------------------------------------------------------------------
    # the guard: a low-privilege user cannot restate a posted anchor
    # ------------------------------------------------------------------

    def test_direct_carrying_amount_write_refused(self):
        """A plain user cannot RPC-inflate the carrying amount, which would
        drive an inflated Reverse writeback."""
        provision = self._recognised_provision()
        # PV of 1000 over one period at 10% = 909.09.
        original = provision.carrying_amount
        self.assertAlmostEqual(original, 909.09, places=2)
        with self.assertRaises(AccessError):
            provision.with_user(self.plain_user).write(
                {'carrying_amount': 100000.0})
        provision.invalidate_recordset()
        self.assertAlmostEqual(provision.carrying_amount, original, places=2)

    def test_direct_utilised_amount_write_refused(self):
        provision = self._recognised_provision()
        with self.assertRaises(AccessError):
            provision.with_user(self.plain_user).write(
                {'utilised_amount': 5000.0})
        provision.invalidate_recordset()
        self.assertAlmostEqual(provision.utilised_amount, 0.0, places=2)

    def test_direct_reimbursement_recognised_write_refused(self):
        provision = self._recognised_provision()
        with self.assertRaises(AccessError):
            provision.with_user(self.plain_user).write(
                {'reimbursement_recognised': 5000.0})
        provision.invalidate_recordset()
        self.assertAlmostEqual(
            provision.reimbursement_recognised, 0.0, places=2)

    def test_currency_and_present_value_are_engine_owned_over_rpc(self):
        draft = self.env['eh.provision'].with_user(self.plain_user).create({
            'name': '/',
            'classification': 'provision',
            'best_estimate': 1000.0,
            'present_value': 999999.0,
            'currency_id': self.env.ref('base.EUR').id,
            'provision_account_id': self.provision_liab.id,
            'expense_account_id': self.account_expense.id,
            'journal_id': self.journal_misc.id,
        })
        self.assertEqual(draft.currency_id, self.company.currency_id)
        self.assertAlmostEqual(draft.present_value, 1000.0, places=2)
        with self.assertRaises(AccessError):
            draft.with_user(self.plain_user).write({
                'currency_id': self.env.ref('base.EUR').id,
                'present_value': 250000.0,
            })
        draft.invalidate_recordset(['currency_id', 'present_value'])
        self.assertEqual(draft.currency_id, self.company.currency_id)
        self.assertAlmostEqual(draft.present_value, 1000.0, places=2)

    def test_unwind_schedule_anchors_are_engine_owned(self):
        provision = self._recognised_provision()
        original = (
            provision.recognition_date,
            provision.last_unwind_date,
            provision.unwound_periods,
        )
        with self.assertRaises(AccessError):
            provision.with_user(self.plain_user).write({
                'recognition_date': '2000-01-01',
                'last_unwind_date': '2099-12-31',
                'unwound_periods': 99,
            })
        provision.invalidate_recordset()
        self.assertEqual(
            (provision.recognition_date, provision.last_unwind_date,
             provision.unwound_periods),
            original,
        )

    def test_posting_accounts_and_journal_freeze_after_recognition(self):
        provision = self._recognised_provision()
        with self.assertRaises(UserError):
            provision.with_user(self.plain_user).write({
                'provision_account_id': self.account_payable.id,
            })
        with self.assertRaises(UserError):
            provision.with_user(self.plain_user).write({
                'journal_id': self.journal_purchase.id,
            })

    def test_posted_move_provenance_cannot_be_repointed(self):
        provision = self._recognised_provision()
        move = provision.move_ids[:1]
        self.assertTrue(move)
        with self.assertRaises(AccessError):
            move.with_user(self.manager_user).write({
                'eh_provision_id': False,
            })
        move.invalidate_recordset(['eh_provision_id'])
        self.assertEqual(move.eh_provision_id, provision)

    def test_every_transition_takes_shared_row_lock(self):
        provision = self._recognised_provision()
        seen = []
        real_execute = self.env.cr.execute

        def _record_execute(*args, **kwargs):
            if args:
                seen.append(str(args[0]))
            return real_execute(*args, **kwargs)

        provision.write({
            'remeasure_estimate': 900.0,
            'remeasure_discount_rate': 10.0,
            'remeasure_rate_basis': 'risk_free_govt',
            'remeasure_rate_source': 'Current transition-lock test curve',
        })
        with patch.object(self.env.cr, 'execute', _record_execute):
            provision.action_remeasure()
        self.assertTrue(any(
            'eh_provision' in query and 'FOR UPDATE' in query
            for query in seen
        ))

    def test_parent_writes_and_component_crud_share_transition_lock(self):
        provision = self.env['eh.provision'].create({
            'name': '/',
            'classification': 'provision',
            'provision_type': 'restructuring',
            'best_estimate': 1000.0,
            'provision_account_id': self.provision_liab.id,
            'expense_account_id': self.account_expense.id,
            'journal_id': self.journal_misc.id,
        })
        seen = []
        real_execute = self.env.cr.execute

        def _record_execute(*args, **kwargs):
            if args:
                seen.append(str(args[0]))
            return real_execute(*args, **kwargs)

        Line = self.env['eh.provision.restructuring.line']
        with patch.object(self.env.cr, 'execute', _record_execute):
            provision.write({'rate_source': 'Board-approved memo'})
            line = Line.create({
                'provision_id': provision.id,
                'name': 'Direct termination cost',
                'cost_kind': 'termination',
                'amount': 1000.0,
            })
            line.write({'name': 'Updated direct termination cost'})
            line.unlink()
        lock_queries = [
            query for query in seen
            if 'eh_provision' in query and 'FOR UPDATE' in query
        ]
        self.assertGreaterEqual(len(lock_queries), 4)

    def test_direct_anchor_write_on_draft_also_refused(self):
        """The guard is provenance-based, not state-based: a plain user has no
        business setting the posted anchor even on a draft."""
        draft = self.env['eh.provision'].create({
            'name': '/', 'classification': 'provision',
            'best_estimate': 1000.0,
            'provision_account_id': self.provision_liab.id,
            'expense_account_id': self.account_expense.id,
            'journal_id': self.journal_misc.id,
        })
        with self.assertRaises(AccessError):
            draft.with_user(self.plain_user).write(
                {'carrying_amount': 42000.0})
        draft.invalidate_recordset()
        self.assertAlmostEqual(draft.carrying_amount, 0.0, places=2)

    # ------------------------------------------------------------------
    # the elevation: the sanctioned actions still work for a real manager
    # ------------------------------------------------------------------

    def test_actions_still_work_for_non_superuser_manager(self):
        """A non-superuser EH Accounting Manager can run every action that
        writes a guarded anchor: the guard-retrofit did not strand them (each
        action elevates through _eh_workflow_action)."""
        provision = self.env['eh.provision'].with_user(
            self.manager_user).create({
                'name': '/', 'classification': 'provision',
                'best_estimate': 1000.0,
                'discount_rate': 10.0, 'periods_to_settlement': 1,
                'expected_settlement_date': '2026-02-01',
                'provision_account_id': self.provision_liab.id,
                'expense_account_id': self.account_expense.id,
                'finance_cost_account_id': self.finance_cost.id,
                'settlement_account_id': self.account_cash.id,
                'reimbursement_account_id': self.reimb_asset.id,
                'journal_id': self.journal_misc.id,
                'action_accounting_date': '2026-01-01',
            })

        # recognise -> carrying anchor written under the action's elevation.
        provision.action_recognise()
        self.assertEqual(provision.state, 'recognised')
        self.assertAlmostEqual(provision.carrying_amount, 909.09, places=2)

        # unwind -> carrying anchor moves once the pinned period boundary is
        # due (909.09 * 10% = 90.91, capped at the undiscounted 1000).
        provision.action_accounting_date = '2026-02-01'
        provision.action_unwind()
        self.assertAlmostEqual(provision.carrying_amount, 1000.0, places=2)

        # remeasure -> carrying + best_estimate anchors move (no discounting
        # left: periods 1 already unwound).
        provision.remeasure_estimate = 600.0
        provision.action_remeasure()
        self.assertAlmostEqual(provision.carrying_amount, 600.0, places=2)

        # recognise a reimbursement -> reimbursement_recognised anchor moves.
        provision.reimbursement_amount = 400.0
        provision.action_recognise_reimbursement()
        self.assertAlmostEqual(
            provision.reimbursement_recognised, 400.0, places=2)

        # utilise -> carrying + utilised anchors move; settles to nil.
        provision.utilise_amount = 600.0
        provision.action_utilise()
        self.assertAlmostEqual(provision.carrying_amount, 0.0, places=2)
        self.assertAlmostEqual(provision.utilised_amount, 600.0, places=2)
        self.assertEqual(provision.state, 'settled')
