# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Pairwise + property tests for the variance decomposition engine.

The oracle is recomputed independently in the test for every generated
case, mirroring the engine's documented rounding order (each amount
rounded to 2dp at the step shown in models/variance_run.py):

    variable elements: flexible = round2(std_price x actual_qty)
                       absorbed = round2(std_price x std_qty x units)
                       price-type = round2(actual_cost - flexible)
                       qty-type   = round2(flexible - absorbed)
    fixed overhead:    budget   = round2(rate x normal_capacity)
                       absorbed = round2(rate x units)
                       spend    = round2(actual_cost - budget)
                       volume   = round2(budget - absorbed)

Invariant asserted on EVERY case (the reconciliation identity): the sum of
the variance lines equals total actual cost minus total standard cost
absorbed, exactly at 2dp, where total absorbed sums the per-element
rounded absorbed amounts. With posting on, the posted entry is balanced,
sealed, and its absorption leg carries exactly the net variance; with
posting off, Post is refused and nothing reaches the ledger.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_base.tests.pairwise import pairwise_cases

# Standard card constants shared by every case (per unit of output).
STD = {
    'material': (2.0, 5.0),           # 2 kg  x  5.00 = 10.00
    'labour': (0.5, 20.0),            # 0.5 h x 20.00 = 10.00
    'variable_overhead': (0.5, 4.0),  # 0.5 h x  4.00 =  2.00
    'fixed_overhead': (1.0, 10.0),    # rate 10.00 / unit
}
CAPACITY = 1000.0

AXES = {
    # Which cost elements exist on both the card and the actuals.
    'elements': [
        ('material',),
        ('material', 'labour'),
        ('labour', 'fixed_overhead'),
        ('material', 'labour', 'variable_overhead', 'fixed_overhead'),
    ],
    # Actual output below / above the budgeted 1,000 units.
    'output': ['under', 'over'],
    'posting': [False, True],
}

OUTPUT_UNITS = {'under': 900.0, 'over': 1100.0}

# Per-element distortion of the actuals versus standard, chosen so no
# variance nets to zero: quantities run 6% over allowed, prices 3% over
# standard (fixed overhead spends 2% over budget).
QTY_FACTOR = 1.06
PRICE_FACTOR = 1.03
FOH_SPEND_FACTOR = 1.02


@tagged('eh_golden', 'eh_account_costing', 'post_install', '-at_install')
class TestPropertyCosting(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.acc_kind = {
            'price': cls._ensure_account(
                cls.env, '5810', 'Price Variance', 'expense'),
            'usage': cls._ensure_account(
                cls.env, '5811', 'Usage Variance', 'expense'),
            'rate': cls._ensure_account(
                cls.env, '5812', 'Rate Variance', 'expense'),
            'efficiency': cls._ensure_account(
                cls.env, '5813', 'Efficiency Variance', 'expense'),
            'spend': cls._ensure_account(
                cls.env, '5814', 'Spend Variance', 'expense'),
            'volume': cls._ensure_account(
                cls.env, '5815', 'Volume Variance', 'expense'),
        }
        cls.acc_absorption = cls._ensure_account(
            cls.env, '5809', 'Absorption Clearing', 'expense')

    # ------------------------------------------------------------------
    # case construction + independent oracle
    # ------------------------------------------------------------------

    def _build_case(self, tag, elements, units):
        """Create an active card + actuals for the element subset.

        Actual figures derive deterministically from the standards:
        qty = round4(allowed x 1.06), cost = round2(qty x price x 1.03);
        fixed overhead cost = round2(budget x 1.02).
        """
        card = self.env['eh.cost.card'].create({
            'item_name': 'PW %s' % tag,
            'normal_capacity': CAPACITY,
            'line_ids': [(0, 0, {
                'element': element,
                'std_qty': STD[element][0],
                'std_price': STD[element][1],
            }) for element in elements],
        })
        card.action_activate()
        act_lines = []
        for element in elements:
            std_qty, std_price = STD[element]
            if element == 'fixed_overhead':
                budget = round(std_qty * std_price * CAPACITY, 2)
                act_lines.append((0, 0, {
                    'element': element,
                    'actual_qty_total': 0.0,
                    'actual_cost_total': round(
                        budget * FOH_SPEND_FACTOR, 2)}))
            else:
                qty = round(std_qty * units * QTY_FACTOR, 4)
                act_lines.append((0, 0, {
                    'element': element,
                    'actual_qty_total': qty,
                    'actual_cost_total': round(
                        qty * std_price * PRICE_FACTOR, 2)}))
        actual = self.env['eh.cost.actual'].create({
            'card_id': card.id,
            'period_start': '2026-01-01', 'period_end': '2026-01-31',
            'units_produced': units,
            'line_ids': act_lines,
        })
        return card, actual

    def _oracle(self, actual, elements, units):
        """Recompute every variance independently of the engine."""
        by_line = {l.element: l for l in actual.line_ids}
        variances = {}   # (element, kind) -> amount
        total_actual = total_absorbed = 0.0
        for element in elements:
            std_qty, std_price = STD[element]
            aline = by_line[element]
            cost = aline.actual_cost_total
            if element == 'fixed_overhead':
                budget = round(std_qty * std_price * CAPACITY, 2)
                absorbed = round(std_qty * std_price * units, 2)
                variances[(element, 'spend')] = round(cost - budget, 2)
                variances[(element, 'volume')] = round(
                    budget - absorbed, 2)
            else:
                qty = aline.actual_qty_total
                flexible = round(std_price * qty, 2)
                absorbed = round(std_price * std_qty * units, 2)
                price_kind = {'material': 'price', 'labour': 'rate',
                              'variable_overhead': 'spend'}[element]
                qty_kind = 'usage' if element == 'material' \
                    else 'efficiency'
                variances[(element, price_kind)] = round(
                    cost - flexible, 2)
                variances[(element, qty_kind)] = round(
                    flexible - absorbed, 2)
            total_actual += cost
            total_absorbed += absorbed
        return variances, round(total_actual, 2), round(total_absorbed, 2)

    # ------------------------------------------------------------------
    # the pairwise sweep
    # ------------------------------------------------------------------

    def test_pairwise_reconciliation_identity(self):
        for idx, case in enumerate(pairwise_cases(AXES)):
            elements = case['elements']
            units = OUTPUT_UNITS[case['output']]
            posting = case['posting']
            card, actual = self._build_case(
                'case %s' % idx, elements, units)
            vals = {
                'period_start': '2026-01-01',
                'period_end': '2026-01-31',
                'actual_ids': [(6, 0, actual.ids)],
            }
            if posting:
                vals.update({
                    'post_variances': True,
                    'journal_id': self.journal_misc.id,
                    'absorption_account_id': self.acc_absorption.id,
                })
                vals.update({
                    '%s_variance_account_id' % kind: account.id
                    for kind, account in self.acc_kind.items()})
            run = self.env['eh.cost.variance.run'].create(vals)
            run.action_compute()

            expected, total_actual, total_absorbed = self._oracle(
                actual, elements, units)
            label = 'case %s %s' % (idx, case)

            # Two lines per element, each matching the oracle exactly.
            self.assertEqual(
                len(run.line_ids), 2 * len(elements),
                '%s: line count %s' % (label, len(run.line_ids)))
            for (element, kind), amount in expected.items():
                line = run.line_ids.filtered(
                    lambda l: l.element == element and l.kind == kind)
                self.assertEqual(
                    len(line), 1, '%s: missing %s/%s' % (
                        label, element, kind))
                self.assertAlmostEqual(
                    line.amount, amount, places=2,
                    msg='%s: %s/%s got %s expected %s' % (
                        label, element, kind, line.amount, amount))

            # The reconciliation identity, every case: sum of variances ==
            # total actual - total standard absorbed, exactly.
            self.assertAlmostEqual(
                run.total_actual_cost, total_actual, places=2,
                msg='%s: total actual' % label)
            self.assertAlmostEqual(
                run.total_absorbed_cost, total_absorbed, places=2,
                msg='%s: total absorbed' % label)
            self.assertAlmostEqual(
                sum(run.line_ids.mapped('amount')),
                total_actual - total_absorbed, places=2,
                msg='%s: identity broken' % label)
            self.assertAlmostEqual(
                run.total_variance, total_actual - total_absorbed,
                places=2, msg='%s: stored total variance' % label)

            if posting:
                run.action_post()
                self.assertEqual(run.state, 'posted', label)
                move = run.move_ids
                self.assertEqual(len(move), 1, label)
                self.assertBalanced(move)
                self.assertTrue(move.eh_sealed, label)
                # The absorption leg carries exactly the net variance:
                # credit when net adverse, debit when net favourable.
                absorption = move.line_ids.filtered(
                    lambda l: l.account_id == self.acc_absorption)
                net = round(total_actual - total_absorbed, 2)
                self.assertEqual(len(absorption), 1, label)
                self.assertAlmostEqual(
                    absorption.credit - absorption.debit, net, places=2,
                    msg='%s: absorption leg %s expected %s' % (
                        label, absorption.credit - absorption.debit, net))
            else:
                with self.assertRaises(UserError):
                    run.action_post()
                self.assertFalse(run.move_ids, label)
                self.assertEqual(run.state, 'computed', label)

    # ------------------------------------------------------------------
    # seeded property trial: random standards and actuals, same invariant
    # ------------------------------------------------------------------

    def test_property_identity_seeded(self):
        """25 seeded random cards/actuals across all four elements: the
        engine's lines must always sum exactly to actual minus absorbed
        (the telescoping construction is rounding-exact by design)."""
        rng = self.seeded_rng(7301)
        elements = ('material', 'labour', 'variable_overhead',
                    'fixed_overhead')
        for trial in range(25):
            units = round(rng.uniform(1, 2000), 4)
            capacity = round(rng.uniform(1, 2500), 4)
            card_lines, act_lines = [], []
            for element in elements:
                std_qty = round(rng.uniform(0.01, 10), 4)
                std_price = round(rng.uniform(0.01, 100), 4)
                card_lines.append((0, 0, {
                    'element': element, 'std_qty': std_qty,
                    'std_price': std_price}))
                if element == 'fixed_overhead':
                    act_lines.append((0, 0, {
                        'element': element, 'actual_qty_total': 0.0,
                        'actual_cost_total': round(
                            rng.uniform(0, std_qty * std_price
                                        * capacity * 2), 2)}))
                else:
                    qty = round(std_qty * units * rng.uniform(0.5, 1.5), 4)
                    act_lines.append((0, 0, {
                        'element': element, 'actual_qty_total': qty,
                        'actual_cost_total': round(
                            qty * std_price * rng.uniform(0.5, 1.5), 2)}))
            card = self.env['eh.cost.card'].create({
                'item_name': 'Seed %s' % trial,
                'normal_capacity': capacity,
                'line_ids': card_lines,
            })
            card.action_activate()
            actual = self.env['eh.cost.actual'].create({
                'card_id': card.id,
                'period_start': '2026-02-01', 'period_end': '2026-02-28',
                'units_produced': units,
                'line_ids': act_lines,
            })
            run = self.env['eh.cost.variance.run'].create({
                'period_start': '2026-02-01', 'period_end': '2026-02-28',
                'actual_ids': [(6, 0, actual.ids)],
            })
            run.action_compute()
            self.assertAlmostEqual(
                sum(run.line_ids.mapped('amount')),
                run.total_actual_cost - run.total_absorbed_cost,
                places=2,
                msg='trial %s: identity broken (units %s)' % (
                    trial, units))
