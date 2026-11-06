# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Workflow-guard regression tests.

The revenue state machines (a contract's draft -> active -> done/cancelled,
and a constraint review's draft -> applied) are enforced in the transition
actions, which post the balanced journal entries and run the completeness
checks. Without the eh.workflow.guard mixin a plain user with write access
could RPC ``write({'state': ...})`` straight past those actions, skipping the
checks and the journal entries entirely. These tests assert the guard refuses
such a direct write for a non-superuser.
"""

from unittest.mock import patch

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_revenue', 'post_install', '-at_install')
class TestRevenueWorkflowGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A plain accounting user: has write access to the model (perm_write=1
        # on group_eh_user) but is neither a manager nor the superuser, so it
        # is exactly the low-privilege RPC vector the guard closes. The test
        # environment itself runs as superuser, for which the guard correctly
        # does not fire, so every guarded write below is issued with_user().
        cls.clerk = cls.env['res.users'].create({
            'name': 'Revenue Clerk',
            'login': 'eh_rev_clerk',
            'email': 'eh_rev_clerk@example.com',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, cls.company.ids)],
            'group_ids': [
                (4, cls.env.ref('eh_account_base.group_eh_user').id)],
        })
        # Accounts a contract needs before revenue can be recognised.
        cls.contract_asset_acc = cls._ensure_account(
            cls.env, '1350', 'Contract Asset', 'asset_current')
        cls.contract_liab_acc = cls._ensure_account(
            cls.env, '2350', 'Contract Liability', 'liability_current')

    def test_contract_state_direct_write_blocked(self):
        contract = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'obligation_ids': [
                (0, 0, {'name': 'Licence', 'standalone_price': 1000.0})],
        })
        self.assertEqual(contract.state, 'draft')
        # A direct write to state, bypassing action_activate and its checks,
        # must be refused for the non-superuser clerk.
        with self.assertRaises(AccessError):
            contract.with_user(self.clerk).write({'state': 'active'})
        contract.invalidate_recordset(['state'])
        self.assertEqual(contract.state, 'draft')

    def test_contract_action_activate_still_works(self):
        # The sanctioned path (the action, which flags the guarded write) must
        # continue to work: the guard closes only the direct-write bypass.
        contract = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'obligation_ids': [
                (0, 0, {'name': 'Licence', 'standalone_price': 1000.0})],
        })
        contract.action_activate()
        self.assertEqual(contract.state, 'active')

    def test_constraint_review_state_direct_write_blocked(self):
        contract = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'obligation_ids': [(0, 0, {
                'name': 'Bonus service',
                'standalone_price': 1000.0,
                'variable_consideration': True,
                'variable_estimate': 100.0,
                'variable_constraint': 100.0,
            })],
        })
        review = self.env['eh.revenue.constraint.review'].create({
            'contract_id': contract.id,
            'obligation_id': contract.obligation_ids[0].id,
        })
        self.assertEqual(review.state, 'draft')
        # A direct write to state, bypassing action_apply (which writes the
        # revised estimate onto the obligation and posts the balanced
        # catch-up), must be refused for the non-superuser clerk.
        with self.assertRaises(AccessError):
            review.with_user(self.clerk).write({'state': 'applied'})
        review.invalidate_recordset(['state'])
        self.assertEqual(review.state, 'draft')

    def _recognised_contract(self):
        """An active contract with one satisfied point-in-time obligation
        whose full allocated price has been posted, so recognised_amount is
        the cumulative-posted anchor (1000). Built as the env user, which is
        granted the manager group here so the recognition run passes its
        _check_manager gate."""
        self.env.user.group_ids |= self.env.ref(
            'eh_account_base.group_eh_manager')
        contract = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'revenue_account_id': self.account_revenue.id,
            'contract_asset_account_id': self.contract_asset_acc.id,
            'contract_liability_account_id': self.contract_liab_acc.id,
            'receivable_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'obligation_ids': [(0, 0, {
                'name': 'Licence', 'standalone_price': 1000.0,
                'satisfaction': 'point_in_time', 'satisfied': True})],
        })
        contract.action_activate()
        contract.action_recognise()
        ob = contract.obligation_ids
        # The sanctioned recognition run advances the anchor (proves the
        # context-flagged internal write still works).
        self.assertEqual(ob.recognised_amount, 1000.0)
        return contract, ob

    def test_recognised_amount_direct_write_blocked(self):
        # recognised_amount is the cumulative-posted anchor the recognition
        # run trusts (to_recognise = target - recognised_amount). A non-manager
        # with model write access must not be able to RPC-write it directly:
        # zeroing it would silently make the whole allocated price recognisable
        # again and drive a double post on the next run.
        contract, ob = self._recognised_contract()
        with self.assertRaises(UserError):
            ob.with_user(self.clerk).write({'recognised_amount': 0.0})
        ob.invalidate_recordset(['recognised_amount', 'to_recognise'])
        self.assertEqual(ob.recognised_amount, 1000.0)
        # The anchor stayed put, so nothing is left to recognise.
        self.assertEqual(ob.to_recognise, 0.0)

    def test_recognised_amount_direct_write_blocked_upward(self):
        # Symmetric guard: inflating the anchor would overstate the IFRS 15
        # contract-asset/liability figure with no GL backing. The same refusal
        # applies whether the write moves the anchor up or down.
        contract, ob = self._recognised_contract()
        with self.assertRaises(UserError):
            ob.with_user(self.clerk).write({'recognised_amount': 5000.0})
        ob.invalidate_recordset(['recognised_amount'])
        self.assertEqual(ob.recognised_amount, 1000.0)

    def test_recognised_amount_forged_context_write_blocked(self):
        # THE hole this refinement closes. The previous guard trusted a
        # context flag (eh_revenue_recognition_run) to distinguish the
        # sanctioned recognition run from a direct write. Odoo passes
        # client-supplied context straight into call_kw, so any RPC client can
        # forge that flag. A non-manager clerk supplying the flag must STILL be
        # refused: provenance is now proven by env.su (the shared
        # eh.workflow.guard mixin), which a forged context key cannot fake.
        # Were the forged write to land it would zero the cumulative-posted
        # anchor and drive a full double post on the next recognition run.
        contract, ob = self._recognised_contract()
        with self.assertRaises(AccessError):
            ob.with_user(self.clerk).with_context(
                eh_revenue_recognition_run=True).write(
                {'recognised_amount': 0.0})
        ob.invalidate_recordset(['recognised_amount', 'to_recognise'])
        # Anchor untouched, so nothing is left to recognise: no double post.
        self.assertEqual(ob.recognised_amount, 1000.0)
        self.assertEqual(ob.to_recognise, 0.0)

    def test_recognition_run_advances_anchor_via_su(self):
        # The legitimate path must keep working: the contract's recognition
        # run advances the guarded anchor through _eh_workflow_write (env.su),
        # not a context flag. Recognise 50% then 100% of an over-time
        # obligation and assert the anchor tracks the posted target each run.
        self.env.user.group_ids |= self.env.ref(
            'eh_account_base.group_eh_manager')
        contract = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'revenue_account_id': self.account_revenue.id,
            'contract_asset_account_id': self.contract_asset_acc.id,
            'contract_liability_account_id': self.contract_liab_acc.id,
            'receivable_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'obligation_ids': [(0, 0, {
                'name': 'Service', 'standalone_price': 1000.0,
                'satisfaction': 'over_time', 'percent_complete': 50.0,
                'method_basis': (
                    'Test evidence: elapsed service depicts transfer.'
                )})],
        })
        contract.action_activate()
        contract.action_recognise()
        ob = contract.obligation_ids
        self.assertEqual(ob.recognised_amount, 500.0)
        # Advance progress and re-run: the anchor must move to the new target
        # through the sanctioned su-elevated write, proving the legitimate
        # recognition path is intact after removing the context-flag branch.
        ob.percent_complete = 100.0
        contract.action_recognise()
        ob.invalidate_recordset(['recognised_amount', 'to_recognise'])
        self.assertEqual(ob.recognised_amount, 1000.0)
        self.assertEqual(ob.to_recognise, 0.0)

    def test_contract_financial_anchors_are_engine_owned(self):
        contract, _ob = self._recognised_contract()
        for field_name in (
            'amount_billed', 'financing_interest_recognised',
            'close_released_amount', 'amount_recognised',
            'contract_asset', 'contract_liability',
        ):
            with self.subTest(field=field_name), self.assertRaises(AccessError):
                contract.with_user(self.clerk).write({field_name: 99999.0})

    def test_obligation_engine_outputs_are_not_rpc_writable(self):
        _contract, obligation = self._recognised_contract()
        for field_name in (
            'allocated_price', 'variable_included', 'target_recognised',
            'to_recognise', 'allocation_frozen', 'frozen_allocation',
            'company_id',
        ):
            value = self.company.id if field_name == 'company_id' else 1.0
            with self.subTest(field=field_name), self.assertRaises(AccessError):
                obligation.with_user(self.clerk).write({field_name: value})

    def test_constraint_review_snapshot_overwrites_client_values(self):
        contract = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'obligation_ids': [(0, 0, {
                'name': 'Variable service',
                'standalone_price': 1000.0,
                'variable_consideration': True,
                'variable_estimate': 125.0,
                'variable_constraint': 75.0,
            })],
        })
        review = self.env['eh.revenue.constraint.review'].create({
            'contract_id': contract.id,
            'obligation_id': contract.obligation_ids.id,
            'previous_estimate': 999999.0,
            'previous_constraint': 999999.0,
        })
        self.assertEqual(review.previous_estimate, 125.0)
        self.assertEqual(review.previous_constraint, 75.0)

    def test_modification_log_is_action_only_and_append_only(self):
        contract, _obligation = self._recognised_contract()
        Modification = self.env['eh.revenue.modification']
        with self.assertRaises(AccessError):
            Modification.create({
                'contract_id': contract.id,
                'method': 'catch_up',
                'price_before': 1.0,
                'price_after': 999999.0,
            })
        contract._apply_modification(
            'separate',
            added_obligations=[{
                'name': 'Added service', 'standalone_price': 100.0,
            }],
            new_transaction_price=100.0,
            description='Approved addition',
        )
        evidence = contract.modification_ids
        self.assertEqual(len(evidence), 1)
        with self.assertRaises(AccessError):
            evidence.write({'price_after': 999999.0})
        with self.assertRaises(AccessError):
            evidence.unlink()

    def test_move_provenance_cannot_be_repointed(self):
        contract, _obligation = self._recognised_contract()
        move = contract.move_ids[:1]
        with self.assertRaises(AccessError):
            move.write({'eh_revenue_contract_id': False})
        move.invalidate_recordset(['eh_revenue_contract_id'])
        self.assertEqual(move.eh_revenue_contract_id, contract)

    def test_financial_transitions_share_contract_row_lock(self):
        contract, _obligation = self._recognised_contract()
        contract.bill_amount = 100.0
        seen = []
        real_execute = self.env.cr.execute

        def _record_execute(*args, **kwargs):
            if args:
                seen.append(str(args[0]))
            return real_execute(*args, **kwargs)

        with patch.object(self.env.cr, 'execute', _record_execute):
            contract.action_bill()
        self.assertTrue(any(
            'eh_revenue_contract' in query and 'FOR UPDATE' in query
            for query in seen
        ))

    def test_plain_contract_write_shares_transition_lock(self):
        contract, _obligation = self._recognised_contract()
        seen = []
        real_execute = self.env.cr.execute

        def _record_execute(*args, **kwargs):
            if args:
                seen.append(str(args[0]))
            return real_execute(*args, **kwargs)

        with patch.object(self.env.cr, 'execute', _record_execute):
            contract.write({'notes': 'Serialized edit'})
        self.assertTrue(any(
            'eh_revenue_contract' in query and 'FOR UPDATE' in query
            for query in seen
        ))

    def test_constraint_review_identity_is_immutable(self):
        first = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'obligation_ids': [(0, 0, {
                'name': 'Variable one', 'standalone_price': 1000.0,
                'variable_consideration': True,
            })],
        })
        second = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 500.0,
            'obligation_ids': [(0, 0, {
                'name': 'Variable two', 'standalone_price': 500.0,
                'variable_consideration': True,
            })],
        })
        review = self.env['eh.revenue.constraint.review'].create({
            'contract_id': first.id,
            'obligation_id': first.obligation_ids.id,
        })
        with self.assertRaises(UserError):
            review.write({
                'contract_id': second.id,
                'obligation_id': second.obligation_ids.id,
            })
        review.invalidate_recordset(['contract_id', 'obligation_id'])
        self.assertEqual(review.contract_id, first)
        self.assertEqual(review.obligation_id, first.obligation_ids)

    def test_constraint_review_edits_share_parent_and_row_locks(self):
        contract = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'obligation_ids': [(0, 0, {
                'name': 'Variable service', 'standalone_price': 1000.0,
                'variable_consideration': True,
            })],
        })
        review = self.env['eh.revenue.constraint.review'].create({
            'contract_id': contract.id,
            'obligation_id': contract.obligation_ids.id,
        })
        seen = []
        real_execute = self.env.cr.execute

        def _record_execute(*args, **kwargs):
            if args:
                seen.append(str(args[0]))
            return real_execute(*args, **kwargs)

        with patch.object(self.env.cr, 'execute', _record_execute):
            review.write({'rationale': 'Current reporting evidence'})
        self.assertTrue(any(
            'eh_revenue_contract' in query and 'FOR UPDATE' in query
            for query in seen
        ))
        self.assertTrue(any(
            'eh_revenue_constraint_review' in query and 'FOR UPDATE' in query
            for query in seen
        ))

    def test_closure_evidence_freezes_after_close(self):
        contract, _obligation = self._recognised_contract()
        contract.bill_amount = 1000.0
        contract.action_bill()
        contract.action_close()
        with self.assertRaises(UserError):
            contract.with_user(self.clerk).write({
                'close_reason': 'Forged after close',
            })

    def test_raw_incompatible_posting_accounts_are_rejected(self):
        bad_asset = self._ensure_account(
            self.env, '5998', 'Wrong Revenue Asset Type', 'expense')
        with self.assertRaises(ValidationError):
            self.env['eh.revenue.contract'].create({
                'partner_id': self.partner_a.id,
                'transaction_price': 1000.0,
                'contract_asset_account_id': bad_asset.id,
                'obligation_ids': [(0, 0, {
                    'name': 'Raw RPC account injection',
                    'standalone_price': 1000.0,
                })],
            })

        contract = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'obligation_ids': [(0, 0, {
                'name': 'Advance financing', 'standalone_price': 1000.0,
            })],
        })
        bad_interest = self._ensure_account(
            self.env, '4998', 'Wrong Advance Interest Type', 'income_other')
        with self.assertRaises(ValidationError):
            contract.write({
                'financing_direction': 'advance',
                'financing_account_id': bad_interest.id,
            })
