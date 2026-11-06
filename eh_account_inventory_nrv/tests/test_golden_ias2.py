# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden IAS 2 worked examples for the assessment-basis engine.

Every expected amount is hand-derived in a comment from the inputs stated in
the test; nothing is read back from the engine to build an expectation.

Category-basis convention implemented by eh.nrv.run._category_allocation
(read from models/nrv_run.py) and asserted by these tests:

* Each product category is one unit of assessment (IAS 2.29 grouping of
  similar or related items). The category requirement is
  max(total cost - total NRV, 0): surpluses and deficits inside the
  category are netted BEFORE the floor at zero, so the category is never
  carried above its aggregate cost.
* The requirement is allocated over the lines with an item-level deficit,
  pro-rata by deficit, each share rounded to company currency with the
  residual on the last deficit line. A surplus line gets nothing (never
  written up above its own cost) and a deficit line never gets more than
  its own deficit (never written down below its own NRV).
* Recoveries stay capped: the closing requirement is always >= 0, so a
  recovery can never exceed the opening write-down (IAS 2.33).

One NRV run is allowed per company and reporting date (unique constraint),
so every run in these tests draws a fresh date from a class sequence.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_base.tests.pairwise import pairwise_cases


@tagged('eh_golden', 'eh_account_inventory_nrv', 'post_install',
        '-at_install')
class TestGoldenIas2(EhGoldenTestCase):
    """IAS 2.28-29 assessment basis: item-by-item vs grouped category."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Posting is manager-gated. The group_ids / groups_id field split
        # across Odoo series is resolved at runtime for backport parity.
        field = ('group_ids' if 'group_ids' in cls.env.user._fields
                 else 'groups_id')
        cls.env.user.write({field: [
            (4, cls.env.ref('eh_account_base.group_eh_manager').id)]})
        cls.writedown_expense = cls._ensure_account(
            cls.env, '5150', 'Inventory Write-down', 'expense')
        cls.allowance = cls._ensure_account(
            cls.env, '1490', 'Inventory Write-down Allowance',
            'asset_current')
        cls.cat_a = cls.env['product.category'].create(
            {'name': 'NRV Golden Cat A'})
        cls.cat_b = cls.env['product.category'].create(
            {'name': 'NRV Golden Cat B'})

    _date_seq = 0

    @classmethod
    def _next_date(cls):
        # One run per company per reporting date: every run gets its own day
        # in 2027 (the module's integration tests use 2026 dates).
        cls._date_seq += 1
        return '2027-%02d-%02d' % (
            1 + (cls._date_seq - 1) // 28, 1 + (cls._date_seq - 1) % 28)

    def _run(self, basis, lines, reporting_date=None):
        prepared_lines = []
        for source in lines:
            values = dict(source)
            if 'opening_writedown' in values:
                values.update({
                    'opening_writedown_manual': True,
                    'manual_opening_writedown': values.pop(
                        'opening_writedown'),
                    'opening_override_reason': 'Golden worked-example opening',
                })
            prepared_lines.append(values)
        return self.env['eh.nrv.run'].create({
            'reporting_date': reporting_date or self._next_date(),
            'assessment_basis': basis,
            'writedown_expense_account_id': self.writedown_expense.id,
            'allowance_account_id': self.allowance.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [(0, 0, vals) for vals in prepared_lines],
        })

    # ------------------------------------------------------------------
    # golden worked examples
    # ------------------------------------------------------------------
    def test_golden_item_vs_category_surplus_shelter(self):
        """Same inputs on both bases; the surplus line only shelters the
        deficit under the category basis.

        Inputs, one category: A cost 100, NRV 80; B cost 100, NRV 130.

        Item basis (IAS 2.29 default): each line floored at zero on its own.
          A: max(100 - 80, 0) = 20; B: max(100 - 130, 0) = 0 (B stays at
          cost; NRV above cost is never recognised, IAS 2.9).
          Closing = 20, movement = +20.
          JE: Dr write-down expense 20 / Cr allowance 20.

        Category basis: the category is one unit of assessment.
          Requirement = max((100 + 100) - (80 + 130), 0) = max(-10, 0) = 0.
          B's 30 surplus fully shelters A's 20 deficit; the category is
          carried at 200 = its aggregate cost (not 210), so nothing is ever
          held above cost. Closing = 0, movement = 0, nothing to post.
        """
        item_lines = [
            {'name': 'A', 'product_category_id': self.cat_a.id,
             'cost': 100.0, 'net_realisable_value': 80.0},
            {'name': 'B', 'product_category_id': self.cat_a.id,
             'cost': 100.0, 'net_realisable_value': 130.0},
        ]
        run_item = self._run('item', item_lines)
        self.assertAlmostEqual(run_item.closing_writedown, 20.0, places=2)
        self.assertAlmostEqual(run_item.movement, 20.0, places=2)
        run_item.action_compute()
        run_item.action_post()
        self.assertMoveLines(run_item.move_id, [
            (self.writedown_expense, 20.0, 0.0),
            (self.allowance, 0.0, 20.0),
        ])
        self.assertBalanced(run_item.move_id)

        run_cat = self._run('category', item_lines)
        self.assertAlmostEqual(run_cat.closing_writedown, 0.0, places=2)
        self.assertAlmostEqual(run_cat.movement, 0.0, places=2)
        for line in run_cat.line_ids:
            self.assertAlmostEqual(line.required_writedown, 0.0, places=2)
        run_cat.action_compute()
        # A nil movement has no journal entry, but its measured close remains
        # a posted roll-forward anchor for the next assessment.
        run_cat.action_post()
        self.assertEqual(run_cat.state, 'posted')
        self.assertFalse(run_cat.move_id)

    def test_golden_category_partial_shelter(self):
        """Category netting with a surplus smaller than the deficit.

        Inputs, one category: A cost 100, NRV 80 (deficit 20); B cost 100,
        NRV 112 (surplus 12).

        Requirement = max(200 - 192, 0) = 8: the 12 surplus absorbs 12 of
        A's 20 deficit. Allocation: A is the only deficit line, so A takes
        the whole 8 (still below its own deficit of 20, so A is not written
        below its own NRV); B takes 0 (never written up).

        Item basis on the same inputs would have been 20, so the case also
        proves the substantive difference between the bases.

        JE: Dr write-down expense 8 / Cr allowance 8.
        """
        run = self._run('category', [
            {'name': 'A', 'product_category_id': self.cat_a.id,
             'cost': 100.0, 'net_realisable_value': 80.0},
            {'name': 'B', 'product_category_id': self.cat_a.id,
             'cost': 100.0, 'net_realisable_value': 112.0},
        ])
        line_a = run.line_ids.filtered(lambda ln: ln.name == 'A')
        line_b = run.line_ids.filtered(lambda ln: ln.name == 'B')
        self.assertAlmostEqual(line_a.required_writedown, 8.0, places=2)
        self.assertAlmostEqual(line_b.required_writedown, 0.0, places=2)
        self.assertAlmostEqual(run.closing_writedown, 8.0, places=2)
        run.action_compute()
        run.action_post()
        self.assertMoveLines(run.move_id, [
            (self.writedown_expense, 8.0, 0.0),
            (self.allowance, 0.0, 8.0),
        ])
        self.assertBalanced(run.move_id)

    def test_golden_category_allocation_prorata(self):
        """The netted requirement spreads over deficit lines pro-rata.

        Inputs, one category: A cost 100, NRV 80 (deficit 20); B cost 50,
        NRV 40 (deficit 10); C cost 100, NRV 112 (surplus 12).

        Requirement = max(250 - 232, 0) = 18.
        Deficit pool = 20 + 10 = 30.
        A share = 18 * 20/30 = 12.00; B share (residual) = 18 - 12 = 6.00;
        C = 0. Every share is under its own deficit, so no line drops below
        its own NRV. JE: Dr expense 18 / Cr allowance 18.
        """
        run = self._run('category', [
            {'name': 'A', 'product_category_id': self.cat_a.id,
             'cost': 100.0, 'net_realisable_value': 80.0},
            {'name': 'B', 'product_category_id': self.cat_a.id,
             'cost': 50.0, 'net_realisable_value': 40.0},
            {'name': 'C', 'product_category_id': self.cat_a.id,
             'cost': 100.0, 'net_realisable_value': 112.0},
        ])
        by_name = {ln.name: ln for ln in run.line_ids}
        self.assertAlmostEqual(
            by_name['A'].required_writedown, 12.0, places=2)
        self.assertAlmostEqual(by_name['B'].required_writedown, 6.0, places=2)
        self.assertAlmostEqual(by_name['C'].required_writedown, 0.0, places=2)
        self.assertAlmostEqual(run.closing_writedown, 18.0, places=2)
        run.action_compute()
        run.action_post()
        self.assertMoveLines(run.move_id, [
            (self.writedown_expense, 18.0, 0.0),
            (self.allowance, 0.0, 18.0),
        ])

    def test_golden_category_allocation_rounding_residual(self):
        """Rounded shares tie exactly to the category requirement.

        Inputs, one category: A cost 100, NRV 90 (deficit 10); B cost 50,
        NRV 45 (deficit 5); C cost 30, NRV 34 (surplus 4).

        Requirement = max(180 - 169, 0) = 11.00.
        Deficit pool = 15. A share = round(11 * 10/15) = round(7.3333)
        = 7.33; B (last deficit line, residual) = 11.00 - 7.33 = 3.67.
        7.33 + 3.67 = 11.00 exactly; C = 0.
        """
        run = self._run('category', [
            {'name': 'A', 'product_category_id': self.cat_a.id,
             'cost': 100.0, 'net_realisable_value': 90.0},
            {'name': 'B', 'product_category_id': self.cat_a.id,
             'cost': 50.0, 'net_realisable_value': 45.0},
            {'name': 'C', 'product_category_id': self.cat_a.id,
             'cost': 30.0, 'net_realisable_value': 34.0},
        ])
        by_name = {ln.name: ln for ln in run.line_ids}
        self.assertAlmostEqual(
            by_name['A'].required_writedown, 7.33, places=2)
        self.assertAlmostEqual(
            by_name['B'].required_writedown, 3.67, places=2)
        self.assertAlmostEqual(
            by_name['C'].required_writedown, 0.0, places=2)
        self.assertAlmostEqual(run.closing_writedown, 11.0, places=2)

    def test_golden_category_recovery_capped_at_prior_writedown(self):
        """Roll-forward under the category basis: a recovery reverses no
        more than the write-down previously recognised (IAS 2.33).

        Period 1, one category: A (product-linked) cost 100, NRV 70
        (deficit 30); B cost 100, NRV 120 (surplus 20).
          Requirement = max(200 - 190, 0) = 10, all allocated to A (the
          only deficit line). Posted JE: Dr expense 10 / Cr allowance 10.

        Period 2, same products: A cost 100, NRV 100; B cost 100, NRV 100.
          Requirement = max(200 - 200, 0) = 0. A's opening rolls forward
          from the prior posted run at 10, so the movement is 0 - 10 = -10:
          the reversal is exactly the 10 previously recognised, although
          A's NRV recovered by 30. JE: Dr allowance 10 / Cr expense 10.
        """
        prod_a = self.env['product.product'].create(
            {'name': 'NRV golden A', 'categ_id': self.cat_a.id})
        prod_b = self.env['product.product'].create(
            {'name': 'NRV golden B', 'categ_id': self.cat_a.id})
        prior = self._run('category', [
            {'name': 'A', 'product_id': prod_a.id,
             'cost': 100.0, 'net_realisable_value': 70.0},
            {'name': 'B', 'product_id': prod_b.id,
             'cost': 100.0, 'net_realisable_value': 120.0},
        ])
        # The category auto-fills from the product.
        self.assertEqual(prior.line_ids.product_category_id, self.cat_a)
        self.assertAlmostEqual(prior.closing_writedown, 10.0, places=2)
        prior.action_compute()
        prior.action_post()
        self.assertMoveLines(prior.move_id, [
            (self.writedown_expense, 10.0, 0.0),
            (self.allowance, 0.0, 10.0),
        ])

        nxt = self._run('category', [
            {'name': 'A', 'product_id': prod_a.id,
             'cost': 100.0, 'net_realisable_value': 100.0},
            {'name': 'B', 'product_id': prod_b.id,
             'cost': 100.0, 'net_realisable_value': 100.0},
        ])
        line_a = nxt.line_ids.filtered(lambda ln: ln.product_id == prod_a)
        self.assertAlmostEqual(line_a.opening_writedown, 10.0, places=2)
        self.assertAlmostEqual(nxt.closing_writedown, 0.0, places=2)
        self.assertAlmostEqual(nxt.movement, -10.0, places=2)
        nxt.action_compute()
        nxt.action_post()
        self.assertMoveLines(nxt.move_id, [
            (self.allowance, 10.0, 0.0),
            (self.writedown_expense, 0.0, 10.0),
        ])
        # Ledger: the allowance built in period 1 is fully released.
        self.assertAlmostEqual(
            self.posted_balance(self.allowance), 0.0, places=2)

    def test_golden_netting_confined_to_category(self):
        """A surplus in one category never shelters a deficit in another.

        Inputs: A in category A, cost 100, NRV 80 (deficit 20); B in
        category B, cost 100, NRV 130 (surplus 30).

        Category A requirement = max(100 - 80, 0) = 20.
        Category B requirement = max(100 - 130, 0) = 0.
        Closing = 20: identical to the item basis, because grouping only
        nets SIMILAR OR RELATED items (IAS 2.29); it is not a licence to
        offset across unrelated classes of inventory.
        """
        run = self._run('category', [
            {'name': 'A', 'product_category_id': self.cat_a.id,
             'cost': 100.0, 'net_realisable_value': 80.0},
            {'name': 'B', 'product_category_id': self.cat_b.id,
             'cost': 100.0, 'net_realisable_value': 130.0},
        ])
        self.assertAlmostEqual(run.closing_writedown, 20.0, places=2)
        run.action_compute()
        run.action_post()
        self.assertMoveLines(run.move_id, [
            (self.writedown_expense, 20.0, 0.0),
            (self.allowance, 0.0, 20.0),
        ])

    # ------------------------------------------------------------------
    # guardrails
    # ------------------------------------------------------------------
    def test_category_basis_requires_category_on_every_line(self):
        run = self._run('category', [
            {'name': 'A', 'product_category_id': self.cat_a.id,
             'cost': 100.0, 'net_realisable_value': 80.0},
            {'name': 'Uncategorised', 'cost': 50.0,
             'net_realisable_value': 40.0},
        ])
        with self.assertRaises(UserError):
            run.action_compute()
        run.line_ids.filtered(
            lambda ln: not ln.product_category_id
        ).product_category_id = self.cat_b
        run.action_compute()
        self.assertEqual(run.state, 'computed')

    def test_category_missing_after_compute_blocks_post(self):
        # Lines stay editable between compute and post; clearing a category
        # in that window must still be caught at post.
        run = self._run('category', [
            {'name': 'A', 'product_category_id': self.cat_a.id,
             'cost': 100.0, 'net_realisable_value': 80.0},
            {'name': 'B', 'product_category_id': self.cat_a.id,
             'cost': 50.0, 'net_realisable_value': 60.0},
        ])
        run.action_compute()
        run.line_ids[1].product_category_id = False
        with self.assertRaises(UserError):
            run.action_post()

    def test_basis_frozen_after_post_and_tracked_on_change(self):
        run = self._run('item', [
            {'name': 'A', 'product_category_id': self.cat_a.id,
             'cost': 100.0, 'net_realisable_value': 80.0}])
        # The basis is a tracked field (audit trail requirement): assert the
        # tracking registration itself; the chatter message rendering is
        # mail-framework plumbing exercised in production, and its precommit
        # timing is not reliably observable inside a test transaction.
        self.assertTrue(run._fields['assessment_basis'].tracking)
        run.assessment_basis = 'category'
        run.assessment_basis = 'item'
        run.action_compute()
        run.action_post()
        # Locked once posted: the basis is part of the recognised figures.
        with self.assertRaises(UserError):
            run.assessment_basis = 'category'
        # The posted movement entry discloses the basis applied.
        self.assertIn('Item by item', run.move_id.ref)

    def test_default_basis_is_item_and_preserves_behaviour(self):
        run = self.env['eh.nrv.run'].create({
            'reporting_date': self._next_date(),
            'writedown_expense_account_id': self.writedown_expense.id,
            'allowance_account_id': self.allowance.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [(0, 0, {
                'name': 'A', 'cost': 100.0, 'net_realisable_value': 80.0})],
        })
        self.assertEqual(run.assessment_basis, 'item')
        # Pre-existing behaviour: max(100 - 80, 0) = 20, no category needed.
        self.assertAlmostEqual(run.closing_writedown, 20.0, places=2)
        run.action_compute()
        run.action_post()
        self.assertEqual(run.state, 'posted')

    def test_category_locked_on_posted_run_line(self):
        # Re-categorising a line of a posted run would regroup the netting
        # and silently move the recognised figures.
        run = self._run('category', [
            {'name': 'A', 'product_category_id': self.cat_a.id,
             'cost': 100.0, 'net_realisable_value': 80.0}])
        run.action_compute()
        run.action_post()
        with self.assertRaises(UserError):
            run.line_ids.product_category_id = self.cat_b

    # ------------------------------------------------------------------
    # pairwise sweep over the basis axes
    # ------------------------------------------------------------------
    # Fixed line sets per mix; every expected closing below is hand-derived
    # from these inputs and stated literally in EXPECTED.
    #   deficit_only:  A(100, 80) deficit 20 + B(50, 40) deficit 10
    #                  item: 20 + 10 = 30
    #                  category: max(150 - 120, 0) = 30 (nothing to net)
    #   surplus_only:  A(100, 120) + B(50, 60), all NRV above cost
    #                  item: 0 + 0 = 0; category: max(150 - 180, 0) = 0
    #   mixed:         A(100, 80) deficit 20 + B(100, 130) surplus 30
    #                  item: 20; category: max(200 - 210, 0) = 0
    MIX_LINES = {
        'deficit_only': [(100.0, 80.0), (50.0, 40.0)],
        'surplus_only': [(100.0, 120.0), (50.0, 60.0)],
        'mixed': [(100.0, 80.0), (100.0, 130.0)],
    }
    EXPECTED = {
        ('item', 'deficit_only'): 30.0,
        ('item', 'surplus_only'): 0.0,
        ('item', 'mixed'): 20.0,
        ('category', 'deficit_only'): 30.0,
        ('category', 'surplus_only'): 0.0,
        ('category', 'mixed'): 0.0,
    }

    def test_pairwise_basis_mix_opening(self):
        axes = {
            'basis': ['item', 'category'],
            'mix': ['deficit_only', 'surplus_only', 'mixed'],
            'opening': ['zero', 'positive'],
        }
        for case in pairwise_cases(axes):
            opening = 5.0 if case['opening'] == 'positive' else 0.0
            lines = []
            for idx, (cost, nrv) in enumerate(self.MIX_LINES[case['mix']]):
                vals = {
                    'name': 'L%d' % idx,
                    'product_category_id': self.cat_a.id,
                    'cost': cost, 'net_realisable_value': nrv,
                }
                if idx == 0 and opening:
                    vals['opening_writedown'] = opening
                lines.append(vals)
            run = self._run(case['basis'], lines)
            expected_closing = self.EXPECTED[(case['basis'], case['mix'])]
            # Movement = closing - opening (the run posts only the delta).
            expected_movement = expected_closing - opening
            self.assertAlmostEqual(
                run.closing_writedown, expected_closing, places=2,
                msg='closing mismatch for %s' % case)
            self.assertAlmostEqual(
                run.movement, expected_movement, places=2,
                msg='movement mismatch for %s' % case)

    # ------------------------------------------------------------------
    # property test: seeded random trials, invariant oracles
    # ------------------------------------------------------------------
    def test_property_category_netting_invariants(self):
        """Random line sets, both bases side by side. Invariants:

        1. no line ever carries a negative write-down (nothing above cost);
        2. a line's allocation never exceeds its own deficit by more than a
           rounding cent (nothing below its own NRV);
        3. each category's closing equals max(round(cost - NRV), 0) exactly
           (netted then floored);
        4. the category-basis closing never exceeds the item-basis closing
           (netting can only shelter, never add);
        5. run movement = closing - total opening.
        """
        rng = self.seeded_rng(20260705)
        categories = [self.cat_a, self.cat_b]
        for trial in range(12):
            n_lines = rng.randint(2, 5)
            specs = []
            for i in range(n_lines):
                cost = round(rng.uniform(0.0, 1000.0), 2)
                nrv = round(rng.uniform(0.0, 1300.0), 2)
                opening = (round(rng.uniform(0.0, 100.0), 2)
                           if rng.random() < 0.4 else 0.0)
                specs.append({
                    'name': 'T%d-%d' % (trial, i),
                    'product_category_id': rng.choice(categories).id,
                    'cost': cost, 'net_realisable_value': nrv,
                    'opening_writedown': opening,
                })
            run_item = self._run('item', [dict(s) for s in specs])
            run_cat = self._run('category', [dict(s) for s in specs])
            currency = run_cat.currency_id

            for line in run_item.line_ids | run_cat.line_ids:
                # Invariant 1: never above cost.
                self.assertGreaterEqual(
                    line.required_writedown, 0.0,
                    'trial %d: negative write-down' % trial)
                # Invariant 2: never below the line's own NRV. The residual
                # line absorbs the cent-rounding of its siblings, so allow
                # up to 4 sibling roundings of half a cent each.
                deficit = max(line.cost - line.net_realisable_value, 0.0)
                self.assertLessEqual(
                    line.required_writedown, deficit + 0.03,
                    'trial %d: allocation above own deficit' % trial)

            # Invariant 3: per-category closing = netted-then-floored.
            for category in categories:
                cat_lines = run_cat.line_ids.filtered(
                    lambda ln: ln.product_category_id == category)
                if not cat_lines:
                    continue
                expected = max(currency.round(
                    sum(cat_lines.mapped('cost'))
                    - sum(cat_lines.mapped('net_realisable_value'))), 0.0)
                self.assertAlmostEqual(
                    sum(cat_lines.mapped('required_writedown')), expected,
                    places=2, msg='trial %d: category closing' % trial)

            # Invariant 4: netting only ever shelters.
            self.assertLessEqual(
                run_cat.closing_writedown,
                run_item.closing_writedown + 0.005,
                'trial %d: category exceeds item closing' % trial)

            # Invariant 5: movement is the delta from opening.
            for run in (run_item, run_cat):
                self.assertAlmostEqual(
                    run.movement,
                    run.closing_writedown
                    - sum(run.line_ids.mapped('opening_writedown')),
                    places=2, msg='trial %d: movement identity' % trial)
