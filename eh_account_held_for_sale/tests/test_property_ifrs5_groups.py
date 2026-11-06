# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Pairwise scenario tests for the IFRS 5 disposal-group allocation engine.

Pairwise sweep over the engine's axes (member count, goodwill member,
fair-value floor, associated liabilities) asserting allocation invariants
rather than hand-picked amounts (those live in test_golden_ifrs5_groups):

* the posted write-down equals the group shortfall and equals the sum of
  the member allocations;
* no member is written below the higher of its fair-value floor and zero;
* liability members never receive an allocation;
* goodwill is consumed before any non-goodwill member absorbs a cent
  (IAS 36.104);
* a subsequent upward remeasure reverses the cumulative write-down of
  the non-goodwill members exactly, and never touches goodwill
  (IFRS 5.22, IAS 36.124);
* every journal entry balances.

Plus explicit cent-rounding cases (the allocation must sum exactly to the
loss after per-member 2dp rounding).
"""

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_base.tests.pairwise import pairwise_cases


@tagged('eh_golden', 'eh_account_held_for_sale', 'post_install',
        '-at_install')
class TestPropertyIfrs5Groups(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.imp = cls._ensure_account(
            cls.env, '5175', 'HFS Group Impairment', 'expense')
        cls.gain = cls._ensure_account(
            cls.env, '4790', 'Group Disposal Gain/Loss', 'income_other')
        cls.acc_gw = cls._ensure_account(
            cls.env, '1790', 'Goodwill Held for Sale', 'asset_current')
        cls.acc_lia = cls._ensure_account(
            cls.env, '2790', 'HFS Associated Liabilities',
            'liability_current')
        cls.member_accounts = [
            cls._ensure_account(
                cls.env, '179%d' % (4 + i), 'HFS Pairwise Member %d' % i,
                'asset_current')
            for i in range(3)
        ]

    def _group(self, lines, fvlcts):
        return self.env['eh.disposal.group'].create({
            'name': '/',
            'fair_value_less_costs': fvlcts,
            'impairment_account_id': self.imp.id,
            'proceeds_account_id': self.account_cash.id,
            'gain_loss_account_id': self.gain.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [(0, 0, line) for line in lines],
        })

    AXES = {
        'members': [2, 3],
        'goodwill': [False, True],
        'floor': [False, True],
        'liabilities': [False, True],
    }
    LOSS = 30000.0
    GW_CARRYING = 20000.0
    LIA_CARRYING = 30000.0
    FLOOR_HEADROOM = 5000.0  # floored member may absorb at most this

    def _build_case(self, case):
        carryings = [100000.0, 60000.0, 40000.0][:case['members']]
        lines = []
        for i, carrying in enumerate(carryings):
            vals = {
                'name': 'M%d' % i,
                'carrying_amount': carrying,
                'account_id': self.member_accounts[i].id,
            }
            if i == 0 and case['floor']:
                # Tight floor: member 0 can absorb at most 5,000.
                vals['fair_value_floor'] = carrying - self.FLOOR_HEADROOM
            lines.append(vals)
        if case['goodwill']:
            lines.append({
                'name': 'GW', 'carrying_amount': self.GW_CARRYING,
                'is_goodwill': True, 'account_id': self.acc_gw.id})
        if case['liabilities']:
            lines.append({
                'name': 'LIA', 'carrying_amount': self.LIA_CARRYING,
                'is_liability': True, 'account_id': self.acc_lia.id})
        signed = (sum(carryings)
                  + (self.GW_CARRYING if case['goodwill'] else 0.0)
                  - (self.LIA_CARRYING if case['liabilities'] else 0.0))
        # FVLCTS engineered so the shortfall is exactly LOSS, which is
        # always allocatable: goodwill (20,000 when present) plus the
        # non-floored members' headroom always exceeds 30,000.
        return self._group(lines, signed - self.LOSS), signed

    def test_pairwise_allocation_invariants(self):
        for number, case in enumerate(pairwise_cases(self.AXES)):
            label = 'case %d: %s' % (number, case)
            group, signed = self._build_case(case)
            group.action_classify()

            asset_lines = group.line_ids.filtered(
                lambda l: not l.is_liability)
            liability_lines = group.line_ids.filtered('is_liability')
            goodwill_lines = asset_lines.filtered('is_goodwill')
            other_lines = asset_lines - goodwill_lines

            # One balanced entry; allocation sums exactly to the loss.
            self.assertEqual(len(group.move_ids), 1,
                             'expected one JE for %s' % label)
            self.assertBalanced(group.move_ids)
            self.assertAlmostEqual(
                sum(asset_lines.mapped('allocated_writedown')),
                self.LOSS, places=2,
                msg='allocation does not sum to loss for %s' % label)
            self.assertAlmostEqual(
                group.carrying_amount, group.fair_value_less_costs,
                places=2,
                msg='group not written to FVLCTS for %s' % label)

            # No member below its floor (or zero); liabilities untouched.
            for line in asset_lines:
                floor = max(line.fair_value_floor, 0.0)
                self.assertGreaterEqual(
                    line.carrying_amount, floor - 0.005,
                    'member below floor for %s' % label)
                self.assertGreaterEqual(
                    line.carrying_amount, -0.005,
                    'member below zero for %s' % label)
            for line in liability_lines:
                self.assertAlmostEqual(
                    line.allocated_writedown, 0.0, places=2,
                    msg='liability received allocation for %s' % label)
                self.assertAlmostEqual(
                    line.carrying_amount, self.LIA_CARRYING, places=2,
                    msg='liability carrying moved for %s' % label)

            # Goodwill first: the loss (30,000) exceeds the goodwill
            # carrying (20,000), so goodwill must be fully consumed
            # before the others absorb the remaining 10,000.
            if case['goodwill']:
                self.assertAlmostEqual(
                    goodwill_lines.carrying_amount, 0.0, places=2,
                    msg='goodwill not consumed first for %s' % label)
                self.assertAlmostEqual(
                    sum(goodwill_lines.mapped('allocated_writedown')),
                    self.GW_CARRYING, places=2,
                    msg='goodwill allocation wrong for %s' % label)
                self.assertAlmostEqual(
                    sum(other_lines.mapped('allocated_writedown')),
                    self.LOSS - self.GW_CARRYING, places=2,
                    msg='non-goodwill remainder wrong for %s' % label)

            # Floored member absorbs at most its headroom.
            if case['floor']:
                floored = group.line_ids.filtered(
                    lambda l: l.name == 'M0')
                self.assertLessEqual(
                    floored.allocated_writedown,
                    self.FLOOR_HEADROOM + 0.005,
                    'floored member over-allocated for %s' % label)

            # Upward remeasure: gain far above the cap reverses exactly
            # the non-goodwill cumulative write-down and nothing else.
            group.fair_value_less_costs = signed + 10000.0
            group.action_remeasure()
            reversible = self.LOSS - sum(
                goodwill_lines.mapped('cumulative_writedown'))
            for line in other_lines:
                self.assertAlmostEqual(
                    line.cumulative_writedown, 0.0, places=2,
                    msg='non-goodwill not fully reversed for %s' % label)
            if case['goodwill']:
                self.assertAlmostEqual(
                    goodwill_lines.carrying_amount, 0.0, places=2,
                    msg='goodwill reversed (IAS 36.124) for %s' % label)
            self.assertAlmostEqual(
                group.carrying_amount,
                signed - (self.GW_CARRYING if case['goodwill'] else 0.0),
                places=2,
                msg='post-reversal carrying wrong for %s' % label)
            self.assertAlmostEqual(
                group.cumulative_writedown,
                self.GW_CARRYING if case['goodwill'] else 0.0, places=2,
                msg='cumulative after reversal wrong for %s' % label)
            self.assertGreaterEqual(reversible, -0.005, label)
            for move in group.move_ids:
                self.assertBalanced(move)

    # ------------------------------------------------------------------
    # Cent-rounding boundaries
    # ------------------------------------------------------------------
    def test_rounding_two_members_uneven_cents(self):
        """A 30,000 and B 10,000 sharing a loss of 100.01.

        Raw shares: A 100.01 x 3/4 = 75.0075 -> 75.01;
                    B 100.01 x 1/4 = 25.0025 -> 25.00.
        Rounded shares already sum to 100.01: no residue correction.
        """
        group = self._group([
            {'name': 'A', 'carrying_amount': 30000.0,
             'account_id': self.member_accounts[0].id},
            {'name': 'B', 'carrying_amount': 10000.0,
             'account_id': self.member_accounts[1].id},
        ], 40000.0 - 100.01)
        group.action_classify()
        a = group.line_ids.filtered(lambda l: l.name == 'A')
        b = group.line_ids.filtered(lambda l: l.name == 'B')
        self.assertAlmostEqual(a.allocated_writedown, 75.01, places=2)
        self.assertAlmostEqual(b.allocated_writedown, 25.00, places=2)
        self.assertAlmostEqual(
            a.allocated_writedown + b.allocated_writedown, 100.01, places=2)
        self.assertBalanced(group.move_ids)

    def test_rounding_three_way_split_residue(self):
        """Three equal members (10,000 each) sharing a loss of 100.00.

        Raw shares: 100 / 3 = 33.3333 -> 33.33 each, summing to 99.99;
        the 0.01 residue lands on the first member with headroom, so the
        allocations are exactly 33.34 / 33.33 / 33.33 and sum to 100.00.
        """
        group = self._group([
            {'name': 'M%d' % i, 'carrying_amount': 10000.0,
             'account_id': self.member_accounts[i].id}
            for i in range(3)
        ], 30000.0 - 100.0)
        group.action_classify()
        lines = group.line_ids.sorted('id')
        self.assertAlmostEqual(
            lines[0].allocated_writedown, 33.34, places=2)
        self.assertAlmostEqual(
            lines[1].allocated_writedown, 33.33, places=2)
        self.assertAlmostEqual(
            lines[2].allocated_writedown, 33.33, places=2)
        self.assertAlmostEqual(
            sum(lines.mapped('allocated_writedown')), 100.0, places=2)
        self.assertBalanced(group.move_ids)
