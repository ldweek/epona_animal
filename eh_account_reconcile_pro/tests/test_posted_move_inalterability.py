# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Audit inalterability: write-off and reconciliation must never redraft
and edit a posted statement move. Both paths must instead leave the
original posted move immutable and book a balanced adjusting entry that
carries the suspense balance onto the target account.
"""

from odoo.tests import tagged

from odoo.addons.eh_account_reconcile_pro.tests.common import (
    EhReconcileIntegrationTestCase,
)


@tagged('eh_account_reconcile_pro', 'integration', 'post_install',
        '-at_install')
class TestPostedMoveInalterability(EhReconcileIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suspense = cls.bank_journal.suspense_account_id
        if not suspense:
            suspense = cls._ensure_account(
                cls.env, '1990', 'Bank Suspense', 'asset_current')
            cls.bank_journal.suspense_account_id = suspense
        suspense.reconcile = True
        cls.suspense_account = suspense

    def _snapshot(self, move):
        """Capture the immutable-relevant state of a posted move."""
        return {
            'state': move.state,
            'name': move.name,
            'line_accounts': tuple(sorted(move.line_ids.mapped('account_id.id'))),
            'line_balances': tuple(sorted(
                round(l.balance, 2) for l in move.line_ids)),
            'write_date': move.write_date,
        }

    def test_write_off_leaves_posted_move_immutable(self):
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        line = self.make_statement_line(7.50, payment_ref='Bank fee')
        move = line.move_id
        self.assertEqual(move.state, 'posted')
        before = self._snapshot(move)

        session.apply_write_off(
            line.id, self.account_expense.id, label='Bank fee')

        move.invalidate_recordset()
        line.invalidate_recordset()
        after = self._snapshot(move)

        # The original posted statement move must be byte-identical in the
        # ways that matter for audit inalterability: same state, same name,
        # same account distribution, same balances. The write-off account
        # must NOT have been grafted onto a line of the original move.
        self.assertEqual(before['state'], after['state'])
        self.assertEqual(before['name'], after['name'])
        self.assertEqual(before['line_accounts'], after['line_accounts'])
        self.assertEqual(before['line_balances'], after['line_balances'])
        self.assertNotIn(
            self.account_expense.id, after['line_accounts'],
            "Write-off account was grafted onto the original posted move; "
            "the posted move was mutated in place.")

        # The reclassification must live on a SEPARATE adjusting entry.
        self.assertTrue(line.is_reconciled)
        adjusting = self.env['account.move'].search([
            ('id', '!=', move.id),
            ('journal_id', '=', self.bank_journal.id),
            ('state', '=', 'posted'),
            ('line_ids.account_id', '=', self.account_expense.id),
        ])
        self.assertTrue(
            adjusting,
            "No adjusting entry carries the write-off reclassification.")
        # The adjusting entry touches both the suspense and write-off
        # accounts and is balanced by construction.
        adj = adjusting[0]
        self.assertIn(self.suspense_account.id,
                      adj.line_ids.mapped('account_id.id'))
        self.assertIn(self.account_expense.id,
                      adj.line_ids.mapped('account_id.id'))
        self.assertAlmostEqual(
            sum(adj.line_ids.mapped('balance')), 0.0, places=2)

    def test_reconcile_leaves_posted_move_immutable(self):
        partner = self.env['res.partner'].create({'name': 'Recon Cust'})
        # Open receivable of 100 that the statement line of 100 should clear.
        recv_aml = self.make_open_invoice_line(partner, 100.0)
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        line = self.make_statement_line(
            100.0, partner=partner, payment_ref='Invoice payment')
        move = line.move_id
        self.assertEqual(move.state, 'posted')
        before = self._snapshot(move)
        receivable_id = self.account_receivable.id

        session.apply_match(line.id, [recv_aml.id], source='manual')

        move.invalidate_recordset()
        recv_aml.invalidate_recordset()
        after = self._snapshot(move)

        self.assertEqual(before['state'], after['state'])
        self.assertEqual(before['name'], after['name'])
        self.assertEqual(before['line_accounts'], after['line_accounts'])
        self.assertEqual(before['line_balances'], after['line_balances'])
        self.assertNotIn(
            receivable_id, after['line_accounts'],
            "Receivable account was grafted onto the original posted "
            "statement move; the posted move was mutated in place.")

        # The candidate receivable AML must actually be cleared, and a
        # separate adjusting entry must carry the reclassification onto the
        # receivable account.
        self.assertTrue(recv_aml.reconciled)
        adjusting = self.env['account.move'].search([
            ('id', '!=', move.id),
            ('journal_id', '=', self.bank_journal.id),
            ('state', '=', 'posted'),
            ('line_ids.account_id', '=', receivable_id),
        ])
        self.assertTrue(
            adjusting,
            "No adjusting entry carries the reconciliation reclassification.")
        adj = adjusting[0]
        self.assertAlmostEqual(
            sum(adj.line_ids.mapped('balance')), 0.0, places=2)
        target_line = adj.line_ids.filtered(
            lambda line: line.account_id == self.account_receivable
        )
        self.assertEqual(target_line.partner_id, partner)

    def test_reclassification_uses_candidate_not_statement_partner(self):
        statement_partner = self.env['res.partner'].create({
            'name': 'Wrong statement partner',
        })
        candidate_partner = self.env['res.partner'].create({
            'name': 'Candidate control partner',
        })
        receivable = self.make_open_invoice_line(candidate_partner, 100.0)
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id,
        )
        statement = self.make_statement_line(
            100.0,
            partner=statement_partner,
            payment_ref='Candidate payment',
        )

        session.apply_match(statement.id, receivable.ids, source='manual')

        adjusting = self.env['account.move'].search([
            ('id', '!=', statement.move_id.id),
            ('journal_id', '=', self.bank_journal.id),
            ('state', '=', 'posted'),
            ('line_ids.account_id', '=', self.account_receivable.id),
        ], order='id desc', limit=1)
        target_line = adjusting.line_ids.filtered(
            lambda line: line.account_id == self.account_receivable
        )
        suspense_line = (adjusting.line_ids - target_line)
        self.assertEqual(target_line.partner_id, candidate_partner)
        self.assertEqual(suspense_line.partner_id, statement_partner)
