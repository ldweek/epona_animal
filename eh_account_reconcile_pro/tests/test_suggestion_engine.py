# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Suggestion engine tests.

Each heuristic gets dedicated coverage: amount, date, partner, reference,
history. Then the combined score is verified at the weighted sum level.
Finally find_suggestions is exercised end to end with a small fixture.

Where possible the tests construct lightweight inputs to keep runtime
short. Real account.bank.statement.line records are used because the
engine introspects fields like commercial_partner_id that need ORM
context.
"""

from datetime import timedelta

from odoo import fields
from odoo.tests import tagged

from .common import EhReconcileIntegrationTestCase


@tagged('eh_account_reconcile_pro', 'integration', 'post_install', '-at_install')
class TestAmountScore(EhReconcileIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.engine = cls.env['eh.reconciliation.suggestion.engine']

    def _setup_pair(self, sl_amount, aml_amount):
        sl = self.make_statement_line(amount=sl_amount, partner=self.partner_a)
        aml = self.make_open_invoice_line(self.partner_a, aml_amount)
        return sl, aml

    def test_exact_match_scores_one(self):
        sl, aml = self._setup_pair(100.0, 100.0)
        self.assertAlmostEqual(self.engine._score_amount(sl, aml), 1.0, places=4)

    def test_within_one_percent_scores_high(self):
        sl, aml = self._setup_pair(100.0, 100.50)
        self.assertAlmostEqual(self.engine._score_amount(sl, aml), 0.9, places=4)

    def test_within_five_percent_scores_medium(self):
        sl, aml = self._setup_pair(100.0, 103.0)
        self.assertAlmostEqual(self.engine._score_amount(sl, aml), 0.5, places=4)

    def test_far_off_scores_zero(self):
        sl, aml = self._setup_pair(100.0, 200.0)
        self.assertEqual(self.engine._score_amount(sl, aml), 0.0)

    def test_zero_amounts_score_zero(self):
        sl, aml = self._setup_pair(0.0, 100.0)
        self.assertEqual(self.engine._score_amount(sl, aml), 0.0)

    def test_amount_uses_absolute_values(self):
        # A negative statement line (vendor payment out) matches a positive
        # AML (receivable) on amount alone if magnitudes match.
        sl = self.make_statement_line(amount=-100.0, partner=self.partner_a)
        aml = self.make_open_invoice_line(self.partner_a, 100.0)
        self.assertEqual(self.engine._score_amount(sl, aml), 1.0)


@tagged('eh_account_reconcile_pro', 'integration', 'post_install', '-at_install')
class TestDateScore(EhReconcileIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.engine = cls.env['eh.reconciliation.suggestion.engine']

    def test_same_day_scores_one(self):
        date = fields.Date.from_string('2026-06-15')
        sl = self.make_statement_line(amount=100, partner=self.partner_a, date=date)
        aml = self.make_open_invoice_line(self.partner_a, 100, date=date)
        self.assertAlmostEqual(self.engine._score_date(sl, aml), 1.0, places=4)

    def test_thirty_days_apart_scores_half(self):
        sl_date = fields.Date.from_string('2026-06-15')
        aml_date = fields.Date.from_string('2026-05-16')  # 30 days earlier
        sl = self.make_statement_line(amount=100, partner=self.partner_a, date=sl_date)
        aml = self.make_open_invoice_line(self.partner_a, 100, date=aml_date)
        score = self.engine._score_date(sl, aml)
        self.assertAlmostEqual(score, 0.5, places=2)

    def test_beyond_window_scores_zero(self):
        sl_date = fields.Date.from_string('2026-06-15')
        aml_date = sl_date - timedelta(days=120)
        sl = self.make_statement_line(amount=100, partner=self.partner_a, date=sl_date)
        aml = self.make_open_invoice_line(self.partner_a, 100, date=aml_date)
        self.assertEqual(self.engine._score_date(sl, aml), 0.0)


@tagged('eh_account_reconcile_pro', 'integration', 'post_install', '-at_install')
class TestPartnerScore(EhReconcileIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.engine = cls.env['eh.reconciliation.suggestion.engine']

    def test_exact_partner_match_scores_one(self):
        sl = self.make_statement_line(amount=100, partner=self.partner_a)
        aml = self.make_open_invoice_line(self.partner_a, 100)
        self.assertAlmostEqual(self.engine._score_partner(sl, aml), 1.0, places=4)

    def test_no_statement_partner_scores_uncertain(self):
        sl = self.make_statement_line(amount=100, partner=None)
        aml = self.make_open_invoice_line(self.partner_a, 100)
        self.assertAlmostEqual(self.engine._score_partner(sl, aml), 0.5, places=4)

    def test_partner_mismatch_scores_zero(self):
        sl = self.make_statement_line(amount=100, partner=self.partner_a)
        aml = self.make_open_invoice_line(self.partner_b, 100)
        self.assertEqual(self.engine._score_partner(sl, aml), 0.0)

    def test_no_aml_partner_scores_zero(self):
        sl = self.make_statement_line(amount=100, partner=self.partner_a)
        # Create an AML with no partner set.
        move = self.post_balanced_move([
            {'account': self.account_revenue, 'credit': 100.0},
            {'account': self.account_receivable, 'debit': 100.0},
        ])
        aml = move.line_ids.filtered(
            lambda l: l.account_id == self.account_receivable,
        )
        self.assertEqual(self.engine._score_partner(sl, aml), 0.0)


@tagged('eh_account_reconcile_pro', 'integration', 'post_install', '-at_install')
class TestReferenceScore(EhReconcileIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.engine = cls.env['eh.reconciliation.suggestion.engine']

    def test_full_token_overlap_scores_high(self):
        sl = self.make_statement_line(
            amount=100, partner=self.partner_a,
            payment_ref='INV-2026-0042 partner alpha',
        )
        aml = self.make_open_invoice_line(
            self.partner_a, 100, ref='INV-2026-0042',
        )
        score = self.engine._score_reference(sl, aml)
        self.assertGreater(score, 0.0)

    def test_no_overlap_scores_zero(self):
        sl = self.make_statement_line(
            amount=100, partner=self.partner_a,
            payment_ref='unrelated memo content',
        )
        aml = self.make_open_invoice_line(
            self.partner_a, 100, ref='completely different identifier',
        )
        self.assertEqual(self.engine._score_reference(sl, aml), 0.0)

    def test_short_tokens_ignored(self):
        # Tokens shorter than 3 chars are filtered as too generic.
        sl = self.make_statement_line(
            amount=100, partner=self.partner_a,
            payment_ref='a b c',
        )
        aml = self.make_open_invoice_line(
            self.partner_a, 100, ref='a b c',
        )
        self.assertEqual(self.engine._score_reference(sl, aml), 0.0)


@tagged('eh_account_reconcile_pro', 'integration', 'post_install', '-at_install')
class TestCombinedScore(EhReconcileIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.engine = cls.env['eh.reconciliation.suggestion.engine']

    def test_all_signals_high_yields_total_near_one(self):
        date = fields.Date.from_string('2026-06-15')
        sl = self.make_statement_line(
            amount=100.0, partner=self.partner_a, date=date,
            payment_ref='INV-2026-0042',
        )
        aml = self.make_open_invoice_line(
            self.partner_a, 100.0, date=date, ref='INV-2026-0042',
        )
        score = self.engine.score_match(sl, aml)
        # Amount 1.0 * 0.40 + Date 1.0 * 0.20 + Partner 1.0 * 0.25
        # plus reference contribution. History likely 0 on a fresh DB.
        # Expect total above 0.85.
        self.assertGreater(score['total'], 0.85)
        self.assertIn('amount', score['rules_fired'])
        self.assertIn('date', score['rules_fired'])
        self.assertIn('partner', score['rules_fired'])

    def test_only_amount_match_scores_low(self):
        # Different partner, far date, no ref overlap. Only amount fires.
        sl = self.make_statement_line(
            amount=100.0, partner=self.partner_a,
            date=fields.Date.from_string('2026-06-15'),
            payment_ref='unrelated',
        )
        aml = self.make_open_invoice_line(
            self.partner_b, 100.0,
            date=fields.Date.from_string('2026-01-01'),
            ref='completely-different',
        )
        score = self.engine.score_match(sl, aml)
        # Amount * 0.40 = 0.40 maximum. Maybe a tiny date bonus depending
        # on the window. Definitely below the default threshold.
        self.assertLess(score['total'], 0.50)


@tagged('eh_account_reconcile_pro', 'integration', 'post_install',
        '-at_install')
class TestHistoryScore(EhReconcileIntegrationTestCase):

    def test_history_score_uses_exact_statement_company(self):
        other_company = self.env['res.company'].create({
            'name': 'Reconcile History Other Company',
        })
        self.env.user.company_ids = [(4, other_company.id)]
        statement_line = self.make_statement_line(
            100.0, partner=self.partner_a, journal=self.bank_journal,
        )
        aml = self.make_open_invoice_line(self.partner_a, 100.0)
        Audit = type(self.env['eh.reconciliation.audit'])
        seen = []

        def scoped_count(audit_self, domain, *args, **kwargs):
            seen.append(domain)
            if ('aml_id.company_id', '=', self.env.company.id) in domain:
                return 0
            # Simulate five sibling-company history rows. Old code used
            # `in env.companies.ids` and would incorrectly score 1.0.
            return 5

        self.patch(Audit, 'search_count', scoped_count)
        score = self.env[
            'eh.reconciliation.suggestion.engine'
        ]._score_history(statement_line, aml)
        self.assertEqual(score, 0.0)
        self.assertTrue(seen)
        self.assertIn(
            ('aml_id.company_id', '=', self.env.company.id), seen[0],
        )


@tagged('eh_account_reconcile_pro', 'integration', 'post_install', '-at_install')
class TestFindSuggestions(EhReconcileIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.engine = cls.env['eh.reconciliation.suggestion.engine']

    def test_returns_top_n_above_threshold_sorted(self):
        date = fields.Date.from_string('2026-06-15')
        # One strong candidate (same partner, same date, exact amount).
        strong_aml = self.make_open_invoice_line(
            self.partner_a, 250.0, date=date, ref='INV-STRONG',
        )
        # One weak candidate (different partner, far date, different ref).
        self.make_open_invoice_line(
            self.partner_b, 250.0,
            date=fields.Date.from_string('2026-02-01'),
            ref='INV-WEAK',
        )
        sl = self.make_statement_line(
            amount=250.0, partner=self.partner_a, date=date,
            payment_ref='INV-STRONG',
        )
        results = self.engine.find_suggestions(sl, limit=10)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0]['aml_id'], strong_aml.id)
        # Results should be sorted descending by score.
        scores = [r['score'] for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_threshold_filters_weak_matches(self):
        # All candidates are weak; with a high threshold, none survive.
        for _i in range(3):
            self.make_open_invoice_line(
                self.partner_b, 999.0,
                date=fields.Date.from_string('2026-01-01'),
                ref='unrelated',
            )
        sl = self.make_statement_line(
            amount=100.0, partner=self.partner_a,
            date=fields.Date.from_string('2026-06-15'),
            payment_ref='match this',
        )
        results = self.engine.find_suggestions(sl, threshold=0.9)
        self.assertEqual(len(results), 0)

    def test_limit_caps_result_count(self):
        date = fields.Date.from_string('2026-06-15')
        for _i in range(8):
            self.make_open_invoice_line(
                self.partner_a, 100.0, date=date, ref='INV-MATCH',
            )
        sl = self.make_statement_line(
            amount=100.0, partner=self.partner_a, date=date,
            payment_ref='INV-MATCH',
        )
        results = self.engine.find_suggestions(sl, limit=3)
        self.assertLessEqual(len(results), 3)

    def test_registered_payment_outstanding_line_is_searchable_candidate(self):
        outstanding = self._ensure_account(
            self.env, '1098', 'Registered Payments Outstanding',
            'asset_current',
        )
        outstanding.reconcile = True
        method_line = self.bank_journal.inbound_payment_method_line_ids[:1]
        self.assertTrue(method_line)
        method_line.payment_account_id = outstanding
        payment = self.env['account.payment'].create({
            'payment_type': 'inbound',
            'partner_type': 'customer',
            'partner_id': self.partner_a.id,
            'amount': 325.0,
            'date': '2026-06-15',
            'journal_id': self.bank_journal.id,
            'payment_method_line_id': method_line.id,
            'memo': 'REGISTERED-PAYMENT-0044',
        })
        payment.action_post()
        payment_line = payment.move_id.line_ids.filtered(
            lambda line: line.account_id == outstanding
        )
        self.assertEqual(len(payment_line), 1)
        self.assertFalse(payment_line.reconciled)

        statement_line = self.make_statement_line(
            amount=325.0, partner=self.partner_a,
            date=fields.Date.from_string('2026-06-16'),
            payment_ref='Bank receipt for registered payment',
        )
        candidates = self.engine._fetch_candidates(statement_line)
        self.assertIn(payment_line, candidates)

        results = self.env[
            'eh.reconciliation.session'
        ].get_suggestions_for_line(
            statement_line.id, limit=50, threshold=0.99,
            search_term=payment.move_id.name,
        )
        self.assertIn(payment_line.id, [row['aml_id'] for row in results])
