# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Batch auto-reconciliation: high-confidence matches applied, ambiguous
and low-confidence lines left for the operator."""

from odoo.tests import tagged

from odoo.addons.eh_account_reconcile_pro.tests.common import (
    EhReconcileIntegrationTestCase,
)


@tagged('eh_account_reconcile_pro', 'integration', 'post_install',
        '-at_install')
class TestAutoReconcile(EhReconcileIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A minimal test chart can leave the bank journal's suspense
        # account non-reconcilable, which makes _perform_reconciliation a
        # silent no-op. Pin a reconcilable suspense so a confirmed match
        # actually clears the line.
        suspense = cls.bank_journal.suspense_account_id
        if not suspense:
            suspense = cls._ensure_account(
                cls.env, '1990', 'Bank Suspense', 'asset_current')
            cls.bank_journal.suspense_account_id = suspense
        suspense.reconcile = True

    def test_auto_reconcile_high_confidence_match(self):
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        aml = self.make_open_invoice_line(
            self.partner_a, 500.0, ref='INV500')
        line = self.make_statement_line(
            500.0, partner=self.partner_a, payment_ref='INV500',
            journal=self.bank_journal)

        summary = session.auto_reconcile()

        self.assertEqual(summary['reconciled'], 1)
        self.assertEqual(summary['by_score'], 1)
        self.assertIn(line.id, summary['matched_line_ids'])
        line.invalidate_recordset()
        aml.invalidate_recordset()
        self.assertTrue(aml.reconciled)
        self.assertTrue(line.is_reconciled)

    def test_auto_reconcile_skips_low_confidence(self):
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        # Open invoice that does not match the statement on amount or ref.
        self.make_open_invoice_line(self.partner_a, 500.0, ref='INVX')
        line = self.make_statement_line(
            999.0, partner=self.partner_a, payment_ref='NOMATCH',
            journal=self.bank_journal)

        summary = session.auto_reconcile()

        self.assertEqual(summary['reconciled'], 0)
        self.assertEqual(summary['skipped_low_score'], 1)
        self.assertFalse(line.is_reconciled)

    def test_auto_reconcile_no_candidate(self):
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        line = self.make_statement_line(
            123.0, partner=self.partner_b, payment_ref='ORPHAN',
            journal=self.bank_journal)

        summary = session.auto_reconcile()

        self.assertEqual(summary['reconciled'], 0)
        self.assertEqual(summary['skipped_no_candidate'], 1)
        self.assertFalse(line.is_reconciled)

    def test_auto_confirm_full_residual_writeoff_rule_is_operational(self):
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        rule = self.env['eh.reconciliation.rule'].create({
            'name': 'Reviewed small bank fee',
            'code': 'reviewed_small_bank_fee',
            'company_id': self.company.id,
            'rule_type': 'write_off',
            'payment_ref_regex': '^SAFE-FEE$',
            'amount_max': 5.0,
            'writeoff_account_id': self.account_expense.id,
            'writeoff_amount_type': 'residual',
            'auto_confirm': True,
        })
        line = self.make_statement_line(
            3.0, payment_ref='SAFE-FEE', journal=self.bank_journal,
        )

        summary = session.action_auto_reconcile()

        self.assertEqual(summary['reconciled'], 1)
        self.assertEqual(summary['by_rule'], 1)
        self.assertIn(line.id, summary['written_off_line_ids'])
        line.invalidate_recordset(['is_reconciled'])
        rule.invalidate_recordset(['fire_count'])
        self.assertTrue(line.is_reconciled)
        self.assertEqual(rule.fire_count, 1)
        audit = self.env['eh.reconciliation.audit'].search([
            ('statement_line_id', '=', line.id),
            ('decision', '=', 'write_off'),
        ])
        self.assertEqual(audit.source, 'rule')
        self.assertEqual(audit.rules_fired, rule.code)

    def test_auto_confirm_rule_is_recognised_below_score_threshold(self):
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        rule = self.env['eh.reconciliation.rule'].create({
            'name': 'Exact low-score auto rule',
            'code': 'exact_low_score_auto',
            'company_id': self.env.company.id,
            'rule_type': 'match',
            'payment_ref_regex': '^RULE-LOW-SCORE$',
            'partner_id': self.partner_a.id,
            'score_boost': 0.01,
            'auto_confirm': True,
        })
        aml = self.make_open_invoice_line(
            self.partner_a, 500.0, ref='unrelated-ledger-reference')
        line = self.make_statement_line(
            500.0, partner=self.partner_a,
            payment_ref='RULE-LOW-SCORE', journal=self.bank_journal,
        )

        summary = session.auto_reconcile(threshold=0.99)

        self.assertEqual(summary['reconciled'], 1)
        self.assertEqual(summary['by_rule'], 1)
        self.assertEqual(summary['by_score'], 0)
        self.assertIn(line.id, summary['matched_line_ids'])
        rule.invalidate_recordset(['fire_count'])
        aml.invalidate_recordset()
        self.assertEqual(rule.fire_count, 1)
        self.assertTrue(aml.reconciled)
