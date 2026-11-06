# -*- encoding: utf-8 -*-
from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import new_test_user, tagged

from .common import EhReconcileIntegrationTestCase


@tagged('eh_account_reconcile_pro', 'post_install', '-at_install')
class TestSessionSecurity(EhReconcileIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.operator = new_test_user(
            cls.env,
            login='eh_reconcile_operator',
            groups='eh_account_base.group_eh_user',
        )
        cls.other_operator = new_test_user(
            cls.env,
            login='eh_reconcile_other_operator',
            groups='eh_account_base.group_eh_user',
        )
        cls.manager = new_test_user(
            cls.env,
            login='eh_reconcile_security_manager',
            groups='eh_account_base.group_eh_manager',
        )

    def test_create_server_stamps_identity_state_and_counters(self):
        session = self.env['eh.reconciliation.session'].with_user(
            self.operator
        ).create({
            'journal_id': self.bank_journal.id,
            'user_id': self.other_operator.id,
            'company_id': self.company.id,
            'opened_at': fields.Datetime.from_string('2000-01-01 00:00:00'),
            'closed_at': fields.Datetime.now(),
            'state': 'closed',
            'matches_made': 999,
            'write_offs': 999,
        })
        self.assertEqual(session.user_id, self.operator)
        self.assertEqual(session.company_id, self.bank_journal.company_id)
        self.assertEqual(session.state, 'open')
        self.assertFalse(session.closed_at)
        self.assertEqual(session.matches_made, 0)
        self.assertEqual(session.write_offs, 0)
        self.assertGreater(session.opened_at.year, 2000)

    def test_non_owner_cannot_close_or_rewrite_session_evidence(self):
        session = self.env['eh.reconciliation.session'].with_user(
            self.operator
        ).open_or_create(self.bank_journal.id)
        with self.assertRaises(AccessError):
            session.with_user(self.other_operator).action_close()
        with self.assertRaises(AccessError):
            session.with_user(self.operator).write({'matches_made': 50})
        with self.assertRaises(AccessError):
            session.with_user(self.operator).write({'name': 'forged'})
        with self.assertRaises(AccessError):
            session.with_user(self.operator).write({'duration_seconds': 999})
        self.assertEqual(session.state, 'open')
        self.assertEqual(session.matches_made, 0)

    def test_rule_usage_telemetry_is_private_engine_output(self):
        rule = self.env['eh.reconciliation.rule'].create({
            'name': 'Security telemetry rule',
            'code': 'security_telemetry_rule',
            'company_id': self.company.id,
            'rule_type': 'match',
        })
        with self.assertRaises(AccessError):
            rule.with_user(self.operator).record_fire()
        with self.assertRaises(AccessError):
            rule.with_user(self.manager).write({'fire_count': 999})
        rule.sudo().record_fire()
        self.assertEqual(rule.fire_count, 1)
        self.assertTrue(rule.last_fired_at)

    def test_session_company_follows_selected_journal(self):
        other_company = self.env['res.company'].create({
            'name': 'Reconciliation Security Company',
            'currency_id': self.env.ref('base.USD').id,
        })
        other_journal = self.env['account.journal'].create({
            'name': 'Other Company Bank',
            'code': 'RSB',
            'type': 'bank',
            'company_id': other_company.id,
        })
        Session = self.env['eh.reconciliation.session'].with_context(
            allowed_company_ids=[self.company.id, other_company.id],
        )
        session = Session.open_or_create(other_journal.id)
        self.assertEqual(session.company_id, other_company)
        self.assertEqual(session.journal_id, other_journal)
        with self.assertRaises(ValidationError):
            Session.create({
                'journal_id': other_journal.id,
                'company_id': self.company.id,
            })

    def test_decision_rejects_line_from_another_journal(self):
        other_journal = self.env['account.journal'].create({
            'name': 'Second Test Bank',
            'code': 'RS2',
            'type': 'bank',
            'company_id': self.company.id,
        })
        line = self.make_statement_line(100.0, journal=other_journal)
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id,
        )
        with self.assertRaises(UserError):
            session.apply_skip(line.id)

    def test_server_verified_suggestion_keeps_its_source(self):
        line = self.make_statement_line(
            amount=100.0,
            partner=self.partner_a,
            payment_ref='SEC-SOURCE',
        )
        aml = self.make_open_invoice_line(
            self.partner_a, 100.0, ref='SEC-SOURCE',
        )
        session = self.env['eh.reconciliation.session'].with_user(
            self.operator
        ).open_or_create(self.bank_journal.id)
        result = session.with_user(self.operator).apply_match(
            line.id, aml.ids, source='suggestion',
        )
        audit = self.env['eh.reconciliation.audit'].search([
            ('session_id', '=', session.id),
            ('decision', '=', 'match'),
        ], limit=1)
        self.assertEqual(audit.source, 'suggestion')
        self.assertEqual(audit.user_id, self.operator)
        self.assertEqual(session.matches_manual, 0)
        self.assertEqual(session.matches_via_suggestion, 1)
        self.assertEqual(result['source'], 'suggestion')
