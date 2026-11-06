# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Counterpart prediction from reconciliation history."""

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.eh_account_reconcile_pro.tests.common import (
    EhReconcileIntegrationTestCase,
)


@tagged('eh_account_reconcile_pro', 'integration', 'post_install',
        '-at_install')
class TestPredictor(EhReconcileIntegrationTestCase):

    def _train(self, payment_ref, account, partner):
        """Record one historical match: a statement line with the given
        text was reconciled to an AML on `account` for `partner`."""
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        move = self.post_balanced_move([
            {'account': account, 'debit': 100.0, 'partner': partner},
            {'account': self.account_cash, 'credit': 100.0},
        ])
        aml = move.line_ids.filtered(lambda l: l.account_id == account)
        train_line = self.make_statement_line(
            100.0, partner=partner, payment_ref=payment_ref)
        self.env['eh.reconciliation.audit'].sudo().create({
            'session_id': session.id,
            'statement_line_id': train_line.id,
            'aml_id': aml.id,
            'user_id': self.env.user.id,
            'decision': 'match',
            'source': 'manual',
            'confidence': 1.0,
            'rules_fired': '',
        })
        return aml

    def test_audit_rows_are_append_only(self):
        """The reconciliation-audit decision trail is strictly append-only:
        write and unlink are refused for everyone, and the old
        caller-supplied context flags no longer open a bypass."""
        aml = self._train('ACME APPEND ONLY', self.account_expense,
                           self.partner_a)
        audit = self.env['eh.reconciliation.audit'].search(
            [('aml_id', '=', aml.id)], limit=1)
        self.assertTrue(audit)
        with self.assertRaises(UserError):
            audit.write({'confidence': 0.0})
        # The old context flag must no longer bypass the guard.
        with self.assertRaises(UserError):
            audit.with_context(eh_internal_audit_write=True).write(
                {'decision': 'skip'})
        with self.assertRaises(UserError):
            audit.unlink()
        with self.assertRaises(UserError):
            audit.with_context(eh_internal_audit_unlink=True).unlink()
        with self.assertRaises(AccessError):
            self.env['eh.reconciliation.audit'].with_user(
                self.env.ref('base.user_admin')
            ).create({
                'session_id': audit.session_id.id,
                'statement_line_id': audit.statement_line_id.id,
                'aml_id': audit.aml_id.id,
                'user_id': self.env.user.id,
                'decision': 'match',
                'source': 'manual',
            })

    def test_predicts_account_and_partner_from_history(self):
        self._train('ACME RENT MONTHLY FEE',
                    self.account_expense, self.partner_a)
        engine = self.env['eh.reconciliation.suggestion.engine']
        new_line = self.make_statement_line(100.0, payment_ref='ACME RENT')

        prediction = engine.predict_counterpart(new_line)

        self.assertEqual(prediction.get('account_id'), self.account_expense.id)
        self.assertEqual(prediction.get('partner_id'), self.partner_a.id)
        self.assertGreater(prediction.get('account_score', 0.0), 0.0)
        self.assertEqual(prediction.get('account_support'), 1)

    def test_no_prediction_without_overlap(self):
        self._train('ACME RENT MONTHLY FEE',
                    self.account_expense, self.partner_a)
        engine = self.env['eh.reconciliation.suggestion.engine']
        new_line = self.make_statement_line(
            100.0, payment_ref='UNRELATED PAYROLL TRANSFER')

        prediction = engine.predict_counterpart(new_line)

        self.assertNotIn('account_id', prediction)
        self.assertNotIn('partner_id', prediction)

    def test_winner_is_most_frequent_overlapping_account(self):
        # Two histories share the 'ACME' token; the rent one also shares
        # 'RENT', so it must win for an 'ACME RENT' line.
        self._train('ACME RENT MONTHLY', self.account_expense, self.partner_a)
        self._train('ACME OTHER SERVICE', self.account_revenue, self.partner_b)
        engine = self.env['eh.reconciliation.suggestion.engine']
        new_line = self.make_statement_line(100.0, payment_ref='ACME RENT')

        prediction = engine.predict_counterpart(new_line)

        self.assertEqual(prediction.get('account_id'), self.account_expense.id)
        self.assertEqual(prediction.get('partner_id'), self.partner_a.id)
