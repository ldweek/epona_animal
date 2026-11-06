# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression: ECL run action_post / action_reverse row-lock idempotency.

A double click or a retried RPC sends two concurrent transactions into
action_post. Both used to read state=='computed', both built and posted a
balanced impairment move, and both stamped 'posted' - leaving TWO posted
entries for one run and orphaning the first move, so the IFRS 9.5.5.8 loss
allowance was silently doubled. action_post now takes a SELECT ... FOR UPDATE
row lock and re-reads the committed state before building the move, so the
loser transaction observes 'posted'/'reversed' and stops at the state guard.

The cross-transaction race cannot be reproduced inside a single test
transaction, so the decisive test primes a STALE ORM cache (the losing
transaction's pre-post snapshot) with a raw SQL state flip and proves the
lock's invalidate/re-read refuses to book a second move.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_ecl', 'integration', 'post_install', '-at_install')
class TestEclPostLock(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.impairment = cls._ensure_account(
            cls.env, '5292', 'Impairment Loss (lock)', 'expense')
        cls.allowance = cls._ensure_account(
            cls.env, '1292', 'Loss Allowance (lock)', 'asset_current')

    # ---- fixtures ----

    def _computed_run(self):
        """A run in 'computed' state carrying a non-zero allowance movement."""
        run = self.env['eh.ecl.run'].create({
            'reporting_date': '2026-06-30',
            'reversal_date': '2026-06-30',
            'opening_allowance': 0.0,
            'impairment_expense_account_id': self.impairment.id,
            'loss_allowance_account_id': self.allowance.id,
            'journal_id': self.journal_misc.id,
            'bucket_ids': [(0, 0, {
                'name': '90+', 'days_from': 91, 'days_to': 0,
                'loss_rate': 25.0, 'stage': '3', 'gross_carrying': 1000.0,
            })],
        })
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        self.assertTrue(run.movement, "Fixture must carry a non-zero movement.")
        return run

    def _ecl_move_count(self):
        return self.env['account.move'].search_count(
            [('journal_id', '=', self.journal_misc.id)])

    # ---- happy path still books exactly one move ----

    def test_post_books_exactly_one_move(self):
        run = self._computed_run()
        before = self._ecl_move_count()
        run.action_post()
        after = self._ecl_move_count()
        self.assertEqual(run.state, 'posted')
        self.assertTrue(run.move_id)
        self.assertEqual(run.move_id.state, 'posted')
        self.assertEqual(
            after - before, 1,
            "Posting must create exactly one impairment move.")

    # ---- ORM-level idempotency ----

    def test_repost_after_posted_creates_no_second_move(self):
        run = self._computed_run()
        run.action_post()
        first_move = run.move_id
        before = self._ecl_move_count()
        with self.assertRaises(UserError):
            run.action_post()
        after = self._ecl_move_count()
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
        re-read 'posted' and refuse, so no second impairment move is booked.

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
            "UPDATE eh_ecl_run SET state = 'posted' WHERE id = %s",
            (run.id,),
        )

        before = self._ecl_move_count()
        with self.assertRaises(UserError):
            run.action_post()
        after = self._ecl_move_count()
        self.assertEqual(
            before, after,
            "The lock must re-read the committed state and book no move.")

    def test_stale_cache_reverse_is_refused_by_lock_reread(self):
        """The same lock protects action_reverse: a stale 'posted' cache while
        the DB row has already moved to 'reversed' must re-read 'reversed' and
        refuse, so no second reversal move is booked.
        """
        run = self._computed_run()
        run.action_post()
        self.assertEqual(run.state, 'posted')

        run.flush_recordset()
        run.invalidate_recordset()
        self.assertEqual(run.state, 'posted')

        # A concurrent transaction has already reversed the run.
        self.env.cr.execute(
            "UPDATE eh_ecl_run SET state = 'reversed' WHERE id = %s",
            (run.id,),
        )

        before = self._ecl_move_count()
        with self.assertRaises(UserError):
            run.action_reverse()
        after = self._ecl_move_count()
        self.assertEqual(
            before, after,
            "The lock must re-read 'reversed' and book no reversal move.")
