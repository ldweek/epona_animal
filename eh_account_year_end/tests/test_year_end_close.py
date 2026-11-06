# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Year-end closing tests.

Posts a small fiscal year of income and expense moves, runs the
closing, and asserts:

* The breakdown lists every contributing account with the right
  signed balance.
* The closing entry is balanced (sum debits == sum credits).
* The closing entry zeroes the income and expense accounts.
* Net profit lands on the retained earnings account.
* Reverse generates a balanced inverse move dated one day after.
* Lock date is bumped on post when lock_after_post is True.
"""

from datetime import date
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_year_end', 'integration', 'post_install', '-at_install')
class TestYearEndClose(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Run = cls.env['eh.year.end.run']
        cls.fy_start = date(2026, 1, 1)
        cls.fy_end = date(2026, 12, 31)

        cls.retained_earnings = cls._ensure_account(
            cls.env, '3100', 'Retained Earnings', 'equity',
        )
        cls.expense2 = cls._ensure_account(
            cls.env, '5050', 'Wages Expense', 'expense',
        )
        cls.revenue2 = cls._ensure_account(
            cls.env, '4050', 'Service Revenue', 'income_other',
        )

        # Post a small fiscal year. Two revenue lines and two expense.
        cls._post_at(date(2026, 3, 15), [
            {'account': cls.account_revenue, 'credit': 5000.0},
            {'account': cls.account_cash, 'debit': 5000.0},
        ])
        cls._post_at(date(2026, 6, 30), [
            {'account': cls.revenue2, 'credit': 3000.0},
            {'account': cls.account_cash, 'debit': 3000.0},
        ])
        cls._post_at(date(2026, 4, 1), [
            {'account': cls.account_expense, 'debit': 1500.0},
            {'account': cls.account_cash, 'credit': 1500.0},
        ])
        cls._post_at(date(2026, 11, 30), [
            {'account': cls.expense2, 'debit': 2000.0},
            {'account': cls.account_cash, 'credit': 2000.0},
        ])

        # Promote the test user so action_post passes the manager guard.
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager',
        )

    @classmethod
    def _post_at(cls, on_date, lines):
        return cls.post_balanced_move(lines, date=on_date)

    def _make_run(self, **overrides):
        vals = {
            'fiscal_year_start': self.fy_start,
            'fiscal_year_end': self.fy_end,
            'company_id': self.env.company.id,
            'journal_id': self.journal_misc.id,
            'retained_earnings_account_id': self.retained_earnings.id,
            'lock_after_post': False,
            # Disabling the lock requires a documented reason (logged in
            # chatter); the lock behaviour itself is exercised separately.
            'no_lock_reason': 'test fixture: lock exercised separately',
        }
        vals.update(overrides)
        return self.Run.create(vals)

    # ---- compute ----

    def test_compute_picks_up_revenue_and_expense_only(self):
        run = self._make_run()
        run.action_compute()
        accounts = set(run.line_ids.mapped('account_id'))
        self.assertIn(self.account_revenue, accounts)
        self.assertIn(self.revenue2, accounts)
        self.assertIn(self.account_expense, accounts)
        self.assertIn(self.expense2, accounts)
        # Cash account should NOT appear.
        self.assertNotIn(self.account_cash, accounts)

    def test_compute_totals_match_postings(self):
        run = self._make_run()
        run.action_compute()
        # Revenue 5000 + 3000 = 8000
        # Expense 1500 + 2000 = 3500
        self.assertAlmostEqual(run.total_income, 8000.0)
        self.assertAlmostEqual(run.total_expense, 3500.0)
        self.assertAlmostEqual(run.net_profit, 4500.0)

    def test_compute_idempotent(self):
        run = self._make_run()
        run.action_compute()
        first_count = run.line_count
        run.action_compute()
        self.assertEqual(run.line_count, first_count)

    # ---- post ----

    def test_post_blocks_when_not_computed(self):
        run = self._make_run()
        with self.assertRaises(UserError):
            run.action_post()

    def test_post_creates_balanced_entry(self):
        run = self._make_run()
        run.action_compute()
        run.action_post()
        move = run.move_id
        self.assertTrue(move and move.state == 'posted')
        debit = sum(move.line_ids.mapped('debit'))
        credit = sum(move.line_ids.mapped('credit'))
        self.assertAlmostEqual(debit, credit)

    def test_post_zeroes_income_accounts(self):
        run = self._make_run()
        run.action_compute()
        run.action_post()
        # Sum of all entries on revenue account in fiscal year should
        # be zero AFTER posting the close.
        AML = self.env['account.move.line']
        revenue_balance = sum(AML.search([
            ('account_id', '=', self.account_revenue.id),
            ('parent_state', '=', 'posted'),
            ('date', '>=', self.fy_start),
            ('date', '<=', self.fy_end),
        ]).mapped('balance'))
        self.assertAlmostEqual(revenue_balance, 0.0)

    def test_post_zeroes_expense_accounts(self):
        run = self._make_run()
        run.action_compute()
        run.action_post()
        AML = self.env['account.move.line']
        expense_balance = sum(AML.search([
            ('account_id', '=', self.account_expense.id),
            ('parent_state', '=', 'posted'),
            ('date', '>=', self.fy_start),
            ('date', '<=', self.fy_end),
        ]).mapped('balance'))
        self.assertAlmostEqual(expense_balance, 0.0)

    def test_post_sweeps_other_expenses(self):
        """Accounts of type ``expense_other`` (IAS 1 Other Expenses) must be
        closed to retained earnings, symmetric with ``income_other`` on the
        revenue side. Omitting them leaves the Other-Expenses balance
        standing and overstates retained earnings by that amount on every
        close.
        """
        type_field = self.env['account.account']._fields['account_type']
        type_values = dict(type_field.selection).keys()
        if 'expense_other' not in type_values:
            self.skipTest(
                "account_type 'expense_other' is Odoo 19+; earlier cores "
                "have no Other-Expenses type to sweep",
            )
        other_exp = self._ensure_account(
            self.env, '5090', 'Other Expenses', 'expense_other',
        )
        self._post_at(date(2026, 5, 20), [
            {'account': other_exp, 'debit': 700.0},
            {'account': self.account_cash, 'credit': 700.0},
        ])
        run = self._make_run()
        run.action_compute()
        # The Other-Expenses account is in scope of the close.
        self.assertIn(other_exp, set(run.line_ids.mapped('account_id')))
        # It contributes to the expense total (3500 base + 700).
        self.assertAlmostEqual(run.total_expense, 4200.0)
        run.action_post()
        AML = self.env['account.move.line']
        balance = sum(AML.search([
            ('account_id', '=', other_exp.id),
            ('parent_state', '=', 'posted'),
            ('date', '>=', self.fy_start),
            ('date', '<=', self.fy_end),
        ]).mapped('balance'))
        self.assertAlmostEqual(balance, 0.0)

    def test_post_pushes_net_to_retained_earnings(self):
        run = self._make_run()
        run.action_compute()
        run.action_post()
        move = run.move_id
        re_legs = move.line_ids.filtered(
            lambda l: l.account_id == self.retained_earnings,
        )
        # Net profit 4500 -> retained earnings credited 4500.
        self.assertEqual(len(re_legs), 1)
        self.assertAlmostEqual(re_legs.credit, 4500.0)
        self.assertAlmostEqual(re_legs.debit, 0.0)

    def test_post_handles_net_loss(self):
        # Add another big expense that turns the net into a loss.
        self._post_at(date(2026, 10, 15), [
            {'account': self.expense2, 'debit': 10000.0},
            {'account': self.account_cash, 'credit': 10000.0},
        ])
        run = self._make_run(
            fiscal_year_end=date(2026, 12, 31),
        )
        run.action_compute()
        # Income 8000 - Expense (3500 + 10000) = -5500
        self.assertAlmostEqual(run.net_profit, -5500.0)
        run.action_post()
        re_legs = run.move_id.line_ids.filtered(
            lambda l: l.account_id == self.retained_earnings,
        )
        # Net loss 5500 -> retained earnings debited 5500.
        self.assertAlmostEqual(re_legs.debit, 5500.0)
        self.assertAlmostEqual(re_legs.credit, 0.0)

    # ---- reverse ----

    def test_reverse_creates_inverse_dated_after(self):
        run = self._make_run()
        run.action_compute()
        run.action_post()
        run.action_reverse()
        self.assertEqual(run.state, 'reversed')
        rev = run.reversal_move_id
        self.assertTrue(rev and rev.state == 'posted')
        self.assertEqual(rev.date, date(2027, 1, 1))
        # Reversal also balances.
        self.assertAlmostEqual(
            sum(rev.line_ids.mapped('debit')),
            sum(rev.line_ids.mapped('credit')),
        )

    def test_reverse_requires_newest_first_chronology(self):
        earlier = self._make_run()
        earlier.action_compute()
        earlier.action_post()

        self._post_at(date(2027, 6, 30), [
            {'account': self.account_revenue, 'credit': 7000.0},
            {'account': self.account_cash, 'debit': 7000.0},
        ])
        later = self._make_run(
            fiscal_year_start=date(2027, 1, 1),
            fiscal_year_end=date(2027, 12, 31),
        )
        later.action_compute()
        later.action_post()

        with self.assertRaisesRegex(UserError, 'newest-first'):
            earlier.action_reverse()
        self.assertEqual(earlier.state, 'posted')
        self.assertFalse(earlier.reversal_move_id)

        later.action_reverse()
        earlier.action_reverse()
        self.assertEqual(earlier.state, 'reversed')

    def test_reversed_run_recomputes_and_recloses_from_operating_pl(self):
        """A corrected close retains both immutable close/reversal cycles."""
        run = self._make_run()
        run.action_compute()
        self.assertAlmostEqual(run.net_profit, 4500.0)
        run.action_post()
        first_close = run.move_id
        if 'eh.soci' in self.env:
            soci = self.env['eh.soci'].create({
                'period_start': self.fy_start,
                'period_end': self.fy_end,
            })
            self.assertIn(
                first_close.id, soci._eh_year_end_entry_move_ids(),
            )
            soci.action_derive_profit_from_ledger()
            self.assertAlmostEqual(soci.profit_for_period, 4500.0)
        if 'eh.statement.tieout' in self.env:
            tieout = self.env['eh.statement.tieout'].create({
                'date_from': self.fy_start,
                'date_to': self.fy_end,
            })
            self.assertAlmostEqual(
                tieout._pl_ledger_aggregate(self.fy_start, self.fy_end),
                4500.0,
            )
        run.action_reverse()
        first_reversal = run.reversal_move_id

        run.action_compute()
        self.assertEqual(run.state, 'computed')
        self.assertAlmostEqual(run.net_profit, 4500.0)
        self.assertIn(first_close, run.closing_move_ids)
        self.assertIn(first_reversal, run.reversal_move_ids)

        run.action_post()
        second_close = run.move_id
        self.assertNotEqual(second_close, first_close)
        self.assertEqual(set(run.closing_move_ids.ids), {
            first_close.id, second_close.id,
        })
        self.assertFalse(run.reversal_move_id)
        run.action_reverse()
        self.assertEqual(len(run.reversal_move_ids), 2)
        self.assertIn(first_reversal, run.reversal_move_ids)
        self.assertIn(run.reversal_move_id, run.reversal_move_ids)

    def test_next_year_compute_excludes_prior_close_reversal(self):
        """The day-after reversal is provenance, not next-year profit."""
        prior = self._make_run()
        prior.action_compute()
        prior.action_post()
        prior.action_reverse()
        # A reversed prior year is economically open. Re-close it before a
        # successor close; durable history remains excluded from measurement.
        prior.action_compute()
        prior.action_post()

        self._post_at(date(2027, 6, 30), [
            {'account': self.account_revenue, 'credit': 7000.0},
            {'account': self.account_cash, 'debit': 7000.0},
        ])
        current = self._make_run(
            fiscal_year_start=date(2027, 1, 1),
            fiscal_year_end=date(2027, 12, 31),
        )
        current.action_compute()
        self.assertAlmostEqual(current.total_income, 7000.0)
        self.assertAlmostEqual(current.total_expense, 0.0)
        self.assertAlmostEqual(current.net_profit, 7000.0)
        if 'eh.soci' in self.env:
            soci = self.env['eh.soci'].create({
                'period_start': date(2027, 1, 1),
                'period_end': date(2027, 12, 31),
            })
            source_ids = soci._eh_year_end_entry_move_ids()
            self.assertTrue(prior.reversal_move_ids)
            for reversal_id in prior.reversal_move_ids.ids:
                self.assertIn(reversal_id, source_ids)
            soci.action_derive_profit_from_ledger()
            self.assertAlmostEqual(soci.profit_for_period, 7000.0)

    # ---- lock date ----

    def test_lock_after_post_advances_lock_date(self):
        run = self._make_run(lock_after_post=True)
        run.action_compute()
        Company = type(self.env.company)
        original_write = Company.write
        lock_writes = []

        def recording_write(records, vals):
            if 'fiscalyear_lock_date' in vals:
                lock_writes.append(dict(vals))
            return original_write(records, vals)

        with patch.object(Company, 'write', recording_write):
            run.action_post()
        self.assertEqual(
            self.env.company.fiscalyear_lock_date,
            self.fy_end,
        )
        self.assertEqual(lock_writes, [{
            'fiscalyear_lock_date': self.fy_end,
        }])

    def test_archived_or_foreign_currency_accounts_fail_before_post(self):
        run = self._make_run()
        run.action_compute()
        archive_field = (
            'active' if 'active' in self.expense2._fields else 'deprecated'
        )
        archived_value = archive_field == 'deprecated'
        self.expense2.write({archive_field: archived_value})
        try:
            with self.assertRaisesRegex(UserError, 'archived'):
                run.action_post()
        finally:
            self.expense2.with_context(active_test=False).write({
                archive_field: not archived_value,
            })
        self.assertFalse(run.move_id)

        foreign = self.env['res.currency'].with_context(
            active_test=False,
        ).search([
            ('id', '!=', self.env.company.currency_id.id),
        ], limit=1)
        self.assertTrue(foreign, 'A secondary base currency must exist.')
        forced_account = self._ensure_account(
            self.env, '49999', 'Forced-currency close regression', 'income',
        )
        forced_account.currency_id = foreign
        income_line = run.line_ids.filtered('income_balance')[:1]
        income_line.sudo().with_context(eh_year_end_engine=True).write({
            'account_id': forced_account.id,
        })
        with self.assertRaisesRegex(UserError, 'forces'):
            run.action_post()
        self.assertFalse(run.move_id)

    def test_branch_close_accepts_root_owned_chart_accounts(self):
        if 'company_ids' not in self.env['account.account']._fields:
            self.skipTest('Shared branch chart starts in Odoo 18.')
        branch = self.env['res.company'].create({
            'name': 'Year-end Shared-chart Branch',
            'parent_id': self.env.company.id,
        })
        self.env.user.write({'company_ids': [(4, branch.id)]})
        branch_env = self.env['account.move'].with_context(
            allowed_company_ids=[self.env.company.id, branch.id],
        ).with_company(branch).env
        journal = self._ensure_journal(
            branch_env, branch, 'general', 'YEBR', 'Year-end Branch Journal',
        )
        move = branch_env['account.move'].create({
            'move_type': 'entry',
            'company_id': branch.id,
            'journal_id': journal.id,
            'date': date(2026, 6, 30),
            'line_ids': [
                (0, 0, {
                    'name': 'Branch revenue',
                    'account_id': self.account_revenue.id,
                    'credit': 900.0,
                }),
                (0, 0, {
                    'name': 'Branch cash',
                    'account_id': self.account_cash.id,
                    'debit': 900.0,
                }),
            ],
        })
        move.action_post()
        run = branch_env['eh.year.end.run'].create({
            'fiscal_year_start': self.fy_start,
            'fiscal_year_end': self.fy_end,
            'company_id': branch.id,
            'journal_id': journal.id,
            'retained_earnings_account_id': self.retained_earnings.id,
            'lock_after_post': False,
            'no_lock_reason': 'branch regression',
        })
        run.action_compute()
        self.assertAlmostEqual(run.net_profit, 900.0, places=2)
        run.action_post()
        self.assertEqual(run.move_id.company_id, branch)
        self.assertEqual(run.state, 'posted')

    def test_lock_skipped_when_already_past(self):
        # Pre-set the lock date to a year BEFORE the one being closed:
        # the advance-lock logic should still bump it forward. Using a
        # lock date at or past fiscal_year_end would (correctly) trip
        # the pre-post lock guard, which is exercised separately.
        self.env.company.sudo().fiscalyear_lock_date = date(2025, 12, 31)
        run = self._make_run(lock_after_post=True)
        run.action_compute()
        run.action_post()
        # Lock date advanced to this run's fiscal year end.
        self.assertEqual(
            self.env.company.fiscalyear_lock_date,
            self.fy_end,
        )

    # ---- constraints ----

    def test_unique_run_per_company_year(self):
        self._make_run()
        with self.assertRaises(Exception):
            self._make_run()

    def test_post_blocked_by_overlapping_posted_run(self):
        # First close posts cleanly.
        first = self._make_run()
        first.action_compute()
        first.action_post()
        self.assertEqual(first.state, 'posted')
        # Change the company's configured year-end after the historical close
        # (a supported governance edge). The new exact fiscal year overlaps
        # the standing calendar-year close, so scope chronology must reject it.
        self.env.company.sudo().write({
            'fiscalyear_last_day': 30,
            'fiscalyear_last_month': '6',
        })
        second = self._make_run(
            fiscal_year_start=date(2026, 7, 1),
            fiscal_year_end=date(2027, 6, 30),
        )
        second.action_compute()
        with self.assertRaisesRegex(UserError, 'overlapping company/branch'):
            second._check_post_lock()
        self.assertEqual(second.state, 'computed')

    def test_post_blocked_when_year_already_locked(self):
        # Company lock date already sits at or past the fiscal year end:
        # the year is closed and frozen, so posting a close into it must
        # be rejected by the pre-post lock guard.
        self.env.company.sudo().fiscalyear_lock_date = self.fy_end
        run = self._make_run(lock_after_post=False)
        run.action_compute()
        with self.assertRaises(UserError):
            run.action_post()
        self.assertEqual(run.state, 'computed')

    def test_invalid_year_dates_rejected(self):
        with self.assertRaises(Exception):
            self.Run.create({
                'fiscal_year_start': date(2026, 12, 31),
                'fiscal_year_end': date(2026, 1, 1),
                'company_id': self.env.company.id,
                'journal_id': self.journal_misc.id,
                'retained_earnings_account_id': self.retained_earnings.id,
            })

    # ---- performance ----

    def test_build_lines_uses_single_batched_create(self):
        # Four contributing accounts (two income, two expense) must
        # produce the breakdown in ONE batched create, not one create
        # per account. Guards the N+1 regression on a full chart.
        run = self._make_run()
        Line = type(self.env['eh.year.end.line'])
        original_create = Line.create
        calls = []

        def counting_create(self2, vals):
            calls.append(vals)
            return original_create(self2, vals)

        with patch.object(Line, 'create', counting_create):
            run.action_compute()

        self.assertEqual(
            len(calls), 1,
            "breakdown lines must be created in one batched create call",
        )
        self.assertIsInstance(calls[0], list)
        self.assertEqual(len(calls[0]), 4)
        self.assertEqual(len(run.line_ids), 4)
