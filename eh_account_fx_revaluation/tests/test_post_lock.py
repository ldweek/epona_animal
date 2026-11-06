# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression: action_post / action_reverse row-lock idempotency.

A double click or a browser retry sends two concurrent RPCs into
action_post. Both used to read state=='computed', both built and posted a
balanced revaluation move, and both stamped 'posted' - leaving TWO posted
entries for one run and orphaning the first move (unrealised FX result
silently doubled). action_post now takes a SELECT ... FOR UPDATE row lock
and re-reads the committed state before building the move, so the loser
transaction observes 'posted'/'reversed' and stops at the state guard.

The cross-transaction race cannot be reproduced inside a single test
transaction, so the decisive test primes a STALE ORM cache (the losing
transaction's pre-post snapshot) with a raw SQL state flip and proves the
lock's invalidate/re-read refuses to book a second move.
"""

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase

from .compat import add_user_groups


@tagged('eh_account_fx_revaluation', 'integration', 'post_install', '-at_install')
class TestFxPostLock(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        add_user_groups(
            cls.env.user,
            cls.env.ref('account.group_account_manager'),
            cls.env.ref('eh_account_base.group_eh_manager'),
        )

        cls.eur = cls.env.ref('base.EUR')
        cls.eur.active = True
        cls.usd = cls.env.ref('base.USD')
        cls.usd.active = True

        cls.gain_account = cls._ensure_account(
            cls.env, '4921', 'Unrealised FX Gain (lock)', 'income_other',
        )
        cls.loss_account = cls._ensure_account(
            cls.env, '5931', 'Unrealised FX Loss (lock)', 'expense',
        )
        cls.account_receivable.eh_fx_revalue = True

        Rate = cls.env['res.currency.rate']
        Rate.search([('currency_id', '=', cls.eur.id)]).unlink()
        Rate.create({
            'currency_id': cls.eur.id,
            'name': '2026-01-01',
            'rate': 1.0,
            'company_id': cls.company.id,
        })

    # ---- fixtures ----

    def _post_eur_invoice_balance(self, amount_eur, amount_company):
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': '2026-02-15',
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {
                    'name': 'EUR receivable',
                    'account_id': self.account_receivable.id,
                    'partner_id': self.partner_a.id,
                    'currency_id': self.eur.id,
                    'amount_currency': amount_eur,
                    'debit': amount_company,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': 'EUR revenue (company ccy)',
                    'account_id': self.account_revenue.id,
                    'debit': 0.0,
                    'credit': amount_company,
                }),
            ],
        })
        move.action_post()
        return move

    def _set_closing_rate(self, date_str, rate):
        self.env['res.currency.rate'].create({
            'currency_id': self.eur.id,
            'name': date_str,
            'rate': rate,
            'company_id': self.company.id,
        })

    def _computed_run(self):
        """A run in 'computed' state carrying a non-zero revaluation line."""
        self._post_eur_invoice_balance(1000.0, 1000.0)
        self._set_closing_rate('2026-03-31', 1.25)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        self.assertTrue(run.line_ids)
        return run

    def _misc_move_count(self):
        return self.env['account.move'].search_count(
            [('journal_id', '=', self.journal_misc.id)])

    def _set_company_lock(self, field_name, value):
        if field_name not in self.company._fields:
            self.skipTest('%s is not available on this Odoo series' % field_name)
        self.env.flush_all()
        self.env.cr.execute(
            'UPDATE res_company SET %s = %%s WHERE id = %%s' % field_name,
            (value, self.company.id),
        )
        self.company.invalidate_recordset([field_name])

    # ---- happy path still books exactly one move ----

    def test_post_books_exactly_one_move(self):
        run = self._computed_run()
        before = self._misc_move_count()
        run.action_post()
        after = self._misc_move_count()
        self.assertEqual(run.state, 'posted')
        self.assertTrue(run.move_id)
        self.assertEqual(run.move_id.state, 'posted')
        self.assertEqual(
            after - before, 1,
            "Posting must create exactly one revaluation move.")

    def test_fiscalyear_lock_preflight_creates_no_entry(self):
        run = self._computed_run()
        before = self._misc_move_count()
        self._set_company_lock('fiscalyear_lock_date', '2026-03-31')

        with self.assertRaises(UserError):
            run.action_post()

        self.assertEqual(run.state, 'computed')
        self.assertFalse(run.move_id)
        self.assertEqual(self._misc_move_count(), before)

    def test_hard_lock_preflight_creates_no_entry(self):
        run = self._computed_run()
        before = self._misc_move_count()
        self._set_company_lock('hard_lock_date', '2026-03-31')

        with self.assertRaises(UserError):
            run.action_post()

        self.assertEqual(run.state, 'computed')
        self.assertFalse(run.move_id)
        self.assertEqual(self._misc_move_count(), before)

    def test_tax_lock_does_not_move_nontax_revaluation_date(self):
        run = self._computed_run()
        self._set_company_lock('tax_lock_date', '2026-03-31')

        run.action_post()

        self.assertEqual(str(run.move_id.date), '2026-03-31')

    def test_manual_reversal_date_is_prechecked_against_new_lock(self):
        run = self._computed_run()
        run.action_post()
        self.assertEqual(run.state, 'posted')
        before = self._misc_move_count()
        self._set_company_lock('fiscalyear_lock_date', str(run.reversal_date))

        with self.assertRaises(UserError):
            run.action_reverse()

        self.assertEqual(run.state, 'posted')
        self.assertFalse(run.reversal_move_id)
        self.assertEqual(self._misc_move_count(), before)

    # ---- ORM-level idempotency ----

    def test_repost_after_posted_creates_no_second_move(self):
        run = self._computed_run()
        run.action_post()
        first_move = run.move_id
        before = self._misc_move_count()
        with self.assertRaises(UserError):
            run.action_post()
        after = self._misc_move_count()
        self.assertEqual(
            before, after,
            "Re-posting a posted run must not book a second move.")
        self.assertEqual(
            run.move_id, first_move,
            "move_id must still point at the single original entry.")

    # ---- the decisive lock test: stale cache is re-read under the lock ----

    def test_stale_cache_repost_is_refused_by_lock_reread(self):
        """Simulate the losing transaction: its ORM cache still says
        'computed' while a committed concurrent post has flipped the DB row
        to 'posted'. The FOR UPDATE + invalidate in _eh_lock_for_post must
        re-read 'posted' and refuse, so no second move is booked.

        On the pre-fix code (no lock, no re-read) action_post trusts the
        stale cached 'computed', builds and posts a move, and this test's
        move-count assertion fails.
        """
        run = self._computed_run()

        # Land a clean, non-dirty 'computed' snapshot in the shared cache.
        run.flush_recordset()
        run.invalidate_recordset()
        self.assertEqual(run.state, 'computed')

        # A concurrent transaction posts the run; our cache stays stale.
        self.env.cr.execute(
            "UPDATE eh_fx_revaluation_run SET state = 'posted' WHERE id = %s",
            (run.id,),
        )

        before = self._misc_move_count()
        with self.assertRaises(UserError):
            run.action_post()
        after = self._misc_move_count()
        self.assertEqual(
            before, after,
            "The lock must re-read the committed state and book no move.")

    def test_transition_locks_input_epochs_not_company_business_row(self):
        run = self._computed_run()
        cursor_class = type(self.env.cr)
        original_execute = cursor_class.execute
        statements = []

        def trace_execute(cursor, query, *args, **kwargs):
            if cursor is self.env.cr:
                statements.append(str(query))
            return original_execute(cursor, query, *args, **kwargs)

        with patch.object(cursor_class, 'execute', trace_execute):
            run._eh_lock_for_transition()

        sql = ' '.join(' '.join(item.split()) for item in statements)
        self.assertIn('eh_account_report_global_version', sql)
        self.assertIn('eh_account_report_company_version', sql)
        self.assertNotIn(
            'SELECT id FROM res_company WHERE id IN', sql,
            "FX transitions must not serialize on the res_company row",
        )
