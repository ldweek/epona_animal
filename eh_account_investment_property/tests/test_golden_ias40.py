# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden IAS 40 worked examples: transfers at fair value on the transfer
date, the transfer audit trail, and the depreciation halt on model switch.

Each test encodes a hand-computed worked example (numbers derived by hand
from the inputs stated in the test, never read back from the engine) and
asserts the exact journal entries line by line.

Transfer conventions implemented by eh.investment.property (read from
models/investment_property.py):

* Fair value model OUT (IAS 40.60-61): the property is remeasured to
  transfer_fair_value first, in its own entry, with the gap to profit or
  loss (Dr property / Cr FV gain on an uplift, mirrored on a deficit);
  the derecognition entry then moves the property at that fair value
  (Dr transfer target / Cr property account), which becomes the deemed
  cost of the destination. transfer_fair_value left at zero means "not
  supplied": the carrying amount, already fair value per IAS 40.33, is
  used and no remeasurement entry posts (legacy behaviour preserved).
* Cost model OUT (IAS 40.59): the carrying amount never changes on a
  transfer. The entry is unchanged from the pre-fair-value build
  (Dr target NBV, Dr accumulated depreciation, Cr property gross cost);
  a fair value supplied at the transfer date is stored on the audit
  trail for disclosure only.
* Transfer IN from a fixed asset (IAS 40.57(d)/.61-62): one balanced
  entry revalues the asset to fair value and derecognises it into the
  property. An uplift is credited to the equity revaluation surplus
  (OCI); a deficit first consumes the asset's own revaluation surplus
  and only the excess is charged to profit or loss. The source asset is
  terminally derecognised against the same sealed transfer move.
* Every transfer writes exactly one immutable audit-trail row: date,
  direction, basis, carrying before, fair value at transfer date, delta
  posted with its P&L/OCI routing, and the move links.

All amounts are in the company currency (USD, 2dp); every expected figure
is derived in a comment next to its assertion.
"""

from datetime import date, timedelta
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_base.tests.pairwise import pairwise_cases


@tagged('eh_golden', 'eh_account_investment_property', 'post_install',
        '-at_install')
class TestGoldenIas40(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.prop_acct = cls._ensure_account(
            cls.env, '1660', 'Investment Property', 'asset_non_current')
        cls.fv_gl = cls._ensure_account(
            cls.env, '4660', 'Investment Property FV Gain/Loss',
            'income_other')
        cls.dep_exp = cls._ensure_account(
            cls.env, '6660', 'Investment Property Depreciation', 'expense')
        cls.accum_dep = cls._ensure_account(
            cls.env, '1661', 'Accumulated Depreciation IP',
            'asset_non_current')
        cls.ppe = cls._ensure_account(
            cls.env, '1560', 'Owner-Occupied Property', 'asset_non_current')
        cls.surplus = cls._ensure_account(
            cls.env, '3660', 'Revaluation Surplus (OCI)', 'equity')
        cls.retained = cls._ensure_account(
            cls.env, '3661', 'Retained Earnings', 'equity')
        cls.opening_clear = cls._ensure_account(
            cls.env, '2160', 'Investment Property Acquisition Clearing',
            'liability_current')
        # Fixed-asset side accounts for the hard-dependent transfer-in path.
        cls.fa_gross = cls._ensure_account(
            cls.env, '1565', 'PP&E at Cost', 'asset_fixed')
        cls.fa_accum = cls._ensure_account(
            cls.env, '1566', 'PP&E Accumulated Depreciation', 'asset_fixed')
        cls.fa_dep_exp = cls._ensure_account(
            cls.env, '6661', 'PP&E Depreciation Expense',
            'expense_depreciation')
        cls.fa_disp_gain = cls._ensure_account(
            cls.env, '4661', 'Gain on Asset Disposal', 'income_other')
        cls.fa_disp_loss = cls._ensure_account(
            cls.env, '6662', 'Loss on Asset Disposal', 'expense')
        cls.asset_category_golden = cls.env['eh.asset.category'].create({
            'name': 'Buildings',
            'code': 'BLDG',
            'method': 'straight_line',
            'useful_life_months': 60,
            'salvage_rate': 0.0,
            'prorate_first_period': False,
            'asset_account_id': cls.fa_gross.id,
            'depreciation_account_id': cls.fa_dep_exp.id,
            'accumulated_depreciation_account_id': cls.fa_accum.id,
            'disposal_gain_account_id': cls.fa_disp_gain.id,
            'disposal_loss_account_id': cls.fa_disp_loss.id,
            'revaluation_reserve_account_id': cls.surplus.id,
            'journal_id': cls.journal_misc.id,
        })
        cls.real_manager = cls.env['res.users'].create({
            'name': 'IAS 40 Real Manager',
            'login': 'ias40_real_manager',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, [cls.company.id])],
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_manager').id,
            ])],
        })

    # ------------------------------------------------------------------
    # fixtures
    # ------------------------------------------------------------------
    def _prop(self, **vals):
        base = {
            'name': '/', 'model_basis': 'fair_value',
            'initial_cost': 500000.0,
            'acquisition_date': '2026-01-01',
            'property_account_id': self.prop_acct.id,
            'opening_counterpart_account_id': self.opening_clear.id,
            'fv_gain_loss_account_id': self.fv_gl.id,
            'transfer_target_account_id': self.ppe.id,
            'revaluation_surplus_account_id': self.surplus.id,
            'journal_id': self.journal_misc.id,
        }
        base.update(vals)
        return self.env['eh.investment.property'].create(base)

    def _cost_prop(self, **vals):
        base = {
            'model_basis': 'cost', 'initial_cost': 500000.0,
            'useful_life_years': 50,
            'depreciation_expense_account_id': self.dep_exp.id,
            'accumulated_depreciation_account_id': self.accum_dep.id,
        }
        base.update(vals)
        return self._prop(**base)

    def _asset(self, cost=400000.0, **overrides):
        """A running straight-line fixed asset with no depreciation posted."""
        vals = {
            'name': '/',
            'code': 'BLDG-GOLD',
            'category_id': self.asset_category_golden.id,
            'acquisition_date': '2026-01-01',
            'in_service_date': '2026-01-31',
            'acquisition_cost': cost,
            'salvage_value': 0.0,
            'method': 'straight_line',
            'useful_life_months': 60,
            'prorate_first_period': False,
            'asset_account_id': self.fa_gross.id,
            'depreciation_account_id': self.fa_dep_exp.id,
            'accumulated_depreciation_account_id': self.fa_accum.id,
            'disposal_gain_account_id': self.fa_disp_gain.id,
            'disposal_loss_account_id': self.fa_disp_loss.id,
            'revaluation_reserve_account_id': self.surplus.id,
            'journal_id': self.journal_misc.id,
        }
        vals.update(overrides)
        asset = self.env['eh.asset'].create(vals)
        asset.action_activate()
        return asset

    def _single_log(self, prop):
        log = prop.transfer_log_ids
        self.assertEqual(len(log), 1, 'expected exactly one audit-trail row')
        return log

    # ------------------------------------------------------------------
    # (b) IP at fair value -> PPE: remeasure to transfer FV, then leave
    # ------------------------------------------------------------------
    def test_golden_fv_out_uplift_remeasures_then_derecognises(self):
        """IP at FV, carrying 500,000; fair value at transfer date 530,000.

        Hand derivation (IAS 40.60-61):
          remeasure  delta = 530,000 - 500,000 = +30,000 to P&L:
                       Dr 1660  30,000 / Cr 4660  30,000
          transfer   deemed cost = FV at transfer date = 530,000:
                       Dr 1560 530,000 / Cr 1660 530,000
        """
        p = self._prop(initial_cost=500000.0)
        p.action_activate()
        p.write({'transfer_fair_value': 530000.0,
                 'transfer_date': date(2026, 3, 31)})
        p.action_transfer_out()
        self.assertEqual(p.state, 'transferred')
        moves = p.move_ids.sorted('id')
        self.assertEqual(len(moves), 3)
        self.assertMoveLines(moves[1], [
            (self.prop_acct, 30000.0, 0.0),
            (self.fv_gl, 0.0, 30000.0),
        ])
        self.assertMoveLines(moves[2], [
            (self.ppe, 530000.0, 0.0),
            (self.prop_acct, 0.0, 530000.0),
        ])
        for move in moves[1:]:
            self.assertBalanced(move)
            self.assertEqual(str(move.date), '2026-03-31')
        # Audit trail: reconstructable from the single row.
        log = self._single_log(p)
        self.assertEqual(log.direction, 'out')
        self.assertEqual(log.basis, 'fair_value')
        self.assertEqual(str(log.date), '2026-03-31')
        self.assertAlmostEqual(log.carrying_before, 500000.0, places=2)
        self.assertAlmostEqual(log.fair_value, 530000.0, places=2)
        self.assertAlmostEqual(log.delta_posted, 30000.0, places=2)
        self.assertEqual(log.delta_routing, 'pl')
        self.assertEqual(log.remeasure_move_id, moves[1])
        self.assertEqual(log.move_id, moves[2])

    def test_golden_fv_out_deficit_remeasures_then_derecognises(self):
        """IP at FV, carrying 500,000; fair value at transfer date 470,000.

        Hand derivation:
          remeasure  delta = 470,000 - 500,000 = -30,000 to P&L:
                       Dr 4660  30,000 / Cr 1660  30,000
          transfer     Dr 1560 470,000 / Cr 1660 470,000
        """
        p = self._prop(initial_cost=500000.0)
        p.action_activate()
        p.transfer_fair_value = 470000.0
        p.action_transfer_out()
        moves = p.move_ids.sorted('id')
        self.assertEqual(len(moves), 3)
        self.assertMoveLines(moves[1], [
            (self.fv_gl, 30000.0, 0.0),
            (self.prop_acct, 0.0, 30000.0),
        ])
        self.assertMoveLines(moves[2], [
            (self.ppe, 470000.0, 0.0),
            (self.prop_acct, 0.0, 470000.0),
        ])
        log = self._single_log(p)
        self.assertAlmostEqual(log.delta_posted, -30000.0, places=2)
        self.assertEqual(log.delta_routing, 'pl')

    def test_golden_fv_out_without_fv_keeps_legacy_single_entry(self):
        """No transfer fair value supplied: the carrying amount (already
        fair value per IAS 40.33) moves unchanged; exactly one entry posts
        and the audit row records fair value = carrying, delta 0."""
        p = self._prop(initial_cost=500000.0)
        p.action_activate()
        p.action_transfer_out()
        moves = p.move_ids.sorted('id')
        self.assertEqual(len(moves), 2)
        self.assertMoveLines(moves[1], [
            (self.ppe, 500000.0, 0.0),
            (self.prop_acct, 0.0, 500000.0),
        ])
        log = self._single_log(p)
        self.assertAlmostEqual(log.fair_value, 500000.0, places=2)
        self.assertAlmostEqual(log.delta_posted, 0.0, places=2)
        self.assertEqual(log.delta_routing, 'none')
        self.assertFalse(log.remeasure_move_id)

    # ------------------------------------------------------------------
    # (a) cost model out: carrying unchanged, FV disclosed only (IAS 40.59)
    # ------------------------------------------------------------------
    def test_golden_cost_out_keeps_carrying_and_logs_disclosed_fv(self):
        """Cost model, cost 500,000, life 50y, one charge posted.

        Hand derivation:
          charge     = 500,000 / 50 = 10,000
          carrying   = 500,000 - 10,000 = 490,000
        IAS 40.59: the transfer does not change the carrying amount and no
        remeasurement posts, even though a fair value of 520,000 is
        supplied; that fair value lands on the audit trail only:
          Dr 1560 490,000, Dr 1661 10,000 / Cr 1660 500,000
        """
        p = self._cost_prop()
        p.action_activate()
        p.depreciation_date = date(2026, 3, 31)
        p.action_depreciate()
        p.write({'transfer_fair_value': 520000.0,
                 'transfer_date': date(2026, 4, 30)})
        p.action_transfer_out()
        self.assertEqual(p.state, 'transferred')
        moves = p.move_ids.sorted('id')
        # one depreciation entry + one transfer entry, no remeasurement
        self.assertEqual(len(moves), 3)
        self.assertMoveLines(moves[2], [
            (self.ppe, 490000.0, 0.0),
            (self.accum_dep, 10000.0, 0.0),
            (self.prop_acct, 0.0, 500000.0),
        ])
        # Measurement is unchanged immediately before transfer; terminal
        # subledger totals are then cleared with the derecognised GL balance.
        self.assertAlmostEqual(p.carrying_amount, 0.0, places=2)
        self.assertAlmostEqual(p.accumulated_depreciation, 0.0, places=2)
        log = self._single_log(p)
        self.assertEqual(log.basis, 'cost')
        self.assertAlmostEqual(log.carrying_before, 490000.0, places=2)
        self.assertAlmostEqual(log.fair_value, 520000.0, places=2)
        self.assertAlmostEqual(log.delta_posted, 0.0, places=2)
        self.assertEqual(log.delta_routing, 'none')
        self.assertFalse(log.remeasure_move_id)

    # ------------------------------------------------------------------
    # (c) PPE -> IP at fair value (IAS 40.57(d)/.61-62)
    # ------------------------------------------------------------------
    def test_golden_transfer_in_downward_to_pl(self):
        """Asset carrying 400,000 (cost 400,000, nothing depreciated),
        fair value at transfer date 380,000.

        Hand derivation (IAS 40.62(a), no surplus on the asset so the whole
        deficit of 400,000 - 380,000 = 20,000 charges P&L):
          Dr 1660 380,000, Dr 4660 20,000 / Cr 1565 400,000
        """
        asset = self._asset(cost=400000.0)
        p = self._prop(initial_cost=0.0)
        p.write({
            'transfer_in_asset_id': asset.id,
            'transfer_fair_value': 380000.0,
            'transfer_date': date(2026, 5, 31),
        })
        p.action_transfer_in()
        self.assertEqual(p.state, 'held')
        self.assertAlmostEqual(p.carrying_amount, 380000.0, places=2)
        # Deemed cost = fair value at the date of change in use.
        self.assertAlmostEqual(p.initial_cost, 380000.0, places=2)
        self.assertEqual(asset.state, 'disposed')
        moves = p.move_ids.sorted('id')
        self.assertEqual(len(moves), 1)
        self.assertMoveLines(moves[0], [
            (self.prop_acct, 380000.0, 0.0),
            (self.fv_gl, 20000.0, 0.0),
            (self.fa_gross, 0.0, 400000.0),
        ])
        log = self._single_log(p)
        self.assertEqual(log.direction, 'in')
        self.assertAlmostEqual(log.carrying_before, 400000.0, places=2)
        self.assertAlmostEqual(log.fair_value, 380000.0, places=2)
        self.assertAlmostEqual(log.delta_posted, -20000.0, places=2)
        self.assertEqual(log.delta_routing, 'pl')
        self.assertEqual(log.move_id, moves[0])
        self.assertEqual(log.source_asset_id, asset.id)
        self.assertEqual(log.source_document, asset.display_name)
        self.assertEqual(asset.disposal_move_id, moves[0])

    def test_golden_transfer_in_uplift_to_oci_surplus(self):
        """Asset carrying 400,000, fair value at transfer date 450,000.

        Hand derivation (IAS 40.61: the uplift of 450,000 - 400,000 =
        50,000 is credited to the equity revaluation surplus, OCI, not
        P&L):
          Dr 1660 450,000 / Cr 1565 400,000, Cr 3660 50,000
        """
        asset = self._asset(cost=400000.0)
        p = self._prop(initial_cost=0.0)
        p.write({
            'transfer_in_asset_id': asset.id,
            'transfer_fair_value': 450000.0,
            'transfer_date': date(2026, 5, 31),
        })
        p.action_transfer_in()
        self.assertEqual(asset.state, 'disposed')
        moves = p.move_ids.sorted('id')
        self.assertEqual(len(moves), 1)
        self.assertMoveLines(moves[0], [
            (self.prop_acct, 450000.0, 0.0),
            (self.fa_gross, 0.0, 400000.0),
            (self.surplus, 0.0, 50000.0),
        ])
        self.assertAlmostEqual(p.carrying_amount, 450000.0, places=2)
        self.assertAlmostEqual(asset.revaluation_surplus, 0.0, places=2)
        self.assertAlmostEqual(p.revaluation_surplus, 50000.0, places=2)
        self.assertEqual(p.revaluation_surplus_account_id, self.surplus)
        log = self._single_log(p)
        self.assertAlmostEqual(log.delta_posted, 50000.0, places=2)
        self.assertEqual(log.delta_routing, 'oci')

    def test_transfer_in_uplift_reverses_prior_pl_decrease_first(self):
        """50,000 uplift reverses 30,000 prior P&L then credits OCI 20,000."""
        asset = self._asset(cost=400000.0)
        asset._eh_workflow_write({'revaluation_pl_decrease': 30000.0})
        p = self._prop(
            initial_cost=0.0,
            transfer_in_asset_id=asset.id,
            transfer_fair_value=450000.0,
            transfer_date=date(2026, 5, 31),
        )

        p.action_transfer_in()

        self.assertMoveLines(p.move_ids, [
            (self.prop_acct, 450000.0, 0.0),
            (self.fa_gross, 0.0, 400000.0),
            (self.fv_gl, 0.0, 30000.0),
            (self.surplus, 0.0, 20000.0),
        ])
        self.assertEqual(asset.revaluation_pl_decrease, 0.0)
        self.assertEqual(asset.revaluation_surplus, 0.0)
        self.assertEqual(p.revaluation_surplus, 20000.0)
        self.assertEqual(p.transfer_log_ids.delta_routing, 'mixed')

    def test_transfer_in_surplus_recycles_on_property_disposal(self):
        asset = self._asset(cost=400000.0)
        p = self._prop(
            initial_cost=0.0,
            transfer_in_asset_id=asset.id,
            transfer_fair_value=450000.0,
            transfer_date=date(2026, 5, 31),
            disposal_date=date(2026, 6, 30),
            disposal_proceeds=450000.0,
            disposal_cash_account_id=self.account_cash.id,
            disposal_gain_loss_account_id=self.fv_gl.id,
            retained_earnings_account_id=self.retained.id,
        )
        p.action_transfer_in()
        self.assertEqual(asset.revaluation_surplus, 0.0)
        self.assertEqual(p.revaluation_surplus, 50000.0)

        p.action_dispose()

        disposal = p.move_ids.sorted('id')[-1]
        self.assertMoveLines(disposal, [
            (self.account_cash, 450000.0, 0.0),
            (self.prop_acct, 0.0, 450000.0),
            (self.surplus, 50000.0, 0.0),
            (self.retained, 0.0, 50000.0),
        ])
        self.assertEqual(p.revaluation_surplus, 0.0)

    def test_transfer_in_rejects_different_source_reserve_account(self):
        other_reserve = self._ensure_account(
            self.env, '3662', 'Other Revaluation Reserve', 'equity',
        )
        asset = self._asset(cost=400000.0)
        p = self._prop(
            initial_cost=0.0,
            transfer_in_asset_id=asset.id,
            transfer_fair_value=450000.0,
            revaluation_surplus_account_id=other_reserve.id,
        )
        with self.assertRaisesRegex(UserError, 'must match'):
            p.action_transfer_in()

    def test_cost_transfer_in_reconstructs_contra_as_real_manager(self):
        asset = self._asset(
            cost=120000.0,
            salvage_value=12000.0,
            useful_life_months=60,
        )
        first_line = asset.depreciation_line_ids.sorted(
            lambda line: (line.depreciation_date, line.sequence),
        )[:1]
        first_line.with_user(self.real_manager).action_post()
        asset.invalidate_recordset(['net_book_value', 'total_depreciated'])
        posted_amount = first_line.amount
        carrying = asset.net_book_value
        p = self._cost_prop(
            initial_cost=0.0,
            transfer_in_asset_id=asset.id,
            transfer_date=max(first_line.depreciation_date, date(2026, 5, 31)),
            useful_life_years=4,
        )

        p.with_user(self.real_manager).action_transfer_in()
        p.invalidate_recordset()

        self.assertEqual(p.state, 'held')
        self.assertAlmostEqual(p.accumulated_depreciation, posted_amount, 2)
        self.assertAlmostEqual(p.carrying_amount, carrying, 2)
        self.assertAlmostEqual(p.residual_value, 12000.0, 2)
        self.assertEqual(p.acquisition_date, p.transfer_date)
        self.assertMoveLines(p.move_ids, [
            (self.prop_acct, 120000.0, 0.0),
            (self.fa_accum, posted_amount, 0.0),
            (self.fa_gross, 0.0, 120000.0),
            (self.accum_dep, 0.0, posted_amount),
        ])

    def test_transfer_in_controlled_reversal_restores_source_asset(self):
        asset = self._asset(cost=400000.0)
        p = self._prop(
            initial_cost=0.0,
            transfer_in_asset_id=asset.id,
            transfer_fair_value=450000.0,
            transfer_date=date(2026, 5, 31),
        )
        p.action_transfer_in()
        self.assertEqual(asset.state, 'disposed')

        p.reversal_date = date.today()
        p.action_reverse()

        self.assertEqual(p.state, 'reversed')
        self.assertEqual(asset.state, 'running')
        self.assertFalse(asset.disposal_move_id)
        asset._eh_assert_not_ip_transferred()

    def test_transfer_in_terminal_owner_blocks_reuse_and_repoint(self):
        asset = self._asset(cost=400000.0)
        first = self._prop(
            initial_cost=0.0, transfer_in_asset_id=asset.id,
            transfer_fair_value=410000.0)
        first.action_transfer_in()
        second = self._prop(
            initial_cost=0.0, transfer_in_asset_id=asset.id,
            transfer_fair_value=420000.0)
        with self.assertRaises(UserError):
            second.action_transfer_in()
        with self.assertRaises(UserError):
            asset.action_resume()
        with self.assertRaises(UserError):
            first.write({'transfer_in_asset_id': asset.id + 1})
        self.assertEqual(first.transfer_log_ids.source_asset_id, asset.id)

    def test_transfer_in_rejects_low_value_pool_source(self):
        asset = self._asset(cost=400000.0)
        pool = self.env['eh.asset.lvp.pool'].create({
            'name': 'IP rejection pool',
            'company_id': self.company.id,
        })
        asset._eh_workflow_write({'lvp_pool_id': pool.id})
        prop = self._prop(
            initial_cost=0.0, transfer_in_asset_id=asset.id,
            transfer_fair_value=400000.0)
        with self.assertRaises(UserError):
            prop.action_transfer_in()

    def test_golden_transfer_in_deficit_consumes_surplus_first(self):
        """Asset carrying 400,000 with a revaluation surplus balance of
        30,000; fair value at transfer date 350,000.

        Hand derivation (IAS 40.62(a): the decrease first reverses the
        surplus carried by that asset, only the excess hits P&L):
          decrease      = 400,000 - 350,000 = 50,000
          dr_to_surplus = min(50,000, 30,000) = 30,000
          dr_to_pl      = 50,000 - 30,000    = 20,000
          Dr 1660 350,000, Dr 3660 30,000, Dr 4660 20,000 / Cr 1565 400,000
        The asset's surplus balance is decremented to zero.
        """
        asset = self._asset(cost=400000.0)
        # Surplus balance as tracked by the assets module (readonly in the
        # UI, ledger-rolled by its revaluation wizard; preset here to state
        # the input directly).
        asset.write({'revaluation_surplus': 30000.0})
        p = self._prop(initial_cost=0.0)
        p.write({
            'transfer_in_asset_id': asset.id,
            'transfer_fair_value': 350000.0,
        })
        p.action_transfer_in()
        moves = p.move_ids.sorted('id')
        self.assertMoveLines(moves[0], [
            (self.prop_acct, 350000.0, 0.0),
            (self.surplus, 30000.0, 0.0),
            (self.fv_gl, 20000.0, 0.0),
            (self.fa_gross, 0.0, 400000.0),
        ])
        self.assertAlmostEqual(asset.revaluation_surplus, 0.0, places=2)
        self.assertAlmostEqual(p.revaluation_surplus, 0.0, places=2)
        log = self._single_log(p)
        self.assertAlmostEqual(log.delta_posted, -50000.0, places=2)
        # 20,000 of the 50,000 deficit reached P&L, so the routing is 'pl'.
        self.assertEqual(log.delta_routing, 'mixed')

    def test_transfer_in_guards(self):
        """Cost-model intake carries source cost unchanged; state still gates."""
        source = self._asset(cost=100000.0)
        # IAS 40.59: transfer at unchanged gross/carrying amount, with no
        # transfer-date remeasurement.
        p_cost = self._cost_prop(transfer_in_asset_id=source.id,
                                 transfer_fair_value=100000.0)
        p_cost.action_transfer_in()
        self.assertEqual(p_cost.state, 'held')
        self.assertAlmostEqual(p_cost.initial_cost, 100000.0, places=2)
        self.assertAlmostEqual(p_cost.carrying_amount, 100000.0, places=2)
        self.assertEqual(p_cost.transfer_log_ids.basis, 'cost')
        self.assertAlmostEqual(p_cost.transfer_log_ids.delta_posted, 0.0)
        # Held property refused: intake opens the ledger position.
        second_source = self._asset(cost=100000.0, code='BLDG-GOLD-2')
        p_held = self._prop(initial_cost=500000.0)
        p_held.action_activate()
        p_held.write({'transfer_in_asset_id': second_source.id,
                      'transfer_fair_value': 100000.0})
        with self.assertRaises(UserError):
            p_held.action_transfer_in()

        no_life_source = self._asset(
            cost=100000.0, code='BLDG-GOLD-NO-LIFE',
        )
        no_life = self._prop(
            model_basis='cost',
            initial_cost=0.0,
            useful_life_years=0,
            depreciation_expense_account_id=self.dep_exp.id,
            accumulated_depreciation_account_id=self.accum_dep.id,
            transfer_in_asset_id=no_life_source.id,
        )
        with self.assertRaisesRegex(UserError, 'remaining useful life'):
            no_life.action_transfer_in()

    def test_assets_is_a_hard_dependency_and_source_is_relational(self):
        module = self.env['ir.module.module'].search([
            ('name', '=', 'eh_account_investment_property'),
        ])
        dependencies = module.dependencies_id.mapped('name')
        self.assertIn('eh_account_assets_pro', dependencies)
        source = self.env['eh.investment.property']._fields[
            'transfer_in_asset_id'
        ]
        self.assertEqual(source.type, 'many2one')
        self.assertEqual(source.comodel_name, 'eh.asset')

    def test_ordinary_property_write_only_reads_selected_source_asset(self):
        asset = self._asset(cost=100000.0)
        p = self._prop(
            initial_cost=0.0,
            transfer_in_asset_id=asset.id,
            transfer_fair_value=100000.0,
        )
        access_modes = []
        asset_model = type(asset)
        original_check = asset_model._eh_check_access

        def tracked_check(records, operation):
            access_modes.append(operation)
            return original_check(records, operation)

        with patch.object(asset_model, '_eh_check_access', tracked_check):
            p.with_user(self.real_manager).write({'description': 'Reviewed'})

        self.assertTrue(access_modes)
        self.assertEqual(set(access_modes), {'read'})

    # ------------------------------------------------------------------
    # depreciation halt on model switch + FV-model depreciation block
    # ------------------------------------------------------------------
    def test_fv_model_never_accepts_depreciation(self):
        """A fair value model property is remeasured, never depreciated
        (IAS 40.33-35): action_depreciate must refuse."""
        p = self._prop(initial_cost=500000.0,
                       useful_life_years=50,
                       depreciation_expense_account_id=self.dep_exp.id,
                       accumulated_depreciation_account_id=self.accum_dep.id)
        p.action_activate()
        with self.assertRaises(UserError):
            p.action_depreciate()

    def test_model_switch_to_fv_cancels_pending_depreciation(self):
        """Switching cost -> fair value before any posting cancels the
        pending depreciation state: accumulated depreciation is zeroed in
        the same write and depreciation stays blocked afterwards."""
        p = self._cost_prop()
        # Draft setup may still switch basis. Activation seals the snapshot,
        # so perform this cleanup before the property becomes held.
        p.accumulated_depreciation = 4000.0
        p.model_basis = 'fair_value'
        self.assertAlmostEqual(p.accumulated_depreciation, 0.0, places=2)
        p.action_activate()
        with self.assertRaises(UserError):
            p.action_depreciate()

    def test_model_switch_frozen_after_posted_move(self):
        """Existing freeze preserved: once a depreciation charge is posted
        the basis cannot be switched at all."""
        p = self._cost_prop()
        p.action_activate()
        p.action_depreciate()
        with self.assertRaises(UserError):
            p.model_basis = 'fair_value'

    # ------------------------------------------------------------------
    # audit trail immutability
    # ------------------------------------------------------------------
    def test_transfer_log_is_immutable(self):
        p = self._prop(initial_cost=500000.0)
        p.action_activate()
        p.transfer_fair_value = 530000.0
        p.action_transfer_out()
        log = self._single_log(p)
        with self.assertRaises(UserError):
            log.write({'note': 'tampered'})
        with self.assertRaises(UserError):
            log.unlink()

    # ------------------------------------------------------------------
    # pairwise sweep over the transfer-out axes
    # ------------------------------------------------------------------
    def test_pairwise_transfer_out_matrix(self):
        """All-pairs sweep over basis x fair-value relation x depreciated.

        Invariants per case (each hand-derivable from the case inputs):
        * every posted entry balances;
        * exactly one audit row, carrying the entered fair value (cost
          model: disclosed as entered, including zero; fair value model:
          the value used, i.e. the carrying amount when not supplied);
        * delta_posted = fv - carrying on the fair value model, always 0
          on the cost model (IAS 40.59); routing 'pl' iff a fair value
          delta posted;
        * the derecognition entry debits the transfer target with the
          deemed cost: the transfer-date fair value on the fair value
          model, the unchanged carrying amount on the cost model.
        Cost-model carrying: 500,000, or 490,000 after one 10,000 charge
        (500,000 / 50). The 'depreciated' axis is meaningless under the
        fair value model (depreciation is blocked) and is forced False.
        """
        axes = {
            'basis': ['cost', 'fair_value'],
            'fv_rel': ['none', 'equal', 'above', 'below'],
            'depreciated': [False, True],
        }
        base_day = date(2026, 6, 1)
        for idx, case in enumerate(pairwise_cases(axes)):
            with self.subTest(case=repr(case)):
                depreciated = case['depreciated'] \
                    and case['basis'] == 'cost'
                if case['basis'] == 'cost':
                    p = self._cost_prop()
                else:
                    p = self._prop(initial_cost=500000.0)
                p.action_activate()
                if depreciated:
                    p.depreciation_date = date(2026, 5, 31)
                    p.action_depreciate()   # 500,000 / 50 = 10,000
                transfer_day = base_day + timedelta(days=idx)
                if case['basis'] == 'cost' and not depreciated:
                    service_days = (transfer_day - date(2026, 1, 1)).days + 1
                    catch_up = self.company.currency_id.round(
                        10000.0 * service_days / 365.0,
                    )
                    carrying = 500000.0 - catch_up
                else:
                    carrying = 490000.0 if depreciated else 500000.0
                entered = {
                    'none': 0.0,
                    'equal': carrying,
                    'above': carrying + 25000.0,
                    'below': carrying - 25000.0,
                }[case['fv_rel']]
                p.write({
                    'transfer_fair_value': entered,
                    'transfer_date': transfer_day,
                })
                p.action_transfer_out()
                self.assertEqual(p.state, 'transferred')
                for move in p.move_ids:
                    self.assertBalanced(move)
                log = self._single_log(p)
                if case['basis'] == 'cost':
                    expected_fv = entered
                    expected_delta = 0.0
                    deemed = carrying
                else:
                    expected_fv = entered or carrying
                    expected_delta = expected_fv - carrying
                    deemed = expected_fv
                self.assertAlmostEqual(log.fair_value, expected_fv,
                                       places=2)
                self.assertAlmostEqual(log.delta_posted, expected_delta,
                                       places=2)
                self.assertAlmostEqual(log.carrying_before, carrying,
                                       places=2)
                expected_routing = (
                    'pl' if case['basis'] == 'fair_value'
                    and abs(expected_delta) >= 0.005 else 'none')
                self.assertEqual(log.delta_routing, expected_routing)
                self.assertEqual(bool(log.remeasure_move_id),
                                 expected_routing == 'pl')
                target_line = log.move_id.line_ids.filtered(
                    lambda l: l.account_id == self.ppe)
                self.assertAlmostEqual(target_line.debit, deemed, places=2)
