# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IAS 37 provisions tests."""

from datetime import date

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

try:  # Odoo 18+ re-exports freeze_time; 16/17 pull it from freezegun directly
    from odoo.tests import freeze_time
except ImportError:  # pragma: no cover - version shim
    from freezegun import freeze_time

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_provisions', 'integration', 'post_install', '-at_install')
class TestProvision(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.provision_liab = cls._ensure_account(
            cls.env, '2900', 'Provisions', 'liability_current')
        cls.finance_cost = cls._ensure_account(
            cls.env, '5700', 'Finance Cost', 'expense')
        cls.reimbursement_asset = cls._ensure_account(
            cls.env, '1915', 'Provision Reimbursement Asset',
            'asset_current')

    def _provision(self, **vals):
        base = {
            'name': '/', 'classification': 'provision',
            'best_estimate': 1000.0,
            'provision_account_id': self.provision_liab.id,
            'expense_account_id': self.account_expense.id,
            'finance_cost_account_id': self.finance_cost.id,
            'settlement_account_id': self.account_cash.id,
            'reimbursement_account_id': self.reimbursement_asset.id,
            'journal_id': self.journal_misc.id,
            'action_accounting_date': '2026-01-01',
        }
        base.update(vals)
        return self.env['eh.provision'].create(base)

    def _bal(self, account):
        lines = self.env['account.move.line'].search([
            ('account_id', '=', account.id), ('parent_state', '=', 'posted')])
        return sum(lines.mapped('debit')) - sum(lines.mapped('credit'))

    def test_present_value_discounting(self):
        p = self._provision(best_estimate=1000.0, discount_rate=10.0,
                            periods_to_settlement=2)
        # 1000 / 1.1^2 = 826.45.
        self.assertAlmostEqual(p.present_value, 826.45, places=2)

    def test_posting_account_types_are_enforced_server_side(self):
        wrong_accounts = (
            ('provision_account_id', self.account_expense),
            ('expense_account_id', self.provision_liab),
            ('finance_cost_account_id', self.account_revenue),
            ('settlement_account_id', self.account_equity),
            ('asset_account_id', self.account_expense),
            ('income_account_id', self.account_cash),
            ('reimbursement_account_id', self.provision_liab),
        )
        for field_name, account in wrong_accounts:
            with self.subTest(field=field_name), self.assertRaises(
                    ValidationError):
                self._provision(**{field_name: account.id})

    def test_recognise_undiscounted(self):
        p = self._provision(best_estimate=1000.0)
        p.action_recognise()
        self.assertEqual(p.state, 'recognised')
        self.assertAlmostEqual(p.carrying_amount, 1000.0, places=2)
        self.assertAlmostEqual(self._bal(self.account_expense), 1000.0,
                               places=2)
        self.assertAlmostEqual(self._bal(self.provision_liab), -1000.0,
                               places=2)

    def test_posting_requires_explicit_date_and_preserves_it_exactly(self):
        p = self._provision(action_accounting_date=False)
        with self.assertRaises(UserError):
            p.action_recognise()
        self.assertFalse(p.move_ids)
        p.action_recognise('2026-03-31')
        self.assertEqual(p.recognition_date, date(2026, 3, 31))
        self.assertEqual(p.move_ids.date, date(2026, 3, 31))

    def test_unwind_catchup_posts_one_exact_move_per_boundary(self):
        p = self._provision(
            best_estimate=1000.0,
            discount_rate=10.0,
            periods_to_settlement=3,
            expected_settlement_date='2028-01-01',
        )
        with freeze_time('2025-01-01'):
            p.action_recognise('2025-01-01')
        recognition = p.move_ids
        with freeze_time('2027-01-01'):
            p.action_unwind('2027-01-01')
        unwind_moves = p.move_ids - recognition
        self.assertEqual(len(unwind_moves), 2)
        self.assertEqual(
            sorted(unwind_moves.mapped('date')),
            [date(2026, 1, 1), date(2027, 1, 1)],
        )
        self.assertEqual(p.unwound_periods, 2)
        self.assertEqual(p.last_unwind_date, date(2027, 1, 1))
        for move in unwind_moves:
            finance_line = move.line_ids.filtered(
                lambda line: line.account_id == self.finance_cost)
            self.assertIn('period ', finance_line.name)
            self.assertIn(fields.Date.to_string(move.date), finance_line.name)

    def test_locked_date_fails_before_provision_anchor_moves(self):
        if 'fiscalyear_lock_date' not in self.company._fields:
            self.skipTest("fiscalyear_lock_date not available")
        p = self._provision(action_accounting_date='2026-03-31')
        self.env.cr.execute(
            "UPDATE res_company SET fiscalyear_lock_date = %s WHERE id = %s",
            ['2026-03-31', self.company.id],
        )
        self.company.invalidate_recordset(['fiscalyear_lock_date'])
        with self.assertRaises(UserError):
            p.action_recognise()
        self.assertEqual(p.state, 'draft')
        self.assertFalse(p.move_ids)
        self.assertAlmostEqual(p.carrying_amount, 0.0, places=2)

    def test_future_date_cannot_defer_move_while_updating_anchor(self):
        p = self._provision(action_accounting_date='2099-12-31')
        with self.assertRaises(UserError):
            p.action_recognise()
        self.assertEqual(p.state, 'draft')
        self.assertFalse(p.move_ids)
        self.assertAlmostEqual(p.carrying_amount, 0.0, places=2)

    def test_unwind_discount(self):
        p = self._provision(
            best_estimate=1000.0,
            discount_rate=10.0,
            periods_to_settlement=1,
            action_accounting_date='2024-01-01',
        )
        p.action_recognise()  # PV = 909.09
        self.assertAlmostEqual(p.carrying_amount, 909.09, places=2)
        # A legacy schedule without a settlement date uses annual
        # anniversaries; it cannot dump the whole discount on day one.
        p.action_accounting_date = '2024-01-02'
        with self.assertRaises(UserError):
            p.action_unwind()
        p.action_accounting_date = '2025-01-01'
        p.action_unwind()  # 909.09 x 10% = 90.91
        self.assertAlmostEqual(p.carrying_amount, 1000.0, places=2)
        self.assertAlmostEqual(self._bal(self.finance_cost), 90.91, places=2)
        # Fully unwound to the undiscounted estimate: no more to unwind.
        with self.assertRaises(UserError):
            p.action_unwind()

    def test_deferred_tax_hook_uses_ledger_balance_as_of_date(self):
        p = self._provision(
            best_estimate=1000.0,
            discount_rate=10.0,
            periods_to_settlement=1,
        )
        with freeze_time('2024-01-01'):
            p.action_accounting_date = '2024-01-01'
            p.action_recognise()
        with freeze_time('2025-01-01'):
            p.action_accounting_date = '2025-01-01'
            p.action_unwind()
        p.utilise_amount = 250.0
        with freeze_time('2025-06-01'):
            p.action_accounting_date = '2025-06-01'
            p.action_utilise()
        with freeze_time('2026-01-01'):
            p.action_accounting_date = '2026-01-01'
            p.action_reverse()

        def carrying(on_date):
            rows = p.eh_deferred_tax_temp_diffs(on_date)
            return rows[0]['carrying_amount'] if rows else 0.0

        self.assertAlmostEqual(carrying('2024-06-30'), 909.09, places=2)
        self.assertAlmostEqual(carrying('2025-03-31'), 1000.0, places=2)
        self.assertAlmostEqual(carrying('2025-12-31'), 750.0, places=2)
        self.assertAlmostEqual(carrying('2026-12-31'), 0.0, places=2)

    def test_unwind_is_time_based_and_not_double_counted(self):
        # A time-pinned provision unwinds period by period as real time passes,
        # and a repeat click inside an already-unwound period accretes nothing
        # (IAS 37.60: the increase in a provision reflecting the passage of
        # time is recognised as a borrowing cost as time elapses).
        p = self._provision(
            best_estimate=1000.0, discount_rate=10.0,
            periods_to_settlement=2,
            expected_settlement_date='2026-01-01')
        with freeze_time('2024-01-01'):
            p.action_accounting_date = '2024-01-01'
            p.action_recognise()  # PV = 1000 / 1.1^2 = 826.45
        self.assertAlmostEqual(p.carrying_amount, 826.45, places=2)

        # One year on: exactly one period has fallen due. 826.45 x 10% = 82.65.
        with freeze_time('2025-01-01'):
            p.action_accounting_date = '2025-01-01'
            p.action_unwind()
        self.assertAlmostEqual(p.carrying_amount, 909.10, places=2)
        self.assertEqual(p.unwound_periods, 1)
        self.assertAlmostEqual(self._bal(self.finance_cost), 82.65, places=2)

        # A repeat click in the same period must not double count: no new
        # period has elapsed, so it is refused and the balances are unchanged.
        with freeze_time('2025-01-01'):
            p.action_accounting_date = '2025-01-01'
            with self.assertRaises(UserError):
                p.action_unwind()
        self.assertAlmostEqual(p.carrying_amount, 909.10, places=2)
        self.assertEqual(p.unwound_periods, 1)
        self.assertAlmostEqual(self._bal(self.finance_cost), 82.65, places=2)

        # Second year on: the second period accretes. 909.09 x 10% = 90.91,
        # bringing the carrying amount to the undiscounted estimate of 1000.
        with freeze_time('2026-01-01'):
            p.action_accounting_date = '2026-01-01'
            p.action_unwind()
        self.assertAlmostEqual(p.carrying_amount, 1000.0, places=2)
        self.assertEqual(p.unwound_periods, 2)
        # Total finance cost over the two periods equals the full discount.
        self.assertAlmostEqual(self._bal(self.finance_cost), 173.55, places=2)

    def test_utilise_and_settle(self):
        p = self._provision(best_estimate=1000.0)
        p.action_recognise()
        p.utilise_amount = 400.0
        p.action_utilise()
        self.assertAlmostEqual(p.carrying_amount, 600.0, places=2)
        self.assertAlmostEqual(self._bal(self.account_cash), -400.0, places=2)
        p.utilise_amount = 600.0
        p.action_utilise()
        self.assertEqual(p.state, 'settled')
        self.assertAlmostEqual(p.carrying_amount, 0.0, places=2)

    def test_over_utilise_blocked(self):
        p = self._provision(best_estimate=1000.0)
        p.action_recognise()
        p.utilise_amount = 1500.0
        with self.assertRaises(UserError):
            p.action_utilise()

    def test_reverse_credits_pnl_and_zeroes_liability(self):
        # A provision no longer required is credited back to P&L against the
        # original expense account and the liability is cleared (IAS 37.59).
        p = self._provision(best_estimate=1000.0)
        p.action_recognise()
        self.assertAlmostEqual(self._bal(self.account_expense), 1000.0,
                               places=2)
        self.assertAlmostEqual(self._bal(self.provision_liab), -1000.0,
                               places=2)
        p.action_reverse()
        self.assertEqual(p.state, 'reversed')
        self.assertAlmostEqual(p.carrying_amount, 0.0, places=2)
        # Expense net of the writeback credit is nil (credited back to P&L).
        self.assertAlmostEqual(self._bal(self.account_expense), 0.0, places=2)
        # Provision liability is zeroed.
        self.assertAlmostEqual(self._bal(self.provision_liab), 0.0, places=2)
        # The reversal entry balances.
        for move in p.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_reverse_after_partial_utilise(self):
        # Reversing after a partial utilisation credits back only the unused
        # carrying amount.
        p = self._provision(best_estimate=1000.0)
        p.action_recognise()
        p.utilise_amount = 300.0
        p.action_utilise()
        self.assertAlmostEqual(p.carrying_amount, 700.0, places=2)
        p.action_reverse()
        self.assertEqual(p.state, 'reversed')
        self.assertAlmostEqual(p.carrying_amount, 0.0, places=2)
        self.assertAlmostEqual(self._bal(self.provision_liab), 0.0, places=2)
        # Original 1000 expense less 700 writeback = 300 utilised remains.
        self.assertAlmostEqual(self._bal(self.account_expense), 300.0,
                               places=2)

    def test_remeasure_increase_then_decrease(self):
        """IAS 37.59: a recognised provision remeasured to a higher estimate
        books the increase as expense; a lower estimate books a writeback.
        The stored best estimate and carrying amount track the revision and
        every entry balances."""
        p = self._provision(best_estimate=1000.0)
        p.action_recognise()
        self.assertAlmostEqual(p.carrying_amount, 1000.0, places=2)

        # Up to 1500: +500 to expense and the liability.
        p.remeasure_estimate = 1500.0
        p.action_remeasure()
        self.assertAlmostEqual(p.carrying_amount, 1500.0, places=2)
        self.assertAlmostEqual(p.best_estimate, 1500.0, places=2)
        self.assertAlmostEqual(self._bal(self.account_expense), 1500.0,
                               places=2)
        self.assertAlmostEqual(self._bal(self.provision_liab), -1500.0,
                               places=2)

        # Down to 600: 900 writeback credited back to the expense account.
        p.remeasure_estimate = 600.0
        p.action_remeasure()
        self.assertAlmostEqual(p.carrying_amount, 600.0, places=2)
        self.assertAlmostEqual(p.best_estimate, 600.0, places=2)
        self.assertAlmostEqual(self._bal(self.account_expense), 600.0,
                               places=2)
        self.assertAlmostEqual(self._bal(self.provision_liab), -600.0,
                               places=2)
        for move in p.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_onerous_remeasure_requires_every_staged_input(self):
        p = self._provision(
            provision_type='onerous',
            best_estimate=700.0,
            unavoidable_cost_fulfil=1200.0,
            contract_benefit_expected=300.0,
            penalty_exit=700.0,
        )
        p.action_recognise()
        before_moves = p.move_ids

        # A caller cannot forge the server provenance bits, and writing only
        # one revised number must not turn the other two into implicit zeros.
        p.write({
            'remeasure_cost_fulfil': 1000.0,
            'remeasure_benefit_expected_staged': True,
            'remeasure_penalty_exit_staged': True,
        })
        self.assertTrue(p.remeasure_cost_fulfil_staged)
        self.assertFalse(p.remeasure_benefit_expected_staged)
        self.assertFalse(p.remeasure_penalty_exit_staged)
        with self.assertRaises(UserError):
            p.action_remeasure()
        self.assertEqual(p.move_ids, before_moves)
        self.assertAlmostEqual(p.carrying_amount, 700.0, places=2)

        # Explicit zero is valid input and is distinguishable from omission.
        p.write({
            'remeasure_benefit_expected': 600.0,
            'remeasure_penalty_exit': 0.0,
        })
        p.action_remeasure()
        self.assertAlmostEqual(p.carrying_amount, 400.0, places=2)
        self.assertFalse(p.remeasure_cost_fulfil_staged)
        self.assertFalse(p.remeasure_benefit_expected_staged)
        self.assertFalse(p.remeasure_penalty_exit_staged)

    def test_reimbursement_is_recapped_and_reversed_with_provision(self):
        p = self._provision(
            best_estimate=1000.0,
            reimbursement_partner_id=self.partner_a.id,
        )
        p.action_recognise()
        p.reimbursement_amount = 800.0
        p.action_recognise_reimbursement()
        self.assertAlmostEqual(p.reimbursement_recognised, 800.0, places=2)
        self.assertAlmostEqual(
            self._bal(self.reimbursement_asset), 800.0, places=2)

        # Remeasurement down to 700 releases the 100 excess asset in the same
        # sealed entry as the liability writeback.
        p.remeasure_estimate = 700.0
        before = p.move_ids
        p.action_remeasure()
        remeasure = p.move_ids - before
        self.assertEqual(len(remeasure), 1)
        self.assertAlmostEqual(p.reimbursement_recognised, 700.0, places=2)
        self.assertAlmostEqual(
            self._bal(self.reimbursement_asset), 700.0, places=2)

        # Utilisation lowers the cap again; the asset cannot remain above the
        # remaining 500 provision carrying amount.
        p.utilise_amount = 200.0
        before = p.move_ids
        p.action_utilise()
        utilisation = p.move_ids - before
        self.assertEqual(len(utilisation), 1)
        self.assertAlmostEqual(p.carrying_amount, 500.0, places=2)
        self.assertAlmostEqual(p.reimbursement_recognised, 500.0, places=2)
        self.assertAlmostEqual(
            self._bal(self.reimbursement_asset), 500.0, places=2)

        # Full provision reversal atomically derecognises the remaining asset.
        before = p.move_ids
        p.action_reverse()
        reversal = p.move_ids - before
        self.assertEqual(len(reversal), 1)
        self.assertAlmostEqual(p.reimbursement_recognised, 0.0, places=2)
        self.assertAlmostEqual(
            self._bal(self.reimbursement_asset), 0.0, places=2)
        reimbursement_lines = p.move_ids.line_ids.filtered(
            lambda line: line.account_id == self.reimbursement_asset
        )
        self.assertTrue(reimbursement_lines)
        self.assertEqual(
            reimbursement_lines.mapped('partner_id'), self.partner_a
        )

    def test_receivable_reimbursement_requires_and_freezes_counterparty(self):
        p = self._provision(
            best_estimate=1000.0,
            reimbursement_account_id=self.account_receivable.id,
        )
        p.action_recognise()
        p.reimbursement_amount = 250.0
        with self.assertRaises(UserError):
            p.action_recognise_reimbursement()

        p.reimbursement_partner_id = self.partner_a
        p.action_recognise_reimbursement()
        receivable_line = p.move_ids.line_ids.filtered(
            lambda line: line.account_id == self.account_receivable
        )
        self.assertEqual(receivable_line.partner_id, self.partner_a)
        with self.assertRaises(UserError):
            p.reimbursement_partner_id = self.partner_b

    def test_remeasure_requires_manager(self):
        p = self._provision(best_estimate=1000.0)
        p.action_recognise()
        p.remeasure_estimate = 1200.0
        user = self.env['res.users'].create({
            'name': 'p', 'login': 'prov_rem_plain@test',
            'email': 'prov_rem_plain@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            p.with_user(user).action_remeasure()

    def test_remeasure_only_when_recognised(self):
        p = self._provision(best_estimate=1000.0)
        p.remeasure_estimate = 1200.0
        # A draft provision cannot be remeasured.
        with self.assertRaises(UserError):
            p.action_remeasure()

    def test_raw_edit_of_estimate_still_frozen(self):
        """The remeasure context flag must not open a raw-edit hole: writing
        best_estimate directly on a recognised provision is still refused."""
        p = self._provision(best_estimate=1000.0)
        p.action_recognise()
        with self.assertRaises(UserError):
            p.best_estimate = 2000.0

    def test_recognition_move_is_sealed(self):
        """The recognition entry is sealed at the GL layer: it cannot be reset
        to draft or have its figures edited in place, so the posted move
        cannot silently desync from the provision. A plain manual entry is
        unaffected and resets to draft normally."""
        p = self._provision(best_estimate=1000.0)
        p.action_recognise()
        move = p.move_ids[:1]
        self.assertTrue(move.eh_sealed)
        self.assertEqual(move.state, 'posted')
        with self.assertRaises(UserError):
            move.button_draft()
        with self.assertRaises(UserError):
            move.write({'state': 'draft'})
        with self.assertRaises(UserError):
            move.line_ids[0].debit = 999.0
        # A normal, unsealed manual entry still resets to draft.
        manual = self.post_balanced_move([
            {'account': self.account_expense, 'debit': 10.0},
            {'account': self.account_cash, 'credit': 10.0}])
        self.assertFalse(manual.eh_sealed)
        manual.button_draft()
        self.assertEqual(manual.state, 'draft')

    def test_recognised_state_cannot_be_raw_written(self):
        """A recognised provision's state moves only through its actions, which
        post the journal entry; a raw ORM state write is refused so the record
        cannot read reversed/settled while the liability still stands."""
        p = self._provision(best_estimate=1000.0)
        p.action_recognise()
        with self.assertRaises(UserError):
            p.write({'state': 'reversed'})
        self.assertEqual(p.state, 'recognised')
        # The sanctioned action still books the writeback and moves the state.
        p.action_reverse()
        self.assertEqual(p.state, 'reversed')
        self.assertAlmostEqual(p.carrying_amount, 0.0, places=2)

    def test_reverse_requires_manager(self):
        p = self._provision(best_estimate=1000.0)
        p.action_recognise()
        user = self.env['res.users'].create({
            'name': 'p', 'login': 'prov_rev_plain@test',
            'email': 'prov_rev_plain@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            p.with_user(user).action_reverse()

    def test_reverse_only_when_recognised(self):
        p = self._provision(best_estimate=1000.0)
        # Draft provision cannot be reversed.
        with self.assertRaises(UserError):
            p.action_reverse()

    def test_contingent_not_recognised(self):
        p = self._provision(classification='contingent_liability',
                            best_estimate=1000.0)
        with self.assertRaises(UserError):
            p.action_recognise()

    def test_recognise_requires_manager(self):
        p = self._provision(best_estimate=1000.0)
        user = self.env['res.users'].create({
            'name': 'p', 'login': 'prov_plain@test',
            'email': 'prov_plain@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            p.with_user(user).action_recognise()

    def test_measurement_frozen_after_recognition(self):
        p = self._provision(best_estimate=1000.0)
        p.action_recognise()
        self.assertAlmostEqual(p.carrying_amount, 1000.0, places=2)
        self.assertAlmostEqual(p.present_value, 1000.0, places=2)
        # Editing best_estimate on a recognised provision must be blocked so
        # present_value cannot silently drift from the posted figure.
        with self.assertRaises(UserError):
            p.best_estimate = 2000.0
        # Discount inputs are frozen too.
        with self.assertRaises(UserError):
            p.discount_rate = 5.0
        with self.assertRaises(UserError):
            p.periods_to_settlement = 3
        # The posted measurement is untouched.
        self.assertAlmostEqual(p.best_estimate, 1000.0, places=2)
        self.assertAlmostEqual(p.present_value, 1000.0, places=2)

    def test_entries_balance(self):
        p = self._provision(
            best_estimate=1000.0, discount_rate=10.0,
            periods_to_settlement=1,
            expected_settlement_date='2026-02-01',
        )
        p.action_recognise()
        p.action_accounting_date = '2026-02-01'
        p.action_unwind()
        p.utilise_amount = 500.0
        p.action_utilise()
        for move in p.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_posted_provision_frozen_and_undeletable_flow_intact(self):
        # (a) a recognised provision's measurement input is frozen at the ORM
        # write layer; (b) it cannot be unlinked (its posted GL move would be
        # orphaned); (c) the normal recognise/reverse flow still works.
        p = self._provision(best_estimate=1000.0)
        # Draft provision is deletable before it posts.
        draft = self._provision(best_estimate=500.0)
        draft.unlink()
        p.action_recognise()
        self.assertEqual(p.state, 'recognised')
        # (a) measurement input frozen at the write layer.
        with self.assertRaises(UserError):
            p.write({'best_estimate': 2000.0})
        # (b) a posted provision cannot be unlinked.
        with self.assertRaises(UserError):
            p.unlink()
        # (c) the reverse flow (a pure state write) still succeeds.
        p.action_reverse()
        self.assertEqual(p.state, 'reversed')
        # Still undeletable once reversed: the posted moves remain.
        with self.assertRaises(UserError):
            p.unlink()
