# -*- encoding: utf-8 -*-
"""Controlled undo preserves immutable ledger and audit provenance."""

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhReconcileIntegrationTestCase


@tagged('eh_account_reconcile_pro', 'integration', 'post_install',
        '-at_install')
class TestControlledReconciliationUndo(EhReconcileIntegrationTestCase):

    def test_match_undo_reopens_exact_graph_and_posts_sealed_reversal(self):
        self.env.user.group_ids |= self.env.ref(
            'eh_account_base.group_eh_manager'
        )
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id,
        )
        statement = self.make_statement_line(
            100.0, partner=self.partner_a, payment_ref='UNDO-MATCH',
        )
        candidate = self.make_open_invoice_line(
            self.partner_a, 100.0, ref='UNDO-MATCH',
        )
        session.apply_match(statement.id, candidate.ids, source='manual')
        audit = self.env['eh.reconciliation.audit'].search([
            ('statement_line_id', '=', statement.id),
            ('decision', '=', 'match'),
        ], limit=1)
        adjusting = audit.adjusting_move_id
        self.assertTrue(audit.decision_group)
        self.assertTrue(adjusting)
        self.assertTrue(adjusting.eh_sealed)

        with self.assertRaisesRegex(UserError, 'Controlled Undo'):
            candidate.remove_move_reconcile()

        undo = audit._eh_undo(
            max(fields.Date.today(), adjusting.date),
            'Customer remittance was assigned to the wrong open invoice.',
        )

        statement.invalidate_recordset(['is_reconciled'])
        candidate.invalidate_recordset(['reconciled', 'amount_residual'])
        self.assertFalse(statement.is_reconciled)
        self.assertFalse(candidate.reconciled)
        self.assertAlmostEqual(candidate.amount_residual, 100.0, places=2)
        self.assertEqual(undo.decision, 'undo')
        self.assertEqual(undo.decision_group, audit.decision_group)
        self.assertEqual(undo.original_audit_id, audit)
        self.assertTrue(undo.reversal_move_id)
        self.assertEqual(undo.reversal_move_id.state, 'posted')
        self.assertTrue(undo.reversal_move_id.eh_sealed)
        self.assertEqual(
            undo.reversal_move_id.reversed_entry_id, adjusting,
        )
        self.assertEqual(adjusting.state, 'posted')
        self.assertTrue(adjusting.eh_sealed)
        with self.assertRaisesRegex(UserError, 'already been undone'):
            audit._eh_undo(
                fields.Date.today(),
                'Second undo attempt must never mutate the ledger again.',
            )
