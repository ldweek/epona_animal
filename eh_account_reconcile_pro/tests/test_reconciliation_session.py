# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Reconciliation session lifecycle and counter tests.

Covers open_or_create idempotency, action_close lifecycle, audit row
creation on apply_match / apply_write_off / apply_skip, counter
increments, and the closed session guard.
"""

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhReconcileIntegrationTestCase


@tagged('eh_account_reconcile_pro', 'integration', 'post_install', '-at_install')
class TestSessionLifecycle(EhReconcileIntegrationTestCase):

    def test_open_or_create_creates_when_absent(self):
        Session = self.env['eh.reconciliation.session']
        session = Session.open_or_create(self.bank_journal.id)
        self.assertTrue(session.exists())
        self.assertEqual(session.state, 'open')
        self.assertEqual(session.user_id, self.env.user)
        self.assertEqual(session.journal_id, self.bank_journal)

    def test_open_or_create_returns_existing(self):
        Session = self.env['eh.reconciliation.session']
        first = Session.open_or_create(self.bank_journal.id)
        second = Session.open_or_create(self.bank_journal.id)
        self.assertEqual(first.id, second.id)

    def test_open_or_create_creates_new_after_close(self):
        Session = self.env['eh.reconciliation.session']
        first = Session.open_or_create(self.bank_journal.id)
        first.action_close()
        second = Session.open_or_create(self.bank_journal.id)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(second.state, 'open')

    def test_action_close_sets_closed_at_and_state(self):
        Session = self.env['eh.reconciliation.session']
        session = Session.open_or_create(self.bank_journal.id)
        session.action_close()
        self.assertEqual(session.state, 'closed')
        self.assertTrue(session.closed_at)
        self.assertGreater(session.duration_seconds, -1)

    def test_action_close_idempotent_on_already_closed(self):
        Session = self.env['eh.reconciliation.session']
        session = Session.open_or_create(self.bank_journal.id)
        session.action_close()
        first_closed_at = session.closed_at
        session.action_close()
        self.assertEqual(session.closed_at, first_closed_at)


@tagged('eh_account_reconcile_pro', 'integration', 'post_install', '-at_install')
class TestSessionDecisions(EhReconcileIntegrationTestCase):

    def setUp(self):
        super().setUp()
        Session = self.env['eh.reconciliation.session']
        self.session = Session.open_or_create(self.bank_journal.id)

    def test_workspace_writeoff_options_use_current_company_accounts(self):
        options = self.session.get_writeoff_options()

        self.assertIn(
            self.account_expense.id,
            [option['id'] for option in options],
        )

    def test_apply_match_creates_audit_row(self):
        sl = self.make_statement_line(
            amount=100.0, partner=self.partner_a,
            payment_ref='INV-001',
        )
        aml = self.make_open_invoice_line(self.partner_a, 100.0, ref='INV-001')
        self.session.apply_match(sl.id, aml.ids, source='manual')
        self.assertEqual(self.session.matches_made, 1)
        self.assertEqual(self.session.matches_manual, 1)
        self.assertEqual(self.session.matches_via_suggestion, 0)
        audit = self.env['eh.reconciliation.audit'].search([
            ('session_id', '=', self.session.id),
        ], limit=1)
        self.assertTrue(audit)
        self.assertEqual(audit.decision, 'match')
        self.assertEqual(audit.source, 'manual')

    def test_apply_match_via_suggestion_increments_correct_counter(self):
        sl = self.make_statement_line(amount=100.0, partner=self.partner_a)
        aml = self.make_open_invoice_line(self.partner_a, 100.0)
        self.session.apply_match(sl.id, aml.ids, source='suggestion')
        self.assertEqual(self.session.matches_via_suggestion, 1)
        self.assertEqual(self.session.matches_manual, 0)

    def test_apply_match_records_confidence_and_rules(self):
        date = fields.Date.from_string('2026-06-15')
        sl = self.make_statement_line(
            amount=100.0, partner=self.partner_a, date=date,
            payment_ref='INV-2026-0042',
        )
        aml = self.make_open_invoice_line(
            self.partner_a, 100.0, date=date, ref='INV-2026-0042',
        )
        self.session.apply_match(sl.id, aml.ids, source='suggestion')
        audit = self.env['eh.reconciliation.audit'].search([
            ('session_id', '=', self.session.id),
        ], limit=1)
        self.assertGreater(audit.confidence, 0.5)
        self.assertIn('amount', audit.rules_fired)
        self.assertIn('partner', audit.rules_fired)

    def test_apply_match_bulk_records_audit_per_aml(self):
        """Bulk match: a single apply_match call with multiple AMLs and
        source='bulk' creates one audit row per AML, all stamped 'bulk',
        and increments matches_made exactly once (not per AML, and neither
        as a manual nor a suggestion match)."""
        sl = self.make_statement_line(
            amount=150.0, partner=self.partner_a, payment_ref='INV-BULK',
        )
        aml1 = self.make_open_invoice_line(
            self.partner_a, 100.0, ref='INV-BULK-1',
        )
        aml2 = self.make_open_invoice_line(
            self.partner_a, 50.0, ref='INV-BULK-2',
        )
        aml_ids = (aml1 + aml2).ids
        self.session.apply_match(sl.id, aml_ids, source='bulk')
        self.assertEqual(self.session.matches_made, 1)
        self.assertEqual(self.session.matches_manual, 0)
        self.assertEqual(self.session.matches_via_suggestion, 0)
        audits = self.env['eh.reconciliation.audit'].search([
            ('session_id', '=', self.session.id),
            ('decision', '=', 'match'),
        ])
        self.assertEqual(len(audits), 2)
        self.assertEqual(set(audits.mapped('source')), {'bulk'})
        self.assertEqual(set(audits.mapped('aml_id').ids), set(aml_ids))

    def test_apply_match_rejects_mixed_candidate_partners(self):
        sl = self.make_statement_line(
            amount=150.0, partner=self.partner_a, payment_ref='MIXED',
        )
        aml_a = self.make_open_invoice_line(
            self.partner_a, 100.0, ref='MIXED-A',
        )
        aml_b = self.make_open_invoice_line(
            self.partner_b, 50.0, ref='MIXED-B',
        )
        before = self.env['account.move'].search_count([
            ('journal_id', '=', self.bank_journal.id),
        ])
        with self.assertRaisesRegex(UserError, 'multiple partners'):
            self.session.apply_match(
                sl.id, (aml_a + aml_b).ids, source='manual',
            )
        self.assertEqual(self.env['account.move'].search_count([
            ('journal_id', '=', self.bank_journal.id),
        ]), before)

    def test_apply_match_drag_drop_counts_as_manual(self):
        """A drag-and-drop match records source='drag_drop' on the audit
        row and counts as a manual match on the session counters."""
        sl = self.make_statement_line(
            amount=100.0, partner=self.partner_a, payment_ref='INV-DND',
        )
        aml = self.make_open_invoice_line(
            self.partner_a, 100.0, ref='INV-DND',
        )
        self.session.apply_match(sl.id, aml.ids, source='drag_drop')
        self.assertEqual(self.session.matches_made, 1)
        self.assertEqual(self.session.matches_manual, 1)
        self.assertEqual(self.session.matches_via_suggestion, 0)
        audit = self.env['eh.reconciliation.audit'].search([
            ('session_id', '=', self.session.id),
            ('decision', '=', 'match'),
        ], limit=1)
        self.assertEqual(audit.source, 'drag_drop')

    def test_apply_match_rejects_empty_aml_list(self):
        sl = self.make_statement_line(amount=100.0, partner=self.partner_a)
        with self.assertRaises(UserError):
            self.session.apply_match(sl.id, [], source='manual')

    def test_apply_match_rejects_unabsorbed_bank_excess_atomically(self):
        sl = self.make_statement_line(
            amount=100.0, partner=self.partner_a,
            payment_ref='PARTIAL-EXCESS',
        )
        aml = self.make_open_invoice_line(
            self.partner_a, 80.0, ref='PARTIAL-EXCESS',
        )
        before = self.env['account.move'].search_count([
            ('journal_id', '=', self.bank_journal.id),
        ])

        with self.assertRaisesRegex(UserError, 'full bank amount'):
            self.session.apply_match(sl.id, aml.ids, source='manual')

        sl.invalidate_recordset(['is_reconciled'])
        aml.invalidate_recordset(['amount_residual', 'reconciled'])
        self.assertFalse(sl.is_reconciled)
        self.assertFalse(aml.reconciled)
        self.assertAlmostEqual(aml.amount_residual, 80.0, places=2)
        self.assertEqual(self.env['account.move'].search_count([
            ('journal_id', '=', self.bank_journal.id),
        ]), before)
        self.assertFalse(self.env['eh.reconciliation.audit'].search_count([
            ('statement_line_id', '=', sl.id),
            ('decision', '=', 'match'),
        ]))

    def test_apply_match_rejects_unknown_statement_line(self):
        with self.assertRaises(UserError):
            self.session.apply_match(99999999, [], source='manual')

    def test_closed_session_rejects_match(self):
        sl = self.make_statement_line(amount=100.0, partner=self.partner_a)
        aml = self.make_open_invoice_line(self.partner_a, 100.0)
        self.session.action_close()
        with self.assertRaises(UserError):
            self.session.apply_match(sl.id, aml.ids, source='manual')

    def test_apply_skip_creates_audit_and_increments_counter(self):
        sl = self.make_statement_line(amount=100.0, partner=self.partner_a)
        self.session.apply_skip(sl.id)
        self.assertEqual(self.session.skips, 1)
        audit = self.env['eh.reconciliation.audit'].search([
            ('session_id', '=', self.session.id),
            ('decision', '=', 'skip'),
        ], limit=1)
        self.assertTrue(audit)
        self.assertFalse(audit.aml_id)

    def test_action_view_audits_returns_filtered_action(self):
        sl = self.make_statement_line(amount=100.0, partner=self.partner_a)
        self.session.apply_skip(sl.id)
        action = self.session.action_view_audits()
        self.assertEqual(action['res_model'], 'eh.reconciliation.audit')
        self.assertIn(('session_id', '=', self.session.id), action['domain'])

    def test_audit_count_reflects_session_decisions(self):
        sl1 = self.make_statement_line(amount=100.0, partner=self.partner_a)
        sl2 = self.make_statement_line(amount=200.0, partner=self.partner_b)
        aml1 = self.make_open_invoice_line(self.partner_a, 100.0)
        self.session.apply_match(sl1.id, aml1.ids, source='manual')
        self.session.apply_skip(sl2.id)
        self.session.invalidate_recordset(['audit_count'])
        self.assertEqual(self.session.audit_count, 2)
