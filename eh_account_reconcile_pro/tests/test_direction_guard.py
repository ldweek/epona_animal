# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Reconciliation direction guard.

A bank statement line may only clear candidate journal items that sit on
the ledger side its reclassified suspense can actually offset. An amount
received (positive) can only net a debit residual (open receivable or a
vendor refund); an amount paid (negative) can only net a credit residual
(open payable or a customer refund).

Matching across the wrong side (a customer deposit pointed at a vendor
bill, both credits) is where core reconcile() silently no-ops: the
suspense clears against the reclass counter-leg, marking the statement
line reconciled, while the cash lands on the wrong-side account and the
candidate stays open. These tests prove the pairing is refused up front,
that no adjusting entry is left behind, and that legitimate same-side
matches (including the outbound-to-payable direction) still succeed.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_reconcile_pro.tests.common import (
    EhReconcileIntegrationTestCase,
)


@tagged('eh_account_reconcile_pro', 'integration', 'post_install',
        '-at_install')
class TestDirectionGuard(EhReconcileIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suspense = cls.bank_journal.suspense_account_id
        if not suspense:
            suspense = cls._ensure_account(
                cls.env, '1990', 'Bank Suspense', 'asset_current')
            cls.bank_journal.suspense_account_id = suspense
        if not suspense.reconcile:
            suspense.sudo().reconcile = True
        cls.suspense_account = suspense

    @classmethod
    def make_open_payable_line(cls, partner, amount, date=None, ref=None):
        """Create and post a balanced entry that produces an open payable
        AML (credit residual, amount_residual < 0) for the partner, as a
        vendor bill's payable leg would. Returns the payable AML."""
        move = cls.post_balanced_move(
            [
                {'account': cls.account_expense, 'debit': amount},
                {'account': cls.account_payable, 'credit': amount,
                 'partner': partner},
            ],
            date=date,
        )
        if ref:
            move.ref = ref
        return move.line_ids.filtered(
            lambda l: l.account_id == cls.account_payable
        )

    @classmethod
    def make_credit_note_receivable_line(cls, partner, amount, date=None,
                                         ref=None):
        """Create and post a balanced entry that produces an open receivable
        AML with a CREDIT residual (amount_residual < 0), as a customer
        credit note's receivable leg would. Sits on the same receivable
        account as :meth:`make_open_invoice_line` so the two can net against
        one bank line. Returns the receivable AML."""
        move = cls.post_balanced_move(
            [
                {'account': cls.account_receivable, 'credit': amount,
                 'partner': partner},
                {'account': cls.account_revenue, 'debit': amount},
            ],
            date=date,
        )
        if ref:
            move.ref = ref
        return move.line_ids.filtered(
            lambda l: l.account_id == cls.account_receivable
        )

    def _posted_move_ids_on(self, account):
        """Return the set of posted move ids that touch ``account``."""
        return set(self.env['account.move'].search([
            ('state', '=', 'posted'),
            ('line_ids.account_id', '=', account.id),
        ]).ids)

    def test_inbound_line_rejects_payable_candidate(self):
        """A received amount matched to an open payable (both credits) is
        refused; no reclass posts and nothing is marked reconciled."""
        partner = self.env['res.partner'].create({'name': 'Contra ACME'})
        payable_aml = self.make_open_payable_line(partner, 500.0)
        # An open payable is a credit residual (opposite side to a receipt).
        self.assertLess(payable_aml.amount_residual, 0.0)

        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        line = self.make_statement_line(
            500.0, partner=partner, payment_ref='Customer deposit')

        moves_before = self._posted_move_ids_on(self.account_payable)

        with self.assertRaises(UserError):
            session.apply_match(line.id, payable_aml.ids, source='manual')

        line.invalidate_recordset()
        payable_aml.invalidate_recordset()

        # The wrong-side pairing must leave everything untouched: the
        # candidate stays open, the statement line is not reconciled, and
        # no adjusting reclass entry was grafted onto the payable account.
        self.assertFalse(payable_aml.reconciled)
        self.assertAlmostEqual(payable_aml.amount_residual, -500.0, places=2)
        self.assertFalse(line.is_reconciled)
        self.assertEqual(
            self._posted_move_ids_on(self.account_payable), moves_before,
            "A reclassification entry was posted onto the payable account "
            "for a wrong-side match.")
        # No audit 'match' row may claim this reconciliation happened.
        self.assertFalse(self.env['eh.reconciliation.audit'].search_count([
            ('statement_line_id', '=', line.id),
            ('decision', '=', 'match'),
        ]))

    def test_outbound_line_rejects_receivable_candidate(self):
        """Symmetric case: a paid amount matched to an open receivable
        (both debits) is refused."""
        partner = self.env['res.partner'].create({'name': 'Contra BETA'})
        recv_aml = self.make_open_invoice_line(partner, 400.0)
        self.assertGreater(recv_aml.amount_residual, 0.0)

        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        line = self.make_statement_line(
            -400.0, partner=partner, payment_ref='Supplier payment')

        with self.assertRaises(UserError):
            session.apply_match(line.id, recv_aml.ids, source='manual')

        line.invalidate_recordset()
        recv_aml.invalidate_recordset()
        self.assertFalse(recv_aml.reconciled)
        self.assertFalse(line.is_reconciled)

    def test_auto_reconcile_skips_wrong_side_candidate(self):
        """Auto-reconcile must not match an inbound line to a top-scoring
        but wrong-side payable candidate; it is skipped, not booked."""
        partner = self.env['res.partner'].create({'name': 'Contra GAMMA'})
        # Only a payable candidate exists: same amount, same partner, same
        # ref, so it scores at the top - yet it is the wrong side.
        self.make_open_payable_line(partner, 750.0, ref='REF750')
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        line = self.make_statement_line(
            750.0, partner=partner, payment_ref='REF750',
            journal=self.bank_journal)

        summary = session.auto_reconcile()

        self.assertEqual(summary['reconciled'], 0)
        self.assertNotIn(line.id, summary['matched_line_ids'])
        line.invalidate_recordset()
        self.assertFalse(line.is_reconciled)

    def test_outbound_line_matches_payable_candidate(self):
        """Guard does not over-block: a paid amount clears an open payable
        (same side) and both the candidate and the line reconcile."""
        partner = self.env['res.partner'].create({'name': 'Vendor DELTA'})
        payable_aml = self.make_open_payable_line(partner, 300.0)
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        line = self.make_statement_line(
            -300.0, partner=partner, payment_ref='Bill payment')

        session.apply_match(line.id, payable_aml.ids, source='manual')

        line.invalidate_recordset()
        payable_aml.invalidate_recordset()
        self.assertTrue(payable_aml.reconciled)
        self.assertTrue(line.is_reconciled)

    def test_inbound_line_matches_receivable_candidate(self):
        """A received amount clears an open receivable (same side)."""
        partner = self.env['res.partner'].create({'name': 'Customer EPS'})
        recv_aml = self.make_open_invoice_line(partner, 250.0)
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        line = self.make_statement_line(
            250.0, partner=partner, payment_ref='Invoice receipt')

        session.apply_match(line.id, recv_aml.ids, source='manual')

        line.invalidate_recordset()
        recv_aml.invalidate_recordset()
        self.assertTrue(recv_aml.reconciled)
        self.assertTrue(line.is_reconciled)

    def test_inbound_line_matches_invoice_plus_credit_note_group(self):
        """Over-restriction fix: an invoice (debit residual) netted against a
        credit note (credit residual) on one receivable account clears a
        single net bank line. The group carries an opposite-sign member, but
        its AGGREGATE residual matches the statement sign, so the match must
        succeed and both candidates plus the line must reconcile."""
        partner = self.env['res.partner'].create({'name': 'Net Customer ZETA'})
        inv_aml = self.make_open_invoice_line(partner, 100.0)
        cn_aml = self.make_credit_note_receivable_line(partner, 30.0)
        # Same receivable account, opposite residual signs, net +70.
        self.assertEqual(inv_aml.account_id, cn_aml.account_id)
        self.assertGreater(inv_aml.amount_residual, 0.0)
        self.assertLess(cn_aml.amount_residual, 0.0)

        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        line = self.make_statement_line(
            70.0, partner=partner, payment_ref='Net settlement')

        # The OWL widget calls apply_match with the whole candidate set for a
        # bulk/drag match; the per-member direction check used to reject the
        # credit note here.
        group = inv_aml + cn_aml
        session.apply_match(line.id, group.ids, source='bulk')

        line.invalidate_recordset()
        group.invalidate_recordset()
        self.assertTrue(inv_aml.reconciled)
        self.assertTrue(cn_aml.reconciled)
        self.assertTrue(line.is_reconciled)
        # The full net reconciliation was recorded, one audit row per member.
        self.assertEqual(
            self.env['eh.reconciliation.audit'].search_count([
                ('statement_line_id', '=', line.id),
                ('decision', '=', 'match'),
            ]), 2)

    def test_inbound_line_rejects_group_that_nets_wrong_side(self):
        """Hole stays closed for groups: a candidate SET whose members sit on
        the same receivable account but whose AGGREGATE residual is a net
        CREDIT (-70) is still refused for a received (+70) bank line, even
        though the group contains a correct-side member. The aggregate guard
        must not let a net wrong-side group through."""
        partner = self.env['res.partner'].create({'name': 'Wrong Net ETA'})
        # Small invoice (+30 debit) plus a large credit note (-100 credit) ->
        # net residual -70, the wrong side for a +70 receipt.
        inv_aml = self.make_open_invoice_line(partner, 30.0)
        cn_aml = self.make_credit_note_receivable_line(partner, 100.0)
        net = sum((inv_aml + cn_aml).mapped('amount_residual'))
        self.assertAlmostEqual(net, -70.0, places=2)

        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        line = self.make_statement_line(
            70.0, partner=partner, payment_ref='Net settlement')

        moves_before = self._posted_move_ids_on(self.account_receivable)

        with self.assertRaises(UserError):
            session.apply_match(
                line.id, (inv_aml + cn_aml).ids, source='bulk')

        line.invalidate_recordset()
        (inv_aml + cn_aml).invalidate_recordset()
        # Nothing posted, nothing reconciled, no match audit row.
        self.assertFalse(inv_aml.reconciled)
        self.assertFalse(cn_aml.reconciled)
        self.assertFalse(line.is_reconciled)
        self.assertEqual(
            self._posted_move_ids_on(self.account_receivable), moves_before,
            "A reclassification entry was posted for a net wrong-side group.")
        self.assertFalse(self.env['eh.reconciliation.audit'].search_count([
            ('statement_line_id', '=', line.id),
            ('decision', '=', 'match'),
        ]))
