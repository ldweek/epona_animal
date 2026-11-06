# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IAS 23 borrowing cost tests."""

from datetime import date

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_borrowing_costs', 'integration', 'post_install',
        '-at_install')
class TestBorrowingCost(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.asset = cls._ensure_account(
            cls.env, '1680', 'Asset Under Construction', 'asset_non_current')
        cls.interest = cls._ensure_account(
            cls.env, '5680', 'Interest Expense', 'expense')

    def _rec(self, **vals):
        base = {
            'name': '/', 'qualifying_asset': 'New plant',
            'asset_account_id': self.asset.id,
            'borrowing_cost_account_id': self.interest.id,
            'journal_id': self.journal_misc.id,
        }
        base.update(vals)
        return self.env['eh.borrowing.cost'].create(base)

    def _bal(self, account):
        lines = self.env['account.move.line'].search([
            ('account_id', '=', account.id), ('parent_state', '=', 'posted')])
        return sum(lines.mapped('debit')) - sum(lines.mapped('credit'))

    def test_specific_net_of_investment_income(self):
        rec = self._rec(specific_borrowing_cost=1000.0,
                        temporary_investment_income=150.0)
        self.assertAlmostEqual(rec.capitalisable, 850.0, places=2)

    def test_excess_investment_income_does_not_erode_general(self):
        # IAS 23.12: temporary investment income on a SPECIFIC borrowing is
        # deducted only from that specific borrowing's own capitalisable
        # amount. Here investment income (600) exceeds the specific borrowing
        # cost (200), so the specific net is floored at zero. It must NOT spill
        # over and reduce the general-borrowing component.
        #   specific net = max(200 - 600, 0)           = 0
        #   general      = 10,000 (base) x 10%         = 1,000
        #   capitalisable                              = 1,000
        # The defect netted -400 into the general pool, giving 600.
        rec = self._rec(
            specific_borrowing_cost=200.0,
            temporary_investment_income=600.0,
            general_expenditure=10000.0,
            capitalisation_rate=10.0)
        self.assertAlmostEqual(rec.capitalisable, 1000.0, places=2)
        # Prove it is not the understated figure the defect produced.
        self.assertNotAlmostEqual(rec.capitalisable, 600.0, places=2)

    def test_general_at_capitalisation_rate(self):
        rec = self._rec(general_expenditure=200000.0,
                        capitalisation_rate=8.0)
        # 200000 x 8% = 16000.
        self.assertAlmostEqual(rec.capitalisable, 16000.0, places=2)

    def test_annual_rate_is_apportioned_for_subannual_period(self):
        rec = self._rec(
            general_expenditure=100000.0,
            capitalisation_rate=12.0,
            capitalisation_rate_basis='annual',
            period_start=date(2026, 1, 1),
            period_end=date(2026, 3, 31),
        )
        self.assertAlmostEqual(
            rec.capitalisable,
            100000.0 * 0.12 * 90.0 / 365.0,
            places=2,
        )

    def test_explicit_period_rate_is_not_apportioned_again(self):
        rec = self._rec(
            general_expenditure=100000.0,
            capitalisation_rate=3.0,
            capitalisation_rate_basis='period',
            period_start=date(2026, 1, 1),
            period_end=date(2026, 3, 31),
        )
        self.assertAlmostEqual(rec.capitalisable, 3000.0, places=2)

    def test_annual_general_rate_requires_period_start_at_posting(self):
        rec = self._rec(
            general_expenditure=100000.0,
            capitalisation_rate=12.0,
            capitalisation_rate_basis='annual',
            period_end=date(2026, 3, 31),
        )
        with self.assertRaisesRegex(UserError, "Set Period Start"):
            rec.action_capitalise()

    def test_general_uses_weighted_average_not_raw_sum(self):
        # Period is a full year. Two dated expenditures:
        #   120,000 outstanding for the whole year (day 1),
        #   120,000 outstanding for exactly half the year (mid-year).
        # Raw sum = 240,000. Weighted-average base = 120,000 + 60,000 =
        # 180,000. At 10% the capitalised general cost must be 18,000
        # (weighted average), NOT 24,000 (raw sum x rate).
        rec = self._rec(
            capitalisation_rate=10.0,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 12, 31),
            expenditure_line_ids=[
                (0, 0, {'date': date(2026, 1, 1), 'amount': 120000.0}),
                (0, 0, {'date': date(2026, 7, 2), 'amount': 120000.0}),
            ])
        total_days = (date(2026, 12, 31) - date(2026, 1, 1)).days + 1
        half_days = (date(2026, 12, 31) - date(2026, 7, 2)).days + 1
        expected_base = 120000.0 + 120000.0 * half_days / total_days
        self.assertAlmostEqual(rec.weighted_average_base, expected_base,
                               places=2)
        self.assertAlmostEqual(rec.capitalisable, expected_base * 0.10,
                               places=2)
        # Prove it is not the raw-sum figure the defect produced.
        self.assertNotAlmostEqual(rec.capitalisable, 24000.0, places=2)

    def test_general_flat_expenditure_still_supported(self):
        # With no dated lines the flat general_expenditure base is used.
        rec = self._rec(general_expenditure=200000.0,
                        capitalisation_rate=8.0)
        self.assertAlmostEqual(rec.capitalisable, 16000.0, places=2)

    def test_capped_at_actual(self):
        rec = self._rec(specific_borrowing_cost=1000.0,
                        general_expenditure=100000.0,
                        capitalisation_rate=10.0,
                        actual_borrowing_cost=9000.0)
        # Uncapped = 1000 + 10000 = 11000, capped at 9000.
        self.assertAlmostEqual(rec.uncapped_amount, 11000.0, places=2)
        self.assertAlmostEqual(rec.capitalisable, 9000.0, places=2)

    def test_capitalise_posts_reclassification(self):
        rec = self._rec(specific_borrowing_cost=850.0)
        rec.action_capitalise()
        self.assertEqual(rec.state, 'capitalised')
        self.assertAlmostEqual(self._bal(self.asset), 850.0, places=2)
        self.assertAlmostEqual(self._bal(self.interest), -850.0, places=2)
        self.assertEqual(rec.move_id.eh_borrowing_cost_id, rec)
        self.assertEqual(rec.move_id.date, rec.period_end)
        self.assertEqual(rec.posted_accounting_date, rec.period_end)

    def test_locked_period_requires_explicit_open_accounting_date(self):
        period_end = date(2026, 1, 31)
        original_lock_date = self.company.fiscalyear_lock_date
        try:
            self.company.fiscalyear_lock_date = period_end
            locked = self._rec(
                specific_borrowing_cost=850.0,
                period_end=period_end,
            )
            caught = None
            try:
                locked.action_capitalise()
            except UserError as exc:
                caught = str(exc)
            self.assertRegex(
                caught or '', 'would move the capitalisation date',
            )
            self.assertFalse(locked.move_ids)
            self.assertEqual(locked.state, 'draft')

            correction_date = date(2026, 2, 1)
            corrected = self._rec(
                specific_borrowing_cost=850.0,
                period_end=period_end,
                accounting_date=correction_date,
            )
            corrected.action_capitalise()
            self.assertEqual(corrected.move_id.date, correction_date)
            self.assertEqual(
                corrected.posted_accounting_date, correction_date,
            )
        finally:
            self.company.fiscalyear_lock_date = original_lock_date

    def test_posting_account_and_journal_types_are_server_enforced(self):
        wrong_journal = self._rec(
            specific_borrowing_cost=850.0,
            journal_id=self.journal_sale.id,
        )
        with self.assertRaisesRegex(UserError, 'general journal'):
            wrong_journal.action_capitalise()

        wrong_asset = self._rec(
            specific_borrowing_cost=850.0,
            asset_account_id=self.account_revenue.id,
        )
        with self.assertRaisesRegex(UserError, 'asset account'):
            wrong_asset.action_capitalise()

        wrong_cost = self._rec(
            specific_borrowing_cost=850.0,
            borrowing_cost_account_id=self.account_revenue.id,
        )
        with self.assertRaisesRegex(UserError, 'expense account'):
            wrong_cost.action_capitalise()

    def test_reverse_correct_and_recapitalise_preserves_history(self):
        rec = self._rec(specific_borrowing_cost=850.0)
        rec.action_capitalise()
        original = rec.move_id
        rec.action_reverse()
        reversal = rec.reversal_move_id
        self.assertEqual(rec.state, 'reversed')
        self.assertEqual(reversal.reversed_entry_id, original)
        self.assertEqual(reversal.eh_borrowing_cost_id, rec)
        self.assertEqual(original.state, 'posted')
        self.assertEqual(reversal.state, 'posted')
        with self.assertRaises(UserError):
            rec.write({'specific_borrowing_cost': 900.0})

        rec.action_reset_to_draft()
        rec.write({'specific_borrowing_cost': 900.0})
        rec.action_capitalise()
        replacement = rec.move_id
        self.assertEqual(rec.state, 'capitalised')
        self.assertNotEqual(replacement, original)
        self.assertEqual(set(rec.move_ids.ids), {
            original.id, reversal.id, replacement.id,
        })
        self.assertTrue(all(move.state == 'posted' for move in rec.move_ids))

    def test_move_provenance_cannot_be_forged(self):
        rec = self._rec(specific_borrowing_cost=850.0)
        with self.assertRaises(AccessError):
            self.env['account.move'].create({
                'move_type': 'entry',
                'journal_id': self.journal_misc.id,
                'eh_borrowing_cost_id': rec.id,
            })

    def test_nil_capitalisable_blocked(self):
        rec = self._rec(specific_borrowing_cost=0.0)
        with self.assertRaises(UserError):
            rec.action_capitalise()

    def test_capitalise_requires_manager(self):
        rec = self._rec(specific_borrowing_cost=850.0)
        user = self.env['res.users'].create({
            'name': 'p', 'login': 'borr_plain@test',
            'email': 'borr_plain@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(AccessError):
            rec.with_user(user).action_capitalise()

    def test_entry_balances(self):
        rec = self._rec(specific_borrowing_cost=850.0)
        rec.action_capitalise()
        move = rec.move_id
        self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                               sum(move.line_ids.mapped('credit')), places=2)

    def test_inputs_frozen_after_capitalised(self):
        rec = self._rec(specific_borrowing_cost=850.0)
        rec.action_capitalise()
        self.assertEqual(rec.state, 'capitalised')
        # Every measurement input is frozen once capitalised.
        with self.assertRaises(UserError):
            rec.specific_borrowing_cost = 1000.0
        with self.assertRaises(UserError):
            rec.capitalisation_rate = 12.0
        with self.assertRaises(UserError):
            rec.general_expenditure = 500000.0
        with self.assertRaises(UserError):
            rec.period_end = date(2027, 1, 1)

    def test_inputs_editable_before_capitalised(self):
        rec = self._rec(specific_borrowing_cost=850.0)
        # Draft: inputs remain editable (default behaviour).
        rec.specific_borrowing_cost = 900.0
        self.assertAlmostEqual(rec.specific_borrowing_cost, 900.0, places=2)

    def test_flat_behaviour_unchanged_when_window_unset(self):
        # With no capitalisation-period control set, the flat single-period
        # base is used unchanged (opt-in behaviour is not triggered).
        rec = self._rec(general_expenditure=200000.0, capitalisation_rate=8.0)
        self.assertFalse(rec._has_capitalisation_window())
        self.assertAlmostEqual(rec.capitalisable, 16000.0, places=2)

    def test_not_capitalised_past_cessation_date(self):
        # The asset became ready for use before the period even opened, so the
        # whole period is past cessation: nothing is capitalisable
        # (IAS 23.22). Same flat inputs would otherwise give 16,000.
        rec = self._rec(
            general_expenditure=200000.0, capitalisation_rate=8.0,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
            cessation_date=date(2025, 12, 31))
        self.assertTrue(rec._has_capitalisation_window())
        self.assertAlmostEqual(rec.weighted_average_base, 0.0, places=2)
        self.assertAlmostEqual(rec.capitalisable, 0.0, places=2)
        # And it cannot be posted: there is no capitalisable amount.
        with self.assertRaises(UserError):
            rec.action_capitalise()

    def test_early_commencement_does_not_inflate_base(self):
        # A commencement date BEFORE the reporting period must not push the
        # active window past the nominal period (which would weight above
        # 100% and over-capitalise). The base is clamped to the period, so it
        # can never exceed the flat general_expenditure.
        rec = self._rec(
            general_expenditure=200000.0, capitalisation_rate=8.0,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
            commencement_date=date(2025, 6, 1))
        self.assertTrue(rec._has_capitalisation_window())
        self.assertLessEqual(rec.weighted_average_base, 200000.0 + 0.01)
        # The whole reporting period is active, so it equals the flat base.
        self.assertAlmostEqual(rec.weighted_average_base, 200000.0, places=2)

    def test_cessation_mid_period_apportions_flat_base(self):
        # Ready for use halfway through: only the pre-cessation fraction of the
        # flat base is capitalised (IAS 23.22-25).
        rec = self._rec(
            general_expenditure=200000.0, capitalisation_rate=10.0,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
            cessation_date=date(2026, 7, 2))
        total_days = (date(2026, 12, 31) - date(2026, 1, 1)).days + 1
        active_days = (date(2026, 7, 2) - date(2026, 1, 1)).days + 1
        expected_base = 200000.0 * active_days / total_days
        self.assertAlmostEqual(rec.weighted_average_base, expected_base,
                               places=2)
        self.assertAlmostEqual(rec.capitalisable, expected_base * 0.10,
                               places=2)

    def test_suspension_span_excluded_from_window(self):
        # A full-year expenditure outstanding from day one, with a suspension
        # covering exactly half the year, is capitalised only for the active
        # half (IAS 23.20-21).
        rec = self._rec(
            capitalisation_rate=10.0,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
            expenditure_line_ids=[
                (0, 0, {'date': date(2026, 1, 1), 'amount': 120000.0}),
            ],
            suspension_line_ids=[
                (0, 0, {'date_start': date(2026, 7, 2),
                        'date_end': date(2026, 12, 31)}),
            ])
        total_days = (date(2026, 12, 31) - date(2026, 1, 1)).days + 1
        active_days = (date(2026, 7, 2) - date(2026, 1, 1)).days + 1
        expected_base = 120000.0 * active_days / total_days
        self.assertAlmostEqual(rec.weighted_average_base, expected_base,
                               places=2)
        self.assertAlmostEqual(rec.capitalisable, expected_base * 0.10,
                               places=2)

    def test_line_add_blocked_after_capitalised(self):
        rec = self._rec(specific_borrowing_cost=850.0)
        rec.action_capitalise()
        self.assertEqual(rec.state, 'capitalised')
        # A new dated expenditure line cannot be added once capitalised.
        with self.assertRaises(UserError):
            self.env['eh.borrowing.cost.line'].create({
                'borrowing_cost_id': rec.id,
                'date': date(2026, 1, 1), 'amount': 5000.0})

    def test_line_unlink_blocked_after_capitalised(self):
        rec = self._rec(
            capitalisation_rate=10.0,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
            expenditure_line_ids=[
                (0, 0, {'date': date(2026, 1, 1), 'amount': 120000.0}),
            ])
        rec.action_capitalise()
        line = rec.expenditure_line_ids[0]
        with self.assertRaises(UserError):
            line.unlink()

    def test_dated_line_frozen_after_capitalised(self):
        rec = self._rec(
            capitalisation_rate=10.0,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 12, 31),
            expenditure_line_ids=[
                (0, 0, {'date': date(2026, 1, 1), 'amount': 120000.0}),
            ])
        rec.action_capitalise()
        line = rec.expenditure_line_ids[0]
        with self.assertRaises(UserError):
            line.amount = 130000.0
        with self.assertRaises(UserError):
            line.date = date(2026, 6, 1)

    def test_children_frozen_while_reversed_then_unlock_on_reset(self):
        rec = self._rec(
            specific_borrowing_cost=850.0,
            expenditure_line_ids=[(0, 0, {
                'date': date(2026, 1, 1), 'amount': 100.0,
            })],
            suspension_line_ids=[(0, 0, {
                'date_start': date(2026, 2, 1),
                'date_end': date(2026, 2, 2),
            })],
        )
        rec.action_capitalise()
        rec.action_reverse()
        line = rec.expenditure_line_ids
        suspension = rec.suspension_line_ids
        with self.assertRaises(UserError):
            line.write({'amount': 125.0})
        with self.assertRaises(UserError):
            suspension.write({'date_end': date(2026, 2, 3)})
        with self.assertRaises(UserError):
            self.env['eh.borrowing.cost.line'].create({
                'borrowing_cost_id': rec.id,
                'date': date(2026, 3, 1),
                'amount': 50.0,
            })
        rec.action_reset_to_draft()
        line.write({'amount': 125.0})
        suspension.write({'date_end': date(2026, 2, 3)})
        self.assertAlmostEqual(line.amount, 125.0, places=2)
