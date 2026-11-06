# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Golden worked examples for the IAS 36 impairment engine and the IAS 16
revaluation wizard.

Each test encodes a hand-computed worked example (numbers derived by hand
from the standard's mechanics, never read back from the engine) and
asserts the exact derived figures and journal entries:

1. Value in use as a discounted cash flow (IAS 36.30-.57). The module's
   PV convention, from eh.asset.cgu._compute_value_in_use: each cash-flow
   row is discounted at amount / (1 + r) ** period with r the per-period
   rate and period the 1-based period count (end-of-period / ordinary
   annuity convention); rows are summed UNROUNDED and a single terminal
   rounding is applied in the CGU currency. A period of 0 is undiscounted.
2. CGU impairment allocation, goodwill first then pro-rata on carrying
   amount (IAS 36.104), posted line-exact through eh.asset.impairment.
3. The IAS 36.117 reversal ceiling: after an impairment the remaining
   schedule re-amortises on the lower base (IAS 36.63), so reversing the
   full cumulative charge later would lift the asset ABOVE its
   depreciated historical cost; the ceiling must bind before the
   cumulative cap does.
4. IAS 16.39/.40 revaluation down-then-up: the decrease charges P&L when
   no surplus exists; the later increase first reverses that P&L charge
   (credit income) and only the excess is credited to the revaluation
   reserve. Both entries asserted line-exact.
5. The IAS 36.117 ceiling for MANUAL-method assets: with no engine
   schedule to replay, the ceiling falls back to a hypothetical straight
   line over useful life (lower of that and cost less posted
   depreciation) instead of raw cost, blocking the over-reversal the old
   cost fallback permitted.
6. The recoverable-amount cap on revaluation uplifts: an uplift beyond
   the latest recorded recoverable amount is blocked, and the manager
   override requires a documented reason logged on the asset.
"""

from datetime import date

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase

from .common import EhAssetTestCase


@tagged('eh_golden', 'eh_account_assets_pro', 'post_install', '-at_install')
class TestGoldenIas36(EhGoldenTestCase, EhAssetTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The revaluation wizard gates on the stock account manager group
        # (the impairment/CGU paths gate on the EH manager group, which
        # EhAssetTestCase already grants).
        cls.env.user.group_ids |= cls.env.ref('account.group_account_manager')

    def _running_asset(self, code, cost, life_months=60, **overrides):
        """A running straight-line asset with no depreciation posted yet,
        so its NBV equals its cost. Full first period (no proration)."""
        vals = {
            'code': code,
            'acquisition_cost': cost,
            'method': 'straight_line',
            'useful_life_months': life_months,
            'salvage_value': 0.0,
            'prorate_first_period': False,
        }
        vals.update(overrides)
        asset = self._make_asset(**vals)
        asset.action_compute_schedule()
        asset.action_activate()
        return asset

    # ------------------------------------------------------------------
    # 1. Value in use: discounted cash flow (IAS 36.30-.57)
    # ------------------------------------------------------------------
    def test_golden_value_in_use_three_year_dcf(self):
        """VIU of 100,000/yr for 3 years at a 10% pre-tax rate.

        Module PV convention (eh.asset.cgu._compute_value_in_use):
        end-of-period flows, amount / (1 + r) ** period, summed unrounded,
        one terminal rounding in the CGU currency (USD, 2dp).

        Hand derivation:
          year 1: 100,000 / 1.1    = 90,909.090909...
          year 2: 100,000 / 1.21   = 82,644.628099...
          year 3: 100,000 / 1.331  = 75,131.480090...
          sum                      = 248,685.199098... -> 248,685.20
        """
        cgu = self.env['eh.asset.cgu'].create({
            'name': 'Golden DCF unit',
            'discount_rate': 10.0,
            'cashflow_ids': [
                (0, 0, {'period': 1, 'amount': 100_000.0}),
                (0, 0, {'period': 2, 'amount': 100_000.0}),
                (0, 0, {'period': 3, 'amount': 100_000.0}),
            ],
            # FVLCD = 240,000 - 5,000 = 235,000, BELOW the VIU, so the
            # recoverable amount must be the VIU (higher-of, IAS 36.18).
            'fair_value': 240_000.0,
            'costs_of_disposal': 5_000.0,
        })
        self.assertAlmostEqual(cgu.value_in_use, 248_685.20, places=2)
        self.assertAlmostEqual(
            cgu.fair_value_less_costs, 235_000.00, places=2,
        )
        # recoverable = max(VIU, FVLCD) = max(248,685.20, 235,000.00)
        self.assertAlmostEqual(
            cgu.recoverable_amount, 248_685.20, places=2,
        )

    # ------------------------------------------------------------------
    # 2. CGU allocation: goodwill first, then pro-rata (IAS 36.104)
    # ------------------------------------------------------------------
    def test_golden_cgu_allocation_goodwill_first_then_prorata(self):
        """CGU carrying 300,000 = goodwill 40,000 + A 156,000 + B 104,000;
        recoverable amount 220,000 -> loss 80,000.

        Hand derivation (IAS 36.104):
          stage 1  goodwill absorbs first, capped at its NBV:
                   min(40,000, 80,000)            = 40,000
          stage 2  remaining 40,000 pro-rata on carrying amount over the
                   non-goodwill base 156,000 + 104,000 = 260,000:
                   A: 40,000 * 156,000 / 260,000  = 24,000
                   B: 40,000 * 104,000 / 260,000  = 16,000
          post-test NBVs: goodwill 0, A 132,000, B 88,000; sum = 220,000
          = the recoverable amount, as IAS 36 requires.
        """
        goodwill = self._running_asset('GLD-GW', 40_000.0, is_goodwill=True)
        asset_a = self._running_asset('GLD-A', 156_000.0)
        asset_b = self._running_asset('GLD-B', 104_000.0)
        cgu = self.env['eh.asset.cgu'].create({
            'name': 'Golden allocation unit',
            # No cash flows (VIU = 0); recoverable amount comes from
            # FVLCD = 220,000 - 0 = 220,000.
            'fair_value': 220_000.0,
            'costs_of_disposal': 0.0,
            'member_ids': [(6, 0, [goodwill.id, asset_a.id, asset_b.id])],
        })
        cgu.invalidate_recordset([
            'carrying_amount', 'recoverable_amount', 'impairment_shortfall',
        ])
        self.assertAlmostEqual(cgu.carrying_amount, 300_000.00, places=2)
        self.assertAlmostEqual(cgu.recoverable_amount, 220_000.00, places=2)
        self.assertAlmostEqual(cgu.impairment_shortfall, 80_000.00, places=2)

        cgu.action_test_now()

        self.assertEqual(cgu.last_test_result, 'impaired')
        expected = {
            goodwill: 40_000.00,
            asset_a: 24_000.00,
            asset_b: 16_000.00,
        }
        imps = cgu.impairment_ids
        self.assertEqual(len(imps), 3)
        for asset, amount in expected.items():
            asset.invalidate_recordset(
                ['net_book_value', 'accumulated_impairment'],
            )
            self.assertAlmostEqual(
                asset.accumulated_impairment, amount, places=2,
                msg='allocation to %s' % asset.code,
            )
            imp = imps.filtered(lambda i: i.asset_id == asset)
            self.assertEqual(len(imp), 1)
            self.assertEqual(imp.state, 'posted')
            # CGU-derived impairments leave the account overrides blank,
            # so posting falls back to the asset's disposal loss account
            # (P&L leg) and accumulated depreciation account (contra).
            self.assertMoveLines(imp.move_id, [
                (self.account_disposal_loss, amount, 0.0),
                (self.account_accum_dep, 0.0, amount),
            ])
        # Post-test carrying amounts, hand-derived above.
        self.assertAlmostEqual(goodwill.net_book_value, 0.00, places=2)
        self.assertAlmostEqual(asset_a.net_book_value, 132_000.00, places=2)
        self.assertAlmostEqual(asset_b.net_book_value, 88_000.00, places=2)
        # The allocation ties EXACTLY to the shortfall.
        self.assertAlmostEqual(
            sum(imps.mapped('amount')), 80_000.00, places=2,
        )

    # ------------------------------------------------------------------
    # 3. Reversal ceiling: depreciated historical cost (IAS 36.117)
    # ------------------------------------------------------------------
    def test_golden_reversal_capped_at_depreciated_historical_cost(self):
        """Cost 120,000, 120-month (10-year) straight line, no salvage.

        Hand derivation, following the module's own schedule mechanics
        (monthly periods; after an impairment posts, the engine wipes the
        unposted lines and re-amortises the post-event NBV over the SAME
        number of remaining periods, per IAS 36.63):

          original schedule:            120,000 / 120 = 1,000 per month
          post months 1-24:             accum dep 24,000, carrying 96,000
          impair 24,000 (posted):       carrying 96,000 - 24,000 = 72,000
          IAS 36.63 re-amortisation:    72,000 / 96 remaining = 750/month
          post months 25-48:            + 24 * 750 = 18,000
                                        total posted dep = 42,000
                                        carrying = 120,000 - 42,000
                                                   - 24,000 = 54,000

          depreciated historical cost at month 48 (the IAS 36.117
          ceiling, computed on the ORIGINAL 1,000/month schedule):
                                        120,000 - 48 * 1,000 = 72,000
          maximum reversal:             72,000 - 54,000      = 18,000

        The cumulative cap alone would allow reversing the full 24,000
        charge (24,000 <= 24,000 cumulative), but that would lift the
        carrying amount to 78,000, above the 72,000 ceiling. The .117
        ceiling must bind FIRST: a 24,000 reversal is refused, an 18,000
        reversal posts and lands the carrying amount exactly on 72,000.
        """
        asset = self._running_asset(
            'GLD-CEIL', 120_000.0, life_months=120,
            in_service_date='2022-01-31',
        )
        # Post months 1-24 of the original 1,000/month schedule.
        lines = asset.depreciation_line_ids.sorted('sequence')
        self.assertEqual(len(lines), 120)
        self.assertAlmostEqual(lines[0].amount, 1_000.00, places=2)
        for line in lines[:24]:
            line.action_post()
        asset.invalidate_recordset(['net_book_value', 'total_depreciated'])
        self.assertAlmostEqual(asset.total_depreciated, 24_000.00, places=2)
        self.assertAlmostEqual(asset.net_book_value, 96_000.00, places=2)

        # Impair 24,000 at the end of year 2: carrying 96,000 -> 72,000.
        charge = self.env['eh.asset.impairment'].create({
            'asset_id': asset.id,
            'impairment_date': '2023-12-31',
            'amount': 24_000.0,
            'is_reversal': False,
            'impairment_account_id': self.account_impairment.id,
            'accumulated_account_id': self.account_accum_dep.id,
            'reason': 'Golden IAS 36.117 ceiling example: charge',
        })
        charge.action_post()
        self.assertMoveLines(charge.move_id, [
            (self.account_impairment, 24_000.00, 0.0),
            (self.account_accum_dep, 0.0, 24_000.00),
        ])
        asset.invalidate_recordset(['net_book_value'])
        self.assertAlmostEqual(asset.net_book_value, 72_000.00, places=2)
        # IAS 36.63: the 96 remaining periods re-amortise the lower base,
        # 72,000 / 96 = 750 per month.
        unposted = asset.depreciation_line_ids.filtered(
            lambda l: not l.is_posted,
        ).sorted('sequence')
        self.assertEqual(len(unposted), 96)
        self.assertAlmostEqual(unposted[0].amount, 750.00, places=2)

        # Post months 25-48 on the re-amortised 750/month schedule.
        for line in unposted[:24]:
            line.action_post()
        asset.invalidate_recordset(['net_book_value', 'total_depreciated'])
        self.assertAlmostEqual(asset.total_depreciated, 42_000.00, places=2)
        self.assertAlmostEqual(asset.net_book_value, 54_000.00, places=2)

        # Recoverable amount has recovered (say 90,000), so management
        # wants to reverse the full 24,000. The cumulative cap permits it,
        # but the IAS 36.117 ceiling (72,000) caps the reversal at 18,000.
        Imp = self.env['eh.asset.impairment']
        with self.assertRaises(ValidationError):
            Imp.create({
                'asset_id': asset.id,
                'impairment_date': '2025-12-31',
                'amount': 24_000.0,
                'is_reversal': True,
                'impairment_account_id': self.account_impairment.id,
                'accumulated_account_id': self.account_accum_dep.id,
                'reason': 'Golden IAS 36.117 ceiling example: over-reversal',
            })

        # The maximum permitted reversal, 18,000, posts and lands the
        # carrying amount exactly on the depreciated historical cost.
        reversal = Imp.create({
            'asset_id': asset.id,
            'impairment_date': '2025-12-31',
            'amount': 18_000.0,
            'is_reversal': True,
            'impairment_account_id': self.account_impairment.id,
            'accumulated_account_id': self.account_accum_dep.id,
            'reason': 'Golden IAS 36.117 ceiling example: capped reversal',
        })
        reversal.action_post()
        self.assertMoveLines(reversal.move_id, [
            (self.account_accum_dep, 18_000.00, 0.0),
            (self.account_impairment, 0.0, 18_000.00),
        ])
        asset.invalidate_recordset(
            ['net_book_value', 'accumulated_impairment'],
        )
        # 120,000 - 42,000 - (24,000 - 18,000) = 72,000 = the ceiling.
        self.assertAlmostEqual(asset.net_book_value, 72_000.00, places=2)
        self.assertAlmostEqual(
            asset.accumulated_impairment, 6_000.00, places=2,
        )
        # IAS 36.63 again: the 72 remaining periods re-amortise 72,000,
        # i.e. 1,000/month, back on the historical-cost trajectory.
        unposted = asset.depreciation_line_ids.filtered(
            lambda l: not l.is_posted,
        ).sorted('sequence')
        self.assertEqual(len(unposted), 72)
        self.assertAlmostEqual(unposted[0].amount, 1_000.00, places=2)

    # ------------------------------------------------------------------
    # 4. Revaluation down then up through the reserve (IAS 16.39/.40)
    # ------------------------------------------------------------------
    def test_golden_revaluation_down_then_up_through_reserve(self):
        """Carrying 100,000; revalue down 15,000, later revalue up 25,000.

        Hand derivation:
          down 15,000 with NO existing surplus (IAS 16.40): the whole
          decrease is a P&L charge.
              Dr Impairment Loss (P&L)   15,000
              Cr Fixed Assets                     15,000
          carrying 85,000; revaluation_pl_decrease = 15,000.

          up 25,000 (IAS 16.39): first reverse the prior P&L decrease
          (credit income) up to 15,000; only the excess 10,000 is
          credited to the revaluation reserve (OCI/equity).
              Dr Fixed Assets            25,000
              Cr Impairment Loss (P&L)            15,000
              Cr Revaluation Reserve              10,000
          carrying 110,000; surplus = 10,000; pl_decrease = 0.
        """
        asset = self._running_asset('GLD-RV', 100_000.0, life_months=120)
        self.assertAlmostEqual(asset.net_book_value, 100_000.00, places=2)

        down = self.env['eh.asset.revalue.wizard'].create({
            'asset_id': asset.id,
            'revalue_date': '2026-03-31',
            'direction': 'impairment',
            'amount': 15_000.0,
            'counterpart_account_id': self.account_impairment.id,
        })
        down.action_revalue()
        down_move = self.env['account.move'].search([
            ('ref', '=', 'Revaluation %s' % asset.display_name),
            ('date', '=', '2026-03-31'),
        ])
        self.assertEqual(len(down_move), 1)
        self.assertEqual(down_move.state, 'posted')
        self.assertMoveLines(down_move, [
            (self.account_impairment, 15_000.00, 0.0),
            (self.account_fixed, 0.0, 15_000.00),
        ])
        self.assertBalanced(down_move)
        asset.invalidate_recordset(['net_book_value'])
        self.assertAlmostEqual(asset.net_book_value, 85_000.00, places=2)
        self.assertAlmostEqual(
            asset.revaluation_pl_decrease, 15_000.00, places=2,
        )
        self.assertAlmostEqual(asset.revaluation_surplus, 0.00, places=2)

        up = self.env['eh.asset.revalue.wizard'].create({
            'asset_id': asset.id,
            'revalue_date': '2026-04-30',
            'direction': 'uplift',
            'amount': 25_000.0,
            'counterpart_account_id': self.account_reval_reserve.id,
            'revaluation_income_account_id': self.account_impairment.id,
        })
        up.action_revalue()
        up_move = self.env['account.move'].search([
            ('ref', '=', 'Revaluation %s' % asset.display_name),
            ('date', '=', '2026-04-30'),
        ])
        self.assertEqual(len(up_move), 1)
        self.assertEqual(up_move.state, 'posted')
        self.assertMoveLines(up_move, [
            (self.account_fixed, 25_000.00, 0.0),
            (self.account_impairment, 0.0, 15_000.00),
            (self.account_reval_reserve, 0.0, 10_000.00),
        ])
        self.assertBalanced(up_move)
        asset.invalidate_recordset(['net_book_value'])
        # 85,000 + 25,000 = 110,000; net adjustment -15,000 + 25,000.
        self.assertAlmostEqual(asset.net_book_value, 110_000.00, places=2)
        self.assertAlmostEqual(
            asset.revaluation_adjustment, 10_000.00, places=2,
        )
        self.assertAlmostEqual(
            asset.revaluation_surplus, 10_000.00, places=2,
        )
        self.assertAlmostEqual(
            asset.revaluation_pl_decrease, 0.00, places=2,
        )

    # ------------------------------------------------------------------
    # 5. Manual method: hypothetical straight-line ceiling (IAS 36.117)
    # ------------------------------------------------------------------
    def test_golden_manual_method_ceiling_hypothetical_straight_line(self):
        """Cost 60,000, useful life 60 months, MANUAL method, in service
        2024-01-01, salvage 0. Management hand-keys and posts 24 monthly
        lines of 800 (slower than straight line) through 2025-12-31.

        Hand derivation:
          posted depreciation:            24 * 800          = 19,200
          impair 30,000 on 2025-12-31:    carrying = 60,000 - 19,200
                                                    - 30,000 = 10,800

          IAS 36.117 ceiling at 2025-12-31 for a manual asset (no
          engine schedule): hypothetical straight line over useful
          life, elapsed month-ends Jan-2024..Dec-2025 = 24:
            hypothetical = 60,000 - (60,000 / 60) * 24      = 36,000
            cost less posted depreciation = 60,000 - 19,200 = 40,800
            ceiling = min(36,000, 40,800)                   = 36,000
          maximum reversal = 36,000 - 10,800                = 25,200

        The OLD fallback (raw cost less posted depreciation, 40,800)
        would have allowed the full 30,000 reversal (cumulative cap
        30,000 <= 30,000; 40,800 - 10,800 = 30,000), lifting the
        carrying amount to 40,800 - far above any systematic
        depreciation trajectory. The hypothetical-SL ceiling must
        block 30,000 and admit exactly 25,200, landing the carrying
        amount on 36,000.
        """
        asset = self._make_asset(
            code='GLD-MAN',
            method='manual',
            acquisition_cost=60_000.0,
            useful_life_months=60,
            salvage_value=0.0,
            in_service_date='2024-01-01',
        )
        asset.action_activate()  # manual: no engine schedule generated
        self.assertFalse(asset.depreciation_line_ids)

        # Hand-key and post 24 monthly lines of 800 (Jan-24..Dec-25).
        Line = self.env['eh.asset.depreciation.line']
        period = date(2024, 1, 31)
        accumulated = 0.0
        for seq in range(1, 25):
            accumulated += 800.0
            line = Line.create({
                'asset_id': asset.id,
                'sequence': seq,
                'depreciation_date': period,
                'amount': 800.0,
                'accumulated': accumulated,
                'remaining_value': 60_000.0 - accumulated,
            })
            line.action_post()
            period = asset._next_period_end(period)
        asset.invalidate_recordset(['net_book_value', 'total_depreciated'])
        self.assertAlmostEqual(asset.total_depreciated, 19_200.00, places=2)
        self.assertAlmostEqual(asset.net_book_value, 40_800.00, places=2)

        charge = self.env['eh.asset.impairment'].create({
            'asset_id': asset.id,
            'impairment_date': '2025-12-31',
            'amount': 30_000.0,
            'is_reversal': False,
            'impairment_account_id': self.account_impairment.id,
            'accumulated_account_id': self.account_accum_dep.id,
            'reason': 'Golden manual-ceiling example: charge',
        })
        charge.action_post()
        asset.invalidate_recordset(['net_book_value'])
        self.assertAlmostEqual(asset.net_book_value, 10_800.00, places=2)

        # The ceiling itself, measured at the reversal date.
        self.assertAlmostEqual(
            asset._ias36_depreciated_cost(as_of_date=date(2025, 12, 31)),
            36_000.00, places=2,
        )

        # Full 30,000 reversal: allowed under the old raw-cost fallback,
        # blocked by the hypothetical straight-line ceiling.
        Imp = self.env['eh.asset.impairment']
        with self.assertRaises(ValidationError):
            Imp.create({
                'asset_id': asset.id,
                'impairment_date': '2025-12-31',
                'amount': 30_000.0,
                'is_reversal': True,
                'impairment_account_id': self.account_impairment.id,
                'accumulated_account_id': self.account_accum_dep.id,
                'reason': 'Golden manual-ceiling example: over-reversal',
            })

        # The maximum permitted reversal posts and lands the carrying
        # amount exactly on the hypothetical depreciated cost.
        reversal = Imp.create({
            'asset_id': asset.id,
            'impairment_date': '2025-12-31',
            'amount': 25_200.0,
            'is_reversal': True,
            'impairment_account_id': self.account_impairment.id,
            'accumulated_account_id': self.account_accum_dep.id,
            'reason': 'Golden manual-ceiling example: capped reversal',
        })
        reversal.action_post()
        self.assertMoveLines(reversal.move_id, [
            (self.account_accum_dep, 25_200.00, 0.0),
            (self.account_impairment, 0.0, 25_200.00),
        ])
        asset.invalidate_recordset(
            ['net_book_value', 'accumulated_impairment'],
        )
        # 60,000 - 19,200 - (30,000 - 25,200) = 36,000 = the ceiling.
        self.assertAlmostEqual(asset.net_book_value, 36_000.00, places=2)
        self.assertAlmostEqual(
            asset.accumulated_impairment, 4_800.00, places=2,
        )

    # ------------------------------------------------------------------
    # 6. Revaluation uplift capped at the recoverable amount (IAS 36)
    # ------------------------------------------------------------------
    def test_golden_uplift_capped_at_recoverable_amount(self):
        """Cost 100,000, 120-month straight line, nothing posted.

        Hand derivation:
          charge 20,000 on 2026-03-31 stating recoverable 80,000:
              carrying 100,000 - 20,000 = 80,000
              asset.recoverable_amount_latest = 80,000
          full reversal 20,000 on 2026-06-30 stating recoverable 101,000
          (conditions recovered; the .117 ceiling is 100,000 with no
          depreciation posted, so 20,000 is admissible):
              carrying back to 100,000
              asset.recoverable_amount_latest = 101,000

          uplift 1,000:  carrying 101,000 <= 101,000 cap -> posts.
          uplift 5,000:  carrying 106,000  > 101,000 cap -> blocked;
                         blocked again with override but no reason;
                         posts with override + reason, and the override
                         is logged on the asset.
        """
        asset = self._running_asset('GLD-CAP', 100_000.0, life_months=120)

        charge = self.env['eh.asset.impairment'].create({
            'asset_id': asset.id,
            'impairment_date': '2026-03-31',
            'amount': 20_000.0,
            'is_reversal': False,
            'recoverable_amount': 80_000.0,
            'impairment_account_id': self.account_impairment.id,
            'accumulated_account_id': self.account_accum_dep.id,
            'reason': 'Golden uplift-cap example: charge',
        })
        charge.action_post()
        self.assertAlmostEqual(
            asset.recoverable_amount_latest, 80_000.00, places=2,
        )
        self.assertEqual(asset.recoverable_amount_date, date(2026, 3, 31))

        reversal = self.env['eh.asset.impairment'].create({
            'asset_id': asset.id,
            'impairment_date': '2026-06-30',
            'amount': 20_000.0,
            'is_reversal': True,
            'recoverable_amount': 101_000.0,
            'impairment_account_id': self.account_impairment.id,
            'accumulated_account_id': self.account_accum_dep.id,
            'reason': 'Golden uplift-cap example: recovery',
        })
        reversal.action_post()
        asset.invalidate_recordset(
            ['net_book_value', 'accumulated_impairment'],
        )
        self.assertAlmostEqual(asset.net_book_value, 100_000.00, places=2)
        self.assertAlmostEqual(
            asset.recoverable_amount_latest, 101_000.00, places=2,
        )

        Wizard = self.env['eh.asset.revalue.wizard']
        # Within the cap: 100,000 + 1,000 = 101,000 <= 101,000.
        within = Wizard.create({
            'asset_id': asset.id,
            'revalue_date': '2026-07-31',
            'direction': 'uplift',
            'amount': 1_000.0,
            'counterpart_account_id': self.account_reval_reserve.id,
        })
        within.action_revalue()
        asset.invalidate_recordset(['net_book_value'])
        self.assertAlmostEqual(asset.net_book_value, 101_000.00, places=2)

        # Beyond the cap: blocked without the override.
        beyond = Wizard.create({
            'asset_id': asset.id,
            'revalue_date': '2026-08-31',
            'direction': 'uplift',
            'amount': 5_000.0,
            'counterpart_account_id': self.account_reval_reserve.id,
        })
        with self.assertRaises(UserError):
            beyond.action_revalue()

        # Override without a reason: still blocked.
        beyond_no_reason = Wizard.create({
            'asset_id': asset.id,
            'revalue_date': '2026-08-31',
            'direction': 'uplift',
            'amount': 5_000.0,
            'counterpart_account_id': self.account_reval_reserve.id,
            'override_recoverable_cap': True,
        })
        with self.assertRaises(UserError):
            beyond_no_reason.action_revalue()

        # Override with a documented reason: posts and logs.
        overridden = Wizard.create({
            'asset_id': asset.id,
            'revalue_date': '2026-08-31',
            'direction': 'uplift',
            'amount': 5_000.0,
            'counterpart_account_id': self.account_reval_reserve.id,
            'override_recoverable_cap': True,
            'override_reason': 'Fresh valuation 2026-08 supports 106,000.',
        })
        overridden.action_revalue()
        asset.invalidate_recordset(['net_book_value'])
        self.assertAlmostEqual(asset.net_book_value, 106_000.00, places=2)
        log = self.env['mail.message'].search([
            ('model', '=', 'eh.asset'),
            ('res_id', '=', asset.id),
        ]).filtered(lambda m: 'OVERRIDDEN' in (m.body or ''))
        self.assertTrue(log)
