# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Year-end closing control-integrity tests.

A posted year-end run carries a posted GL closing entry, so its inputs are
frozen at the ORM write layer, it cannot be deleted, and a plain (non-manager)
user cannot raw-reset its state to lift the freeze. These probes assert:

* A posted run's input field is frozen at write.
* A posted run cannot be unlinked.
* A plain user cannot raw-reset a posted run's state.
* The normal compute / post / reverse flow still works end to end.
"""

from datetime import date

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_year_end', 'integration', 'post_install', '-at_install')
class TestYearEndIntegrity(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Run = cls.env['eh.year.end.run']
        cls.fy_start = date(2026, 1, 1)
        cls.fy_end = date(2026, 12, 31)

        cls.retained_earnings = cls._ensure_account(
            cls.env, '3100', 'Retained Earnings', 'equity',
        )

        # A small fiscal year: one revenue and one expense posting so the
        # close produces a non-empty balanced move.
        cls._post_at(date(2026, 3, 15), [
            {'account': cls.account_revenue, 'credit': 5000.0},
            {'account': cls.account_cash, 'debit': 5000.0},
        ])
        cls._post_at(date(2026, 4, 1), [
            {'account': cls.account_expense, 'debit': 1500.0},
            {'account': cls.account_cash, 'credit': 1500.0},
        ])

        # Manager group so the sanctioned action_post / action_reverse pass.
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager',
        )

    @classmethod
    def _post_at(cls, on_date, lines):
        return cls.post_balanced_move(lines, date=on_date)

    def _posted_run(self):
        run = self.Run.create({
            'fiscal_year_start': self.fy_start,
            'fiscal_year_end': self.fy_end,
            'company_id': self.env.company.id,
            'journal_id': self.journal_misc.id,
            'retained_earnings_account_id': self.retained_earnings.id,
            'lock_after_post': False,
            # Disabling the lock requires a documented reason (logged in
            # chatter); the lock behaviour itself is exercised separately.
            'no_lock_reason': 'test fixture: lock exercised separately',
        })
        run.action_compute()
        run.action_post()
        self.assertEqual(run.state, 'posted')
        return run

    # ---- (a) input frozen once posted ----

    def test_posted_input_field_is_frozen(self):
        run = self._posted_run()
        with self.assertRaises(UserError):
            run.write({'journal_id': self.journal_misc.id,
                       'fiscal_year_start': date(2026, 2, 1)})
        # A pure audit-stamp write with no frozen field still passes.
        run.write({'notes': 'annotation after post'})
        self.assertEqual(run.notes, 'annotation after post')

    def test_posted_line_figure_is_frozen(self):
        run = self._posted_run()
        line = run.line_ids[:1]
        self.assertTrue(line)
        with self.assertRaises(UserError):
            line.write({'income_balance': 999.0})

    # ---- (b) posted run cannot be deleted ----

    def test_posted_run_cannot_be_unlinked(self):
        run = self._posted_run()
        with self.assertRaises(UserError):
            run.unlink()

    def test_posted_line_cannot_be_unlinked(self):
        run = self._posted_run()
        line = run.line_ids[:1]
        with self.assertRaises(UserError):
            line.unlink()

    # ---- (c) plain user cannot raw-reset state ----

    def test_plain_user_cannot_reset_posted_state(self):
        run = self._posted_run()
        plain = self.env['res.users'].create({
            'name': 'Plain Accountant',
            'login': 'plain_ye_user',
            'email': 'plain_ye@example.com',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        with self.assertRaises(UserError):
            run.with_user(plain).write({'state': 'draft'})
        # State unchanged.
        self.assertEqual(run.state, 'posted')

    # ---- (d) normal flow still works ----

    def test_normal_post_reverse_flow(self):
        run = self._posted_run()
        run.action_reverse()
        self.assertEqual(run.state, 'reversed')
        self.assertTrue(run.reversal_move_id)
        self.assertEqual(run.reversal_move_id.state, 'posted')
