# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Posting a reclassification must never silently mutate the suspense
account's reconcile configuration.

Config immutability: a live posting path must not rewrite chart-of-accounts
settings as a side effect. When the suspense account carrying the open
statement-line residual is not reconcilable, the reclassification cannot
clear against the original suspense line; ``_post_reclassification_entry``
must refuse with a clear UserError naming the account rather than silently
flipping ``account.reconcile`` to True.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_reconcile_pro.tests.common import (
    EhReconcileIntegrationTestCase,
)


@tagged('eh_account_reconcile_pro', 'integration', 'post_install',
        '-at_install')
class TestSuspenseConfigImmutable(EhReconcileIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A dedicated suspense-style account we fully control. Start it
        # reconcilable so a genuine open (unreconciled, nonzero-residual)
        # line can be produced on it.
        cls.suspense_account = cls._ensure_account(
            cls.env, '1991', 'Reclass Suspense', 'asset_current')
        cls.suspense_account.reconcile = True

    def _make_open_suspense_line(self, amount):
        """Post a balanced entry that leaves an open, unreconciled line with
        a nonzero residual on the controlled suspense account, and return
        that line."""
        move = self.post_balanced_move([
            {'account': self.suspense_account, 'debit': amount},
            {'account': self.account_revenue, 'credit': amount},
        ])
        line = move.line_ids.filtered(
            lambda l: l.account_id == self.suspense_account)
        # Sanity: a reconcilable account carries a nonzero residual.
        self.assertTrue(line)
        self.assertFalse(line.reconciled)
        return line

    def test_reclass_on_non_reconcilable_account_raises_and_keeps_config(self):
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        open_suspense = self._make_open_suspense_line(30.0)

        # Misconfigure: the account under the open residual is not
        # reconcilable at reclassification time.
        self.suspense_account.reconcile = False
        self.assertFalse(self.suspense_account.reconcile)

        # Posting the reclassification must refuse rather than silently
        # rewriting the account's reconcile flag.
        with self.assertRaises(UserError):
            session._post_reclassification_entry(
                open_suspense, self.account_expense, 'Reclass')

        # The account's reconcile config is byte-identical to what the user
        # set: the live posting path did not mutate chart-of-accounts config.
        self.suspense_account.invalidate_recordset()
        self.assertFalse(
            self.suspense_account.reconcile,
            "Posting silently flipped the suspense account's reconcile "
            "configuration; a live posting path must never mutate "
            "chart-of-accounts settings as a side effect.")

        # The refusal is UP FRONT: no adjusting entry was created at all (in
        # any state) on the target account. This distinguishes the correct
        # fix (raise before creating any move) from a path that creates/posts
        # a move and only then fails on the native non-reconcilable guard.
        written = self.env['account.move.line'].search([
            ('account_id', '=', self.account_expense.id),
            ('move_id', '!=', open_suspense.move_id.id),
        ])
        self.assertFalse(
            written,
            "A counter move was created before the refusal; the config check "
            "must reject up front, before any adjusting entry exists.")

    def test_reclass_on_reconcilable_account_still_posts(self):
        # Control: a properly configured (reconcilable) account reclassifies
        # as before. Default/unset behaviour is unchanged.
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        open_suspense = self._make_open_suspense_line(30.0)
        self.assertTrue(self.suspense_account.reconcile)

        adjusting = session._post_reclassification_entry(
            open_suspense, self.account_expense, 'Reclass')

        self.assertEqual(adjusting.state, 'posted')
        open_suspense.invalidate_recordset()
        self.assertTrue(open_suspense.reconciled)
        # The reclassification landed on the target account.
        self.assertIn(
            self.account_expense,
            adjusting.line_ids.mapped('account_id'))
