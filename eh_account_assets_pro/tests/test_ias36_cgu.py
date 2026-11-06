# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Tests for the IAS 36 cash-generating-unit recoverable-amount engine.

The engine DERIVES an impairment from a recoverable-amount computation
(higher of value in use via a DCF schedule and fair value less costs of
disposal) rather than having it hand-keyed, then posts it through the
existing eh.asset.impairment path allocated pro-rata across the CGU's
member assets (goodwill first). These tests fail without the engine.
"""

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from .common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestIas36Cgu(EhAssetTestCase):

    def _member(self, code, cost, **overrides):
        """A running straight-line asset whose NBV equals cost (no periods
        posted yet), so the CGU carrying amount is the sum of member
        costs."""
        vals = {
            'code': code,
            'acquisition_cost': cost,
            'method': 'straight_line',
            'useful_life_months': 60,
            'salvage_value': 0.0,
            'prorate_first_period': False,
        }
        vals.update(overrides)
        asset = self._make_asset(**vals)
        asset.action_compute_schedule()
        asset.action_activate()
        return asset

    # ---- recoverable amount computation ----

    def test_value_in_use_discounts_cash_flows(self):
        """VIU is the PV of the projected cash flows at the discount
        rate: 1100 one period out at 10% = 1000."""
        cgu = self.env['eh.asset.cgu'].create({
            'name': 'DCF unit',
            'discount_rate': 10.0,
            'cashflow_ids': [
                (0, 0, {'period': 1, 'amount': 1100.0}),
            ],
        })
        self.assertAlmostEqual(cgu.value_in_use, 1000.0, places=2)

    def test_recoverable_is_higher_of_viu_and_fvlcd(self):
        cgu = self.env['eh.asset.cgu'].create({
            'name': 'Higher-of unit',
            'discount_rate': 10.0,
            'cashflow_ids': [
                (0, 0, {'period': 1, 'amount': 1100.0}),  # VIU = 1000
            ],
            'fair_value': 1500.0,
            'costs_of_disposal': 100.0,  # FVLCD = 1400
        })
        self.assertAlmostEqual(cgu.value_in_use, 1000.0, places=2)
        self.assertAlmostEqual(cgu.fair_value_less_costs, 1400.0, places=2)
        self.assertAlmostEqual(cgu.recoverable_amount, 1400.0, places=2)

    # ---- impairment triggered ----

    def test_cgu_below_carrying_triggers_prorata_impairment(self):
        """A CGU whose VIU and FVLCD are both below carrying amount
        triggers an impairment equal to the shortfall, allocated pro-rata
        across the (non-goodwill) member assets, and the posted moves
        balance."""
        a = self._member('CGU-A', 60_000.0)
        b = self._member('CGU-B', 40_000.0)
        cgu = self.env['eh.asset.cgu'].create({
            'name': 'Impaired unit',
            'discount_rate': 10.0,
            # VIU: 66,000 one period out at 10% = 60,000
            'cashflow_ids': [
                (0, 0, {'period': 1, 'amount': 66_000.0}),
            ],
            # FVLCD = 55,000 (below VIU, so recoverable = 60,000)
            'fair_value': 58_000.0,
            'costs_of_disposal': 3_000.0,
            'member_ids': [(6, 0, [a.id, b.id])],
        })
        cgu.invalidate_recordset([
            'carrying_amount', 'recoverable_amount', 'impairment_shortfall',
        ])
        self.assertAlmostEqual(cgu.carrying_amount, 100_000.0, places=2)
        self.assertAlmostEqual(cgu.recoverable_amount, 60_000.0, places=2)
        self.assertAlmostEqual(cgu.impairment_shortfall, 40_000.0, places=2)

        cgu.action_test_now()

        self.assertEqual(cgu.last_test_result, 'impaired')
        a.invalidate_recordset(['net_book_value'])
        b.invalidate_recordset(['net_book_value'])
        # Pro-rata on carrying amount: A 60% -> 24,000, B 40% -> 16,000.
        self.assertAlmostEqual(a.accumulated_impairment, 24_000.0, places=2)
        self.assertAlmostEqual(b.accumulated_impairment, 16_000.0, places=2)
        # The two NBVs now sum to the recoverable amount.
        self.assertAlmostEqual(
            a.net_book_value + b.net_book_value, 60_000.0, places=2,
        )
        # Allocation ties EXACTLY to the shortfall.
        total_alloc = sum(cgu.impairment_ids.mapped('amount'))
        self.assertAlmostEqual(total_alloc, 40_000.0, places=2)
        # Every posted impairment move balances by construction.
        for imp in cgu.impairment_ids:
            self.assertEqual(imp.state, 'posted')
            move = imp.move_id
            self.assertEqual(move.state, 'posted')
            self.assertAlmostEqual(
                sum(move.line_ids.mapped('debit')),
                sum(move.line_ids.mapped('credit')),
                places=2,
            )

    def test_goodwill_absorbs_loss_first(self):
        """IAS 36.104: the loss reduces goodwill first, then pro-rata
        across the other assets."""
        goodwill = self._member('CGU-GW', 10_000.0, is_goodwill=True)
        a = self._member('CGU-A2', 60_000.0)
        b = self._member('CGU-B2', 30_000.0)
        cgu = self.env['eh.asset.cgu'].create({
            'name': 'Goodwill unit',
            # No cash flows and no fair value -> recoverable amount 0,
            # shortfall = full carrying amount 100,000.
            'member_ids': [(6, 0, [goodwill.id, a.id, b.id])],
        })
        cgu.invalidate_recordset(['carrying_amount', 'impairment_shortfall'])
        self.assertAlmostEqual(cgu.carrying_amount, 100_000.0, places=2)
        self.assertAlmostEqual(cgu.impairment_shortfall, 100_000.0, places=2)

        cgu.action_test_now()
        for rec in (goodwill, a, b):
            rec.invalidate_recordset(['net_book_value'])
        # Goodwill fully written off first (10,000). Remaining 90,000
        # spread pro-rata across A (60,000) and B (30,000): A 60,000,
        # B 30,000 -> both to zero.
        self.assertAlmostEqual(goodwill.accumulated_impairment, 10_000.0, places=2)
        self.assertAlmostEqual(a.accumulated_impairment, 60_000.0, places=2)
        self.assertAlmostEqual(b.accumulated_impairment, 30_000.0, places=2)
        self.assertAlmostEqual(
            sum(cgu.impairment_ids.mapped('amount')), 100_000.0, places=2,
        )

    def test_individual_floor_caps_and_redistributes_allocation(self):
        """IAS 36.105 headroom is enforced per member, with any capped
        proportional share redistributed to members that can absorb it."""
        a = self._member(
            'CGU-FLOOR-A', 60_000.0, cgu_impairment_floor=50_000.0,
        )
        b = self._member('CGU-FLOOR-B', 40_000.0)
        cgu = self.env['eh.asset.cgu'].create({
            'name': 'Individual floor unit',
            'fair_value': 60_000.0,
            'member_ids': [(6, 0, [a.id, b.id])],
        })
        cgu.action_test_now()

        # The unconstrained 60/40 split would be 24,000 / 16,000.  A can
        # absorb only 10,000 above its floor, so B receives the remaining
        # 30,000 and the full 40,000 shortfall still reconciles exactly.
        a.invalidate_recordset(['net_book_value'])
        b.invalidate_recordset(['net_book_value'])
        self.assertAlmostEqual(a.accumulated_impairment, 10_000.0, places=2)
        self.assertAlmostEqual(b.accumulated_impairment, 30_000.0, places=2)
        self.assertAlmostEqual(a.net_book_value, 50_000.0, places=2)
        self.assertAlmostEqual(b.net_book_value, 10_000.0, places=2)
        self.assertAlmostEqual(
            sum(cgu.impairment_ids.mapped('amount')), 40_000.0, places=2,
        )
        event = cgu.test_event_ids
        self.assertEqual(len(event), 1)
        self.assertIn('"ias36_individual_floor":50000.0', event.basis_json)

    def test_unallocable_floor_shortfall_posts_nothing(self):
        """An aggregate shortfall beyond member headroom is rejected before
        an impairment, move, or completed-test event can be persisted."""
        a = self._member(
            'CGU-FLOOR-X', 60_000.0, cgu_impairment_floor=55_000.0,
        )
        b = self._member(
            'CGU-FLOOR-Y', 40_000.0, cgu_impairment_floor=35_000.0,
        )
        cgu = self.env['eh.asset.cgu'].create({
            'name': 'Unallocable floor unit',
            'fair_value': 60_000.0,
            'member_ids': [(6, 0, [a.id, b.id])],
        })
        move_count = self.env['account.move'].search_count([])

        with self.assertRaises(UserError):
            cgu.action_test_now()

        self.assertEqual(self.env['account.move'].search_count([]), move_count)
        self.assertFalse(cgu.impairment_ids)
        self.assertFalse(cgu.test_event_ids)
        self.assertFalse(cgu.last_test_date)
        self.assertFalse(cgu.last_test_result)

    def test_goodwill_impairment_cannot_be_reversed(self):
        """IAS 36.124: an impairment loss recognised for goodwill shall not
        be reversed. Even when the cumulative-balance headroom would permit
        it (a 4,000 reversal against a 10,000 charge), the reversal must be
        refused because the asset is goodwill."""
        goodwill = self._member('CGU-GW-REV', 10_000.0, is_goodwill=True)
        Imp = self.env['eh.asset.impairment']
        Imp.create({
            'asset_id': goodwill.id,
            'impairment_date': '2026-06-30',
            'amount': 10_000.0,
            'is_reversal': False,
            'impairment_account_id': self.account_impairment.id,
            'accumulated_account_id': self.account_accum_dep.id,
            'reason': 'IAS 36 reversal test',
        })
        with self.assertRaises(ValidationError):
            Imp.create({
                'asset_id': goodwill.id,
                'impairment_date': '2026-09-30',
                'amount': 4_000.0,
                'is_reversal': True,
                'impairment_account_id': self.account_impairment.id,
                'accumulated_account_id': self.account_accum_dep.id,
                'reason': 'IAS 36 reversal test',
            })

    def test_non_goodwill_reversal_within_balance_allowed(self):
        """The IAS 36.124 goodwill bar must not leak onto ordinary assets:
        a reversal within the cumulative-impairment headroom on a normal
        asset still posts."""
        asset = self._member('CGU-ORD-REV', 10_000.0)
        Imp = self.env['eh.asset.impairment']
        charge = Imp.create({
            'asset_id': asset.id,
            'impairment_date': '2026-06-30',
            'amount': 6_000.0,
            'is_reversal': False,
            'impairment_account_id': self.account_impairment.id,
            'accumulated_account_id': self.account_accum_dep.id,
            'reason': 'IAS 36 reversal test',
        })
        charge.action_post()
        # Should not raise.
        reversal = Imp.create({
            'asset_id': asset.id,
            'impairment_date': '2026-09-30',
            'amount': 2_000.0,
            'is_reversal': True,
            'impairment_account_id': self.account_impairment.id,
            'accumulated_account_id': self.account_accum_dep.id,
            'reason': 'IAS 36 reversal test',
        })
        self.assertTrue(reversal)

    # ---- no impairment ----

    def test_cgu_above_carrying_triggers_nothing(self):
        """A CGU whose recoverable amount exceeds carrying amount records
        a passed test and creates no impairment."""
        a = self._member('CGU-OK-A', 60_000.0)
        b = self._member('CGU-OK-B', 40_000.0)
        cgu = self.env['eh.asset.cgu'].create({
            'name': 'Healthy unit',
            'discount_rate': 10.0,
            # VIU: 132,000 one period out at 10% = 120,000 > 100,000
            'cashflow_ids': [
                (0, 0, {'period': 1, 'amount': 132_000.0}),
            ],
            'member_ids': [(6, 0, [a.id, b.id])],
        })
        cgu.invalidate_recordset([
            'carrying_amount', 'recoverable_amount', 'impairment_shortfall',
        ])
        self.assertAlmostEqual(cgu.recoverable_amount, 120_000.0, places=2)
        self.assertAlmostEqual(cgu.impairment_shortfall, 0.0, places=2)

        cgu.action_test_now()

        self.assertEqual(cgu.last_test_result, 'passed')
        self.assertFalse(cgu.impairment_ids)
        a.invalidate_recordset(['net_book_value'])
        b.invalidate_recordset(['net_book_value'])
        self.assertAlmostEqual(a.net_book_value, 60_000.0, places=2)
        self.assertAlmostEqual(b.net_book_value, 40_000.0, places=2)

    # ---- opt-in / SoD ----

    def test_manual_impairment_still_works_unassigned(self):
        """The manual hand-keyed impairment path is unaffected: an asset
        with no CGU can still be impaired directly."""
        asset = self._member('NO-CGU', 50_000.0)
        self.assertFalse(asset.cgu_id)
        imp = self.env['eh.asset.impairment'].create({
            'asset_id': asset.id,
            'impairment_date': '2026-06-30',
            'amount': 5_000.0,
            'reason': 'Hand-keyed impairment, no CGU',
        })
        imp.action_post()
        asset.invalidate_recordset(['net_book_value', 'accumulated_impairment'])
        self.assertFalse(imp.cgu_id)
        self.assertAlmostEqual(asset.accumulated_impairment, 5_000.0, places=2)
        self.assertAlmostEqual(asset.net_book_value, 45_000.0, places=2)

    def test_non_manager_cannot_run_test(self):
        a = self._member('CGU-SOD', 60_000.0)
        cgu = self.env['eh.asset.cgu'].create({
            'name': 'SoD unit',
            'member_ids': [(6, 0, [a.id])],
        })
        clerk = self._make_non_manager_user()
        with self.assertRaises(UserError):
            cgu.with_user(clerk).action_test_now()
        self.assertFalse(cgu.impairment_ids)
