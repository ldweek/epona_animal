# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IFRS 13 fair value tests."""

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_fair_value', 'integration', 'post_install', '-at_install')
class TestFairValue(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.fv_asset = cls._ensure_account(
            cls.env, '1600', 'Investments at FV', 'asset_current')
        cls.fv_gain = cls._ensure_account(
            cls.env, '4600', 'Fair Value Gain/Loss', 'income_other')
        cls.fv_oci = cls._ensure_account(
            cls.env, '3600', 'FVOCI Reserve', 'equity')
        cls.fv_liability = cls._ensure_account(
            cls.env, '2600', 'Liabilities at FV', 'liability_current')

    def _item(self, **vals):
        base = {
            'name': '/', 'nature': 'financial_asset', 'level': '1',
            'prior_carrying': 1000.0, 'fair_value': 1000.0, 'routing': 'pl',
            'balance_sheet_account_id': self.fv_asset.id,
            'gain_loss_account_id': self.fv_gain.id,
            'oci_account_id': self.fv_oci.id,
            'journal_id': self.journal_misc.id,
        }
        base.update(vals)
        return self.env['eh.fair.value.item'].create(base)

    def _bal(self, account):
        lines = self.env['account.move.line'].search([
            ('account_id', '=', account.id), ('parent_state', '=', 'posted')])
        return sum(lines.mapped('debit')) - sum(lines.mapped('credit'))

    def test_remeasurement_computed(self):
        item = self._item(prior_carrying=1000.0, fair_value=1200.0)
        self.assertAlmostEqual(item.remeasurement, 200.0, places=2)

    def test_gain_to_pl(self):
        item = self._item(prior_carrying=1000.0, fair_value=1200.0,
                          routing='pl')
        item.action_remeasure()
        self.assertEqual(item.state, 'measured')
        self.assertAlmostEqual(self._bal(self.fv_asset), 200.0, places=2)
        self.assertAlmostEqual(self._bal(self.fv_gain), -200.0, places=2)
        # Carrying rolled forward; a second remeasure at the same value is nil.
        self.assertAlmostEqual(item.prior_carrying, 1200.0, places=2)
        with self.assertRaises(UserError):
            item.action_remeasure()

    def test_loss_to_pl(self):
        item = self._item(prior_carrying=1000.0, fair_value=800.0)
        item.action_remeasure()
        self.assertAlmostEqual(self._bal(self.fv_asset), -200.0, places=2)
        self.assertAlmostEqual(self._bal(self.fv_gain), 200.0, places=2)

    def test_gain_to_oci(self):
        item = self._item(prior_carrying=1000.0, fair_value=1300.0,
                          routing='oci')
        item.action_remeasure()
        self.assertAlmostEqual(self._bal(self.fv_asset), 300.0, places=2)
        self.assertAlmostEqual(self._bal(self.fv_oci), -300.0, places=2)
        self.assertFalse(self._bal(self.fv_gain))

    def test_incremental_remeasurement(self):
        item = self._item(prior_carrying=1000.0, fair_value=1200.0)
        item.action_remeasure()
        # A measured item is frozen; reopen it before recording a new value.
        item.action_reset_to_draft()
        item.fair_value = 1500.0
        item.action_remeasure()
        # Only the 300 increment posts the second time.
        self.assertAlmostEqual(self._bal(self.fv_asset), 500.0, places=2)

    def test_liability_rise_is_loss(self):
        # A financial liability whose fair value rises: the liability
        # increases (Cr balance sheet) and the movement is a loss (Dr P&L).
        item = self._item(
            nature='financial_liability',
            prior_carrying=1000.0, fair_value=1200.0, routing='pl',
            balance_sheet_account_id=self.fv_liability.id)
        item.action_remeasure()
        self.assertEqual(item.state, 'measured')
        # Balance-sheet account is credited (liability up): net balance -200.
        self.assertAlmostEqual(self._bal(self.fv_liability), -200.0, places=2)
        # Gain/Loss account is debited (a loss): net balance +200.
        self.assertAlmostEqual(self._bal(self.fv_gain), 200.0, places=2)

    def test_liability_fall_is_gain(self):
        # A financial liability whose fair value falls: the liability
        # decreases (Dr balance sheet) and the movement is a gain (Cr P&L).
        item = self._item(
            nature='financial_liability',
            prior_carrying=1000.0, fair_value=800.0, routing='pl',
            balance_sheet_account_id=self.fv_liability.id)
        item.action_remeasure()
        # Balance-sheet account is debited (liability down): net balance +200.
        self.assertAlmostEqual(self._bal(self.fv_liability), 200.0, places=2)
        # Gain/Loss account is credited (a gain): net balance -200.
        self.assertAlmostEqual(self._bal(self.fv_gain), -200.0, places=2)

    def test_liability_entry_balances(self):
        item = self._item(
            nature='financial_liability',
            prior_carrying=1000.0, fair_value=1200.0,
            balance_sheet_account_id=self.fv_liability.id)
        item.action_remeasure()
        for move in item.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_measured_item_is_frozen(self):
        item = self._item(prior_carrying=1000.0, fair_value=1200.0)
        item.action_remeasure()
        self.assertEqual(item.state, 'measured')
        for vals in ({'fair_value': 1500.0}, {'prior_carrying': 500.0},
                     {'routing': 'oci'}, {'nature': 'financial_liability'},
                     {'balance_sheet_account_id': self.fv_gain.id},
                     {'gain_loss_account_id': self.fv_asset.id}):
            with self.assertRaises(UserError):
                item.write(vals)

    def test_reset_to_draft_requires_manager(self):
        item = self._item(prior_carrying=1000.0, fair_value=1200.0)
        item.action_remeasure()
        user = self.env['res.users'].create({
            'name': 'p2', 'login': 'fv_plain2@test',
            'email': 'fv_plain2@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            item.with_user(user).action_reset_to_draft()

    def test_entries_balance(self):
        item = self._item(prior_carrying=1000.0, fair_value=1200.0)
        item.action_remeasure()
        for move in item.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_remeasure_requires_manager(self):
        item = self._item(prior_carrying=1000.0, fair_value=1200.0)
        user = self.env['res.users'].create({
            'name': 'p', 'login': 'fv_plain@test', 'email': 'fv_plain@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            item.with_user(user).action_remeasure()

    def test_cancel_requires_manager(self):
        item = self._item(prior_carrying=1000.0, fair_value=1200.0)
        item.action_remeasure()
        self.assertEqual(item.state, 'measured')
        user = self.env['res.users'].create({
            'name': 'p3', 'login': 'fv_plain3@test',
            'email': 'fv_plain3@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            item.with_user(user).action_cancel()
        # State is unchanged: a non-manager could not cancel it.
        self.assertEqual(item.state, 'measured')

    def test_cancel_draft_item_without_accounting_history(self):
        item = self._item(prior_carrying=1000.0, fair_value=1200.0)
        self.assertFalse(item.move_ids)
        item.action_cancel()
        self.assertEqual(item.state, 'cancelled')

    def test_cancel_measured_item_with_posted_history_is_blocked(self):
        item = self._item(prior_carrying=1000.0, fair_value=1200.0)
        item.action_remeasure()
        posted_moves = item.move_ids
        self.assertTrue(posted_moves)
        self.assertTrue(all(move.state == 'posted' for move in posted_moves))

        with self.assertRaisesRegex(UserError, 'journal-entry history'):
            item.action_cancel()

        self.assertEqual(item.state, 'measured')
        self.assertEqual(item.move_ids, posted_moves)

    def test_cancel_reset_draft_item_with_history_is_blocked(self):
        item = self._item(prior_carrying=1000.0, fair_value=1200.0)
        item.action_remeasure()
        item.action_reset_to_draft()
        self.assertEqual(item.state, 'draft')
        self.assertTrue(item.move_ids)

        with self.assertRaisesRegex(UserError, 'journal-entry history'):
            item.action_cancel()

        self.assertEqual(item.state, 'draft')

    def test_level3_inputs_recorded(self):
        item = self._item(level='3',
                          unobservable_inputs='DCF, WACC 12%, growth 2%')
        self.assertEqual(item.level, '3')
        self.assertIn('WACC', item.unobservable_inputs)

    def test_rollforward_closing_balance(self):
        # Opening 1000 + PL gain 150 + OCI gain 50 + purchases 400
        # + issues 20 - sales 300 - settlements 30 + transfers in 200
        # - transfers out 90 = 1400.
        item = self._item(level='3', fair_value=1400.0)
        rf = self.env['eh.fair.value.rollforward'].create({
            'item_id': item.id,
            'opening_balance': 1000.0,
            'gains_losses_in_pl': 150.0,
            'gains_losses_in_oci': 50.0,
            'purchases': 400.0,
            'issues': 20.0,
            'sales': 300.0,
            'settlements': 30.0,
            'transfers_into_level3': 200.0,
            'transfers_out_of_level3': 90.0,
        })
        self.assertAlmostEqual(rf.closing_balance, 1400.0, places=2)
        self.assertTrue(item.ties_to_fair_value)

    def test_rollforward_does_not_tie(self):
        item = self._item(level='3', fair_value=999.0)
        self.env['eh.fair.value.rollforward'].create({
            'item_id': item.id, 'opening_balance': 1000.0,
        })
        # closing_balance = 1000 != fair_value 999.
        self.assertFalse(item.ties_to_fair_value)

    def test_ties_uses_latest_period(self):
        item = self._item(level='3', fair_value=1200.0)
        self.env['eh.fair.value.rollforward'].create({
            'item_id': item.id,
            'period_start': '2025-01-01', 'period_end': '2025-12-31',
            'opening_balance': 800.0, 'gains_losses_in_pl': 100.0,
        })
        self.env['eh.fair.value.rollforward'].create({
            'item_id': item.id,
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'opening_balance': 900.0, 'gains_losses_in_pl': 300.0,
        })
        # Latest period closing = 1200 == fair_value.
        self.assertTrue(item.ties_to_fair_value)

    def test_no_rollforward_does_not_tie(self):
        item = self._item(level='3', fair_value=1000.0)
        self.assertFalse(item.ties_to_fair_value)

    # ---- IFRS 9.4.1.2A / 9.5.7.5 ----

    def test_pl_balance_sheet_account_rejected(self):
        # The position leg must be a balance-sheet account; a P&L account
        # (here the income gain/loss account) is rejected by the constraint.
        with self.assertRaises(UserError):
            self._item(balance_sheet_account_id=self.fv_gain.id)

    def test_fvoci_debt_cannot_recycle_before_derecognition(self):
        # Reserve settlement is atomic with disposal, never a standalone
        # posting while instrument remains recognised.
        item = self._item(
            prior_carrying=1000.0, fair_value=1300.0, routing='oci',
            fvoci_classification='fvoci_debt')
        item.action_remeasure()
        self.assertAlmostEqual(self._bal(self.fv_oci), -300.0, places=2)
        self.assertAlmostEqual(item.oci_reserve_balance, -300.0, places=2)
        with self.assertRaisesRegex(UserError, 'part of Derecognise'):
            item.action_recycle()
        self.assertFalse(item.recycled)
        self.assertEqual(item.state, 'measured')
        self.assertAlmostEqual(self._bal(self.fv_oci), -300.0, places=2)
        self.assertAlmostEqual(self._bal(self.fv_gain), 0.0, places=2)

    def test_fvoci_equity_transfer_waits_for_derecognition(self):
        equity_dest = self._ensure_account(
            self.env, '3601', 'Retained Reserve', 'equity')
        item = self._item(
            prior_carrying=1000.0, fair_value=1300.0, routing='oci',
            fvoci_classification='fvoci_equity',
            gain_loss_account_id=equity_dest.id)
        item.action_remeasure()
        self.assertAlmostEqual(self._bal(self.fv_oci), -300.0, places=2)
        gain_before = self._bal(self.fv_gain)
        with self.assertRaisesRegex(UserError, 'part of Derecognise'):
            item.action_recycle()
        self.assertFalse(item.recycled)
        self.assertAlmostEqual(self._bal(self.fv_oci), -300.0, places=2)
        self.assertAlmostEqual(self._bal(equity_dest), 0.0, places=2)
        self.assertAlmostEqual(self._bal(self.fv_gain), gain_before, places=2)

    def test_reset_keeps_instrument_identity_and_routing_frozen(self):
        item = self._item(
            prior_carrying=1000.0,
            fair_value=1200.0,
            routing='oci',
            fvoci_classification='fvoci_debt',
        )
        item.action_remeasure()
        item.action_reset_to_draft()
        for vals in (
            {'nature': 'financial_liability'},
            {'fvoci_classification': 'fvtpl'},
            {'routing': 'pl'},
            {'balance_sheet_account_id': self.fv_liability.id},
            {'gain_loss_account_id': self.fv_asset.id},
            {'prior_carrying': 1.0},
        ):
            with self.assertRaisesRegex(UserError, 'posted history'):
                item.write(vals)
        item.write({'fair_value': 1300.0})
        item.action_remeasure()
        self.assertAlmostEqual(item.prior_carrying, 1300.0, places=2)

    def test_recycle_requires_manager(self):
        item = self._item(
            prior_carrying=1000.0, fair_value=1300.0, routing='oci',
            fvoci_classification='fvoci_debt')
        item.action_remeasure()
        user = self.env['res.users'].create({
            'name': 'p4', 'login': 'fv_plain4@test',
            'email': 'fv_plain4@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            item.with_user(user).action_recycle()

    def test_fvtpl_has_nothing_to_recycle(self):
        item = self._item(
            prior_carrying=1000.0, fair_value=1300.0, routing='pl',
            fvoci_classification='fvtpl')
        item.action_remeasure()
        with self.assertRaises(UserError):
            item.action_recycle()

    def test_measured_item_frozen_and_undeletable_flow_intact(self):
        # (a) a measured item's measurement input is frozen at the ORM write
        # layer; (b) it cannot be unlinked (its posted GL move would be
        # orphaned); (c) the normal remeasure flow still works.
        item = self._item(prior_carrying=1000.0, fair_value=1200.0)
        # (c) remeasure posts and moves to measured.
        item.action_remeasure()
        self.assertEqual(item.state, 'measured')
        # (a) the fair value input is frozen once measured.
        with self.assertRaises(UserError):
            item.write({'fair_value': 1500.0})
        # (b) a measured item with a posted move cannot be unlinked.
        with self.assertRaises(UserError):
            item.unlink()
        # A never-posted draft item stays deletable.
        draft = self._item(prior_carrying=100.0, fair_value=100.0)
        draft.unlink()
