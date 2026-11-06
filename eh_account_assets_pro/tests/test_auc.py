# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Asset under construction (AUC) capitalisation tests.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestAuc(EhAssetTestCase):

    def test_auc_blocks_compute_schedule(self):
        a = self._make_asset(is_under_construction=True)
        with self.assertRaises(UserError):
            a.action_compute_schedule()

    def test_auc_blocks_activation(self):
        a = self._make_asset(is_under_construction=True)
        with self.assertRaises(UserError):
            a.action_activate()

    def test_capitalise_unflags_and_activates(self):
        a = self._make_asset(
            is_under_construction=True,
            auc_account_id=self.account_fixed.id,
        )
        a.action_capitalise()
        self.assertFalse(a.is_under_construction)
        self.assertEqual(a.state, 'running')
        self.assertTrue(a.depreciation_line_ids)
        self.assertTrue(a.capitalised_at)
        self.assertEqual(a.capitalised_by_id, self.env.user)
        with self.assertRaises(UserError):
            a.action_set_to_draft()
        with self.assertRaises(UserError):
            a.write({'is_under_construction': True})
        with self.assertRaises(UserError):
            a.action_capitalise()

    def test_capitalisation_cannot_precede_acquisition(self):
        a = self._make_asset(is_under_construction=True)
        with self.assertRaises(UserError):
            a.action_capitalise('2025-12-31')
        self.assertEqual(a.state, 'draft')
        self.assertTrue(a.is_under_construction)
        self.assertFalse(a.capitalised_at)

    def test_capitalise_refuses_non_auc(self):
        a = self._make_asset(is_under_construction=False)
        with self.assertRaises(UserError):
            a.action_capitalise()

    def test_capitalise_zero_cost_blocked(self):
        # acquisition_cost=0 must be rejected by the DB check constraint
        # before any record reaches action_capitalise(). The constraint
        # is the authoritative guard; this test confirms it fires.
        from psycopg2.errors import CheckViolation
        a = self._make_asset(is_under_construction=True)
        with self.assertRaises(CheckViolation):
            with self.env.cr.savepoint():
                self.env.cr.execute(
                    "UPDATE eh_asset SET acquisition_cost = 0 "
                    "WHERE id = %s",
                    (a.id,),
                )
