# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Freeze-after-post guards for the asset schedule.

Once a depreciation line has produced its journal entry, its measurement
fields (amount, date, running totals) and the parent asset's cost inputs
(acquisition_cost, salvage_value) must not be re-based by an in-place ORM
write; a correction has to be a further posting or a revaluation.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestFreezeAfterPost(EhAssetTestCase):

    @staticmethod
    def _direct_write_value(record, field_name):
        value = record[field_name]
        if record._fields[field_name].type == 'many2one':
            return value.id or False
        return value

    def _posted_asset(self):
        # A long schedule whose service start is recent enough that only the
        # earliest line is due: this leaves later lines unposted so the
        # editable-before-post path can also be exercised.
        asset = self._make_asset(
            code='IT-FRZ-1',
            in_service_date='2025-12-31',
            acquisition_cost=120000.0,
            useful_life_months=120,
        )
        asset.action_activate()
        asset.depreciation_line_ids.sorted('sequence')[0].action_post()
        posted = asset.depreciation_line_ids.filtered(lambda l: l.is_posted)
        self.assertTrue(posted, "expected at least one posted line")
        return asset, posted[0]

    def test_posted_line_amount_frozen(self):
        _asset, line = self._posted_asset()
        with self.assertRaises(UserError):
            line.amount = line.amount + 100.0

    def test_posted_line_date_frozen(self):
        _asset, line = self._posted_asset()
        with self.assertRaises(UserError):
            line.depreciation_date = '2025-06-30'

    def test_unposted_line_still_editable(self):
        asset, _line = self._posted_asset()
        unposted = asset.depreciation_line_ids.filtered(
            lambda l: not l.is_posted)
        self.assertTrue(unposted, "expected some unposted lines to remain")
        # Editing a not-yet-posted line stays allowed (default behaviour).
        unposted[0].amount = unposted[0].amount  # no-op write must not raise

    def test_asset_cost_frozen_after_post(self):
        asset, _line = self._posted_asset()
        with self.assertRaises(UserError):
            asset.acquisition_cost = 15000.0

    def test_asset_salvage_frozen_after_post(self):
        asset, _line = self._posted_asset()
        with self.assertRaises(UserError):
            asset.salvage_value = 500.0

    def test_every_posted_asset_measurement_input_is_frozen(self):
        asset, _line = self._posted_asset()
        for field_name in asset._FROZEN_AFTER_POST:
            with self.subTest(field=field_name):
                value = self._direct_write_value(asset, field_name)
                with self.assertRaises(UserError):
                    asset.write({field_name: value})

    def test_asset_cost_editable_before_post(self):
        asset = self._make_asset(code='IT-FRZ-2')
        # Draft asset, nothing posted: cost inputs remain editable.
        asset.acquisition_cost = 40000.0
        self.assertEqual(asset.acquisition_cost, 40000.0)

    # ---- impairment freeze / SoD (IAS 16.39, IAS 36) ----

    def _posted_impairment(self):
        asset = self._make_asset(
            code='IT-IMP-FRZ',
            in_service_date='2025-12-31',
            acquisition_cost=120000.0,
            useful_life_months=120,
        )
        asset.action_activate()
        asset.depreciation_line_ids.sorted('sequence')[0].action_post()
        imp = self.env['eh.asset.impairment'].create({
            'asset_id': asset.id,
            'impairment_date': '2026-03-31',
            'amount': 1500.0,
            'is_reversal': False,
            'reason': 'Freeze test',
            'impairment_account_id': self.account_impairment.id,
            'accumulated_account_id': self.account_accum_dep.id,
        })
        imp.action_post()
        self.assertEqual(imp.state, 'posted')
        return asset, imp

    def test_posted_impairment_amount_frozen(self):
        _asset, imp = self._posted_impairment()
        with self.assertRaises(UserError):
            imp.amount = imp.amount + 100.0

    def test_posted_impairment_is_reversal_frozen(self):
        _asset, imp = self._posted_impairment()
        with self.assertRaises(UserError):
            imp.is_reversal = True

    def test_every_posted_impairment_input_is_frozen(self):
        _asset, imp = self._posted_impairment()
        for field_name in imp._FROZEN_AFTER_POST:
            with self.subTest(field=field_name):
                value = self._direct_write_value(imp, field_name)
                with self.assertRaises(UserError):
                    imp.write({field_name: value})

    def test_posted_impairment_unlink_blocked(self):
        _asset, imp = self._posted_impairment()
        with self.assertRaises(UserError):
            imp.unlink()

    def test_cancelled_impairment_remains_frozen_and_undeletable(self):
        asset, imp = self._posted_impairment()
        original_move = imp.move_id
        asset.invalidate_recordset([
            'net_book_value', 'accumulated_impairment',
        ])
        impaired_nbv = asset.net_book_value
        imp.action_cancel()
        self.assertEqual(imp.state, 'cancelled')
        self.assertEqual(original_move.state, 'posted')
        self.assertTrue(imp.reversal_move_id)
        self.assertEqual(imp.reversal_move_id.state, 'posted')
        self.assertTrue(imp.reversal_move_id.eh_sealed)
        asset.invalidate_recordset([
            'net_book_value', 'accumulated_impairment',
        ])
        self.assertAlmostEqual(asset.accumulated_impairment, 0.0, places=2)
        self.assertAlmostEqual(
            asset.net_book_value, impaired_nbv + imp.amount, places=2,
        )
        self.assertAlmostEqual(
            sum((original_move | imp.reversal_move_id).line_ids.mapped('balance')),
            0.0, places=2,
        )
        with self.assertRaises(UserError):
            imp.write({'reason': 'Rewrite the cancelled evidence'})
        with self.assertRaises(UserError):
            imp.unlink()

    def test_draft_impairment_still_editable(self):
        asset = self._make_asset(code='IT-IMP-DRAFT')
        asset.action_activate()
        imp = self.env['eh.asset.impairment'].create({
            'asset_id': asset.id,
            'impairment_date': '2026-03-31',
            'amount': 500.0,
            'is_reversal': False,
            'reason': 'Draft edit test',
        })
        # A draft impairment stays editable (default behaviour).
        imp.amount = 600.0
        self.assertAlmostEqual(imp.amount, 600.0, places=2)
        imp.unlink()  # and deletable while draft

    def test_impairment_create_posted_state_blocked(self):
        """Create-append negative test: injecting a row directly in the posted
        state would move the asset's carrying amount without a balanced JE."""
        asset = self._make_asset(code='IT-IMP-CRE')
        asset.action_activate()
        with self.assertRaises(UserError):
            self.env['eh.asset.impairment'].create({
                'asset_id': asset.id,
                'impairment_date': '2026-03-31',
                'amount': 500.0,
                'is_reversal': False,
                'reason': 'Create-append hole',
                'state': 'posted',
            })

    def test_impairment_create_cancelled_state_blocked(self):
        asset = self._make_asset(code='IT-IMP-CANCELLED-CREATE')
        with self.assertRaises(UserError):
            self.env['eh.asset.impairment'].create({
                'asset_id': asset.id,
                'impairment_date': '2026-03-31',
                'amount': 500.0,
                'is_reversal': False,
                'reason': 'Cancelled create bypass',
                'state': 'cancelled',
            })

    def test_non_manager_cannot_cancel_impairment(self):
        _asset, imp = self._posted_impairment()
        clerk = self._make_non_manager_user()
        with self.assertRaises(UserError):
            imp.with_user(clerk).action_cancel()

    def test_posted_asset_frozen_and_undeletable_flow_intact(self):
        # (a) an asset with a posted depreciation line has its cost inputs
        # frozen at the ORM write layer; (b) it cannot be unlinked (its posted
        # GL entries would be orphaned); (c) the normal activate / post flow
        # still works (exercised by _posted_asset).
        asset, line = self._posted_asset()
        self.assertTrue(line.is_posted)
        # (a) cost input frozen once a line has posted.
        with self.assertRaises(UserError):
            asset.write({'acquisition_cost': 15000.0})
        # (b) an asset with posted depreciation cannot be unlinked.
        with self.assertRaises(UserError):
            asset.unlink()
        # A draft asset with no posted line stays deletable.
        draft = self._make_asset(code='IT-FRZ-DEL')
        draft.unlink()
