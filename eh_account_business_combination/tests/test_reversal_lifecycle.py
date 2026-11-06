# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Controlled reversal regressions for IFRS 3 and IAS 28 sealed entries."""

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_account_business_combination', 'post_install', '-at_install')
class TestBusinessCombinationReversalLifecycle(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.goodwill = cls._ensure_account(
            cls.env, '1861', 'Reversal Goodwill', 'asset_non_current')
        cls.clearing = cls._ensure_account(
            cls.env, '1862', 'Reversal Acquisition Clearing', 'asset_current')
        cls.gain = cls._ensure_account(
            cls.env, '4861', 'Reversal Bargain Gain', 'income_other')
        cls.ppe = cls._ensure_account(
            cls.env, '1661', 'Reversal PPA Asset', 'asset_fixed')
        cls.payable = cls._ensure_account(
            cls.env, '2661', 'Reversal PPA Liability', 'liability_current')
        cls.contingent = cls._ensure_account(
            cls.env, '2861', 'Reversal Contingent Liability',
            'liability_non_current')
        cls.contingent_pnl = cls._ensure_account(
            cls.env, '5861', 'Reversal Contingent P&L', 'expense')
        cls.investment = cls._ensure_account(
            cls.env, '1863', 'Reversal Associate Investment',
            'asset_non_current')
        cls.share_profit = cls._ensure_account(
            cls.env, '4862', 'Reversal Share of Profit', 'income_other')
        cls.impairment = cls._ensure_account(
            cls.env, '5862', 'Reversal Associate Impairment', 'expense')
        cls.disposal = cls._ensure_account(
            cls.env, '4863', 'Reversal Disposal Gain', 'income_other')

    def _combination(self):
        return self.env['eh.business.combination'].create({
            'name': '/',
            'acquiree_name': 'Reversal Target',
            'consideration_transferred': 900.0,
            'nci_amount': 0.0,
            'goodwill_account_id': self.goodwill.id,
            'clearing_account_id': self.clearing.id,
            'gain_account_id': self.gain.id,
            'journal_id': self.journal_misc.id,
            'contingent_consideration_initial_fv': 100.0,
            'contingent_classification': 'liability',
            'contingent_account_id': self.contingent.id,
            'contingent_pnl_account_id': self.contingent_pnl.id,
            'asset_line_ids': [
                (0, 0, {
                    'name': 'PPE',
                    'account_id': self.ppe.id,
                    'fair_value': 1200.0,
                }),
                (0, 0, {
                    'name': 'Payable',
                    'account_id': self.payable.id,
                    'fair_value': 300.0,
                    'is_liability': True,
                }),
            ],
        })

    def _assert_verified_reversal(self, original, reversal):
        self.assertEqual(original.state, 'posted')
        self.assertTrue(original.eh_sealed)
        self.assertEqual(reversal.state, 'posted')
        self.assertTrue(reversal.eh_sealed)
        self.assertEqual(reversal.reversed_entry_id, original)
        self.assertBalanced(original)
        self.assertBalanced(reversal)

    def test_initial_recognition_reverses_only_from_owner(self):
        combination = self._combination()
        combination.action_recognise_ppa()
        original = combination.move_id
        with self.assertRaises(UserError):
            original._reverse_moves(cancel=False)

        combination.action_reverse(accounting_date=str(original.date))
        self.assertEqual(combination.state, 'reversed')
        self._assert_verified_reversal(original, combination.reversal_move_id)
        self.assertEqual(
            combination.reversal_move_id
            .eh_business_combination_reversal_id,
            combination,
        )
        self.assertAlmostEqual(self.posted_balance(self.goodwill), 0.0, places=2)
        self.assertAlmostEqual(self.posted_balance(self.ppe), 0.0, places=2)
        self.assertAlmostEqual(self.posted_balance(self.payable), 0.0, places=2)
        with self.assertRaises(UserError):
            combination.action_reverse()
        with self.assertRaises(UserError):
            combination.action_cancel()
        with self.assertRaises(UserError):
            combination.unlink()

    def test_downstream_entries_reverse_lifo_then_acquisition(self):
        combination = self._combination()
        combination.action_recognise_ppa()
        ppe_line = combination.asset_line_ids.filtered(
            lambda line: not line.is_liability)

        adjustment_1 = self.env['eh.bizcombo.adjustment'].create({
            'combination_id': combination.id,
            'name': 'First appraisal',
            'line_ids': [(0, 0, {
                'asset_line_id': ppe_line.id,
                'revised_fair_value': 1250.0,
            })],
        })
        adjustment_1.action_apply()
        adjustment_2 = self.env['eh.bizcombo.adjustment'].create({
            'combination_id': combination.id,
            'name': 'Final appraisal',
            'line_ids': [(0, 0, {
                'asset_line_id': ppe_line.id,
                'revised_fair_value': 1270.0,
            })],
        })
        adjustment_2.action_apply()

        remeasure_1 = self.env['eh.bizcombo.contingent.remeasure'].create({
            'combination_id': combination.id,
            'new_fair_value': 140.0,
        })
        remeasure_1.action_apply()
        remeasure_2 = self.env['eh.bizcombo.contingent.remeasure'].create({
            'combination_id': combination.id,
            'new_fair_value': 125.0,
        })
        remeasure_2.action_apply()

        with self.assertRaises(UserError):
            combination.action_reverse()
        with self.assertRaises(UserError):
            adjustment_1.action_reverse()
        with self.assertRaises(UserError):
            remeasure_1.action_reverse()

        adjustment_2.action_reverse(
            accounting_date=str(adjustment_2.move_id.date))
        self.assertAlmostEqual(ppe_line.fair_value, 1250.0, places=2)
        adjustment_1.action_reverse(
            accounting_date=str(adjustment_1.move_id.date))
        self.assertAlmostEqual(ppe_line.fair_value, 1200.0, places=2)
        self.assertEqual(
            adjustment_1.reversal_move_id
            .eh_bizcombo_adjustment_reversal_id,
            adjustment_1,
        )

        remeasure_2.action_reverse(
            accounting_date=str(remeasure_2.move_id.date))
        self.assertAlmostEqual(
            combination.contingent_consideration_current_fv, 140.0, places=2)
        remeasure_1.action_reverse(
            accounting_date=str(remeasure_1.move_id.date))
        self.assertAlmostEqual(
            combination.contingent_consideration_current_fv, 100.0, places=2)
        self.assertEqual(
            remeasure_1.reversal_move_id.eh_bizcombo_remeasure_reversal_id,
            remeasure_1,
        )

        combination.action_reverse(
            accounting_date=str(combination.move_id.date))
        self.assertEqual(combination.state, 'reversed')
        self.assertAlmostEqual(self.posted_balance(self.goodwill), 0.0, places=2)
        self.assertAlmostEqual(self.posted_balance(self.ppe), 0.0, places=2)
        self.assertAlmostEqual(self.posted_balance(self.payable), 0.0, places=2)
        self.assertAlmostEqual(self.posted_balance(self.contingent), 0.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.contingent_pnl), 0.0, places=2)

    def test_equity_investment_reverses_all_entries_before_cancel(self):
        investment = self.env['eh.equity.investment'].create({
            'name': '/',
            'investee_name': 'Reversal Associate',
            'ownership_pct': 25.0,
            'cost_of_investment': 1000.0,
            'investment_account_id': self.investment.id,
            'share_of_profit_account_id': self.share_profit.id,
            'cash_account_id': self.account_cash.id,
            'impairment_account_id': self.impairment.id,
            'disposal_gain_loss_account_id': self.disposal.id,
            'journal_id': self.journal_misc.id,
        })
        investment.action_activate()
        investment.investee_profit = 400.0
        investment.action_pickup_profit()
        investment.dividend_received = 50.0
        investment.action_record_dividend()
        originals = investment.move_ids
        with self.assertRaises(UserError):
            originals[:1]._reverse_moves(cancel=False)

        latest_date = max(originals.mapped('date'))
        investment.action_reverse_and_cancel(accounting_date=str(latest_date))
        self.assertEqual(investment.state, 'cancelled')
        self.assertAlmostEqual(investment.carrying_amount, 0.0, places=2)
        self.assertAlmostEqual(
            investment.cumulative_share_of_profit, 0.0, places=2)
        self.assertAlmostEqual(
            investment.cumulative_dividends, 0.0, places=2)
        self.assertEqual(len(investment.reversal_move_ids), len(originals))
        for reversal in investment.reversal_move_ids:
            self.assertTrue(reversal.eh_sealed)
            self.assertTrue(reversal.reversed_entry_id in originals)
            self.assertEqual(
                reversal.eh_equity_investment_reversal_id, investment)
        self.assertAlmostEqual(
            self.posted_balance(self.investment), 0.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.share_profit), 0.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.account_cash), 0.0, places=2)
        with self.assertRaises(UserError):
            investment.unlink()
