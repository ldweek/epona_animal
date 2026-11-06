# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Learned match rules from reconciliation history."""

from odoo.tests import tagged

from odoo.addons.eh_account_reconcile_pro.tests.common import (
    EhReconcileIntegrationTestCase,
)


@tagged('eh_account_reconcile_pro', 'integration', 'post_install',
        '-at_install')
class TestLearnedRules(EhReconcileIntegrationTestCase):

    def _train_match(self, payment_ref, partner):
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        move = self.post_balanced_move([
            {'account': self.account_expense, 'debit': 100.0,
             'partner': partner},
            {'account': self.account_cash, 'credit': 100.0},
        ])
        aml = move.line_ids.filtered(
            lambda l: l.account_id == self.account_expense)
        line = self.make_statement_line(
            100.0, partner=partner, payment_ref=payment_ref)
        self.env['eh.reconciliation.audit'].sudo().create({
            'session_id': session.id,
            'statement_line_id': line.id,
            'aml_id': aml.id,
            'user_id': self.env.user.id,
            'decision': 'match',
            'source': 'manual',
            'confidence': 1.0,
            'rules_fired': '',
        })

    def test_learns_rule_for_recurring_partner(self):
        for i in range(3):
            self._train_match('ACMECORP INVOICE %d' % i, self.partner_a)
        Rule = self.env['eh.reconciliation.rule']

        created = Rule.learn_from_history(min_support=3)

        learned = created.filtered(
            lambda r: r.code == 'learned_acmecorp')
        self.assertTrue(learned)
        self.assertEqual(learned.partner_id, self.partner_a)
        # The learned rule matches a new ACMECORP line (case-insensitive).
        new_line = self.make_statement_line(
            50.0, payment_ref='ACMECORP NEW BILL')
        self.assertTrue(learned.matches_statement_line(new_line))

    def test_below_support_threshold_creates_nothing(self):
        for i in range(2):
            self._train_match('RARETOKEN %d' % i, self.partner_a)
        Rule = self.env['eh.reconciliation.rule']

        created = Rule.learn_from_history(min_support=3)

        self.assertFalse(created.filtered(
            lambda r: r.code == 'learned_raretoken'))

    def test_idempotent(self):
        for i in range(3):
            self._train_match('ACMECORP INVOICE %d' % i, self.partner_a)
        Rule = self.env['eh.reconciliation.rule']

        first = Rule.learn_from_history(min_support=3)
        second = Rule.learn_from_history(min_support=3)

        self.assertTrue(first.filtered(lambda r: r.code == 'learned_acmecorp'))
        self.assertFalse(
            second.filtered(lambda r: r.code == 'learned_acmecorp'))
