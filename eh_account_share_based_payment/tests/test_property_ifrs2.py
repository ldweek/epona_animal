# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Pairwise scenario tests for the IFRS 2 measurement engine.

Axes: settlement (equity/cash) x condition kind (service/non-market/market)
x graded vesting (off/on) x expected forfeiture (0/10/25 percent). Every
value pair of every axis pair runs at least once (all-pairs generator from
eh_account_base). For each case the cumulative measure is taken through the
public engine path (draft period runs, action_compute, cumulative_target
read back) at five period ends and checked against an oracle recomputed
independently in the test.

Fixed inputs per case (all amounts hand-derivable):

    grant 2026-01-01, 200 instruments to one grantee
    cliff plans:  2-year vesting (24 months), grant-date FV 10.00
    graded plans: tranche 1 = 60% vesting 2027-01-01 (12m), FV 10.00
                  tranche 2 = 40% vesting 2028-01-01 (24m), FV  8.00
    cash runs:    constant current FV 12.00 at every period end

    expected-to-vest n = 200 x (1 - f/100) -> 200 / 180 / 150

Vested fraction convention (engine): calendar months elapsed, including a
proportional current month, over the exact service period. The five period
ends sit at 6 / 12 / 18 / 24 /
30 months, so the cliff fractions are 0.25 / 0.50 / 0.75 / 1 / 1 and the
graded tranche-1 fractions are 0.50 / 1 / 1 / 1 / 1 with tranche-2 equal to
the cliff ones.

Oracles (company currency 2dp; every product below is exact at 2dp because
n is an integer, portions are 0.6/0.4 and fractions are quarters):

    equity cliff   c(m) = n x 10 x frac(m, 24)
    equity graded  c(m) = n x 0.6 x 10 x frac(m, 12)
                        + n x 0.4 x  8 x frac(m, 24)
    cash cliff     L(m) = n x 12 x frac(m, 24)
    cash graded    L(m) = n x 12 x (0.6 x frac(m, 12) + 0.4 x frac(m, 24))

Worked check for one case (equity, graded, f = 25 -> n = 150):
    m=6:  150x0.6x10x0.5 + 150x0.4x8x0.25 = 450 + 120   = 570.00
    m=12: 900 + 240 = 1,140.00        m=18: 900 + 360   = 1,260.00
    m=24 and m=30: 900 + 480 = 1,380.00 = 150 x 9.20 (full measure)

Invariants asserted on every case:
  * the engine cumulative equals the oracle at every period end;
  * equity cumulatives are monotonically non-decreasing over time (the
    estimates are held constant inside a case) and cap at the full measure;
  * the cash liability equals current FV x expected-to-vest x vested
    fraction at every period end (IFRS 2.30);
  * condition-failure semantics at the final date: a service forfeiture
    zeroes the measure, a failed non-market condition zeroes it (full
    reversal path), a failed MARKET condition leaves it unchanged
    (IFRS 2.21: never trued up for the market outcome).
"""

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_base.tests.pairwise import pairwise_cases

AXES = {
    'settlement': ['equity', 'cash'],
    'condition': ['service', 'non_market', 'market'],
    'graded': [False, True],
    'forfeiture': [0.0, 10.0, 25.0],
}

GRANTED = 200
CLIFF_FV = 10.0
CASH_FV = 12.0
T1_PORTION, T1_FV, T1_MONTHS = 0.6, 10.0, 12
T2_PORTION, T2_FV, T2_MONTHS = 0.4, 8.0, 24
CLIFF_MONTHS = 24

# Exact grant-date anniversaries used by the property matrix.
DATES = [
    ('2026-07-01', 6),
    ('2027-01-01', 12),
    ('2027-07-01', 18),
    ('2028-01-01', 24),
    ('2028-07-01', 30),
]


def _frac(months, total):
    return min(months / total, 1.0)


@tagged('eh_golden', 'eh_account_share_based_payment',
        'post_install', '-at_install')
class TestPropertyIfrs2(EhGoldenTestCase):
    """All-pairs settlement x condition x graded x forfeiture sweep."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.sbp_expense = cls._ensure_account(
            cls.env, '6150', 'Share-based Payment Expense', 'expense')
        cls.sbp_reserve = cls._ensure_account(
            cls.env, '3150', 'SBP Equity Reserve', 'equity')
        cls.sbp_liability = cls._ensure_account(
            cls.env, '2350', 'SBP Liability', 'liability_current')

    # ------------------------------------------------------------------
    # case builders and oracle
    # ------------------------------------------------------------------
    def _build_plan(self, case):
        vals = {
            'name': '/',
            'settlement': case['settlement'],
            'condition_kind': case['condition'],
            'grant_date': '2026-01-01',
            'vesting_years': 2,
            'vesting_months': 0,
            'expense_account_id': self.sbp_expense.id,
            'equity_account_id': self.sbp_reserve.id,
            'liability_account_id': self.sbp_liability.id,
            'settlement_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
            'grant_ids': [(0, 0, {
                'partner_id': self.partner_a.id,
                'instruments_granted': GRANTED,
                'grant_date_fair_value': CLIFF_FV,
                'expected_forfeiture_pct': case['forfeiture'],
            })],
        }
        if case['graded']:
            vals['graded_vesting'] = True
            vals['tranche_ids'] = [
                (0, 0, {'name': 'T1', 'portion_pct': T1_PORTION * 100,
                        'vesting_end_date': '2027-01-01',
                        'fair_value': T1_FV}),
                (0, 0, {'name': 'T2', 'portion_pct': T2_PORTION * 100,
                        'vesting_end_date': '2028-01-01',
                        'fair_value': T2_FV}),
            ]
        plan = self.env['eh.sbp.plan'].create(vals)
        plan.action_activate()
        return plan

    @staticmethod
    def _oracle(case, months):
        """Independent recomputation of the cumulative measure."""
        n = GRANTED * (1.0 - case['forfeiture'] / 100.0)
        if case['settlement'] == 'cash':
            if case['graded']:
                per_unit = CASH_FV * (
                    T1_PORTION * _frac(months, T1_MONTHS)
                    + T2_PORTION * _frac(months, T2_MONTHS))
            else:
                per_unit = CASH_FV * _frac(months, CLIFF_MONTHS)
        else:
            if case['graded']:
                per_unit = (T1_PORTION * T1_FV * _frac(months, T1_MONTHS)
                            + T2_PORTION * T2_FV * _frac(months, T2_MONTHS))
            else:
                per_unit = CLIFF_FV * _frac(months, CLIFF_MONTHS)
        return round(n * per_unit, 2)

    def _compute_target(self, plan, day, existing=None):
        """Cumulative measure through the public engine path."""
        values = {
            'plan_id': plan.id,
            'period_end': day,
        }
        if plan.settlement == 'cash' and plan.graded_vesting:
            values['tranche_value_ids'] = [(0, 0, {
                'tranche_id': tranche.id,
                'current_fair_value': CASH_FV,
            }) for tranche in plan.tranche_ids]
        else:
            values['current_fair_value'] = (
                CASH_FV if plan.settlement == 'cash' else 0.0
            )
        run = existing or self.env['eh.sbp.period.run'].create(values)
        run.action_compute()
        return run, run.cumulative_target

    # ------------------------------------------------------------------
    # the sweep
    # ------------------------------------------------------------------
    def test_pairwise_cumulative_measure(self):
        for case in pairwise_cases(AXES):
            plan = self._build_plan(case)
            grant = plan.grant_ids
            previous = 0.0
            last_run = None
            full = self._oracle(case, DATES[-1][1])
            for day, months in DATES:
                last_run, got = self._compute_target(plan, day)
                expected = self._oracle(case, months)
                self.assertAlmostEqual(
                    got, expected, places=2,
                    msg='case %s at %s: engine %s != oracle %s' % (
                        case, day, got, expected))
                if case['settlement'] == 'equity':
                    # Monotonic under constant estimates, capped at full.
                    self.assertGreaterEqual(
                        got, previous - 0.005,
                        'case %s at %s: equity cumulative fell from %s '
                        'to %s' % (case, day, previous, got))
                    self.assertLessEqual(
                        got, full + 0.005,
                        'case %s at %s: cumulative %s exceeds the full '
                        'measure %s' % (case, day, got, full))
                previous = got

            # Final date reaches the full measure (all fractions capped).
            self.assertAlmostEqual(
                previous, full, places=2,
                msg='case %s: final measure %s != full measure %s' % (
                    case, previous, full))

            # Condition-failure semantics on the last (still draft) run.
            if case['condition'] == 'service':
                grant.forfeited = True
                _, after = self._compute_target(
                    plan, DATES[-1][0], existing=last_run)
                self.assertAlmostEqual(
                    after, 0.0, places=2,
                    msg='case %s: a service forfeiture must zero the '
                        'measure, got %s' % (case, after))
            elif case['condition'] == 'non_market':
                grant.condition_failed = True
                _, after = self._compute_target(
                    plan, DATES[-1][0], existing=last_run)
                self.assertAlmostEqual(
                    after, 0.0, places=2,
                    msg='case %s: a failed non-market condition must '
                        'reverse in full, got %s' % (case, after))
            else:  # market
                grant.condition_failed = True
                _, after = self._compute_target(
                    plan, DATES[-1][0], existing=last_run)
                self.assertAlmostEqual(
                    after, full, places=2,
                    msg='case %s: a failed market condition must never '
                        'true up (IFRS 2.21), got %s vs %s' % (
                            case, after, full))
