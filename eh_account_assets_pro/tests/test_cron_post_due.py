# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Due-only, rotating, savepoint-isolated asset and lease posting crons."""

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestAssetsProPostDueCrons(EhAssetTestCase):

    def test_asset_cron_filters_future_and_rotates_past_failure(self):
        future = self._make_asset(
            code='CRON-FUTURE', acquisition_date='2035-01-01',
            in_service_date='2035-01-31', acquisition_cost=100.0,
            useful_life_months=1,
        )
        bad = self._make_asset(
            code='CRON-BAD', acquisition_date='2025-01-01',
            in_service_date='2025-01-31', acquisition_cost=100.0,
            useful_life_months=1,
        )
        good = self._make_asset(
            code='CRON-GOOD', acquisition_date='2025-02-01',
            in_service_date='2025-02-28', acquisition_cost=100.0,
            useful_life_months=1,
        )
        (future | bad | good).action_activate()
        key = 'eh_account_assets_pro.asset_post_due_cursor'
        self.env['ir.config_parameter'].sudo().set_param(key, '0')

        LineClass = type(self.env['eh.asset.depreciation.line'])
        original_post = LineClass.action_post

        def _fail_bad(lines):
            if any(line.asset_id == bad for line in lines):
                raise UserError('deliberate bad asset')
            return original_post(lines)

        with patch.object(LineClass, 'action_post', _fail_bad):
            first_failures = self.env['eh.asset']._cron_post_due(batch_size=1)
            second_failures = self.env['eh.asset']._cron_post_due(batch_size=1)

        future.depreciation_line_ids.invalidate_recordset()
        bad.depreciation_line_ids.invalidate_recordset()
        good.depreciation_line_ids.invalidate_recordset()
        self.assertEqual([row[0] for row in first_failures], [bad.id])
        self.assertFalse(second_failures)
        self.assertFalse(future.depreciation_line_ids.is_posted)
        self.assertFalse(bad.depreciation_line_ids.is_posted)
        self.assertTrue(good.depreciation_line_ids.is_posted)

    def test_lease_cron_rolls_back_failed_lease_and_rotates(self):
        future = self._make_lease(
            reference='CRON-LEASE-FUTURE', commencement_date='2035-01-31',
            term_months=2, cadence='monthly', payment_timing='arrears',
            payment_amount=100.0, incremental_borrowing_rate=0.0,
        )
        bad = self._make_lease(
            reference='CRON-LEASE-BAD', commencement_date='2025-01-31',
            term_months=2, cadence='monthly', payment_timing='arrears',
            payment_amount=100.0, incremental_borrowing_rate=0.0,
        )
        good = self._make_lease(
            reference='CRON-LEASE-GOOD', commencement_date='2025-01-31',
            term_months=2, cadence='monthly', payment_timing='arrears',
            payment_amount=100.0, incremental_borrowing_rate=0.0,
        )
        (future | bad | good).action_activate()
        bad_lines = bad.schedule_line_ids.sorted('sequence')
        key = 'eh_account_assets_pro.lease_post_due_cursor'
        self.env['ir.config_parameter'].sudo().set_param(key, '0')

        LineClass = type(self.env['eh.lease.schedule.line'])
        original_post = LineClass.action_post

        def _fail_second_bad_line(lines):
            if bad_lines[1] in lines:
                raise UserError('deliberate second-line failure')
            return original_post(lines)

        with patch.object(LineClass, 'action_post', _fail_second_bad_line):
            first_failures = self.env['eh.lease.contract']._cron_post_due(
                batch_size=1,
            )
            second_failures = self.env['eh.lease.contract']._cron_post_due(
                batch_size=1,
            )

        bad.schedule_line_ids.invalidate_recordset(['is_posted', 'move_id'])
        good.schedule_line_ids.invalidate_recordset(['is_posted', 'move_id'])
        self.assertEqual([row[0] for row in first_failures], [bad.id])
        self.assertFalse(second_failures)
        # First bad row posted before second raised, but lease-level savepoint
        # rolled the entire partial posting back.
        self.assertFalse(any(bad.schedule_line_ids.mapped('is_posted')))
        self.assertFalse(any(bad.schedule_line_ids.mapped('move_id')))
        self.assertFalse(self.env['account.move'].search([
            ('ref', 'like', 'Lease %s period %%' % bad.display_name),
        ]), "the failed lease must not leave an orphan sealed draft move")
        self.assertFalse(any(future.schedule_line_ids.mapped('is_posted')))
        self.assertTrue(all(good.schedule_line_ids.mapped('is_posted')))
        self.assertTrue(all(good.schedule_line_ids.mapped('move_id')))
