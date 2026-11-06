# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Pairwise scenario matrix for the flexible-budget engine.

Axes: cost behaviour {fixed, variable, semi_variable} x activity level
{under, over budgeted} x quantity data {present, absent}. For every
generated case the tie identities must hold:

* static variance = flexed variance + volume variance (always);
* price variance + efficiency variance = flexed variance and residual
  zero (variable lines with quantity data, constructed consistently:
  posted actual = actual qty x actual price and budgeted amount =
  budgeted qty x budgeted price);
* price and efficiency variances zero in every other case.

Expected flexed amounts are recomputed in the test from the documented
convention (fixed part + variable part x activity ratio), never read
back from the engine.

Each case runs in its own budget and its own calendar month so posted
actuals never bleed between cases.
"""

from calendar import monthrange
from datetime import date

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_base.tests.pairwise import pairwise_cases


AXES = {
    'behaviour': ['fixed', 'variable', 'semi_variable'],
    'activity': ['under', 'over'],
    'qty_data': ['y', 'n'],
}

# Constant case parameters (all 2dp/4dp-exact so oracles are exact):
BUDGET_ACTIVITY = 100.0
ACTUAL_ACTIVITY = {'under': 80.0, 'over': 125.0}   # ratios 0.8 / 1.25
BUDGETED_QTY = 100.0
BUDGETED_PRICE = 5.0
ACTUAL_QTY = {'under': 90.0, 'over': 130.0}
ACTUAL_PRICE = 5.5
FIXED_BUDGET = 800.0
VARIABLE_BUDGET = 500.0        # = BUDGETED_QTY x BUDGETED_PRICE
SEMI_BUDGET = 900.0
SEMI_FIXED_PORTION = 300.0     # variable component 600.0
PLAIN_ACTUAL = 777.0           # posted actual when no consistent qty data


@tagged('eh_golden', 'eh_account_budget_pro', 'post_install', '-at_install')
class TestFlexBudgetPairwise(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Budget = cls.env['eh.budget.budget']
        cls.pw_opex = cls._ensure_account(
            cls.env, '5340', 'Flex Pairwise Opex', 'expense')

    @staticmethod
    def _case_month(index):
        """A distinct calendar month per case (2026 onward)."""
        year = 2026 + index // 12
        month = index % 12 + 1
        return (
            date(year, month, 1),
            date(year, month, monthrange(year, month)[1]),
        )

    def _build_case(self, index, case):
        period_from, period_to = self._case_month(index)
        budgeted = {
            'fixed': FIXED_BUDGET,
            'variable': VARIABLE_BUDGET,
            'semi_variable': SEMI_BUDGET,
        }[case['behaviour']]
        line_vals = {
            'account_id': self.pw_opex.id,
            'period_from': period_from,
            'period_to': period_to,
            'budgeted_amount': budgeted,
            'behaviour': case['behaviour'],
        }
        if case['behaviour'] == 'semi_variable':
            line_vals['fixed_portion'] = SEMI_FIXED_PORTION
        actual_qty = ACTUAL_QTY[case['activity']]
        if case['qty_data'] == 'y':
            line_vals.update({
                'budgeted_qty': BUDGETED_QTY,
                'budgeted_unit_price': BUDGETED_PRICE,
                'actual_qty': actual_qty,
                'actual_unit_price': ACTUAL_PRICE,
            })
        budget = self.Budget.create({
            'code': 'flexpw_%02d' % index,
            'name': 'flexpw_%02d' % index,
            'date_from': period_from,
            'date_to': period_to,
            'line_ids': [(0, 0, line_vals)],
            'activity_period_ids': [(0, 0, {
                'period_from': period_from,
                'period_to': period_to,
                'budgeted_activity': BUDGET_ACTIVITY,
                'actual_activity': ACTUAL_ACTIVITY[case['activity']],
            })],
        })
        # Consistent ledger for the qty split: actual = qty x price.
        # Everywhere else post an arbitrary but fixed amount.
        if case['behaviour'] == 'variable' and case['qty_data'] == 'y':
            posted = actual_qty * ACTUAL_PRICE
        else:
            posted = PLAIN_ACTUAL
        self.post_balanced_move(
            [
                {'account': self.pw_opex, 'debit': posted},
                {'account': self.account_cash, 'credit': posted},
            ],
            date=period_from.replace(day=10),
        )
        return budget.line_ids, budgeted, posted

    def test_pairwise_tie_identities(self):
        cases = pairwise_cases(AXES)
        self.assertTrue(cases, "pairwise generator must emit cases")
        for index, case in enumerate(cases):
            line, budgeted, posted = self._build_case(index, case)
            tag = "case %d %r" % (index, case)

            ratio = ACTUAL_ACTIVITY[case['activity']] / BUDGET_ACTIVITY
            self.assertAlmostEqual(
                line.activity_ratio, ratio, places=4,
                msg="%s: ratio from the activity register" % tag)

            # Expected flexed amount from the documented convention.
            if case['behaviour'] == 'fixed':
                expected_flexed = budgeted
            elif case['behaviour'] == 'variable':
                expected_flexed = budgeted * ratio
            else:
                expected_flexed = (
                    SEMI_FIXED_PORTION
                    + (budgeted - SEMI_FIXED_PORTION) * ratio
                )
            expected_static = posted - budgeted
            expected_volume = expected_flexed - budgeted
            expected_flexed_var = expected_static - expected_volume

            self.assertAlmostEqual(
                line.flexed_amount, expected_flexed, places=2,
                msg="%s: flexed amount" % tag)
            self.assertAlmostEqual(
                line.variance_amount, expected_static, places=2,
                msg="%s: static variance" % tag)
            self.assertAlmostEqual(
                line.volume_variance, expected_volume, places=2,
                msg="%s: volume variance" % tag)
            self.assertAlmostEqual(
                line.flexed_variance, expected_flexed_var, places=2,
                msg="%s: flexed variance" % tag)
            # Tie identity 1: static = flexed variance + volume variance
            self.assertAlmostEqual(
                line.variance_amount,
                line.flexed_variance + line.volume_variance, places=2,
                msg="%s: three-way tie must hold" % tag)

            split_active = (
                case['behaviour'] == 'variable' and case['qty_data'] == 'y')
            if split_active:
                actual_qty = ACTUAL_QTY[case['activity']]
                expected_price = (ACTUAL_PRICE - BUDGETED_PRICE) * actual_qty
                expected_flexed_qty = BUDGETED_QTY * ratio
                expected_eff = (
                    (actual_qty - expected_flexed_qty) * BUDGETED_PRICE)
                self.assertAlmostEqual(
                    line.flexed_qty, expected_flexed_qty, places=4,
                    msg="%s: flexed qty" % tag)
                self.assertAlmostEqual(
                    line.price_variance, expected_price, places=2,
                    msg="%s: price variance" % tag)
                self.assertAlmostEqual(
                    line.efficiency_variance, expected_eff, places=2,
                    msg="%s: efficiency variance" % tag)
                # Tie identity 2: split reconciles to the flexed variance
                self.assertAlmostEqual(
                    line.price_variance + line.efficiency_variance,
                    line.flexed_variance, places=2,
                    msg="%s: price + efficiency = flexed variance" % tag)
                self.assertAlmostEqual(
                    line.split_residual, 0.0, places=2,
                    msg="%s: consistent data leaves no residual" % tag)
            else:
                self.assertAlmostEqual(
                    line.price_variance, 0.0, places=2,
                    msg="%s: split inactive, price variance zero" % tag)
                self.assertAlmostEqual(
                    line.efficiency_variance, 0.0, places=2,
                    msg="%s: split inactive, efficiency variance zero" % tag)
                self.assertAlmostEqual(
                    line.split_residual, 0.0, places=2,
                    msg="%s: split inactive, residual zero" % tag)
