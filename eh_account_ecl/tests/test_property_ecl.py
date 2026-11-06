# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Property and pairwise scenario tests for the ECL engine.

Exemplar for the IFRS 10/10 program harness layers (see
docs/IFRS_10_10_PROGRAM_PLAN.md): a pairwise sweep over the engine's
scenario axes plus seeded randomized trials, both asserting engine
invariants rather than hand-picked amounts. Golden worked examples live in
the per-standard test_golden_* files; these tests catch the interaction
and boundary bugs a fixed example cannot.
"""

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_base.tests.pairwise import pairwise_cases


@tagged('eh_golden', 'eh_account_ecl', 'post_install', '-at_install')
class TestPropertyEcl(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.impairment = cls._ensure_account(
            cls.env, '5290', 'Impairment Loss', 'expense')
        cls.allowance = cls._ensure_account(
            cls.env, '1290', 'Loss Allowance', 'asset_current')

    _date_seq = 0

    @classmethod
    def _next_date(cls):
        # One ECL run is allowed per company and reporting date (unique
        # constraint), so every scenario case gets its own date.
        cls._date_seq += 1
        return '2026-%02d-%02d' % (
            1 + (cls._date_seq - 1) // 28, 1 + (cls._date_seq - 1) % 28)

    def _run(self, opening, buckets):
        return self.env['eh.ecl.run'].create({
            'reporting_date': self._next_date(),
            'opening_allowance': opening,
            'impairment_expense_account_id': self.impairment.id,
            'loss_allowance_account_id': self.allowance.id,
            'journal_id': self.journal_misc.id,
            'bucket_ids': [(0, 0, b) for b in buckets],
        })

    def _assert_invariants(self, run, case_label):
        # Invariant 1: the closing allowance is the sum of its buckets.
        self.assertAlmostEqual(
            run.closing_allowance,
            sum(run.bucket_ids.mapped('ecl_effective')), places=2,
            msg='closing != sum of buckets for %s' % case_label)
        # Invariant 2: no bucket books a negative allowance and none books
        # more than its gross carrying (loss rates live in [0, 100]).
        for bucket in run.bucket_ids:
            self.assertGreaterEqual(
                bucket.ecl_effective, -0.005,
                'negative bucket ECL in %s' % case_label)
            self.assertLessEqual(
                bucket.ecl_effective, bucket.gross_carrying + 0.005,
                'bucket ECL above gross carrying in %s' % case_label)
        # Invariant 3: movement reconciles opening to closing.
        self.assertAlmostEqual(
            run.movement, run.closing_allowance - run.opening_allowance,
            places=2, msg='movement broken for %s' % case_label)

    AXES = {
        'opening': [0.0, 500.0],
        'rate_low': [0.0, 1.0],
        'rate_high': [25.0, 100.0],
        'gross_scale': [0.0, 1000.0, 12345.67],
    }

    def test_pairwise_matrix(self):
        for case in pairwise_cases(self.AXES):
            label = repr(case)
            buckets = [
                {'name': 'Current', 'days_from': 0, 'days_to': 30,
                 'loss_rate': case['rate_low'],
                 'gross_carrying': case['gross_scale']},
                {'name': '90+', 'days_from': 91, 'days_to': 0,
                 'loss_rate': case['rate_high'], 'stage': '3',
                 'gross_carrying': case['gross_scale'] * 2},
            ]
            run = self._run(case['opening'], buckets)
            run.action_compute()
            self._assert_invariants(run, label)

    def test_seeded_random_trials(self):
        rng = self.seeded_rng(20260705)
        for trial in range(10):
            buckets = []
            for i in range(rng.randint(1, 5)):
                buckets.append({
                    'name': 'B%d-%d' % (trial, i),
                    'days_from': i * 30, 'days_to': (i + 1) * 30 - 1,
                    'loss_rate': round(rng.uniform(0.0, 100.0), 3),
                    'gross_carrying': round(rng.uniform(0.0, 1e6), 2),
                })
            opening = round(rng.uniform(0.0, 5e4), 2)
            run = self._run(opening, buckets)
            run.action_compute()
            self._assert_invariants(run, 'trial %d (seed 20260705)' % trial)

    def test_posted_movement_balances(self):
        run = self._run(100.0, [
            {'name': '90+', 'days_from': 91, 'days_to': 0,
             'loss_rate': 25.0, 'stage': '3', 'gross_carrying': 1000.0},
        ])
        run.action_compute()
        run.action_post()
        # 1000 x 25% = 250 closing; movement over the 100 opening = 150.
        move = run.move_id if hasattr(run, 'move_id') else run.move_ids
        for entry in move:
            self.assertBalanced(entry)
        self.assertAlmostEqual(self.posted_balance(self.impairment),
                               150.0, places=2)
        self.assertAlmostEqual(self.posted_balance(self.allowance),
                               -150.0, places=2)
